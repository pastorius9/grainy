"""Restart, interruption, stale-source and transaction guarantees for reviews."""
from copy import deepcopy
from pathlib import Path
import json,sqlite3,threading,zlib
import pytest
from luma.catalog import Catalog
from luma.dust_batch_dialog import DustBatchDialog
from luma.dust_store import DustReviewStore,unpack
from luma.dust_batch import BATCH_KEY
from test_dust_batch import app,window,scanned,review
from test_dust_ui import wait


def test_restart_preserves_candidates_checked_manual_size_options_and_cursor(window,monkeypatch):
    w=window;d=DustBatchDialog(w);d.detailed.setChecked(True);d.soft.setChecked(True);d.suppress_grain.setChecked(True);scanned(d)
    def choose(editor):
        editor.select_all(False);editor.add_candidate(.25,.32);editor.scale.setValue(65)
    review(d,1,monkeypatch,choose);expected=deepcopy(d.entries);ident=d.session_id;d.reject()
    other=Catalog(w.catalog.directory)
    try:
        header,entries,bad=DustReviewStore(other).load(ident)
        assert not bad and entries==expected and header['current_index']==1 and header['options']['soft']
    finally:other.close()
    import luma.dust_batch_dialog as module
    monkeypatch.setattr(module,'detect',lambda *a,**k:pytest.fail('Completed detection must not repeat'))
    restored=DustBatchDialog(w);wait(lambda:not restored.loading)
    assert restored.session_id==ident and restored.entries==expected and restored.current_index()==1
    assert restored.soft.isChecked() and restored.suppress_grain.isChecked() and not w.settings['retouch']
    def inspect(editor):
        assert len(editor.selected())==1 and editor.view.spots[editor.selected()[0]]['manual']
        assert editor.scale.value()==65
    review(restored,1,monkeypatch,inspect)
    restored.apply();wait(lambda:not w.render_running)
    assert len(w.catalog.photo(w.test_ids[1])['settings']['retouch'])==1
    assert all(not w.catalog.photo(i)['settings']['retouch'] for i in (w.test_ids[0],w.test_ids[2]))
    restored.reject();again=DustBatchDialog(w);wait(lambda:not again.loading)
    assert again.entries[1]['state']=='applied' and not again.apply_button.isEnabled();again.reject()


def test_filament_geometry_and_search_conditions_survive_review_restart_and_undo(window,monkeypatch):
    w=window;d=DustBatchDialog(w);d.scratches.setChecked(True);d.scratch_width.setValue(7);scanned(d)
    d.resize(740,590);d.show()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents();assert d.list.height()>=120
    d.search_scroll.ensureWidgetVisible(d.scratch_width);QApplication.processEvents()
    assert d.search_scroll.viewport().rect().contains(d.scratch_width.mapTo(d.search_scroll.viewport(),d.scratch_width.rect().center()))
    def draw(editor):
        editor.select_all(False);editor.add_stroke([[.2,.2],[.45,.4],[.6,.3]]);editor.scale.setValue(75)
        editor.repair_method.setCurrentIndex(editor.repair_method.findData('texture'))
    review(d,1,monkeypatch,draw);saved=deepcopy(d.entries);ident=d.session_id;d.reject()
    restored=DustBatchDialog(w);wait(lambda:not restored.loading)
    assert restored.session_id==ident and restored.entries==saved
    assert restored.scratches.isChecked() and restored.scratch_width.value()==7
    operation=saved[1]['operation'];assert operation['version']==4 and len(operation['spots'][0]['parts'])>10
    assert operation['repair_method']=='texture' and saved[1]['review']['repair_method']=='texture'
    bad=deepcopy(saved[1]);bad['operation']['repair_method']='unknown'
    with pytest.raises(ValueError):DustReviewStore.validate(bad)
    bad=deepcopy(saved[1]);bad['review']['repair_method']='smooth'
    with pytest.raises(ValueError):DustReviewStore.validate(bad)
    restored.apply();wait(lambda:not w.render_running)
    assert w.catalog.photo(w.test_ids[1])['settings']['retouch'][-1]==operation
    restored.restore(False);wait(lambda:not w.render_running)
    assert not w.catalog.photo(w.test_ids[1])['settings']['retouch']
    restored.restore(True);wait(lambda:not w.render_running)
    assert w.catalog.photo(w.test_ids[1])['settings']['retouch'][-1]==operation
    restored.reject()


