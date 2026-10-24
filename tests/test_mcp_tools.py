"""The AI connection's tools through the real server: a DesktopSession on a temporary library (Qt offscreen)."""
import base64
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog

from test_studio_ui import app,wait
from mcp_support import make_session,close_session,add_photos,gradient,call,start_call,finish,rpc
from luma import mcp_service,mcp_protocol,mcp_tools,photo_service
from luma.engine import defaults,load_image,resize_float,auto_tone_settings
from luma.i18n import set_language


@pytest.fixture(autouse=True)
def korean(monkeypatch):
    from luma import mcp_http
    monkeypatch.setattr(mcp_http,'POLL_SECONDS',.05)      # stopping a server takes milliseconds
    yield
    set_language('ko')


@pytest.fixture
def session(app,tmp_path):
    value=make_session(app,tmp_path)
    yield value
    close_session(app,value)


@pytest.fixture
def gate(monkeypatch):
    """Automatic-edit jobs wait in their worker until released; tools return a job id at once."""
    from luma import engine
    original=engine.load_image;release=threading.Event();entered=threading.Event()
    def slow(*args,**kwargs):
        entered.set();assert release.wait(30);return original(*args,**kwargs)
    monkeypatch.setattr(engine,'load_image',slow);monkeypatch.setattr(mcp_service,'WAIT_SECONDS',0)
    yield SimpleNamespace(release=release,entered=entered)
    release.set()


def settings_of(w,ids):return [w.catalog.photo(i)['settings'] for i in ids]


def wait_job(session,job,timeout=30):
    wait(lambda:session.mcp.jobs[job].state!='running',timeout*1000)
    return session.mcp.jobs[job]


# --- read tools -----------------------------------------------------------------------------------

def test_list_photos_pages_filters_and_folders(session,tmp_path):
    w=session.window;a=add_photos(w,tmp_path/'Roll 1',3);b=add_photos(w,tmp_path/'Roll 2',2)
    w.catalog.update(a[1],rating=4,flag=1,label='빨강');w.catalog.update(b[0],rating=5,flag=-1)
    page=call(session,'list_photos',{'limit':2}).value
    assert page['total']==5 and page['offset']==0 and page['next']==2 and [p['id'] for p in page['photos']]==a[:2]
    assert page['dirs']==[str(tmp_path/'Roll 1')] and page['photos'][1]=={'id':a[1],'name':'photo-1.png','r':4,'f':1,'l':'red'}
    last=call(session,'list_photos',{'limit':2,'offset':4}).value
    assert 'next' not in last and [p['id'] for p in last['photos']]==[b[1]] and last['photos'][0]=={'id':b[1],'name':'photo-1.png'}
    assert [p['id'] for p in call(session,'list_photos',{'folder':str(tmp_path/'Roll 2')}).value['photos']]==b
    assert [p['id'] for p in call(session,'list_photos',{'rating_min':4,'sort':'rating','descending':True}).value['photos']]==[b[0],a[1]]
    assert [p['id'] for p in call(session,'list_photos',{'flag':'rejected'}).value['photos']]==[b[0]]
    folders=call(session,'list_photos',{'group':'folders'}).value
    assert folders=={'total':2,'folders':[{'path':str(tmp_path/'Roll 1'),'n':3},{'path':str(tmp_path/'Roll 2'),'n':2}]}
    mixed=call(session,'list_photos',{'limit':5,'folder':str(tmp_path),'recursive':True}).value
    assert mixed['dirs']==[str(tmp_path/'Roll 1'),str(tmp_path/'Roll 2')] and mixed['photos'][3]['d']==1
    assert call(session,'list_photos',{'recursive':True}).error
    # folders by name: text is part of the folder path (any case, either slash); other filters are refused
    for text in ('roll 2',tmp_path.name+'/ROLL 2'):
        named=call(session,'list_photos',{'group':'folders','text':text}).value
        assert named=={'total':1,'folders':[{'path':str(tmp_path/'Roll 2'),'n':2}]},text
    assert call(session,'list_photos',{'group':'folders','text':'Roll','limit':1}).value['next']==1
    for extra in ({'rating_min':4},{'flag':'picked'},{'sort':'rating'},{'folder':str(tmp_path),'recursive':True}):
        refused=call(session,'list_photos',{'group':'folders',**extra})
        assert refused.error and "group 'folders' takes only folder, text, offset and limit" in refused.text,extra


