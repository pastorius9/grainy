"""No account requests: catalog transactions and the local Codex protocol peer."""
import json
import os
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
import numpy as np
from PIL import Image
from PySide6.QtWidgets import QApplication,QProgressDialog
from PySide6.QtCore import QEventLoop,QTimer,Qt
from PySide6.QtTest import QTest
from luma.catalog import Catalog
from luma.engine import defaults,develop
from luma.commands import validate_proposal,changed_settings,CommandSession
from luma.command_batch import capture,targets,apply,restore,BATCH_KEY
from luma.app import MainWindow
from luma.auth import CodexAuth
from luma.command_dialog import CommandDialog


@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])


def wait(predicate):
    loop=QEventLoop();timer=QTimer();timer.setInterval(5)
    timer.timeout.connect(lambda:loop.quit() if predicate() else None)
    deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(loop.quit)
    timer.start();deadline.start(6000)
    if not predicate():loop.exec()
    timer.stop();deadline.stop();assert predicate()


def populate(catalog,tmp_path):
    ids=[]
    for index,relative in enumerate(['same/a.png','same/b.png','same/c.png','same/sub/d.png','same2/e.png']):
        path=tmp_path/relative;path.parent.mkdir(parents=True,exist_ok=True)
        pixels=np.arange(96*64*3,dtype=np.uint8).reshape(64,96,3)
        Image.fromarray(pixels).save(path)
        values=defaults();values.update(exposure=index*.2,contrast=index*5,saturation=index*3)
        ids.append(catalog.add(path,values))
    video=catalog.add(tmp_path/'same/movie.mp4')
    return ids,video


@pytest.fixture
def catalog(tmp_path):
    value=Catalog(tmp_path/'data');yield value;value.close()


def bw(scope='folder',value=True):
    return validate_proposal({'message':'test','action':'adjust','scope':scope,
        'changes':[{'key':'monochrome','value':value,'operation':'set'}]})


def test_folder_membership_and_independent_values(catalog,tmp_path):
    ids,video=populate(catalog,tmp_path)
    catalog.update(ids[1],rating=5,flag=-1)
    snapshot=capture(catalog,ids[0],[ids[0]],str(tmp_path/'same'))
    assert snapshot['scopes']['folder']==ids[:3]
    records=targets(snapshot,'folder');before={i:catalog.photo(i)['settings'] for i in ids+[video]}
    fingerprints={i:Path(catalog.photo(i)['path']).read_bytes() for i in ids}
    assert apply(catalog,records,[changed_settings(r['settings'],bw()) for r in records])==ids[:3]
    for ident in ids+[video]:
        assert catalog.photo(ident)['settings']=={**before[ident],'monochrome':ident in ids[:3]}
    for ident in ids:assert Path(catalog.photo(ident)['path']).read_bytes()==fingerprints[ident]
    assert restore(catalog)==ids[:3]
    assert all(catalog.photo(i)['settings']==before[i] for i in ids+[video])
    assert restore(catalog,True)==ids[:3]
    source=np.random.default_rng(31).random((25,31,3),dtype=np.float32)
    image=develop(source,catalog.photo(ids[0])['settings'])
    np.testing.assert_allclose(image[...,0],image[...,1],atol=1e-6)
    np.testing.assert_allclose(image[...,1],image[...,2],atol=1e-6)


def test_relative_exposure_preserves_each_base_and_clamps(catalog,tmp_path):
    ids,_=populate(catalog,tmp_path)
    snapshot=capture(catalog,ids[0],[ids[0],ids[2]])
    proposal=validate_proposal({'message':'test','action':'adjust','scope':'selection',
        'changes':[{'key':'exposure','value':.5,'operation':'add'}]})
    rows=targets(snapshot,'selection');apply(catalog,rows,[changed_settings(r['settings'],proposal) for r in rows])
    assert catalog.photo(ids[0])['settings']['exposure']==.5
    assert catalog.photo(ids[2])['settings']['exposure']==.9
    assert catalog.photo(ids[1])['settings']['exposure']==.2
    assert changed_settings({**defaults(),'exposure':4.9},proposal)['exposure']==5