def test_close_while_scanning_resumes_only_unfinished_photos(window,monkeypatch):
    import luma.dust_batch_dialog as module
    entered=threading.Event();release=threading.Event();calls=[];real=module.detect
    def delayed(*a,**kw):
        calls.append(1)
        if len(calls)==2:entered.set();assert release.wait(5)
        return real(*a,**kw)
    monkeypatch.setattr(module,'detect',delayed)
    d=DustBatchDialog(window);d.start();wait(entered.is_set);ident=d.session_id;d.reject();release.set();wait(lambda:not window.jobs)
    header,entries,bad=d.store.load(ident)
    assert [e['state'] for e in entries]==['ready','pending','pending']
    resumed=DustBatchDialog(window);scanned(resumed)
    assert len(calls)==4 and all(e['state']=='ready' for e in resumed.entries);resumed.reject()


@pytest.mark.parametrize('change',['source','settings','deleted'])
def test_reopen_isolates_stale_photo_without_discarding_other_reviews(window,monkeypatch,change):
    w=window;d=DustBatchDialog(w);scanned(d);review(d,0,monkeypatch);review(d,1,monkeypatch);saved=deepcopy(d.entries[0]);d.reject()
    ident=w.test_ids[1];row=w.catalog.photo(ident)
    if change=='source':Path(row['path']).write_bytes(b'changed')
    elif change=='settings':w.catalog.edit(ident,{**row['settings'],'contrast':43})
    else:
        with w.catalog.db:w.catalog.db.execute('DELETE FROM photos WHERE id=?',(ident,))
    resumed=DustBatchDialog(w);wait(lambda:not resumed.loading)
    assert resumed.entries[0]==saved and resumed.entries[1]['state']=='failed'
    assert resumed.entries[1]['operation'] is None and resumed.entries[2]['state']=='ready'
    resumed.apply();wait(lambda:not w.render_running)
    assert len(w.catalog.photo(w.test_ids[0])['settings']['retouch'])==1
    if change!='deleted':assert not w.catalog.photo(ident)['settings']['retouch']
    resumed.reject()


def test_new_conditions_and_other_folder_leave_prior_work_available(window,monkeypatch):
    w=window;d=DustBatchDialog(w);scanned(d);review(d,0,monkeypatch);old=d.session_id;expected=deepcopy(d.entries)
    d.minimum.setValue(5)
    assert d.session_id is None and not d.apply_button.isEnabled()
    assert d.store.load(old)[1]==expected
    scanned(d);new=d.session_id;assert new!=old and len(d.store.sessions())==2
    d.work_selector.setCurrentIndex(d.work_selector.findData(old))
    wait(lambda:not d.loading)
    assert d.entries==expected and d.minimum.value()==3;d.reject()
    w.select_folder(str(Path(w.catalog.photo(w.test_ids[4])['path']).parent));wait(lambda:not w.library_loading)
    other=DustBatchDialog(w);assert other.session_id is None and len(other.entries)==1
    scanned(other);assert len(other.store.sessions())==3
    other.work_selector.setCurrentIndex(other.work_selector.findData(old))
    wait(lambda:not other.loading)
    assert other.entries==expected and 'roll' in other.scope_note.text();other.reject()


def test_save_failure_keeps_last_checkpoint_and_can_retry_without_photo_edit(window,monkeypatch):
    w=window;d=DustBatchDialog(w);scanned(d);ident=d.session_id;previous=d.store.load(ident)[1]
    with monkeypatch.context() as patch:
        patch.setattr(d.store,'save',lambda *a,**k:(_ for _ in ()).throw(sqlite3.OperationalError('disk full')))
        review(d,0,monkeypatch)
        assert d.save_error and not d.apply_button.isEnabled() and not d.work_selector.isEnabled()
        assert d.store.load(ident)[1]==previous and not w.settings['retouch']
    d.save_button.click()
    assert not d.save_error and d.store.load(ident)[1][0]['state']=='reviewed' and d.apply_button.isEnabled();d.reject()


def test_initial_save_failure_can_retry_before_detection(window,monkeypatch):
    d=DustBatchDialog(window)
    with monkeypatch.context() as patch:
        patch.setattr(d.store,'create',lambda *a,**k:(_ for _ in ()).throw(sqlite3.OperationalError('disk full')))
        d.start();assert d.save_error and not d.running and d.session_id is None
    d.save_button.click();assert not d.save_error and d.session_id
    scanned(d);assert all(e['state']=='ready' for e in d.entries);d.reject()