def test_list_photos_selection_and_current_scope_and_rejects_both(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',4)
    w.activate(ids[1]);w.filmstrip.restore_selection([ids[1],ids[3]],ids[1])
    assert [p['id'] for p in call(session,'list_photos',{'scope':'selection'}).value['photos']]==[ids[1],ids[3]]
    assert [p['id'] for p in call(session,'list_photos',{'scope':'current'}).value['photos']]==[ids[1]]
    both=call(session,'list_photos',{'scope':'current','rating_min':1})
    assert both.error and 'give either filters or scope, not both' in both.text


def test_get_photo_reports_only_changed_values(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',2);other=add_photos(w,tmp_path/'q',1);copy=w.catalog.virtual_copy(ids[1])
    w.catalog.set_settings(ids[0],{**w.catalog.photo(ids[0])['settings'],'exposure':.304,'contrast':12.,'crop':[.1,.1,.9,.9]})
    value=call(session,'get_photo',{'ids':[ids[0],ids[1],987,other[0],copy]}).value
    first,second,third,fourth=value['photos']
    assert first['edits']=={'exposure':.3,'contrast':12} and first['other']==['crop'] and first['name']=='photo-0.png'
    assert 'edits' not in second and value['missing']==[987] and 'note' not in value
    # each folder once, as list_photos gives it; the file name only where it is not the photo's name
    assert value['dirs']==[str(tmp_path/'p'),str(tmp_path/'q')] and not [p for p in value['photos'] if 'path' in p]
    assert 'd' not in first and 'file' not in first and third['d']==1 and 'file' not in third and 'v' not in third
    assert fourth['name']=='photo-1.png · 사본' and fourth['file']=='photo-1.png' and fourth['v']==1 and 'd' not in fourth


def test_read_tool_while_slider_held_adds_no_undo_entry_and_notes_it(session,tmp_path,monkeypatch):
    w=session.window;ids=add_photos(w,tmp_path/'p',1);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    w.set_setting('exposure',.7);depth=len(w.undo_stack)
    monkeypatch.setattr(w,'interacting',lambda:True)
    value=call(session,'get_photo',{'ids':ids}).value
    assert value['note']=='the open photo has unsaved changes; values shown are the last saved ones'
    assert 'edits' not in value['photos'][0] and len(w.undo_stack)==depth and w.catalog.photo(ids[0])['settings']['exposure']==0
    monkeypatch.setattr(w,'interacting',lambda:False)
    value=call(session,'get_photo',{'ids':ids}).value
    assert 'note' not in value and value['photos'][0]['edits']=={'exposure':.7} and len(w.undo_stack)==depth+1


def test_get_preview_returns_bounded_jpeg(session,tmp_path,monkeypatch):
    from luma import mcp_tools
    w=session.window;ids=add_photos(w,tmp_path/'p',3,size=(900,600))
    Path(w.catalog.photo(ids[2])['path']).unlink()
    result=call(session,'get_preview',{'ids':ids,'size':300})
    assert not result.error and len(result.images)==2
    total=0
    for block,preview in zip(result.images,result.value['previews']):
        data=base64.b64decode(block['data']);total+=len(block['data'])
        assert block['mimeType']=='image/jpeg' and len(data)<=100_000
        with Image.open(io.BytesIO(data)) as image:
            assert max(image.size)<=300 and image.size==(preview['w'],preview['h']) and 'icc_profile' not in image.info
    assert total<=410_000 and result.value['errors']==[{'id':ids[2],'error':'original missing and no offline preview'}]
    monkeypatch.setattr(mcp_tools,'RESULT_BYTES',1)
    small=call(session,'get_preview',{'ids':ids[:2],'size':128})
    assert not small.images and all('left out to keep the reply small' in e['error'] for e in small.value['errors'])


def test_contact_sheet_is_one_labelled_image_and_reuses_current_thumbnails(session,tmp_path,monkeypatch):
    from luma import preview_store,photo_images
    w=session.window;ids=add_photos(w,tmp_path/'p',8);w.catalog.update(ids[0],rating=3,flag=1)
    for ident in ids:preview_store.rebuild(w.catalog.directory,ident,force=True)
    rendered=[];original=photo_images.render_preview
    monkeypatch.setattr(photo_images,'render_preview',lambda *a,**k:rendered.append(a[0]['id']) or original(*a,**k))
    result=call(session,'contact_sheet',{'ids':ids,'cell':120})
    assert not result.error and len(result.images)==1 and not rendered
    assert result.value=={'ids':ids,'columns':6,'total':8}
    with Image.open(io.BytesIO(base64.b64decode(result.images[0]['data']))) as sheet:
        assert sheet.size==(6*120+5*2,2*120+2) and len(base64.b64decode(result.images[0]['data']))<=200_000
    assert photo_images.mark(ids[0],3,1)==f'#{ids[0]}  3*  P'
    w.catalog.set_settings(ids[3],{**w.catalog.photo(ids[3])['settings'],'exposure':1.})      # its thumbnail is stale now
    paged=call(session,'contact_sheet',{'where':{'folder':str(tmp_path/'p')},'offset':2,'count':3}).value
    assert paged=={'ids':ids[2:5],'columns':3,'total':8,'next':5} and rendered==[ids[3]]


def test_contact_sheet_shows_a_36_frame_roll_in_one_call(session,tmp_path,monkeypatch):
    import numpy as np
    from luma import photo_images
    w=session.window;ids=add_photos(w,tmp_path/'roll',37,size=(60,40))
    monkeypatch.setattr(photo_images,'cached_thumbnail',lambda *args:None)
    monkeypatch.setattr(photo_images,'render_preview',lambda *args,**kwargs:np.full((20,30,3),128,np.uint8))
    result=call(session,'contact_sheet',{'where':{'folder':str(tmp_path/'roll')}})
    assert not result.error and result.value['ids']==ids[:36] and result.value['next']==36 and result.value['total']==37
    with Image.open(io.BytesIO(base64.b64decode(result.images[0]['data']))) as sheet:assert sheet.size==(970,970)
    rest=call(session,'contact_sheet',{'where':{'folder':str(tmp_path/'roll')},'offset':36}).value
    assert rest['ids']==ids[36:] and 'next' not in rest


# --- apply_edits ----------------------------------------------------------------------------------

def test_apply_edits_groups_relative_values_and_refreshes_open_photo(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',3);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    w.catalog.set_settings(ids[2],{**w.catalog.photo(ids[2])['settings'],'exposure':4.9})
    messages=[];w.statusBar().messageChanged.connect(messages.append)
    result=call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.3}},{'ids':[ids[0]],'set':{'temperature':-8}},
                                                {'where':{'folder':str(tmp_path/'p')},'add':{'contrast':5}}]})
    assert not result.error and result.value['changed']==3 and result.value['unchanged']==0 and result.value['errors']==[]
    assert result.value['batch'].startswith('b')
    values=settings_of(w,ids)
    assert values[0]['exposure']==pytest.approx(.3) and values[0]['temperature']==-8 and values[1]['temperature']==0
    assert values[2]['exposure']==5 and all(v['contrast']==5 for v in values)
    assert w.settings['exposure']==pytest.approx(.3) and w.adjustments['exposure'].value.value()==pytest.approx(.3)
    assert 'AI 보정 · 3장 변경' in messages
    w.undo();w.commit()
    assert w.catalog.photo(ids[0])['settings']['exposure']==0 and w.catalog.photo(ids[0])['settings']['temperature']==0
    snapshots=w.catalog.snapshots(ids[1]);assert len(snapshots)==1 and snapshots[0]['name'].startswith('AI 작업 전 · ')