@pytest.mark.parametrize('mutation',['edit','remove','move'])
def test_stale_target_rejects_entire_batch(catalog,tmp_path,mutation):
    ids,_=populate(catalog,tmp_path);snapshot=capture(catalog,ids[0],ids[:3])
    rows=targets(snapshot,'selection')
    if mutation=='edit':catalog.edit(ids[2],{**rows[2]['settings'],'exposure':1})
    else:
        with catalog.db:
            if mutation=='remove':catalog.db.execute('DELETE FROM photos WHERE id=?',(ids[2],))
            else:catalog.db.execute('UPDATE photos SET path=? WHERE id=?',('changed.png',ids[2]))
    before=catalog.db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
    with pytest.raises(ValueError):apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows])
    assert catalog.photo(ids[0])['settings']==rows[0]['settings']
    assert catalog.db.execute('SELECT COUNT(*) FROM history').fetchone()[0]==before
    assert catalog.preference(BATCH_KEY) is None


def test_sql_failure_rolls_back_edits_histories_and_undo(catalog,tmp_path):
    ids,_=populate(catalog,tmp_path);rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    catalog.db.execute(f"CREATE TRIGGER fail_batch BEFORE UPDATE OF edits ON photos WHEN NEW.id={ids[1]} BEGIN SELECT RAISE(ABORT,'injected'); END;")
    before=catalog.db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows])
    assert all(catalog.photo(r['id'])['settings']==r['settings'] for r in rows)
    assert catalog.db.execute('SELECT COUNT(*) FROM history').fetchone()[0]==before
    assert catalog.preference(f'undo:{ids[0]}') is None
    assert catalog.preference(BATCH_KEY) is None


@pytest.mark.parametrize('mutation',['settings','undo'])
def test_batch_undo_preserves_later_edits(catalog,tmp_path,mutation):
    ids,_=populate(catalog,tmp_path);rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows])
    if mutation=='settings':catalog.edit(ids[2],{**catalog.photo(ids[2])['settings'],'contrast':44})
    else:catalog.save_preference(f'undo:{ids[2]}',{'undo':[],'redo':[]})
    before=[catalog.photo(i)['settings'] for i in ids]
    with pytest.raises(ValueError):restore(catalog)
    assert [catalog.photo(i)['settings'] for i in ids]==before


def test_persistent_batch_and_noop(tmp_path):
    catalog=Catalog(tmp_path/'data');ids,_=populate(catalog,tmp_path)
    rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows]);catalog.close()
    catalog=Catalog(tmp_path/'data')
    assert restore(catalog)==ids[:3]
    assert restore(catalog,True)==ids[:3]
    state=catalog.preference(BATCH_KEY)
    rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    assert apply(catalog,rows,[r['settings'] for r in rows])==[]
    assert catalog.preference(BATCH_KEY)==state
    catalog.close()


def test_batch_profile_references_survive_maintenance(catalog,tmp_path):
    from test_rawcolor import make_dcp,record
    from luma.catalog_maintenance import optimize
    from luma.profile_store import verify_references
    profile=record(make_dcp());values={**defaults(),'dcp_profile':profile}
    ids=[catalog.add(tmp_path/f'{i}.nef',values) for i in range(2)]
    rows=targets(capture(catalog,ids[0],ids),'selection')
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows])
    raw=catalog.db.execute('SELECT value FROM preferences WHERE key=?',(BATCH_KEY,)).fetchone()[0]
    assert '"data":' not in raw and 'data_ref' in raw
    assert len(verify_references(catalog.db))==1
    optimize(catalog.directory)
    restore(catalog);assert all(catalog.photo(i)['settings']['dcp_profile']==profile for i in ids)


def test_batch_auto_tone_cancellation_writes_nothing(app,tmp_path,monkeypatch):
    import time
    import luma.app as module
    window=MainWindow(tmp_path/'data');ids,_=populate(window.catalog,tmp_path)
    try:
        window.select_folder(str(tmp_path/'same'));window.activate(ids[0])
        wait(lambda:window.source is not None and not window.library_loading)
        snapshot=window.command_context();before=[window.catalog.photo(i)['settings'] for i in ids]
        original=module.load_image
        def slow(*args,**kwargs):time.sleep(.1);return original(*args,**kwargs)
        monkeypatch.setattr(module,'load_image',slow)
        QTimer.singleShot(20,lambda:window.findChild(QProgressDialog).reject())
        proposal=validate_proposal({'message':'auto','scope':'folder','action':'auto_tone','changes':[]})
        with pytest.raises(ValueError,match='취소'):window.apply_command(snapshot,proposal)
        assert [window.catalog.photo(i)['settings'] for i in ids]==before
        assert window.catalog.preference(BATCH_KEY) is None
        assert not window.command_running
    finally:window.close()


