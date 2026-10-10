import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Event
import uuid

import pytest
from luma.catalog import Catalog
from luma.lightroom_import import import_catalog, inspect_catalog, report_text


def insert(db, table, **values):
    # Real 15.5.1 table definitions, with independently authored test records.
    columns = list(db.execute(f'PRAGMA table_info({table})'))
    if any(c[1] == 'id_global' and c[3] for c in columns):
        values.setdefault('id_global', uuid.uuid4().hex)
    db.execute(f'INSERT INTO {table}({",".join(values)}) VALUES({",".join("?" for _ in values)})', tuple(values.values()))


def fixture_catalog(path, folder):
    schema = json.loads(Path(__file__).with_name('fixtures').joinpath('lightroom-15.5.1-schema.json').read_text())
    with sqlite3.connect(path) as db:
        for statement in schema['tables'].values():
            db.execute(statement)
        insert(db, 'Adobe_variablesTable', id_local=1, name='Adobe_storeProviderID', value='fixture-catalog-1', type='string')
        insert(db, 'AgLibraryRootFolder', id_local=1, absolutePath=str(folder))
        insert(db, 'AgLibraryFolder', id_local=2, rootFolder=1, pathFromRoot='')
        for i in range(2):
            insert(db, 'AgLibraryFile', id_local=10+i, baseName=f'photo{i}', extension='jpg', folder=2)
            insert(db, 'Adobe_images', id_local=100+i, rootFile=10+i, rating=4-i, pick=1-i,
                   captureTime='2026-09-20T11:22:33', colorLabels='Red')
        insert(db, 'Adobe_images', id_local=99, rootFile=10, masterImage=100, copyName='흑백 대안', rating=2, pick=-1)
        insert(db, 'AgLibraryKeyword', id_local=1, name='여행')
        insert(db, 'AgLibraryKeyword', id_local=2, parent=1, name='서울')
        insert(db, 'AgLibraryKeywordImage', id_local=1, image=100, tag=2)
        insert(db, 'AgLibraryKeywordImage', id_local=2, image=99, tag=1)
        insert(db, 'AgLibraryIPTC', id_local=1, image=100, caption='사진 설명', copyright='저작권')
        insert(db, 'AgInternedIptcCreator', id_local=1, value='촬영자')
        insert(db, 'AgHarvestedIptcMetadata', id_local=1, image=100, creatorRef=1)
        insert(db, 'AgHarvestedExifMetadata', id_local=1, image=100, hasGPS=1, gpsLatitude=0.0, gpsLongitude=127.0)
        insert(db, 'AgLibraryCollection', id_local=1, name='여행 모음', creationId='com.adobe.ag.library.group')
        insert(db, 'AgLibraryCollection', id_local=2, name='서울', parent=1, creationId='com.adobe.ag.library.collection')
        insert(db, 'AgLibraryCollection', id_local=3, name='별 다섯', parent=1, creationId='com.adobe.ag.library.smart_collection')
        insert(db, 'AgLibraryCollection', id_local=4, name='quick collection', systemOnly=1)
        insert(db, 'AgLibraryCollectionImage', id_local=1, image=99, collection=2)
        insert(db, 'AgLibraryCollectionContent', id_local=1, collection=3, owningModule='ag.library.smart_collection',
               content='s = { { criteria = "rating", operation = ">=", value = 5 }, combine = "intersect" }')
        insert(db, 'AgLibraryFolderStack', id_local=1)
        insert(db, 'AgLibraryFolderStackImage', id_local=1, image=100, stack=1, position=1)
        insert(db, 'AgLibraryFolderStackImage', id_local=2, image=101, stack=1, position=2)
        insert(db, 'Adobe_imageDevelopSettings', id_local=1, image=100, text='s = { Exposure2012 = 1.25 }')
        insert(db, 'Adobe_libraryImageDevelopHistoryStep', id_local=1, image=100, name='Exposure', text='never_execute()')
        insert(db, 'Adobe_libraryImageDevelopSnapshot', id_local=1, image=99, name='대안', text='snapshot source text')