def test_apply_edits_commits_pending_editor_change_first(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',1);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    w.set_setting('contrast',15)
    assert not call(session,'apply_edits',{'edits':[{'scope':'current','add':{'exposure':.5}}]}).error
    stored=w.catalog.photo(ids[0])['settings'];assert stored['contrast']==15 and stored['exposure']==.5
    w.undo();assert w.settings['contrast']==15 and w.settings['exposure']==0
    w.undo();assert w.settings['contrast']==0


@pytest.mark.parametrize('arguments',[
    {'edits':[{'ids':[1],'set':{'exposure':6}}]},{'edits':[{'ids':[1],'set':{'rotation':1}}]},
    {'edits':[{'ids':[1],'scope':'current','set':{'exposure':1}}]},{'edits':[{'set':{'exposure':1}}]},
    {'edits':[{'ids':[1]}]},{'edits':[{'ids':[1],'add':{'monochrome':True}}]},{'edits':[]},{'edit':[]}])
def test_apply_edits_invalid_arguments_change_nothing(session,tmp_path,arguments):
    w=session.window;ids=add_photos(w,tmp_path/'p',1);before=settings_of(w,ids)
    result=call(session,'apply_edits',arguments)
    assert result.error and settings_of(w,ids)==before and photo_service.batch_index(w.catalog)==[]


def test_apply_edits_reports_videos_missing_ids_unknown_sizes_and_non_negatives_per_photo(session,tmp_path):
    w=session.window;a,b,c=add_photos(w,tmp_path/'p',3);video=w.catalog.add(tmp_path/'p'/'clip.mp4')
    w.catalog.update(c,metadata=json.dumps({'width':240,'height':160}))
    result=call(session,'apply_edits',{'edits':[{'ids':[a,b,c,video,9999],'add':{'exposure':.2}},
        {'ids':[b],'set':{'straighten':3}},{'ids':[c],'add':{'negative_density':.5}}]})
    assert not result.error and result.value['changed']==1
    errors={e['id']:e['error'] for e in result.value['errors']}
    assert errors=={video:'video files cannot be edited',9999:'not in the catalog',
        b:f'photo {b}: its size is not known yet; open it once in Grainy',
        c:f'photo {c} is not an inverted negative; negative_density and negative_range apply only after invert_negatives'}
    assert w.catalog.photo(a)['settings']['exposure']==pytest.approx(.2)
    assert w.catalog.photo(b)['settings']['exposure']==0 and w.catalog.photo(c)['settings']['exposure']==0
    nothing=call(session,'apply_edits',{'edits':[{'ids':[video,9999],'add':{'exposure':.2}}]})
    assert nothing.error and nothing.text.startswith('none of the photos can be used')


# --- automatic edits and jobs ---------------------------------------------------------------------

def expected_tone(record):
    s=record['settings'];source,_=load_image(record['path'],720,s['working_space'],raw_options=s)
    return auto_tone_settings(resize_float(source,720),s)


def test_auto_tone_matches_menu_values_per_photo(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6)+add_photos(w,tmp_path/'q',1,low=.2,high=.9)
    expected=[expected_tone(w.catalog.photo(i))[0] for i in ids]
    result=call(session,'auto_tone',{'ids':ids})
    assert not result.error and result.value['changed']==3 and result.value['flat']==[] and result.value['edited_meanwhile']==[]
    assert settings_of(w,ids)==expected and result.value['batch'].startswith('b')
    flat=add_photos(w,tmp_path/'flat',1,low=.5,high=.5)
    assert call(session,'auto_tone',{'ids':flat}).value['flat']==flat


def test_auto_color_strength_and_statuses(session,tmp_path):
    from luma.auto_color import auto_color_settings
    w=session.window;cast=add_photos(w,tmp_path/'cast',1,tint=(1.,.92,.75));mono=add_photos(w,tmp_path/'mono',1)
    w.catalog.set_settings(mono[0],{**w.catalog.photo(mono[0])['settings'],'monochrome':True})
    record=w.catalog.photo(cast[0]);s=record['settings']
    expected=auto_color_settings(resize_float(load_image(record['path'],720,s['working_space'],raw_options=s)[0],720),s,.5)[0]
    result=call(session,'auto_color',{'ids':cast+mono,'strength':50})
    assert not result.error and result.value['skipped']['monochrome']==mono and result.value['changed']==1
    assert w.catalog.photo(cast[0])['settings']==expected
    # without strength: the strength last used in Grainy (30 here, not the 100 used when none was saved)
    fresh=add_photos(w,tmp_path/'fresh',1,tint=(1.,.92,.75));record=w.catalog.photo(fresh[0]);s=record['settings']
    source=resize_float(load_image(record['path'],720,s['working_space'],raw_options=s)[0],720)
    expected=auto_color_settings(source,s,.3)[0]
    assert expected!=auto_color_settings(source,s,1.)[0]
    w.catalog.save_preference('auto_color_amount',30)
    again=call(session,'auto_color',{'ids':fresh})
    assert not again.error and w.catalog.photo(fresh[0])['settings']==expected


def test_invert_negatives_single_and_roll_match_menu(session,tmp_path):
    from luma import negative
    w=session.window;ids=add_photos(w,tmp_path/'roll',3,tint=(1.,.7,.45),low=.2,high=.9)
    records=[w.catalog.photo(i) for i in ids]
    def source(r):
        s={**r['settings'],'negative':True}
        return resize_float(load_image(r['path'],720,s['working_space'],raw_options=s)[0],720),s
    single=[negative.converted(*source(r)) for r in records]
    from luma.engine import normalized
    result=call(session,'invert_negatives',{'ids':ids})
    assert not result.error and result.value['changed']==3
    assert settings_of(w,ids)==[normalized(v) for v in single]
    assert call(session,'undo_last_batch',{}).value['photos']==3
    samples=[negative.sample(*source(r)) for r in records]
    together=negative.converted_together(samples,[r['settings'] for r in records])
    roll=call(session,'invert_negatives',{'ids':ids,'roll':True})
    assert not roll.error and settings_of(w,ids)==[normalized(v) for v in together]
    assert photo_service.last_batch(w.catalog)['label']=='AI 네거티브 반전 (한 롤)'


def test_roll_needs_two_photos(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',1)
    result=call(session,'invert_negatives',{'ids':ids,'roll':True})
    assert result.error and 'at least 2 photos' in result.text and photo_service.batch_index(w.catalog)==[]


def test_long_job_returns_handle_and_get_job_finishes(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6)
    handle=call(session,'auto_tone',{'ids':ids}).value
    assert handle=={'job':'j1','state':'running','done':0,'total':2}
    assert w.mcp.stop_button.isVisible() or not w.isVisible()
    gate.release.set()
    pending=start_call(session,'get_job',{'job':'j1','wait':20})
    view=finish(pending).value
    assert view['state']=='done' and view['done']==2 and view['changed']==2 and view['job']=='j1' and view['tool']=='auto_tone'
    assert call(session,'get_job',{'job':'j77'}).error


def test_stop_button_cancels_job_and_changes_nothing(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6);before=settings_of(w,ids)
    assert call(session,'auto_tone',{'ids':ids}).value['state']=='running'
    assert gate.entered.wait(10)
    w.mcp.stop_button.click();gate.release.set()
    job=wait_job(session,'j1')
    assert job.state=='cancelled' and 'stopped in Grainy; nothing was changed' in job.error
    assert settings_of(w,ids)==before and photo_service.batch_index(w.catalog)==[]
    assert call(session,'get_job',{'job':'j1','wait':0}).value['state']=='cancelled'


def test_disabling_during_job_writes_nothing(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6);before=settings_of(w,ids)
    assert call(session,'auto_tone',{'ids':ids}).value['state']=='running'
    assert gate.entered.wait(10)
    session.mcp.stop();gate.release.set()
    job=wait_job(session,'j1')
    assert job.state=='cancelled' and job.error=='the AI connection was turned off or restarted; nothing was changed'
    wait(lambda:not w.jobs)
    assert settings_of(w,ids)==before


def test_job_finish_waits_for_closed_dialog_then_applies(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6)
    expected=[expected_tone(w.catalog.photo(i))[0] for i in ids]
    assert call(session,'auto_tone',{'ids':ids}).value['state']=='running'
    dialog=QDialog(w);dialog.open()
    gate.release.set()
    wait(lambda:session.mcp.jobs['j1'].phase=='waiting for Grainy',15000)
    assert settings_of(w,ids)!=expected and call(session,'get_job',{'job':'j1','wait':0}).value['phase']=='waiting for Grainy'
    dialog.close()
    job=wait_job(session,'j1')
    assert job.state=='done' and settings_of(w,ids)==expected


def test_photo_edited_or_relinked_during_job_is_left_out(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',3,low=.3,high=.6)
    assert call(session,'auto_tone',{'ids':ids}).value['state']=='running'
    assert gate.entered.wait(10)
    w.catalog.edit(ids[1],{**w.catalog.photo(ids[1])['settings'],'contrast':30})
    moved=gradient(tmp_path/'moved'/'photo-2.png',low=.3,high=.6)
    with w.catalog.db:w.catalog.db.execute('UPDATE photos SET path=? WHERE id=?',(str(moved),ids[2]))
    gate.release.set()
    job=wait_job(session,'j1')
    assert job.state=='done' and job.result['edited_meanwhile']==[ids[1],ids[2]] and job.result['changed']==1
    assert w.catalog.photo(ids[1])['settings']['contrast']==30 and w.catalog.photo(ids[2])['settings']['exposure']==0


@pytest.mark.parametrize('state',['modal','maintenance','import','command','interacting','stroke','crop','transaction'])
def test_busy_states_are_tool_errors_saying_nothing_changed(session,tmp_path,monkeypatch,state):
    w=session.window;ids=add_photos(w,tmp_path/'p',2);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    before=settings_of(w,ids);undo=[]
    if state=='modal':dialog=QDialog(w);dialog.open();undo.append(dialog.close)
    elif state=='maintenance':w.maintenance_running=True;undo.append(lambda:setattr(w,'maintenance_running',False))
    elif state=='import':w.import_busy=True;undo.append(lambda:setattr(w,'import_busy',False))
    elif state=='command':w.command_running=True;undo.append(lambda:setattr(w,'command_running',False))
    elif state=='interacting':monkeypatch.setattr(w,'interacting',lambda:True)
    elif state=='stroke':monkeypatch.setattr(w.studio,'pending_stroke',{'points':[]})
    elif state=='crop':w.start_crop();undo.append(w.cancel_crop)
    else:w.catalog.db.execute('BEGIN');undo.append(w.catalog.db.rollback)
    try:
        result=call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.4}}]})
        assert result.error and result.text.startswith('Grainy is busy: ') and result.text.endswith('Nothing was changed; try again in a moment.')
        if state in ('interacting','stroke','crop'):
            assert not call(session,'apply_edits',{'edits':[{'ids':[ids[1]],'add':{'exposure':.4}}]}).error
            before[1]=w.catalog.photo(ids[1])['settings']
    finally:
        for step in undo:step()
    assert settings_of(w,ids)==before