@pytest.mark.parametrize('key,value,operation,scope',[
    ('monochrome',1,'set','folder'),('monochrome',True,'add','folder'),
    ('exposure',True,'set','folder'),('exposure',.5,'multiply','folder'),
    ('exposure',.5,'set','all_library'),('path',1,'set','folder')])
def test_scope_and_typed_changes_validated(key,value,operation,scope):
    with pytest.raises(ValueError):validate_proposal({'message':'x','action':'adjust','scope':scope,
        'changes':[{'key':key,'value':value,'operation':operation}]})


def test_real_window_folder_filter_batch_and_undo(app,tmp_path):
    window=MainWindow(tmp_path/'data');ids,_=populate(window.catalog,tmp_path)
    try:
        window.select_folder(str(tmp_path/'same'));window.search.setText('a.png');window.refresh_lists()
        window.activate(ids[0]);wait(lambda:window.source is not None and not window.library_loading)
        assert window.visible_ids==[ids[0]]
        snapshot=window.command_context();assert snapshot['scopes']['folder']==ids[:3]
        window.manager.auto_sync=True;window.selected_ids=lambda:ids
        assert window.apply_command(snapshot,bw())==3
        assert window.settings['monochrome'] and window.bw.isChecked()
        assert not window.catalog.photo(ids[3])['settings']['monochrome']
        assert window.restore_command_batch()==3 and not window.settings['monochrome']
        assert window.restore_command_batch(True)==3 and window.settings['monochrome']
        current=window.command_context()
        assert window.apply_command(current,bw('current',False))==1
        assert window.catalog.photo(ids[1])['settings']['monochrome']
        window.undo();assert window.settings['monochrome']
    finally:window.close()


def test_protocol_folder_bw_and_review_controls(app,tmp_path,monkeypatch):
    monkeypatch.setenv('LUMA_FAKE_AUTH','folder_bw')
    catalog=Catalog(tmp_path/'data');ids,_=populate(catalog,tmp_path)
    home=tmp_path/'auth';home.mkdir();(home/'fake-signed-in').write_text('test')
    client=CodexAuth(home,command=[sys.executable,str(Path(__file__).with_name('fake_codex_auth.py'))])
    client.start();wait(lambda:client.connected)
    snapshot=capture(catalog,ids[0],[ids[0]],str(tmp_path/'same'));applied=[]
    dialog=CommandDialog(client,context=lambda:snapshot,apply=lambda *args:applied.append(args))
    try:
        dialog.show();dialog.input.setPlainText('같은 폴더의 모든 사진을 흑백으로 전환해줘')
        QTest.mouseClick(dialog.send_button,Qt.MouseButton.LeftButton)
        wait(lambda:dialog.apply_button.isVisible())
        assert '3장' in dialog.apply_button.text() and '하위 폴더' in dialog.proposal_label.text()
        assert dialog.proposal['changes']['monochrome'] is True
        QTest.mouseClick(dialog.apply_button,Qt.MouseButton.LeftButton)
        assert len(applied)==1 and applied[0][1]['scope']=='folder'
        calls=[json.loads(line) for line in (home/'calls.jsonl').read_text().splitlines()]
        prompt=next(call['params']['input'][0]['text'] for call in calls if call['method']=='turn/start')
        assert '"folder": 3' in prompt
        assert str(tmp_path) not in prompt and 'a.png' not in prompt and 'records' not in prompt
        assert 'monochrome' in prompt
    finally:dialog.close();client.shutdown();catalog.close()


def test_batch_auto_tone_per_photo_and_failure_atomicity(app,tmp_path):
    from luma.engine import load_image,auto_tone_settings,resize_float
    window=MainWindow(tmp_path/'data');ids,_=populate(window.catalog,tmp_path)
    try:
        window.select_folder(str(tmp_path/'same'));window.activate(ids[0])
        wait(lambda:window.source is not None and not window.library_loading)
        snapshot=window.command_context();records=targets(snapshot,'folder')
        expected=[auto_tone_settings(resize_float(load_image(r['path'],720,r['settings']['working_space'],raw_options=r['settings'])[0],720),r['settings'])[0] for r in records]
        proposal=validate_proposal({'message':'auto','scope':'folder','action':'auto_tone','changes':[]})
        window.apply_command(snapshot,proposal)
        assert [window.catalog.photo(i)['settings'] for i in ids[:3]]==expected
        window.restore_command_batch()
        snapshot=window.command_context();Path(records[2]['path']).unlink()
        before=[window.catalog.photo(i)['settings'] for i in ids]
        with pytest.raises(ValueError,match='변경한 사진은 없습니다'):window.apply_command(snapshot,proposal)
        assert [window.catalog.photo(i)['settings'] for i in ids]==before
    finally:window.close()