def test_apply_and_undo_checkpoint_failure_roll_back_photos_history_and_queue(window,monkeypatch):
    w=window;d=DustBatchDialog(w);scanned(d);review(d,0,monkeypatch);ident=d.session_id;real=d.store.save
    def fail_nested(*a,**kw):
        if kw.get('nested'):raise sqlite3.OperationalError('injected checkpoint failure')
        return real(*a,**kw)
    with monkeypatch.context() as patch:
        patch.setattr(d.store,'save',fail_nested);d.apply()
        assert not w.settings['retouch'] and not w.catalog.histories(w.test_ids[0])
        assert w.catalog.preference(BATCH_KEY) is None and d.store.load(ident)[1][0]['state']=='reviewed'
    d.apply();wait(lambda:not w.render_running)
    assert d.store.load(ident)[1][0]['state']=='applied'
    with monkeypatch.context() as patch:
        patch.setattr(d.store,'save',fail_nested);d.restore(False)
        assert w.catalog.photo(w.test_ids[0])['settings']['retouch']
        assert w.catalog.preference(BATCH_KEY)['state']=='applied' and d.store.load(ident)[1][0]['state']=='applied'
    d.restore(False);wait(lambda:not w.render_running)
    assert not w.settings['retouch'] and d.store.load(ident)[1][0]['state']=='pending';d.reject()


def test_only_changed_photo_is_written_and_old_writer_cannot_overwrite(window):
    d=DustBatchDialog(window);scanned(d);ident=d.session_id
    before=[bytes(r[0]) for r in window.catalog.db.execute('SELECT payload FROM dust_entries WHERE session_id=? ORDER BY ordinal',(ident,))]
    old_revision=d.revision;changed=deepcopy(d.entries[1]);changed.update(state='failed',error='retry required')
    writes=window.catalog.db.total_changes
    d.revision=d.store.save(ident,old_revision,[(1,changed)],1)
    assert window.catalog.db.total_changes-writes==2  # One header and one photo checkpoint.
    after=[bytes(r[0]) for r in window.catalog.db.execute('SELECT payload FROM dust_entries WHERE session_id=? ORDER BY ordinal',(ident,))]
    assert before[0]==after[0] and before[2]==after[2] and before[1]!=after[1]
    other=Catalog(window.catalog.directory)
    try:
        with pytest.raises(ValueError,match='다른 창'):DustReviewStore(other).save(ident,old_revision,[(0,changed)],0)
        assert DustReviewStore(other).load(ident)[1][1]==changed
    finally:other.close()
    d.entries[1]=changed;d.reject()


@pytest.mark.parametrize('damage',['compressed','coordinate','selected'])
def test_damaged_photo_checkpoint_isolated_and_other_candidates_retained(window,monkeypatch,damage):
    d=DustBatchDialog(window);scanned(d);review(d,0,monkeypatch);ident=d.session_id;saved=deepcopy(d.entries[2]);d.reject()
    db=window.catalog.db;blob=db.execute('SELECT payload FROM dust_entries WHERE session_id=? AND ordinal=0',(ident,)).fetchone()[0]
    if damage=='compressed':payload=b'broken zlib'
    else:
        value=json.loads(unpack(blob))
        if damage=='coordinate':value['result']['spots'][0]['x']=float('nan')
        else:value['review']['checked']=[999999]
        payload=zlib.compress(json.dumps(value).encode())
    with db:db.execute('UPDATE dust_entries SET payload=? WHERE session_id=? AND ordinal=0',(payload,ident))
    resumed=DustBatchDialog(window);wait(lambda:not resumed.loading)
    assert resumed.entries[0]['state']=='failed' and '손상' in resumed.entries[0]['error']
    assert resumed.entries[2]==saved and not resumed.apply_button.isEnabled()
    resumed.list.setCurrentItem(resumed.list.topLevelItem(0));resumed.retry();wait(lambda:not resumed.busy and not resumed.running)
    assert resumed.entries[0]['state']=='ready';resumed.reject()


def test_backup_restores_queue_and_delete_keeps_edits_and_undo(window,monkeypatch,tmp_path):
    from PySide6.QtWidgets import QMessageBox
    w=window;d=DustBatchDialog(w);scanned(d);review(d,0,monkeypatch);ident=d.session_id;expected=deepcopy(d.entries)
    backup=tmp_path/'saved/catalog.sqlite';w.catalog.backup(backup)
    other=Catalog(backup.parent)
    try:assert DustReviewStore(other).load(ident)[1]==expected
    finally:other.close()
    d.apply();wait(lambda:not w.render_running);settings=deepcopy(w.settings);undo=w.catalog.preference(BATCH_KEY)
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Yes)
    d.forget_work()
    assert d.session_id is None and not d.store.sessions()
    assert w.settings==settings and w.catalog.preference(BATCH_KEY)==undo;d.reject()


