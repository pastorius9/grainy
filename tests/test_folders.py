import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import hashlib
import sys
import unicodedata
import json
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt,QEventLoop,QTimer
from PySide6.QtTest import QTest
from luma.catalog import Catalog
from luma.engine import defaults
from luma.folders import folder_nodes,path_key,in_folder
from luma.app import MainWindow,configure_application


def wait_for(predicate,timeout=15):
    if predicate(): return
    loop=QEventLoop()
    poll=QTimer()
    poll.timeout.connect(lambda:loop.quit() if predicate() else None)
    poll.start(10)
    deadline=QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(timeout*1000)
    loop.exec()
    poll.stop(); deadline.stop()
    assert predicate(),'folder UI timed out'


def settled(window):
    return not window.library_loading and not window.render_running and (window.current_id is None or window.source is not None)


@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([])
    configure_application(app)
    return app


def make_photo(path,color=(110,140,90)):
    path.parent.mkdir(parents=True,exist_ok=True)
    Image.new('RGB',(420,280),color).save(path)
    return path


def test_hierarchy_counts_case_insensitive_paths_and_prefix_boundaries(tmp_path):
    root=tmp_path/'사진'
    paths=[root/'서울'/'a.jpg',root/'서울'/'야경'/'b.jpg',root/'서울역'/'c.jpg',root/'부산'/'야경'/'d.jpg']
    rows=[{'id':i,'path':str(p)} for i,p in enumerate(paths)]
    nodes={n['key']:n for n in folder_nodes(rows,[str(root),str(root/'서울')])}
    assert nodes[path_key(root)]['total']==4
    assert nodes[path_key(root/'서울')]['total']==2
    assert nodes[path_key(root/'서울')]['direct']==1
    assert nodes[path_key(root/'서울'/'야경')]['total']==1
    assert nodes[path_key(root/'부산'/'야경')]['total']==1
    assert in_folder(paths[1],root/'서울',True)
    assert not in_folder(paths[1],root/'서울',False)
    assert not in_folder(paths[2],root/'서울',True)
    if os.name=='nt':
        assert in_folder(r'C:\PHOTO\Album\a.jpg',r'c:\photo',True)
        assert not in_folder(r'D:\photo\a.jpg',r'C:\photo',True)
    elif sys.platform=='darwin':     # case and Unicode composition are ignored, as the default volume does
        assert in_folder('/PHOTO/Album/a.jpg','/photo',True)
        assert in_folder(unicodedata.normalize('NFD','/사진/서울/a.jpg'),'/사진',True)
        assert not in_folder('/photo-b/a.jpg','/photo',True)


def test_old_catalog_migration_retains_edits_and_generates_folders(tmp_path):
    cat=Catalog(tmp_path/'data')
    path=tmp_path/'기존 사진'/'원본.jpg'
    ident=cat.add(path)
    edit=defaults(); edit['exposure']=.65
    cat.edit(ident,edit)
    cat.update(ident,rating=5,keywords='필름')
    # Simulate the exact old catalog schema by removing only the new tables.
    cat.db.executescript('DROP TABLE folder_roots; DROP TABLE preferences;')
    cat.close()
    migrated=Catalog(tmp_path/'data')
    assert migrated.photo(ident)['settings']==edit
    assert migrated.photo(ident)['rating']==5
    assert migrated.photo(ident)['keywords']=='필름'
    assert str(path.parent) in migrated.folder_roots()
    assert migrated.add(str(path).upper())==ident
    assert len(migrated.photos())==1
    migrated.close()