# --- compact (v2) groups, metadata and conflict kinds (the AI connection's batches) ----------------

from luma.command_batch import BatchConflict,CHANGED,HISTORY,UNDO_DEPTH


def photo_states(catalog,ids):
    return [(catalog.photo(i)['settings'],catalog.preference(f'undo:{i}')) for i in ids]


def test_meta_is_kept_and_restore_checks_expect(catalog,tmp_path):
    ids,_=populate(catalog,tmp_path);rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    key='undo:mcp:b123456'
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows],batch_key=key,
          meta={'id':'b123456','tool':'apply_edits'},compact=True)
    group=catalog.preference(key)
    assert group['id']=='b123456' and group['tool']=='apply_edits' and group['v']==2 and group['state']=='applied'
    with pytest.raises(BatchConflict) as caught:restore(catalog,batch_key=key,expect='bffffff')
    assert caught.value.kind=='none' and str(caught.value)=='실행 취소할 일괄 작업이 없습니다.'
    assert all(catalog.photo(i)['settings']['monochrome'] for i in ids[:3])
    assert restore(catalog,batch_key=key,expect='b123456')==ids[:3]
    group=catalog.preference(key);assert group['state']=='undone' and group['id']=='b123456' and group['tool']=='apply_edits'
    assert restore(catalog,True,batch_key=key,expect='b123456')==ids[:3]
    # The full format keeps meta too.
    rows=targets(capture(catalog,ids[0],ids[3:5]),'selection')
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows],meta={'id':'x1'})
    assert catalog.preference(BATCH_KEY)['id']=='x1' and 'v' not in catalog.preference(BATCH_KEY)


def test_conflicts_are_batch_conflicts_with_kind_and_same_messages(catalog,tmp_path):
    ids,_=populate(catalog,tmp_path);rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    for redo,text in ((False,'실행 취소할 일괄 작업이 없습니다.'),(True,'다시 실행할 일괄 작업이 없습니다.')):
        with pytest.raises(BatchConflict) as caught:restore(catalog,redo)
        assert caught.value.kind=='none' and str(caught.value)==text and isinstance(caught.value,ValueError)
    catalog.edit(ids[1],{**rows[1]['settings'],'exposure':2})
    with pytest.raises(BatchConflict) as caught:apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows])
    assert caught.value.kind=='changed' and str(caught.value)==CHANGED
    rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows])
    catalog.save_preference(f'undo:{ids[0]}',{'undo':[],'redo':[]})
    with pytest.raises(BatchConflict) as caught:restore(catalog)
    assert caught.value.kind=='history' and str(caught.value)==HISTORY


def test_codex_groups_keep_the_v1_format(catalog,tmp_path):
    ids,_=populate(catalog,tmp_path);rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows])
    group=catalog.preference(BATCH_KEY)
    assert set(group)=={'state','records'} and group['state']=='applied'
    assert all(set(r)=={'id','path','before','after','undo_before','undo_after'} for r in group['records'])
    assert group['records'][0]['undo_after']=={'undo':[rows[0]['settings']],'redo':[]}


def stacked(catalog,ids,depth,redo,order=False):
    for ident in ids:
        base=catalog.photo(ident)['settings']
        undo=[{**base,'exposure':-i/100} for i in range(depth)]
        if order and undo:undo[0]=dict(reversed(list(undo[0].items())))    # an older entry in another key order
        catalog.save_preference(f'undo:{ident}',{'undo':undo,'redo':[{**base,'contrast':float(i)} for i in range(redo)]})


