import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from copy import deepcopy
from pathlib import Path
import hashlib
import sqlite3
import threading

import cv2
import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication,QDialog
from PySide6.QtCore import QTimer

from luma.app import MainWindow,configure_application
from luma.catalog import Catalog
from luma.dust_dialog import DustDialog
from luma.dust_batch_dialog import DustBatchDialog
from luma.dust_batch import capture_record,apply_reviewed,restore,BATCH_KEY
from luma.engine import defaults,load_image,develop
from luma.command_batch import apply as command_apply,BATCH_KEY as COMMAND_KEY
from test_dust_ui import wait,ready


@pytest.fixture(scope='module')
def app():
    value=QApplication.instance() or QApplication([]);configure_application(value);return value


@pytest.fixture
def window(app,tmp_path):
    w=MainWindow(tmp_path/'catalog');ids=[]
    for index,name in enumerate(['roll/a.png','roll/b.png','roll/c.png','roll/sub/d.png','other/e.png']):
        gray=np.full((260,420),.5,np.float32)
        cv2.circle(gray,(65+index*60,70+index*18),3,.13,-1)
        cv2.circle(gray,(330-index*40,210-index*22),3,.86,-1)
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True)
        Image.fromarray(np.uint8(np.repeat(gray[...,None],3,-1)*255)).save(path)
        settings=defaults();settings.update(exposure=index*.2,contrast=index*5)
        ids.append(w.catalog.add(path,settings))
    video=w.catalog.add(tmp_path/'roll/video.mp4')
    w.select_folder(str(tmp_path/'roll'));w.activate(ids[0]);w.show()
    wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
    w.test_ids=ids;w.test_video=video
    yield w
    for dialog in w.findChildren(QDialog):
        if isinstance(dialog,(DustDialog,DustBatchDialog)) and not dialog.closed:dialog.reject()
    wait(lambda:not w.jobs and not w.render_running);w.close()


def scanned(d):
    wait(lambda:not d.loading)
    d.start();wait(lambda:not d.busy and not d.running)


def review(d,index,monkeypatch,choose=lambda editor:None):
    original=DustDialog.exec
    def run(editor):
        timer=QTimer(editor);timer.setInterval(5)
        def check():
            if editor.result is not None and not editor.busy:
                timer.stop();choose(editor);editor.apply_selection()
        timer.timeout.connect(check);timer.start()
        timeout=QTimer(editor);timeout.setSingleShot(True);timeout.timeout.connect(editor.reject);timeout.start(10000)
        return original(editor)
    with monkeypatch.context() as patch:
        patch.setattr(DustDialog,'exec',run)
        d.list.setCurrentItem(d.list.topLevelItem(index));d.review_selected()


def test_folder_scopes_independent_detection_review_no_rescan_and_atomic_apply(window,monkeypatch,tmp_path):
    import luma.dust_batch_dialog as module
    import luma.dust_dialog as single
    from luma.library import write_sidecar,read_sidecar
    w=window;ids=w.test_ids;before={i:deepcopy(w.catalog.photo(i)['settings']) for i in ids}
    hashes={i:hashlib.sha256(Path(w.catalog.photo(i)['path']).read_bytes()).hexdigest() for i in ids}
    calls=[];real=module.detect
    def detect(*args,**kwargs):calls.append(1);return real(*args,**kwargs)
    monkeypatch.setattr(module,'detect',detect)
    monkeypatch.setattr(single,'detect',lambda *_args,**_kwargs:pytest.fail('Cached review reran detection'))
    d=DustBatchDialog(w);d.show();assert [e['record']['id'] for e in d.entries]==ids[:3]
    scanned(d);assert calls==[1,1,1] and [e['state'] for e in d.entries]==['ready']*3
    assert all(e['result']['total']==2 for e in d.entries)
    assert d.entries[0]['result']['spots'][0]['x']!=d.entries[1]['result']['spots'][0]['x']
    assert not d.apply_button.isEnabled() and all(w.catalog.photo(i)['settings']==before[i] for i in ids)
    review(d,0,monkeypatch)
    review(d,1,monkeypatch,lambda editor:editor.select_all(False))
    assert d.entries[1]['state']=='reviewed' and not d.entries[1]['operation']['spots']
    assert all(w.catalog.photo(i)['settings']==before[i] for i in ids)
    w.manager.auto_sync=True
    monkeypatch.setattr(w,'selected_ids',lambda:ids)
    d.apply();wait(lambda:not w.render_running)
    assert d.entries[0]['state']=='applied' and calls==[1,1,1]
    applied=w.catalog.photo(ids[0])['settings']
    assert len(applied['retouch'])==1 and applied['retouch'][0]['spots']==d.entries[0]['operation']['spots']
    assert all(w.catalog.photo(i)['settings']==before[i] for i in ids[1:])
    assert len(w.catalog.histories(ids[0]))==1 and not w.catalog.histories(ids[1])
    assert w.catalog.preference(COMMAND_KEY) is None
    sidecar=tmp_path/'batch.xmp';write_sidecar(w.catalog.photo(ids[0]),sidecar)
    assert read_sidecar(sidecar)['settings']['retouch']==applied['retouch']
    source,_=load_image(w.catalog.photo(ids[0])['path']);actual=develop(source,applied);original=develop(source,before[ids[0]])
    assert np.count_nonzero(np.any(actual!=original,axis=-1))>0
    np.testing.assert_array_equal(actual[:30],original[:30])
    d.restore(False);wait(lambda:not w.render_running);assert w.settings==before[ids[0]]
    d.restore(True);wait(lambda:not w.render_running);assert w.settings==applied
    for i in ids:assert hashlib.sha256(Path(w.catalog.photo(i)['path']).read_bytes()).hexdigest()==hashes[i]
    d.reject()


