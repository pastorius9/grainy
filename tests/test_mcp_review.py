"""Regression cases for the MCP review, using disposable libraries only."""
import threading
import time
import json
from pathlib import Path

import numpy as np
import pytest

from test_studio_ui import app,wait
from test_mcp_tools import session,korean,settings_of,wait_job
from mcp_support import add_photos,gradient,call,start_call,finish
from luma import mcp_service,mcp_protocol,mcp_tools,photo_service


@pytest.mark.parametrize('tool', ['apply_edits','open_photo'])
def test_pending_healing_blocks_reload(session,tmp_path,tool):
    w=session.window;ids=add_photos(w,tmp_path/'photos',2);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    marks=[{'points':[(.3,.4)],'radius':.02}];w.studio.healing_strokes=marks.copy()
    args={'edits':[{'ids':[ids[0]],'add':{'exposure':.5}}]} if tool=='apply_edits' else {'id':ids[1]}
    result=call(session,tool,args)
    assert result.error and 'unfinished brush or healing marks' in result.text
    assert w.studio.healing_strokes==marks and w.current_id==ids[0]


def test_import_completion_failure_finishes_job(session,tmp_path_factory,monkeypatch):
    w=session.window;folder=tmp_path_factory.mktemp('import-failure');gradient(folder/'scan.png')
    shown=[];monkeypatch.setattr(w,'show_error',lambda *a:shown.append(a))
    monkeypatch.setattr(w.catalog,'add',lambda *a,**k:(_ for _ in ()).throw(OSError('저장 실패')))
    monkeypatch.setattr(mcp_service,'WAIT_SECONDS',0)
    job=call(session,'import_folder',{'path':str(folder)}).value['job']
    try:
        wait(lambda:session.mcp.jobs[job].state!='running' or shown,5000)
        value=session.mcp.jobs[job]
        assert value.state=='failed' and value.finished.is_set()
        assert not w.import_busy and not w.import_pending_paths and not w.import_quiet and not shown
    finally:w.cancel_import();w.import_busy=False


def test_export_history_failure_keeps_outputs_and_finishes(session,tmp_path,monkeypatch):
    from luma import workflow_data
    w=session.window;ids=add_photos(w,tmp_path/'photos',1);shown=[]
    monkeypatch.setattr(w,'show_error',lambda *a:shown.append(a))
    monkeypatch.setattr(workflow_data,'record_exports',lambda *a:(_ for _ in ()).throw(OSError('이력 저장 실패')))
    monkeypatch.setattr(mcp_service,'WAIT_SECONDS',0)
    job=call(session,'export_photos',{'ids':ids}).value['job']
    wait(lambda:session.mcp.jobs[job].state!='running' or shown,10000)
    value=session.mcp.jobs[job]
    assert value.state=='failed' and value.finished.is_set()
    assert value.result['exported']==1 and Path(value.result['files'][0]['path']).is_file()
    assert not w.export_running and not shown


@pytest.mark.parametrize('tool',['get_preview','contact_sheet'])
def test_started_render_obeys_deadline_and_releases_lock(session,tmp_path,monkeypatch,tool):
    from luma import photo_images
    ids=add_photos(session.window,tmp_path/'photos',1)
    entered=threading.Event();release=threading.Event()
    def slow(*a,**k):
        entered.set();release.wait(4);return np.zeros((20,30,3),np.uint8)
    monkeypatch.setattr(photo_images,'render_preview',slow)
    monkeypatch.setattr(photo_images,'cached_thumbnail',lambda *a:None)
    monkeypatch.setattr(mcp_protocol,'DEADLINE_SECONDS',.4)
    monkeypatch.setattr(mcp_tools,'RENDER_MARGIN',0)
    timer=threading.Timer(2,release.set);timer.start()
    try:
        request=start_call(session,tool,{'ids':ids})
        wait(entered.is_set,2000)
        result=finish(request,5)
        assert request.seconds<1.4 and not release.is_set(),result
        assert session.mcp.render_lock.acquire(blocking=False)
        session.mcp.render_lock.release()
    finally:release.set();timer.cancel()


def test_export_folder_checks_stay_off_gui_thread(session,tmp_path,monkeypatch):
    ids=add_photos(session.window,tmp_path/'photos',1)
    original=Path.is_dir;threads=[];destination=mcp_service.pictures_folder()
    def checked(path):
        if path==destination:threads.append(threading.current_thread())
        return original(path)
    monkeypatch.setattr(Path,'is_dir',checked)
    result=call(session,'export_photos',{'ids':ids})
    assert not result.error
    assert all(t is not threading.main_thread() for t in threads)


def test_recursive_import_uses_only_scanned_files(session,tmp_path_factory,monkeypatch):
    w=session.window;folder=tmp_path_factory.mktemp('scan-once');gradient(folder/'first.png')
    original=photo_service.image_files
    def scan(*a,**k):
        found=original(*a,**k);gradient(folder/'late.png');return found
    monkeypatch.setattr(photo_service,'image_files',scan)
    result=call(session,'import_folder',{'path':str(folder),'recursive':True})
    assert not result.error and result.value['imported']==1
    assert [p['name'] for p in w.catalog.photos()]==['first.png']
    assert not w.import_quiet and not w.import_pending_paths