@pytest.mark.parametrize('depth,redo,order',[(0,0,False),(79,0,False),(80,0,False),(80,0,True),(5,3,False),(UNDO_DEPTH,2,True)])
def test_compact_group_undo_redo_matches_full_format(tmp_path,depth,redo,order):
    from luma.engine import normalized
    results=[]
    for name,compact in (('full',False),('compact',True)):
        catalog=Catalog(tmp_path/name);ids,_=populate(catalog,tmp_path/name);targeted=ids[:3]
        stacked(catalog,targeted,depth,redo,order)
        # One photo has never been edited: it has no undo preference at all.
        catalog.db.execute('DELETE FROM preferences WHERE key=?',(f'undo:{targeted[2]}',));catalog.db.commit()
        initial=photo_states(catalog,ids)
        rows=targets(capture(catalog,ids[0],targeted),'selection')
        apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows],compact=compact)
        applied=photo_states(catalog,ids)
        assert restore(catalog)==targeted;undone=photo_states(catalog,ids)
        assert restore(catalog,True)==targeted;redone=photo_states(catalog,ids)
        assert restore(catalog)==targeted;again=photo_states(catalog,ids)
        # Undo gives back exactly what was there (a photo without an undo preference gets the empty state).
        assert [s for s,_ in undone]==[s for s,_ in initial]
        assert [u for (_,u),(_,before) in zip(undone,initial) if before is not None]==[before for _,before in initial if before is not None]
        assert redone==applied and again==undone
        assert all(len(state['undo'])<=UNDO_DEPTH for _,state in applied if state)
        results.append((initial,applied,[s for s,_ in undone],redone))
        catalog.close()
    assert results[0]==results[1]


@pytest.mark.parametrize('mutation',['edit','relink','history','redo-edit'])
def test_compact_group_refuses_after_member_edit_relink_or_history_change(catalog,tmp_path,mutation):
    ids,_=populate(catalog,tmp_path);rows=targets(capture(catalog,ids[0],ids[:3]),'selection')
    stacked(catalog,ids[:3],4,0)
    apply(catalog,rows,[changed_settings(r['settings'],bw()) for r in rows],compact=True)
    if mutation=='redo-edit':restore(catalog)
    if mutation in ('edit','redo-edit'):catalog.edit(ids[2],{**catalog.photo(ids[2])['settings'],'contrast':44})
    elif mutation=='relink':
        with catalog.db:catalog.db.execute('UPDATE photos SET path=? WHERE id=?',('moved.png',ids[2]))
    else:
        state=catalog.preference(f'undo:{ids[2]}');catalog.save_preference(f'undo:{ids[2]}',{**state,'redo':[state['undo'][0]]})
    before=photo_states(catalog,ids);group=catalog.preference(BATCH_KEY)
    with pytest.raises(BatchConflict) as caught:restore(catalog,mutation=='redo-edit')
    assert caught.value.kind==('history' if mutation=='history' else 'changed')
    assert photo_states(catalog,ids)==before and catalog.preference(BATCH_KEY)==group


def test_compact_group_size_and_speed(tmp_path):
    """500 photos with full 80-entry undo stacks: one settings object per photo is kept, and apply plus undo
    take about 5 s on the development PC (i9-12900K); the bound leaves room for a loaded test machine."""
    import time
    from luma.engine import defaults
    catalog=Catalog(tmp_path/'data');base=defaults()
    try:
        with catalog.db:
            for n in range(500):
                catalog.db.execute('INSERT INTO photos(path,name,edits,metadata,imported) VALUES(?,?,?,?,?)',
                    (str(tmp_path/f'p{n}.png'),f'p{n}.png',json.dumps({**base,'exposure':n/1000}),'{}','x'))
            ids=[r[0] for r in catalog.db.execute('SELECT id FROM photos ORDER BY id')]
            for ident in ids:
                state={'undo':[{**base,'exposure':-i/100} for i in range(UNDO_DEPTH)],'redo':[]}
                catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(f'undo:{ident}',json.dumps(state)))
        records=[{'id':i,'path':catalog.photo(i)['path'],'settings':catalog.photo(i)['settings']} for i in ids]
        before=catalog.preference(f'undo:{ids[7]}')
        started=time.perf_counter()
        assert len(apply(catalog,records,[{**r['settings'],'contrast':10.} for r in records],batch_key='undo:mcp:big',compact=True))==500
        applied=time.perf_counter()-started
        size=len(catalog.db.execute("SELECT value FROM preferences WHERE key='undo:mcp:big'").fetchone()[0])
        started=time.perf_counter();restore(catalog,batch_key='undo:mcp:big');undone=time.perf_counter()-started
        print(f'500 photos x {UNDO_DEPTH}: apply {applied:.2f} s, undo {undone:.2f} s, group {size/1e6:.2f} MB')
        assert size<2_000_000
        assert applied+undone<15
        assert catalog.preference(f'undo:{ids[7]}')==before and catalog.photo(ids[7])['settings']==records[7]['settings']
    finally:catalog.close()