@pytest.fixture
def catalogs(tmp_path):
    source = tmp_path/'fixture.lrcat'
    fixture_catalog(source, tmp_path)
    dest = Catalog(tmp_path/'luma')
    try:
        yield source, dest
    finally:
        dest.close()


def test_real_schema_import_structures_and_exact_archive(catalogs):
    source, dest = catalogs
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    info = inspect_catalog(source)
    assert info['counts']['Adobe_images'] == 3
    result = import_catalog(source, dest.directory)
    assert result['imported'] == 3 and result['copies'] == 1 and result['collections'] == 4
    photos = [dest.photo(p['id']) for p in dest.photos()]
    master = next(p for p in photos if p['path'].endswith('photo0.jpg') and p['virtual_source'] is None)
    virtual = next(p for p in photos if p['virtual_source'] is not None)
    assert (master['rating'], master['flag'], master['label']) == (4, 1, '빨강')
    assert (virtual['rating'], virtual['flag'], virtual['virtual_source']) == (2, -1, master['id'])
    assert virtual['name'].endswith('흑백 대안')
    assert master['keywords'] == '여행|서울' and virtual['keywords'] == '여행'
    assert master['user_metadata'] == {'caption':'사진 설명', 'copyright':'저작권', 'creator':'촬영자',
        'DateTimeOriginal':'2026:09:20 11:22:33', 'latitude':0., 'longitude':127.,
        '_last_edit_time':'2001-01-01T00:00:00+00:00'}
    assert master['stack_id'] == photos[1]['stack_id']
    assert all(p['settings']['exposure'] == 0 for p in photos)
    groups = {c['name']: c for c in dest.collections()}
    assert groups['서울']['parent_id'] == groups['여행 모음']['id']
    assert dest.collection_ids(groups['서울']['id']) == {virtual['id']}
    assert groups['별 다섯']['rules'] is not None
    assert all(c['rules'] is None for name,c in groups.items() if name!='별 다섯')
    assert 'quick collection' not in groups
    assert len(dest.collection_ids(result['collection_root'])) == 3
    assert result['converted_smart'] == 1 and 'smart_collection' not in result['warnings']
    archived = dest.db.execute("SELECT payload FROM lightroom_archive WHERE kind='Adobe_libraryImageDevelopHistoryStep'").fetchone()[0]
    assert json.loads(archived)['text'] == 'never_execute()'
    assert dest.db.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 0
    assert dest.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with sqlite3.connect(result['backup']) as db:
        assert db.execute('SELECT COUNT(*) FROM photos').fetchone()[0] == 0
    assert '변환하지 않음' in report_text(result)


def test_reimport_preserves_user_changes_and_changed_source_makes_copy(catalogs):
    source, dest = catalogs
    first = import_catalog(source, dest.directory)
    ident = dest.photos()[0]['id']
    dest.edit(ident, {'exposure': .75})
    dest.update(ident, rating=5, keywords='keep mine')
    original = dest.photo(ident)
    second = import_catalog(source, dest.directory)
    assert second['imported'] == 0 and second['reused'] == 3 and second['collections'] == 0
    assert second['collection_root'] == first['collection_root']
    assert dest.photo(ident) == original
    with sqlite3.connect(source) as db:
        db.execute('UPDATE Adobe_images SET rating=1 WHERE id_local=100')
    third = import_catalog(source, dest.directory)
    assert third['imported'] == 1 and third['reused'] == 2
    assert dest.photo(ident) == original
    assert dest.photos()[-1]['virtual_source'] == ident
    assert third['warnings']['stack_existing'] == 1


def test_existing_luma_photo_never_overwritten(catalogs):
    source, dest = catalogs
    ident = dest.add(source.parent/'photo0.jpg', settings={'exposure': 2})
    dest.update(ident, rating=5, keywords='Luma', label='초록')
    original = dest.photo(ident)
    result = import_catalog(source, dest.directory)
    assert result['imported'] == 3 and result['copies'] == 2
    assert dest.photo(ident) == original
    assert len(dest.photos()) == 4