def test_initialize_and_tools_list_answer_while_a_modal_dialog_is_open(session,tmp_path):
    from mcp_support import Pending
    port,token=session.mcp.bound_port,session.mcp.token
    pending=[Pending(port,token,{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}}),
             Pending(port,token,{'jsonrpc':'2.0','id':2,'method':'tools/list'}),Pending(port,token,{'jsonrpc':'2.0','id':3,'method':'ping'})]
    blocked=time.monotonic()              # the GUI thread is blocked, as by a long task or a native dialog
    while not all(p.done() for p in pending) and time.monotonic()-blocked<8:time.sleep(.05)
    assert all(p.done() for p in pending) and all(p.seconds<3 for p in pending)
    assert pending[0].value[1]['result']['protocolVersion']=='2025-06-18' and len(pending[1].value[1]['result']['tools'])==len(mcp_tools.DEFINITIONS)
    dialog=QDialog(session.window);dialog.open()
    try:
        assert not call(session,'list_photos',{}).error
        assert 'a dialog or menu is open' in call(session,'apply_edits',{'edits':[{'scope':'selection','add':{'exposure':1}}]}).text
    finally:dialog.close()


def test_every_tool_replies_within_deadline(session,tmp_path,monkeypatch):
    from mcp_support import Pending
    from luma import photo_images
    monkeypatch.setattr(mcp_protocol,'DEADLINE_SECONDS',2);monkeypatch.setattr(mcp_service,'GRACE_SECONDS',1)
    w=session.window;ids=add_photos(w,tmp_path/'p',2)
    monkeypatch.setattr(photo_images,'render_preview',lambda *a,**k:time.sleep(5))
    arguments={'list_photos':{},'get_photo':{'ids':ids},'get_preview':{'ids':ids},'contact_sheet':{'ids':ids},
        'apply_edits':{'edits':[{'ids':ids,'add':{'exposure':.1}}]},'auto_tone':{'ids':ids},'auto_color':{'ids':ids},
        'invert_negatives':{'ids':ids},'undo_last_batch':{},'export_photos':{'ids':ids},'open_photo':{'id':ids[0]},
        'get_job':{'job':'j1','wait':40},'list_films':{'type':'presets'},'import_folder':{'path':str(tmp_path/'p')},
        'create_virtual_copies':{'id':ids[0],'count':1},'set_labels':{'ids':ids,'rating':1},
        'snapshot':{'ids':ids,'action':'save','name':'x'}}
    assert set(arguments)=={tool['name'] for tool in mcp_tools.DEFINITIONS}
    port,token=session.mcp.bound_port,session.mcp.token
    calls=[Pending(port,token,{'jsonrpc':'2.0','id':n,'method':'tools/call','params':{'name':name,'arguments':args}})
           for n,(name,args) in enumerate(arguments.items())]
    time.sleep(4)                       # the GUI thread is blocked through every request's deadline
    wait(lambda:all(p.done() for p in calls),20000)
    assert all(p.error is None and p.value[0]==200 for p in calls)
    assert max(p.seconds for p in calls)<2+1+1.5
    slow=photo_service.freeze
    monkeypatch.setattr(photo_service,'freeze',lambda *a:(time.sleep(4),slow(*a))[1])
    pending=start_call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.1}}]});result=finish(pending)
    assert result.error and 'did not finish it within the time limit' in result.text and pending.seconds<2+1+1
    # Preparation is read-only; a timed-out preparation must never advance to the write phase.
    assert photo_service.batch_index(w.catalog)==[] and all(s['exposure']==0 for s in settings_of(w,ids))


