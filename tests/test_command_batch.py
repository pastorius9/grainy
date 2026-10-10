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