def test_profile_referenced_only_by_saved_review_survives_optimization_and_backup(tmp_path):
    from dataclasses import asdict
    from luma.dust import Options
    from luma.dust_batch import capture_record,capture_scope_ids
    from luma.profile_store import verify_references
    from luma.catalog_maintenance import optimize
    from luma.engine import defaults
    from test_profile_store import settings
    cat=Catalog(tmp_path/'catalog')
    try:
        values=settings();ident=cat.add(tmp_path/'photo.nef',values)
        entry=dict(record=capture_record(cat,ident),state='pending',result=None,options=None,operation=None,review=None,error='')
        store=DustReviewStore(cat);session,_=store.create(capture_scope_ids(cat,ident,[ident],None),'selection',asdict(Options()),[entry])
        cat.set_settings(ident,defaults())
        assert verify_references(cat.db)=={values['dcp_profile']['sha256']}
        optimize(cat.directory)
        assert store.load(session)[1][0]==entry
        backup=cat.backup(tmp_path/'backup/catalog.sqlite');other=Catalog(backup.parent)
        try:assert DustReviewStore(other).load(session)[1][0]==entry
        finally:other.close()
        with cat.db:cat.db.execute('UPDATE profile_assets SET data=?',(b'corrupt',))
        with pytest.raises(ValueError):verify_references(cat.db)
    finally:cat.close()


def test_restore_old_backup_migrates_review_tables_without_losing_photos(tmp_path):
    from luma.library import restore_backup
    cat=Catalog(tmp_path/'catalog')
    try:
        ident=cat.add(tmp_path/'photo.png');backup=cat.backup(tmp_path/'old.sqlite')
        with sqlite3.connect(backup) as old:
            old.execute('DROP TABLE dust_entries');old.execute('DROP TABLE dust_sessions')
        restore_backup(cat,backup)
        assert cat.photo(ident) is not None and DustReviewStore(cat).sessions()==[]
    finally:cat.close()


@pytest.mark.parametrize('action',['stop','close'])
def test_async_restore_keeps_gui_alive_and_ignores_cancelled_result(window,monkeypatch,action):
    import luma.dust_batch_dialog as module
    from PySide6.QtCore import QTimer
    w=window;d=DustBatchDialog(w);scanned(d);expected=deepcopy(d.entries);ident=d.session_id
    entered=threading.Event();release=threading.Event();real=module.read_work;thread_ids=[]
    def delayed(*args):
        thread_ids.append(threading.get_ident());result=real(*args)
        entered.set();assert release.wait(5);return result
    monkeypatch.setattr(module,'read_work',delayed)
    d.new_work();pending=deepcopy(d.entries);d.load_work(ident);wait(entered.is_set)
    assert d.loading and d.busy and not d.review_button.isEnabled() and d.stop_button.isEnabled()
    ticks=[];timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:ticks.append(1));timer.start()
    try:
        wait(lambda:len(ticks)>=4)
        assert thread_ids==[next(i for i in thread_ids if i!=threading.get_ident())]
        if action=='stop':d.stop()
        else:d.reject()
        release.set();wait(lambda:not w.jobs)
        assert d.entries==pending and d.session_id is None and d.store.load(ident)[1]==expected
        if action=='stop':
            assert not d.loading and not d.busy and d.scan_button.isEnabled()
            monkeypatch.setattr(module,'read_work',real);d.load_work(ident);wait(lambda:not d.loading)
            assert d.entries==expected and d.session_id==ident;d.reject()
        else:assert d.closed
    finally:timer.stop();release.set()


def test_worker_reader_is_read_only_consistent_and_cancellable(window):
    from luma.dust_store import read_work
    from luma.dust import Cancelled
    w=window;d=DustBatchDialog(w);scanned(d);expected=deepcopy(d.entries);cancel=threading.Event();progress=[]
    def tick(done,total):
        progress.append((done,total));cancel.set()
    with pytest.raises(Cancelled):read_work(w.catalog.directory,d.session_id,cancel,tick)
    assert progress==[(1,3)] and d.store.load(d.session_id)[1]==expected
    with Catalog.open_reader(w.catalog.directory) as reader:
        assert reader.photo(w.test_ids[0])==w.catalog.photo(w.test_ids[0])
        with pytest.raises(sqlite3.OperationalError,match='readonly'):reader.db.execute('DELETE FROM dust_entries')
        w.catalog.update(w.test_ids[0],rating=4)
        assert reader.photo(w.test_ids[0])['rating']==0
    assert w.catalog.photo(w.test_ids[0])['rating']==4;d.reject()