def test_one_write_job_at_a_time(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6)
    assert call(session,'auto_tone',{'ids':ids}).value['state']=='running'
    second=call(session,'auto_color',{'ids':ids})
    assert second.error and 'job j1 (auto_tone) is still running; call get_job' in second.text
    assert 'still running' in call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':1}}]}).text
    gate.release.set();assert wait_job(session,'j1').state=='done'


def test_undo_last_batch_undo_redo_and_conflict(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',2)
    bid=call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.5}}]}).value['batch']
    assert call(session,'undo_last_batch',{}).value=={'undone':bid,'tool':'apply_edits','photos':2}
    assert all(s['exposure']==0 for s in settings_of(w,ids)) and w.statusBar().currentMessage()=='AI 실행 취소 · 2장 변경'
    assert call(session,'undo_last_batch',{'redo':True}).value=={'redone':bid,'tool':'apply_edits','photos':2}
    assert 'is not the most recent batch' in call(session,'undo_last_batch',{'batch':'b000000'}).text
    w.catalog.edit(ids[1],{**w.catalog.photo(ids[1])['settings'],'contrast':9})
    conflict=call(session,'undo_last_batch',{'batch':bid})
    assert conflict.error and 'can no longer be undone' in conflict.text and w.catalog.photo(ids[0])['settings']['exposure']==.5


def test_export_default_folder_with_subfolder_never_overwrites_and_keeps_quick_export_preference(session,tmp_path):
    from luma.quick_export import PREFERENCE
    w=session.window;ids=add_photos(w,tmp_path/'p',2);w.catalog.save_preference(PREFERENCE,{'folder':'kept'})
    first=call(session,'export_photos',{'ids':ids,'subfolder':'Roll 12','quality':80}).value
    root=mcp_service.pictures_folder()
    assert first['exported']==2 and first['failed']==0 and Path(first['folder'])==(root/'Roll 12').resolve()
    second=call(session,'export_photos',{'ids':ids,'subfolder':'Roll 12','format':'PNG','naming':'R12_{n:02d}'}).value
    third=call(session,'export_photos',{'ids':ids,'subfolder':'Roll 12'}).value
    names=sorted(p.name for p in (root/'Roll 12').iterdir())
    assert names==['R12_01.png','R12_02.png','photo-0_grainy.jpg','photo-0_grainy_2.jpg','photo-1_grainy.jpg','photo-1_grainy_2.jpg']
    assert second['exported']==third['exported']==2 and w.catalog.preference(PREFERENCE)=={'folder':'kept'}
    assert len(w.catalog.preference('export_history',[]))==6
    for bad in ({'ids':ids,'subfolder':'../out'},{'ids':ids,'naming':'{n:>9}'},{'ids':ids,'format':'JPEG XL','color_space':'Display P3'}):
        assert call(session,'export_photos',bad).error