def test_copy_source_busy_is_rejected(session,tmp_path,monkeypatch):
    w=session.window;a,b=add_photos(w,tmp_path/'photos',2);w.activate(a)
    wait(lambda:w.source is not None and not w.render_running)
    w.set_setting('exposure',.7);monkeypatch.setattr(w,'interacting',lambda:True)
    result=call(session,'apply_edits',{'edits':[{'ids':[b],'copy_from':a}]})
    assert result.error and 'adjusting the open photo' in result.text
    assert w.catalog.photo(b)['settings']['exposure']==0


def test_copy_source_sees_earlier_entry(session,tmp_path):
    w=session.window;a,b=add_photos(w,tmp_path/'photos',2)
    result=call(session,'apply_edits',{'edits':[{'ids':[a],'set':{'exposure':1}}, {'ids':[b],'copy_from':a}]})
    assert not result.error and [v['exposure'] for v in settings_of(w,[a,b])]==[1,1]


def test_whole_settings_folder_is_protected(session,tmp_path):
    from luma.preferences import Preferences
    session.preferences=Preferences(tmp_path/'private-settings'/'settings.json')
    root=session.preferences.path.parent
    with pytest.raises(photo_service.ServiceError,match='manages itself'):
        photo_service.named_folder(str(root/'exports'),session.mcp.own_folders())


def test_import_checks_resolved_files(session,tmp_path_factory,monkeypatch):
    w=session.window;folder=tmp_path_factory.mktemp('resolved');file=gradient(folder/'linked.png')
    protected=gradient(w.catalog.directory/'private.png');original=Path.resolve
    def resolved(path,*a,**k):return protected if path==file else original(path,*a,**k)
    monkeypatch.setattr(Path,'resolve',resolved)
    result=call(session,'import_folder',{'path':str(folder),'recursive':True})
    assert not w.catalog.photos(),result


@pytest.mark.parametrize('stem,preset,naming', [('x'*220,'p'*60,'{stem}_{preset}'),('x','CON','{preset}'),('trailing. ','','{stem}')])
def test_substituted_export_names_are_valid(tmp_path,stem,preset,naming):
    from luma.engine import defaults
    record={'id':1,'path':str(tmp_path/(stem+'.png')),'name':'source','settings':defaults(),
            'info':{},'user_metadata':{photo_service.PRESET_KEY:preset},'keywords':''}
    names=[]
    def exporter(source,destination,*a,**k):names.append(destination.name);return (10,10)
    result=photo_service.export_records([record,record],tmp_path,naming=naming,exporter=exporter)
    assert not result['failures'] and len(names)==2
    for name in names:
        assert len(name.encode('utf-8'))<=255 and len(name.encode('utf-16-le'))//2<=255
        assert name.split('.')[0].strip().upper() not in photo_service.DEVICES
        assert not Path(name).stem.endswith(('.', ' '))


def test_contact_sheet_commits_pending_screen_edits(session,tmp_path,monkeypatch):
    from luma import photo_images
    w=session.window;ids=add_photos(w,tmp_path/'photos',1);w.activate(ids[0])
    wait(lambda:w.source is not None and not w.render_running)
    w.set_setting('exposure',.7);w.commit_timer.stop()
    seen=[]
    monkeypatch.setattr(photo_images,'cached_thumbnail',lambda *a:None)
    def render(record,*a):seen.append(record['settings']['exposure']);return np.zeros((20,30,3),np.uint8)
    monkeypatch.setattr(photo_images,'render_preview',render)
    result=call(session,'contact_sheet',{'ids':ids})
    assert not result.error and seen==[.7]


def test_export_errors_are_english(session,tmp_path,monkeypatch):
    import luma.app
    ids=add_photos(session.window,tmp_path/'photos',1)
    monkeypatch.setattr(luma.app,'export_image',lambda *a,**k:(_ for _ in ()).throw(ValueError('색 공간 변환 실패')))
    result=call(session,'export_photos',{'ids':ids})
    assert not result.error and result.value['failed']==1
    assert '색 공간' not in result.text and 'could not export' in result.text


def test_failed_export_reply_includes_partial_files(session,tmp_path,monkeypatch):
    from luma import workflow_data
    ids=add_photos(session.window,tmp_path/'photos',1)
    monkeypatch.setattr(workflow_data,'record_exports',lambda *a:(_ for _ in ()).throw(OSError('이력 저장 실패')))
    result=call(session,'export_photos',{'ids':ids})
    assert result.error
    value=json.loads(result.text)
    assert value['state']=='failed' and value['exported']==1
    assert Path(value['files'][0]['path']).is_file() and value['files'][0]['bytes']>0