def test_review_reopen_keeps_manual_size_checks_and_pages(window,monkeypatch):
    d=DustBatchDialog(window);d.show();scanned(d)
    def choose(editor):
        editor.select_all(False);editor.add_candidate(.25,.32);editor.scale.setValue(65)
    review(d,1,monkeypatch,choose)
    saved=deepcopy(d.entries[1]['operation'])
    def inspect(editor):
        assert len(editor.selected())==1 and editor.view.spots[editor.selected()[0]]['manual']
        assert editor.scale.value()==65
    review(d,1,monkeypatch,inspect)
    assert d.entries[1]['operation']==saved and not window.settings['retouch'];d.reject()


def test_selection_excludes_video_and_does_not_expand_scope(window,monkeypatch):
    w=window;monkeypatch.setattr(w,'selected_ids',lambda:[w.test_ids[0],w.test_ids[4],w.test_video])
    d=DustBatchDialog(w);d.scope.setCurrentIndex(0)
    assert [e['record']['id'] for e in d.entries]==[w.test_ids[0],w.test_ids[4]]
    scanned(d);assert len(d.entries)==2;d.reject()


def test_cancel_retains_finished_photos_and_resume_skips_them(window,monkeypatch):
    import luma.dust_batch_dialog as module
    entered=threading.Event();release=threading.Event();calls=[];real=module.detect
    def delayed(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:entered.set();assert release.wait(5)
        return real(*args,**kwargs)
    monkeypatch.setattr(module,'detect',delayed)
    d=DustBatchDialog(window);d.show();d.start();wait(entered.is_set)
    d.stop();release.set();wait(lambda:not d.busy and not d.running)
    assert [e['state'] for e in d.entries]==['ready','pending','pending']
    scanned(d);assert [e['state'] for e in d.entries]==['ready']*3 and len(calls)==4
    assert not window.settings['retouch'];d.reject()


def test_close_discards_late_results_and_writes_no_edits(window,monkeypatch):
    import luma.dust_batch_dialog as module
    entered=threading.Event();release=threading.Event();real=module.detect
    def delayed(*args,**kwargs):entered.set();assert release.wait(5);return real(*args,**kwargs)
    monkeypatch.setattr(module,'detect',delayed)
    d=DustBatchDialog(window);d.start();wait(entered.is_set);d.reject();release.set();wait(lambda:not window.jobs)
    assert d.closed and d.entries[0]['result'] is None and not window.settings['retouch']


def test_bad_file_does_not_stop_other_photos_and_retry_only_scans_selected(window):
    w=window;path=Path(w.catalog.photo(w.test_ids[1])['path']);original=path.read_bytes();path.write_bytes(b'not a photo')
    d=DustBatchDialog(w);d.show();scanned(d)
    assert [e['state'] for e in d.entries]==['ready','failed','ready']
    path.write_bytes(original);d.list.setCurrentItem(d.list.topLevelItem(1));d.retry()
    wait(lambda:not d.busy and not d.running)
    assert [e['state'] for e in d.entries]==['ready']*3;d.reject()


@pytest.mark.parametrize('change',['settings','source','path','delete'])
def test_apply_rejects_stale_member_without_any_partial_save(window,monkeypatch,change):
    w=window;d=DustBatchDialog(w);scanned(d)
    review(d,0,monkeypatch);review(d,1,monkeypatch)
    ident=w.test_ids[1];row=w.catalog.photo(ident)
    if change=='settings':w.catalog.edit(ident,{**row['settings'],'exposure':1.1})
    elif change=='source':Path(row['path']).write_bytes(b'changed')
    else:
        with w.catalog.db:
            if change=='path':w.catalog.db.execute('UPDATE photos SET path=? WHERE id=?',('moved.png',ident))
            else:w.catalog.db.execute('DELETE FROM photos WHERE id=?',(ident,))
    count=w.catalog.db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
    d.apply()
    assert not w.catalog.photo(w.test_ids[0])['settings']['retouch']
    assert w.catalog.db.execute('SELECT COUNT(*) FROM history').fetchone()[0]==count
    assert w.catalog.preference(BATCH_KEY) is None;d.reject()


def test_sql_failure_rolls_back_and_separate_undo_survives_restart(window,monkeypatch):
    w=window;ids=w.test_ids;d=DustBatchDialog(w);scanned(d)
    review(d,0,monkeypatch);review(d,1,monkeypatch)
    records=[capture_record(w.catalog,ids[4])]
    command_apply(w.catalog,records,[{**records[0]['settings'],'monochrome':True}]);command_state=w.catalog.preference(COMMAND_KEY)
    w.catalog.db.execute(f"CREATE TRIGGER fail_dust BEFORE UPDATE OF edits ON photos WHEN NEW.id={ids[1]} BEGIN SELECT RAISE(ABORT,'injected'); END;")
    with pytest.raises(sqlite3.IntegrityError):apply_reviewed(w.catalog,d.entries)
    assert all(not w.catalog.photo(i)['settings']['retouch'] for i in ids)
    assert not w.catalog.histories(ids[0]) and w.catalog.preference(BATCH_KEY) is None
    w.catalog.db.execute('DROP TRIGGER fail_dust');d.apply();wait(lambda:not w.render_running)
    assert w.catalog.preference(COMMAND_KEY)==command_state
    other=Catalog(w.catalog.directory)
    try:
        assert restore(other)==ids[:2]
        assert all(not other.photo(i)['settings']['retouch'] for i in ids[:2])
        assert restore(other,True)==ids[:2]
        after=other.photo(ids[1])['settings'];other.edit(ids[1],{**after,'contrast':42})
        with pytest.raises(ValueError):restore(other)
        assert other.photo(ids[0])['settings']['retouch']
    finally:other.close()
    d.reject()


def test_changed_conditions_clear_approval_and_require_detection(window,monkeypatch):
    d=DustBatchDialog(window);scanned(d);review(d,0,monkeypatch)
    assert d.apply_button.isEnabled();d.minimum.setValue(4)
    assert not d.apply_button.isEnabled() and all(e['state']=='pending' and e['operation'] is None for e in d.entries)
    d.minimum.setValue(60);d.start();assert not d.busy and '최소' in d.status.text();d.reject()


def test_changed_settings_during_review_and_recheck_after_scan(window,monkeypatch):
    w=window;d=DustBatchDialog(w);scanned(d)
    entry=d.entries[0]
    editor=DustDialog(w,record=entry['record'],cached=dict(result=entry['result'],options=entry['options']))
    editor.show();ready(editor)
    row=w.catalog.photo(entry['record']['id']);w.catalog.edit(row['id'],{**row['settings'],'contrast':35})
    editor.apply_selection();assert editor.operation is None and '바뀌' in editor.status.text();editor.reject()
    d.list.setCurrentItem(d.list.topLevelItem(0));d.review_selected()
    assert d.entries[0]['state']=='failed' and not d.apply_button.isEnabled();d.reject()


def test_review_rescan_updates_cached_results_and_does_not_apply_cancelled_changes(window,monkeypatch):
    import luma.dust_dialog as module
    d=DustBatchDialog(window);scanned(d);review(d,0,monkeypatch)
    previous=deepcopy(d.entries[0]['operation']);original=module.DustDialog.exec
    def cancel_edit(editor):
        poll=QTimer(editor);poll.setInterval(5)
        def check():
            if editor.result is not None and not editor.busy:
                poll.stop();editor.select_all(False);editor.reject()
        poll.timeout.connect(check);poll.start();return original(editor)
    with monkeypatch.context() as patch:
        patch.setattr(module.DustDialog,'exec',cancel_edit);d.review_selected()
    assert d.entries[0]['operation']==previous
    entry=d.entries[0];editor=DustDialog(window,record=entry['record'],cached=dict(result=entry['result'],options=entry['options']),review_state=entry['review'])
    editor.show();ready(editor);editor.minimum.setValue(10)
    assert editor.result is None;editor.scan();ready(editor);editor.apply_selection()
    assert editor.operation['detection']['minimum']==10 and editor.review_state is not None
    d.reject()


def test_pending_current_edit_is_saved_and_blocks_batch_undo(window,monkeypatch):
    w=window;d=DustBatchDialog(w);scanned(d);review(d,0,monkeypatch)
    d.apply();wait(lambda:not w.render_running)
    w.set_setting('exposure',.75)
    d.restore(False);wait(lambda:not w.render_running)
    assert w.catalog.photo(w.current_id)['settings']['exposure']==.75
    assert w.settings['retouch'] and w.catalog.preference(BATCH_KEY)['state']=='applied'
    assert '바뀌' in d.status.text();d.reject()


def test_batch_soft_option_reaches_each_scan_and_survives_cached_review(window,monkeypatch):
    d=DustBatchDialog(window);d.show();assert not d.soft.isEnabled()
    d.detailed.setChecked(True);d.soft.setChecked(True);scanned(d)
    assert all(e['options']['soft'] for e in d.entries)
    review(d,0,monkeypatch);assert d.entries[0]['operation']['detection']['soft']
    d.soft.setChecked(False);assert all(e['state']=='pending' for e in d.entries)
    d.reject()