def test_export_last_destination_requires_previous_export(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',1)
    result=call(session,'export_photos',{'ids':ids,'destination':'last'})
    assert result.error and result.text=="the person has not exported from Grainy yet; use destination 'default'"
    folder=tmp_path/'mine';folder.mkdir();w.start_export(ids,folder);wait(lambda:not w.export_running)
    value=call(session,'export_photos',{'ids':ids,'destination':'last','subfolder':'ai'}).value
    assert Path(value['folder'])==(folder/'ai').resolve() and value['exported']==1
    folder.rename(tmp_path/'elsewhere')              # as when the drive of the last export is not connected now
    gone=call(session,'export_photos',{'ids':ids,'destination':'last'})
    assert gone.error and gone.text.startswith(f"the folder of the person's last export ({folder}) is not available now")
    assert gone.text.endswith("use destination 'default'") and 'has not exported' not in gone.text


def test_export_refused_while_exporting(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',1);w.export_running=True
    try:
        result=call(session,'export_photos',{'ids':ids})
        assert result.error and 'an export is already running' in result.text
    finally:w.export_running=False


def test_open_photo_shows_photo_without_changing_filters(session,tmp_path):
    w=session.window;a=add_photos(w,tmp_path/'A',2);b=add_photos(w,tmp_path/'B',1)
    w.select_folder(str(tmp_path/'A'));wait(lambda:not w.library_loading)
    folder,mode=w.folder_filter,w.filter_mode
    assert call(session,'open_photo',{'id':a[1]}).value=={'opened':a[1]}
    assert w.current_id==a[1] and w.stack.currentIndex()==0 and w.active_list().selected_ids()==[a[1]]
    value=call(session,'open_photo',{'id':b[0],'view':'library'}).value
    assert value=={'opened':b[0],'note':'not in the current list view (filters or another folder are shown)'}
    assert w.current_id==b[0] and w.stack.currentIndex()==1 and w.folder_filter==folder and w.filter_mode==mode
    assert call(session,'open_photo',{'id':999}).error


def test_language_switch_cancels_job_and_server_keeps_answering(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6);before=settings_of(w,ids)
    assert call(session,'auto_tone',{'ids':ids}).value['state']=='running'
    assert gate.entered.wait(10)
    threading.Timer(.3,gate.release.set).start()
    assert session.change_language('en')
    job=session.mcp.jobs['j1']
    assert job.state=='cancelled' and job.error=='Grainy closed or reopened its window; nothing was changed'
    assert session.window is not w and session.window.mcp.service is session.mcp
    assert [p['id'] for p in call(session,'list_photos',{}).value['photos']]==ids
    assert settings_of(session.window,ids)==before
    messages=[];session.window.statusBar().messageChanged.connect(messages.append)
    assert not call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.1}}]}).error
    assert 'AI edit · 2 photos changed' in messages


def test_close_cancels_running_job_and_stops_server(session,tmp_path,gate):
    w=session.window;ids=add_photos(w,tmp_path/'p',2,low=.3,high=.6);port=session.mcp.bound_port
    assert call(session,'auto_tone',{'ids':ids}).value['state']=='running'
    assert gate.entered.wait(10)
    quits=[];session.app=SimpleNamespace(quit=lambda:quits.append(1))
    threading.Timer(.3,gate.release.set).start()
    assert w.close()
    assert session.mcp.jobs['j1'].state=='cancelled'
    session.last_window_closed()        # what Qt's app.exec() calls once the last window is closed
    assert not session.mcp.running
    wait(lambda:quits==[1])
    import socket
    with pytest.raises(OSError):socket.create_connection(('127.0.0.1',port),timeout=1).close()
    assert not (session.mcp.folder/'endpoint').exists()


# --- virtual copies, presets, copied settings, curves, snapshots, labels, films, import, export to a folder -----------

def test_virtual_copies_start_from_the_open_edits_and_take_their_own_film_looks(session,tmp_path):
    from luma import film_look
    w=session.window;ids=add_photos(w,tmp_path/'p',1);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    w.set_setting('exposure',.6)                          # not saved yet: the copies start from what the editor shows
    copies=call(session,'create_virtual_copies',{'id':ids[0],'count':3}).value['copies']
    assert len(set(copies))==3 and ids[0] not in copies
    master=w.catalog.photo(ids[0])
    for copy in copies:
        row=w.catalog.photo(copy)
        assert row['virtual_source']==ids[0] and row['path']==master['path'] and row['settings']==master['settings']
    assert master['settings']['exposure']==pytest.approx(.6)
    films=[entry['name'] for entry in film_look.catalog()[:3]]
    result=call(session,'apply_edits',{'edits':[{'ids':[c],'set':{'film_look':f}} for c,f in zip(copies,films)]})
    assert not result.error and result.value['changed']==3
    assert [w.catalog.photo(c)['settings']['film_look'] for c in copies]==[film_look.find(f)[0] for f in films]
    listed=call(session,'list_photos',{}).value['photos']
    assert [p.get('v') for p in listed]==[None,1,1,1]
    assert call(session,'undo_last_batch',{}).value['photos']==3
    assert all(w.catalog.photo(c) is not None for c in copies)          # undoing the edits leaves the copies
    video=w.catalog.add(tmp_path/'p'/'clip.mp4')
    for bad,text in (({'id':ids[0],'count':21},'at most 20'),({'id':ids[0],'count':0},'at least 1'),
                     ({'id':999,'count':1},'not in the catalog'),({'id':video,'count':1},'is a video')):
        reply=call(session,'create_virtual_copies',bad);assert reply.error and text in reply.text,bad