def test_folders_filter_sync_and_session_restore(app,tmp_path):
    root=tmp_path/'촬영 라이브러리'
    seoul=root/'서울'
    busan=root/'부산'
    files=[make_photo(seoul/'portrait.jpg'),make_photo(seoul/'야경'/'night.jpg'),
           make_photo(busan/'sea.jpg',(70,130,210))]
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    w=MainWindow(tmp_path/'catalog')
    w.show()
    errors=[]
    w.show_error=errors.append
    w.import_paths([str(root)])
    wait_for(lambda:len(w.catalog.photos())==3 and not w.import_scans and not w.import_busy and settled(w))
    assert w.folder_items[path_key(root)].text(1)=='3'
    assert w.folder_items[path_key(seoul)].text(1)=='2'
    item=w.folder_items[path_key(seoul)]
    w.folder_tree.scrollToItem(item)
    QTest.mouseClick(w.folder_tree.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,w.folder_tree.visualItemRect(item).center())
    wait_for(lambda:settled(w))
    assert w.folder_filter==str(seoul)
    assert len(w.visible_ids)==2
    assert all(in_folder(w.catalog.photo(i)['path'],seoul,True) for i in w.visible_ids)
    w.include_subfolders.setChecked(False)
    wait_for(lambda:settled(w))
    assert len(w.visible_ids)==1
    assert w.folder_items[path_key(seoul)].text(1)=='1'
    portrait=w.current_id
    w.set_setting('exposure',.42)
    w.commit()
    w.set_rating(5)
    w.set_filter('rated')
    wait_for(lambda:settled(w))
    assert w.visible_ids==[portrait]
    w.search.setText('no-match')
    wait_for(lambda:settled(w))
    assert not w.visible_ids and w.current_id is None and not w.selected_ids()
    w.search.clear()
    wait_for(lambda:settled(w))
    assert w.current_id==portrait
    w.set_filter('all')
    wait_for(lambda:settled(w))
    assert w.folder_filter is None and len(w.visible_ids)==3
    w.select_folder(seoul)
    wait_for(lambda:settled(w))
    w.folder_search.setText('야경')
    assert not w.folder_items[path_key(seoul/'야경')].isHidden()
    assert w.folder_items[path_key(busan)].isHidden()
    w.folder_search.clear()
    assert not w.folder_items[path_key(busan)].isHidden()
    # Sync only adds new files; existing originals and edits are retained.
    new=make_photo(seoul/'new.jpg',(170,100,65))
    w.sync_folder(str(seoul))
    wait_for(lambda:not w.import_scans and not w.import_busy and not w.library_loading and len(w.catalog.photos())==4)
    w.sync_folder(str(seoul))
    wait_for(lambda:not w.import_scans and not w.import_busy and not w.library_loading)
    assert len(w.catalog.photos())==4
    assert w.catalog.photo(portrait)['settings']['exposure']==.42
    assert w.catalog.photo(portrait)['rating']==5
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    empty=root/'빈 폴더'; empty.mkdir()
    w.import_paths([str(empty)])
    wait_for(lambda:not w.import_scans and not w.import_busy and not w.library_loading)
    assert path_key(empty) in w.folder_items
    w.select_folder(empty)
    wait_for(lambda:settled(w))
    assert w.current_id is None and not w.visible_ids
    w.select_folder(seoul)
    wait_for(lambda:settled(w))
    w.folder_tree.setCurrentItem(w.folder_items[path_key(seoul)])
    w.folder_items[path_key(busan)].setExpanded(False)
    out=Path(__file__).resolve().parents[1]/'validation'
    w.grab().save(str(out/'05-folders.png'))
    w.close(); QApplication.processEvents()
    restored=MainWindow(tmp_path/'catalog')
    restored.show()
    wait_for(lambda:restored.current_id is not None and settled(restored))
    assert restored.folder_filter==str(seoul)
    assert not restored.include_subfolders.isChecked()
    assert len(restored.visible_ids)==2
    assert restored.catalog.photo(portrait)['settings']['exposure']==.42
    restored.close(); QApplication.processEvents()
    assert not errors,errors
    report={'passed':True,'checks':['nested folders','counts','actual folder click','subfolder toggle',
        'rating and search scoped to folder','empty folder clears active photo','safe incremental sync',
        'old edits and originals preserved','folder search','session restore']}
    (out/'folders-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


def test_add_parent_keeps_existing_folder_selected(app,tmp_path):
    album=tmp_path/'사진'/'여행'
    photo=make_photo(album/'one.jpg')
    w=MainWindow(tmp_path/'data'); w.show()
    ident=w.catalog.add(photo)
    w.refresh_lists(); w.select_folder(album)
    wait_for(lambda:settled(w))
    w.show_parent_folder(str(album))
    wait_for(lambda:settled(w))
    assert path_key(album.parent) in w.folder_items
    assert w.folder_items[path_key(album)].parent()==w.folder_items[path_key(album.parent)]
    assert w.folder_filter==str(album)
    assert w.visible_ids==[ident]
    w.close(); QApplication.processEvents()


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS spells Finder-made names decomposed')
def test_decomposed_korean_names_from_the_disk_stay_one_photo_through_import_search_move_and_relink(tmp_path):
    from threading import Event
    from luma.folder_sync import new_files
    from luma.library import move_files,move_folder,relink_folder
    from luma.library_query import query
    nfd=lambda text:unicodedata.normalize('NFD',text);nfc=lambda text:unicodedata.normalize('NFC',str(text))
    root=tmp_path/nfd('여행');album=root/nfd('서울 야경');album.mkdir(parents=True)
    files=[make_photo(album/nfd(f'밤거리{n}.jpg')) for n in range(2)]
    listed=[os.path.join(folder,name) for folder,_,names in os.walk(root) for name in names]
    assert all(path!=nfc(path) for path in listed)                     # the disk hands the names back decomposed
    cat=Catalog(tmp_path/'data');ids=[cat.add(path) for path in sorted(listed)]
    stored=[cat.photo(i)['path'] for i in ids]
    assert stored==[nfc(p) for p in stored] and all(Path(p).is_file() for p in stored)
    assert [cat.add(path) for path in sorted(listed)]==ids and len(cat.photos())==2          # the same files, not new ones
    assert new_files(cat.folder_roots(),{path_key(p) for p in stored},settle=0)==[]           # nor for the folder watcher
    database=cat.directory/'catalog.sqlite'
    assert query(database,{'search':'밤거리'},Event())['ids']==ids                              # typed text is composed
    assert query(database,{'folder':nfc(root),'recursive':True},Event())['ids']==ids
    assert query(database,{'folder':str(root),'recursive':True},Event())['ids']==ids
    target=root/nfd('부산');target.mkdir()
    move_files(cat,{stored[0]:str(target/nfd('바다.jpg'))})
    moved=cat.photo(ids[0]);assert moved['path']==nfc(moved['path']) and moved['name']=='바다.jpg' and Path(moved['path']).is_file()
    renamed=tmp_path/nfd('여행 2026');move_folder(cat,str(root),str(renamed))
    assert all(cat.photo(i)['path']==nfc(cat.photo(i)['path']) and Path(cat.photo(i)['path']).is_file() for i in ids)
    assert cat.folder_roots()==[nfc(r) for r in cat.folder_roots()]
    again=tmp_path/nfd('다시 연결');renamed.rename(again)                 # moved outside the app, found again by its disk name
    disk_name=next(str(p) for p in tmp_path.iterdir() if nfc(p.name)=='다시 연결')
    assert relink_folder(cat,nfc(renamed),disk_name)==2
    assert all(cat.photo(i)['path']==nfc(cat.photo(i)['path']) and Path(cat.photo(i)['path']).is_file() for i in ids)
    assert len(cat.photos())==2 and query(database,{'folder':nfc(again),'recursive':True},Event())['ids']==ids
    cat.close()