@pytest.mark.parametrize('stage', ['안전 백업 만드는 중', 'Lightroom 기록 읽는 중', '사진 분류 가져오는 중', '컬렉션과 스택 정리 중'])
def test_cancellation_rolls_back_every_record(catalogs, stage):
    source, dest = catalogs
    if stage == 'Lightroom 기록 읽는 중':
        with sqlite3.connect(source) as db:
            for i in range(300):
                insert(db, 'Adobe_libraryImageDevelopHistoryStep', id_local=10+i, image=100, text=str(i))
    ident = dest.add(source.parent/'keep.jpg')
    snapshot = list(dest.db.iterdump())
    cancel = Event()
    def progress(status):
        if status['stage'] == stage:
            cancel.set()
    result = import_catalog(source, dest.directory, cancel, progress)
    assert result['cancelled'] and result['imported'] == 0
    assert list(dest.db.iterdump()) == snapshot
    assert dest.photo(ident)
    assert not list(dest.directory.glob('backups/*.pending'))
    if stage == '안전 백업 만드는 중':
        assert not result['backup'] and not list(dest.directory.glob('backups/before-lightroom-*.sqlite'))


def test_deleted_import_and_reused_sqlite_id_are_not_confused(catalogs):
    source, dest = catalogs
    import_catalog(source, dest.directory)
    ids = [p['id'] for p in dest.photos()]
    dest.remove_photos(ids)
    replacement = dest.add(source.parent/'unrelated.jpg')
    assert replacement in ids
    result = import_catalog(source, dest.directory)
    assert result['imported'] == 3 and result['reused'] == 0
    assert dest.photo(replacement)['path'].endswith('unrelated.jpg')


def test_wal_snapshot_and_same_catalog_at_new_path(catalogs):
    source, dest = catalogs
    with sqlite3.connect(source) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('UPDATE Adobe_images SET rating=5 WHERE id_local=100')
        writer.commit()
        assert Path(str(source)+'-wal').exists()
        result = import_catalog(source, dest.directory)
        assert dest.photos()[0]['rating'] == 5
        moved = source.with_name('moved.lrcat')
        with sqlite3.connect(moved) as copy:
            writer.backup(copy)
    again = import_catalog(moved, dest.directory)
    assert again['source_key'] == result['source_key']
    assert again['reused'] == 3 and again['imported'] == 0


def test_cycles_delimiters_bad_paths_and_broken_virtuals_reported(catalogs):
    source, dest = catalogs
    with sqlite3.connect(source) as db:
        db.execute('UPDATE AgLibraryKeyword SET parent=2 WHERE id_local=1')
        db.execute('UPDATE AgLibraryCollection SET parent=2 WHERE id_local=1')
        db.execute('UPDATE Adobe_images SET masterImage=999 WHERE id_local=99')
        insert(db, 'AgLibraryKeyword', id_local=3, name='one,two')
        insert(db, 'AgLibraryKeywordImage', id_local=3, image=100, tag=3)
        insert(db, 'AgLibraryFile', id_local=12, folder=2, baseName='../bad', extension='jpg')
        insert(db, 'Adobe_images', id_local=102, rootFile=12)
    result = import_catalog(source, dest.directory)
    assert result['imported'] == 2 and result['skipped'] == 2
    assert result['warnings']['keyword_cycle'] == 2
    assert result['warnings']['keyword_separator'] == 1
    assert result['warnings']['virtual_master'] == 1
    assert result['warnings']['unsupported_path_or_format'] == 1
    assert result['warnings']['collection_cycle'] >= 2
    for row in dest.collections():
        assert row['parent_id'] != row['id']


def test_error_mid_import_rolls_back_and_source_is_read_only(catalogs):
    source, dest = catalogs
    snapshot = list(dest.db.iterdump())
    before = source.read_bytes()
    def fail(status):
        if status['stage'] == '컬렉션과 스택 정리 중':
            raise OSError('simulated disk failure')
    with pytest.raises(OSError, match='simulated'):
        import_catalog(source, dest.directory, progress=fail)
    assert list(dest.db.iterdump()) == snapshot
    assert source.read_bytes() == before