def test_apply_edits_presets_copied_settings_and_curves_in_one_undoable_batch(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',4)
    curve=[[0,0],[.3,.2],[.7,.85],[1,1]];red=[[0,0],[.5,.6],[1,1]]
    source={**w.catalog.photo(ids[0])['settings'],'crop':[.1,.1,.9,.9],'panels_off':['effects'],'vignette':-20.}
    w.catalog.set_settings(ids[0],source)
    shaped=call(session,'apply_edits',{'edits':[{'ids':[ids[0]],'set':{'curve':curve,'rgb_curves':[red,[[0,0],[1,1]],[[0,0],[1,1]]]},
                                                 'add':{'exposure':.4}}]})
    assert not shaped.error and shaped.value['changed']==1
    seen=call(session,'get_photo',{'ids':[ids[0]]}).value['photos'][0]['edits']
    assert seen['curve']==curve and seen['rgb_curves'][0]==red and seen['exposure']==.4
    w.catalog.save_preset('Mine',{**w.catalog.photo(ids[3])['settings'],'contrast':22.,'vibrance':15.});w.refresh_presets()
    w.catalog.set_settings(ids[3],{**w.catalog.photo(ids[3])['settings'],'crop':[.2,.2,.8,.8]})
    result=call(session,'apply_edits',{'edits':[{'ids':ids[1:3],'copy_from':ids[0]},{'ids':[ids[2]],'add':{'exposure':.1}},
                                                {'ids':[ids[3]],'preset':'mine','add':{'contrast':3}}]})
    assert not result.error and result.value['changed']==3,result
    one,two,three=settings_of(w,ids[1:])
    assert one['curve']==[[float(x),float(y)] for x,y in curve] and one['exposure']==pytest.approx(.4) and one['crop'] is None
    assert one['panels_off']==['effects'] and one['vignette']==-20                  # the pasted panel stays switched off
    assert two['exposure']==pytest.approx(.5) and two['rgb_curves'][0]==[[0.,0.],[.5,.6],[1.,1.]]
    assert three['contrast']==25 and three['vibrance']==15 and three['crop']==[.2,.2,.8,.8]    # a preset keeps the framing
    assert w.catalog.photo(ids[3])['user_metadata'][photo_service.PRESET_KEY]=='Mine'
    framed=call(session,'apply_edits',{'edits':[{'ids':[ids[1]],'copy_from':ids[0],'groups':['crop']}]})
    assert framed.value['changed']==1 and w.catalog.photo(ids[1])['settings']['crop']==[.1,.1,.9,.9]
    assert call(session,'undo_last_batch',{}).value['photos']==1
    assert call(session,'undo_last_batch',{}).value=={'undone':result.value['batch'],'tool':'apply_edits','photos':3}
    assert all(s['curve']==defaults()['curve'] and s['exposure']==0 for s in settings_of(w,ids[1:3]))
    assert settings_of(w,[ids[3]])[0]['contrast']==0 and photo_service.PRESET_KEY not in w.catalog.photo(ids[3])['user_metadata']
    for bad,text in (({'ids':[ids[1]],'preset':'Nope'},"no preset named 'Nope'; presets: "),
                     ({'ids':[ids[1]],'preset':'Mine','copy_from':ids[0]},'give preset or copy_from, not both'),
                     ({'ids':[ids[1]],'groups':['crop'],'add':{'exposure':1}},'groups needs copy_from'),
                     ({'ids':[ids[1]],'copy_from':999},'copy_from photo 999 is not in the catalog'),
                     ({'ids':[ids[1]],'groups':['crop']},'needs preset, copy_from, set or add'),
                     ({'ids':[ids[1]],'copy_from':ids[0],'groups':['sky']},'must be one of'),
                     ({'ids':[ids[1]],'set':{'curve':[[0,0],[.5,1.5],[1,1]]}},'curve[1][1] must be at most 1'),
                     ({'ids':[ids[1]],'set':{'curve':[[0,0],[.6,.5],[.5,.6],[1,1]]}},'curve must be 2 to 16'),
                     ({'ids':[ids[1]],'set':{'film_look':'zzzz qqqq'}},'call list_films')):
        reply=call(session,'apply_edits',{'edits':[bad]});assert reply.error and text in reply.text,(bad,reply.text)
    assert 'Soft daylight' in call(session,'apply_edits',{'edits':[{'ids':[ids[1]],'preset':'Nope'}]}).text


def test_snapshots_save_list_and_restore_as_an_undoable_batch(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',2)
    assert not call(session,'apply_edits',{'edits':[{'ids':ids,'set':{'exposure':.5}}]}).error
    assert call(session,'snapshot',{'ids':ids,'action':'save','name':' warm '}).value=={'saved':2,'errors':[]}
    assert not call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.5,'contrast':10}}]}).error
    listed=call(session,'snapshot',{'ids':ids+[999],'action':'list'}).value
    names=[s['name'] for s in listed['photos'][0]['snapshots']]
    assert names[0]=='warm' and names[1].startswith('AI 작업 전 · ') and listed['missing']==[999]
    assert [s['name'] for s in w.catalog.snapshots(ids[0])][0]=='warm'           # the ones Grainy's Snapshot menu shows
    restored=call(session,'snapshot',{'where':{'folder':str(tmp_path/'p')},'action':'restore','name':'WARM'}).value
    assert restored['changed']==2 and all(s['exposure']==.5 and s['contrast']==0 for s in settings_of(w,ids))
    assert photo_service.last_batch(w.catalog)['tool']=='snapshot'
    assert call(session,'undo_last_batch',{}).value['tool']=='snapshot'
    assert all(s['exposure']==1 and s['contrast']==10 for s in settings_of(w,ids))
    missing=call(session,'snapshot',{'ids':ids,'action':'restore','name':'cool'})
    assert missing.error and "no snapshot named 'cool'" in missing.text
    for bad in ({'ids':ids,'action':'save'},{'ids':ids,'action':'restore','name':'  '},{'ids':ids,'action':'drop','name':'x'}):
        assert call(session,'snapshot',bad).error,bad


def test_set_labels_changes_rating_flag_and_label_in_a_batch(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',3)
    value=call(session,'set_labels',{'ids':ids[:2]+[999],'rating':4,'flag':'picked','label':'green'}).value
    assert value.pop('batch').startswith('b')
    assert value=={'changed':2,'unchanged':0,'errors':[{'id':999,'error':'not in the catalog'}]}
    assert [(r['rating'],r['flag'],r['label']) for r in map(w.catalog.photo,ids)]==[(4,1,'초록'),(4,1,'초록'),(0,0,'')]
    assert call(session,'set_labels',{'ids':ids[:2],'rating':4}).value['unchanged']==2
    assert call(session,'set_labels',{'where':{'label':'green'},'label':'none','flag':'rejected'}).value['changed']==2
    assert [p.get('f') for p in call(session,'list_photos',{}).value['photos']]==[-1,-1,None]
    assert w.catalog.photo(ids[0])['label']=='' and photo_service.last_batch(w.catalog)['tool']=='set_labels'
    for bad in ({'ids':ids},{'ids':ids,'rating':6},{'ids':ids,'label':'pink'}):assert call(session,'set_labels',bad).error,bad


def test_list_films_by_type_and_the_presets(session,tmp_path):
    from luma import film_look
    films=call(session,'list_films',{}).value['films']
    assert len(films)==len(film_look.catalog()) and set(films[0])>={'name','iso','type','maker'}
    bw=call(session,'list_films',{'type':'bw'}).value['films']
    assert bw and all(f['type']=='bw' for f in bw) and any(f.get('cine') for f in bw)     # Double-X is a b&w cine stock
    cine=call(session,'list_films',{'type':'cine'}).value['films']
    assert cine and all(f['cine']==1 for f in cine) and {f['type'] for f in cine}=={'color','bw','slide'}
    w=session.window;w.catalog.save_preset('Mine',defaults());w.refresh_presets()
    presets=call(session,'list_films',{'type':'presets'}).value
    assert 'Soft daylight' in presets['built_in'] and presets['saved']==['Mine']


def test_import_folder_adds_new_photos_once_and_reports_their_ids(session,tmp_path,tmp_path_factory):
    w=session.window;folder=tmp_path_factory.mktemp('scans')
    files=[gradient(folder/f'scan-{n}.png') for n in range(2)]+[gradient(folder/'sub'/'deep.png')]
    (folder/'notes.txt').write_text('x',encoding='utf-8')
    before=[f.read_bytes() for f in files]
    first=call(session,'import_folder',{'path':str(folder)}).value
    assert first['imported']==2 and first['already']==0 and first['recursive'] is False and len(first['ids'])==2
    assert sorted(Path(w.catalog.photo(i)['path']).name for i in first['ids'])==['scan-0.png','scan-1.png']
    assert [p['id'] for p in call(session,'list_photos',{'folder':first['folder']}).value['photos']]==first['ids']
    second=call(session,'import_folder',{'path':str(folder),'recursive':True}).value
    assert second['imported']==1 and second['already']==2 and Path(w.catalog.photo(second['ids'][0])['path']).name=='deep.png'
    again=call(session,'import_folder',{'path':str(folder),'recursive':True}).value
    assert again=={'imported':0,'ids':[],'already':3,'folder':first['folder'],'recursive':True}
    assert [f.read_bytes() for f in files]==before and len(w.catalog.photos())==3
    assert [job.tool for job in session.mcp.jobs.values()]==['import_folder']*3
    for bad,text in (('relative','absolute'),(str(folder/'missing'),'not a folder'),(str(files[0]),'not a folder'),
                     (str(w.catalog.directory),'a folder Grainy manages itself'),(str(session.mcp.folder),'a folder Grainy manages itself')):
        reply=call(session,'import_folder',{'path':bad});assert reply.error and text in reply.text,(bad,reply.text)


def test_import_folder_reports_unreadable_files_in_its_result_without_a_message_box(session,tmp_path_factory,monkeypatch):
    # A modal message box would leave every later AI call busy until a person closes it.
    import luma.app
    shown=[];monkeypatch.setattr(luma.app.QMessageBox,'information',lambda *a,**k:shown.append(a[1:]))
    w=session.window;folder=tmp_path_factory.mktemp('mixed')
    gradient(folder/'good.png');(folder/'broken.png').write_bytes(b'not an image')
    value=call(session,'import_folder',{'path':str(folder)}).value
    assert value['imported']==1 and value['not_imported']==1 and value['failures']==['broken.png']
    assert not shown and not w.import_quiet
    assert not call(session,'apply_edits',{'edits':[{'ids':value['ids'],'add':{'exposure':.3}}]}).error
    w.import_paths([str(folder)])                                               # the person imports the same folder
    wait(lambda:not w.import_busy and not w.import_scans and not w.import_queue)
    assert len(shown)==1 and 'broken.png' in shown[0][1]                       # people still see what could not be read


def test_export_to_a_named_folder_with_a_size_box_and_metadata_names(session,tmp_path,tmp_path_factory):
    from luma import film_look
    w=session.window;ids=add_photos(w,tmp_path/'p',2,size=(240,160))
    film=film_look.catalog()[0]['name']
    assert not call(session,'apply_edits',{'edits':[{'ids':[ids[0]],'set':{'film_look':film}},
                                                    {'ids':[ids[1]],'preset':'Soft daylight'}]}).error
    out=tmp_path_factory.mktemp('exports')/'roll 12'/'web'                 # made when missing
    arguments={'ids':ids,'path':str(out),'width':120,'naming':'{film}_{preset}_{stem}','format':'PNG'}
    value=call(session,'export_photos',arguments).value
    assert value['exported']==2 and value['failed']==0 and Path(value['folder'])==out.resolve()
    assert sorted(p.name for p in out.iterdir())==sorted([f'{film}_photo-0.png','Soft daylight_photo-1.png'])
    for path in out.iterdir():
        with Image.open(path) as image:assert image.size==(120,80)
    again=call(session,'export_photos',{**arguments,'width':100,'height':100,'fit':'cover','destination':'path'}).value
    assert again['exported']==2 and len(list(out.iterdir()))==4                   # never replaced: a number is added
    with Image.open(out/'Soft daylight_photo-1_2.png') as image:assert image.size==(150,100)
    for bad,text in (({'ids':ids,'path':'relative/out'},'absolute'),({'ids':ids,'path':str(w.catalog.directory/'x')},'manages itself'),
                     ({'ids':ids,'path':str(session.mcp.folder)},'manages itself'),
                     ({'ids':ids,'path':str(out),'subfolder':'x','longest':100,'width':50},'not both'),
                     ({'ids':ids,'path':str(out),'fit':'cover'},'fit needs width'),({'ids':ids,'destination':'path'},'needs path'),
                     ({'ids':ids,'destination':'default','path':str(out)},'needs path'),
                     ({'ids':ids,'naming':'{stem}_{lens}'},'naming may contain only')):
        reply=call(session,'export_photos',bad);assert reply.error and text in reply.text,(bad,reply.text)
    assert not (w.catalog.directory/'x').exists()