def test_invalid_catalog_not_silently_accepted(tmp_path):
    source = tmp_path/'wrong.lrcat'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE Adobe_images(id_local INTEGER)')
    with pytest.raises(ValueError, match='지원하지 않습니다'):
        inspect_catalog(source)


def test_legacy_reader_does_not_replace_master_rating_with_copy(catalogs):
    from luma.extras import lightroom_records
    source, _ = catalogs
    records = lightroom_records(source)
    original = next(r for r in records if r['path'].endswith('photo0.jpg'))
    assert original['rating'] == 4 and original['flag'] == 1


@pytest.mark.parametrize('conversion',[False,True])
def test_qt_import_preview_result_and_cancel(catalogs, monkeypatch,conversion):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QEventLoop, QTimer
    from luma.app import MainWindow
    from luma.lightroom_import_dialog import begin_import
    source, dest = catalogs
    app = QApplication.instance() or QApplication([])
    window = MainWindow(dest.directory)
    errors, notices, reviews = [], [], []
    window.show_error = errors.append
    monkeypatch.setattr(QMessageBox, 'information', lambda parent, title, text: notices.append(text))
    def review(dialog):
        reviews.append(dialog.text())
        assert not dialog.checkBox().isChecked()
        dialog.checkBox().setChecked(conversion)
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, 'exec', review)
    def wait():
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: loop.quit() if not window.maintenance_running else None)
        timer.start()
        QTimer.singleShot(20000, loop.quit)
        loop.exec()
        timer.stop()
        assert not window.maintenance_running
    try:
        window.show()
        begin_import(window, source)
        assert window.extras.lightroom_dialog.isVisible()
        wait()
        assert not errors and len(reviews) == 1 and len(notices) == 1
        assert len(window.catalog.photos()) == 3
        assert window.extras.last_lightroom_import['converted_photos']==int(conversion)
        assert window.photo_model.check_timer.isActive()
        assert not window.extras.lightroom_dialog.running
        assert '같은 모습은 보장되지 않습니다' in reviews[0]
        begin_import(window, source)
        window.extras.lightroom_dialog.request_cancel()
        wait()
        assert len(reviews) == 1 and len(window.catalog.photos()) == 3
        assert not errors
    finally:
        window.close()
        app.processEvents()


def test_optin_current_history_snapshots_and_persistent_undo(catalogs):
    source,dest=catalogs
    with sqlite3.connect(source) as db:
        db.execute('DELETE FROM Adobe_libraryImageDevelopHistoryStep')
        db.execute('UPDATE Adobe_imageDevelopSettings SET text=?',('s={Exposure2012=1.25,Blacks2012=-20,CameraProfile="Adobe Color"}',))
        for i,value in enumerate((0,.5,1.25)):
            insert(db,'Adobe_libraryImageDevelopHistoryStep',id_local=10+i,image=100,name=f'노출 {value}',
                   dateCreated=100+i,text=f's={{Exposure2012={value},Blacks2012=-20,CameraProfile="Adobe Color"}}')
        db.execute('UPDATE Adobe_libraryImageDevelopSnapshot SET text=?',('s={Exposure2012=-.25,ConvertToGrayscale=true}',))
    source_bytes=source.read_bytes()
    result=import_catalog(source,dest.directory,convert_edits=True)
    assert (result['converted_photos'],result['converted_history'],result['converted_snapshots'])==(1,3,1)
    assert result['warnings']['develop_field:CameraProfile']==4
    master=next(p for p in dest.photos() if p['path'].endswith('photo0.jpg') and p['virtual_source'] is None)
    ident=master['id'];virtual=next(p for p in dest.photos() if p['virtual_source']==ident)
    assert dest.photo(ident)['settings']['exposure']==1.25
    assert [s['exposure'] for s in dest.preference(f'undo:{ident}')['undo']]==[0,.5]
    history=dest.histories(ident)
    assert all(h['label'].startswith('Lightroom 상태 · ') for h in history)
    assert history[-1]['created']=='2001-01-01T00:01:40+00:00'
    snapshot=dest.snapshots(virtual['id'])[0]
    assert snapshot['settings']['exposure']==-.25 and snapshot['settings']['monochrome']
    dest.edit(ident,{'exposure':2.5});dest.save_preference(f'undo:{ident}',{'undo':[{'exposure':1}],'redo':[]})
    before=dest.photo(ident);before_history=dest.histories(ident)
    again=import_catalog(source,dest.directory,convert_edits=True)
    assert again['reused']==3 and again['imported']==again['converted_history']==0
    assert dest.photo(ident)==before and dest.histories(ident)==before_history
    assert dest.preference(f'undo:{ident}')['undo']==[{'exposure':1}]
    assert source.read_bytes()==source_bytes
    assert 'CameraProfile' in report_text(result)


def test_history_gap_clears_undo_continuity_and_cancel_is_atomic(catalogs,monkeypatch):
    from luma import adobe_history
    source,dest=catalogs
    with sqlite3.connect(source) as db:
        db.execute('DELETE FROM Adobe_libraryImageDevelopHistoryStep')
        for i,text in enumerate(('s={Exposure2012=0}','not_lua()','s={Exposure2012=.5}')):
            insert(db,'Adobe_libraryImageDevelopHistoryStep',id_local=10+i,image=100,dateCreated=i,text=text)
    before=list(dest.db.iterdump());cancel=Event();original=adobe_history.decode_row;calls=[]
    def cancelling(*args,**kwargs):
        result=original(*args,**kwargs);calls.append(1)
        if len(calls)==2:cancel.set()
        return result
    monkeypatch.setattr(adobe_history,'decode_row',cancelling)
    assert import_catalog(source,dest.directory,cancel=cancel,convert_edits=True)['cancelled']
    assert list(dest.db.iterdump())==before
    monkeypatch.setattr(adobe_history,'decode_row',original)
    result=import_catalog(source,dest.directory,convert_edits=True)
    ident=dest.photos()[0]['id']
    assert result['converted_history']==2 and result['warnings']['develop_unreadable']>=1
    assert [s['exposure'] for s in dest.preference(f'undo:{ident}')['undo']]==[.5]


def test_dynamic_smart_import_updates_and_unsupported_rules_remain_whole(catalogs):
    from luma.library_query import query
    source,dest=catalogs
    with sqlite3.connect(source) as db:
        insert(db,'AgLibraryCollection',id_local=5,name='Unknown compound',creationId='com.adobe.ag.library.smart_collection')
        insert(db,'AgLibraryCollectionContent',id_local=5,collection=5,owningModule='ag.library.smart_collection',
               content='s={{criteria="rating",operation=">=",value=1},{criteria="unknown",operation="==",value=1}}')
        insert(db,'AgLibraryCollectionImage',id_local=5,image=100,collection=5)
        insert(db,'AgInternedExifCameraModel',id_local=1,value='Nikon D3S')
        insert(db,'AgInternedExifCameraSN',id_local=1,value='TEST-123')
        db.execute('UPDATE AgHarvestedExifMetadata SET cameraModelRef=1,cameraSNRef=1,isoSpeedRating=800')
    result=import_catalog(source,dest.directory)
    assert result['converted_smart']==1 and result['warnings']['smart_collection']==1
    ident=dest.photos()[0]['id'];meta=dest.photo(ident)['user_metadata']
    assert (meta['camera'],meta['serial'],meta['iso'])==('Nikon D3S','TEST-123',800)
    groups={c['name']:c for c in dest.collections()}
    smart=groups['별 다섯']['id'];options={'collection':smart}
    assert query(dest.directory/'catalog.sqlite',options,Event())['ids']==[]
    dest.update(ident,rating=5)
    assert query(dest.directory/'catalog.sqlite',options,Event())['ids']==[ident]
    fallback=groups['Unknown compound · 스마트 규칙 보관']
    assert fallback['rules'] is None and dest.collection_ids(fallback['id'])=={ident}
    assert 'unknown' in report_text(result)
