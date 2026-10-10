import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sqlite3
import pytest
from PySide6.QtWidgets import QApplication,QMessageBox
from test_studio_ui import app,wait
from test_folder_sync import photo
from luma.catalog import Catalog
from luma.app import MainWindow
from luma.folders import path_key,left_out,folder_nodes
from luma.folder_sync import new_files
from luma.folder_locations import relink_plan,apply_relink


def test_catalog_removal_takes_photos_roots_and_watches_but_no_file(tmp_path):
    catalog=Catalog(tmp_path/'catalog')
    keep=catalog.add(photo(tmp_path/'photos'/'keep.jpg'))
    gone=[catalog.add(photo(tmp_path/'photos'/'roll'/name)) for name in ('a.jpg','sub/b.jpg')]
    other=catalog.add(photo(tmp_path/'roll 2'/'c.jpg'))                       # a name that only starts the same
    roll=tmp_path/'photos'/'roll'
    catalog.register_folder(roll/'sub');copy=catalog.virtual_copy(gone[0])
    catalog.db.execute('INSERT INTO collections(name) VALUES(?)',('set',));collection=catalog.db.execute('SELECT id FROM collections').fetchone()[0]
    catalog.db.executemany('INSERT INTO collection_members VALUES(?,?)',[(collection,i) for i in gone+[keep]])
    catalog.save_preference(f'undo:{gone[0]}',{'undo':[],'redo':[]})
    catalog.save_preference('watch_folders',[str(roll/'sub'),str(tmp_path/'photos')])
    catalog.save_preference('removed_folders',[str(roll/'sub'/'old'),str(tmp_path/'elsewhere')])
    assert sorted(catalog.remove_folder(roll))==sorted(gone+[copy])
    assert {p['id'] for p in catalog.photos()}=={keep,other}
    assert catalog.folder_roots()==sorted([str(tmp_path/'photos'),str(tmp_path/'roll 2')],key=str.casefold)
    assert catalog.preference('watch_folders')==[str(tmp_path/'photos')]
    assert catalog.removed_folders()==[str(tmp_path/'elsewhere'),str(roll)]      # the mark below it is covered
    assert catalog.collection_ids(collection)=={keep} and catalog.preference(f'undo:{gone[0]}') is None
    assert (roll/'a.jpg').is_file() and (roll/'sub'/'b.jpg').is_file()
    catalog.restore_folder(tmp_path/'photos')                                    # imported again from above
    assert catalog.removed_folders()==[str(tmp_path/'elsewhere')]
    catalog.remove_folder(roll);catalog.register_folder(roll)                    # registered again: no longer removed
    assert str(roll) not in catalog.removed_folders()
    catalog.close()


def test_many_photos_leave_in_one_step(tmp_path):
    catalog=Catalog(tmp_path/'catalog')
    with catalog.db:
        catalog.db.executemany('INSERT INTO photos(path,name,edits,metadata,imported) VALUES(?,?,?,?,?)',
            [(str(tmp_path/'big'/f'{n:05d}.jpg'),f'{n:05d}.jpg','{}','{}','') for n in range(1300)])
    keep=catalog.add(photo(tmp_path/'small'/'keep.jpg'))
    assert len(catalog.remove_folder(tmp_path/'big'))==1300
    assert [p['id'] for p in catalog.photos()]==[keep]
    catalog.close()


def test_automatic_import_and_the_folder_list_leave_a_removed_folder_out(tmp_path):
    root=tmp_path/'photos'
    a=photo(root/'a.jpg');b=photo(root/'roll'/'b.jpg');c=photo(root/'roll'/'day'/'c.jpg');d=photo(root/'Roll 2'/'d.jpg')
    assert new_files([str(root)],set(),removed=[str(root/'roll')])==[str(a),str(d)]
    # A folder imported again inside the removed one is read; the rest of the removed folder is not.
    assert new_files([str(root),str(root/'roll'/'day')],set(),removed=[str(root/'roll')])==[str(a),str(c),str(d)]
    assert len(new_files([str(root)],set()))==4 and b.is_file()
    roots,removed=[str(root),str(root/'roll'/'day')],[str(root/'roll')]
    assert left_out(root/'roll',roots,removed) and left_out(root/'roll'/'other',roots,removed)
    assert not left_out(root/'roll'/'day',roots,removed) and not left_out(root/'roll'/'day'/'x',roots,removed)
    assert not left_out(root/'Roll 2',roots,removed) and not left_out(root,roots,removed)


def test_removed_folders_follow_a_new_location(tmp_path):
    catalog=Catalog(tmp_path/'catalog')
    old=tmp_path/'old';kept=catalog.add(photo(old/'keep'/'a.jpg'));catalog.add(photo(old/'roll'/'b.jpg'))
    catalog.register_folder(old);catalog.remove_folder(old/'roll')
    new=tmp_path/'new';old.rename(new)
    apply_relink(catalog,relink_plan(catalog,old,new))
    assert catalog.removed_folders()==[str(new/'roll')] and catalog.photo(kept)['path']==str(new/'keep'/'a.jpg')
    catalog.close()


@pytest.fixture
def window(app,tmp_path,monkeypatch):
    root=tmp_path/'photos';first=photo(root/'one.jpg');second=photo(root/'roll'/'two.jpg');photo(root/'roll'/'day'/'three.jpg')
    w=MainWindow(tmp_path/'catalog');w.show_error=lambda text:pytest.fail(text)
    w.folder_sync.timer.stop()
    ids=[w.catalog.add(first),w.catalog.add(second),w.catalog.add(root/'roll'/'day'/'three.jpg')]
    w.catalog.register_folder(root);w.select_folder(root);w.show()
    wait(lambda:w.current_id in ids and w.source is not None and not w.library_loading and not w.folder_panel.running)
    answers=[];reply=[QMessageBox.StandardButton.Yes]
    def question(*args):
        answers.append(args[2]);return reply[0]
    monkeypatch.setattr(QMessageBox,'question',question)
    w.reply=reply
    yield w,root,ids,answers
    w.maintenance_running=False;wait(lambda:not w.render_running);w.close();QApplication.processEvents()


def settled(w):
    return not w.library_loading and not w.folder_panel.running and not w.folder_sync.running and not w.import_busy and not w.import_scans


def test_removing_a_folder_inside_a_registered_one(window):
    w,root,ids,answers=window;roll=root/'roll'
    w.folder_panel.timer.setInterval(100)
    wait(lambda:path_key(roll/'day') in w.folder_items)
    w.reply[0]=QMessageBox.StandardButton.No
    w.remove_folder(str(roll))
    assert len(answers)==1 and '2' in answers[0] and len(w.catalog.photos())==3
    assert not list((w.catalog.directory/'backups').glob('folder-removal-*')) and not w.maintenance_running
    w.reply[0]=QMessageBox.StandardButton.Yes
    w.select_folder(roll/'day');wait(lambda:settled(w) and w.current_id==ids[2])
    assert w.remove_folder_button.isEnabled()
    w.remove_folder(str(roll))
    assert [p['id'] for p in w.catalog.photos()]==[ids[0]] and w.catalog.folder_roots()==[str(root)]
    assert (roll/'two.jpg').is_file() and (roll/'day'/'three.jpg').is_file()
    assert w.folder_filter is None and w.catalog.removed_folders()==[str(roll)]
    wait(lambda:settled(w) and w.current_id==ids[0] and path_key(roll) not in w.folder_items)
    assert not w.remove_folder_button.isEnabled() and path_key(root) in w.folder_items
    backups=list((w.catalog.directory/'backups').glob('folder-removal-*.sqlite'));assert len(backups)==1
    with sqlite3.connect(backups[0]) as db:assert db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]==3
    # The folder is still on disk below a registered folder: neither listed nor imported by itself.
    photo(roll/'later.jpg')
    w.folder_panel.signature=None;w.folder_panel.refresh();wait(lambda:settled(w))
    assert path_key(roll) not in w.folder_items and path_key(roll/'day') not in w.folder_items
    assert w.folder_sync.scan();wait(lambda:settled(w))
    assert len(w.catalog.photos())==1
    w.extras.watch_paths=[str(root)];w.extras.watch_scan();wait(lambda:settled(w))     # a watched folder above it
    assert len(w.catalog.photos())==1 and w.catalog.removed_folders()==[str(roll)]
    # Asked for again: everything in it comes back as a new import.
    w.sync_folder(str(roll));wait(lambda:settled(w) and len(w.catalog.photos())==4)
    assert w.catalog.removed_folders()==[]
    wait(lambda:settled(w) and path_key(roll) in w.folder_items)
    w.remove_folder(str(roll));wait(lambda:settled(w) and len(w.catalog.photos())==1)
    w.sync_folder(str(root));wait(lambda:settled(w) and len(w.catalog.photos())==4)    # ... or the folder above it
    assert w.catalog.removed_folders()==[]


def test_header_button_removes_the_selected_registered_folder(window):
    w,root,ids,answers=window
    assert w.remove_folder_button.isEnabled() and w.removable_folder()==str(root)
    photo(root/'new.jpg')
    assert w.folder_sync.scan()                                              # still running: it will list new.jpg
    w.remove_folder_button.click()
    assert answers and w.catalog.photos()==[] and w.catalog.folder_roots()==[]
    wait(lambda:settled(w) and not w.folder_items)
    assert w.current_id is None and not w.remove_folder_button.isEnabled() and len(w.catalog.photos())==0
    assert (root/'one.jpg').is_file() and (root/'roll'/'two.jpg').is_file() and w.catalog.folder_roots()==[]
    w.set_filter('all');wait(lambda:settled(w));assert w.removable_folder() is None
    w.remove_folder();assert len(answers)==1                                 # nothing selected: nothing asked


def test_removal_waits_for_other_catalog_work_and_keeps_everything_when_the_backup_fails(window,monkeypatch):
    w,root,ids,answers=window;notes=[]
    monkeypatch.setattr(QMessageBox,'information',lambda *args:notes.append(args[2]))
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:notes.append(args[2]))
    w.export_running=True;w.remove_folder(str(root));w.export_running=False
    assert len(notes)==1 and not answers and len(w.catalog.photos())==3
    def fail(*args):raise OSError('fixture disk full')
    monkeypatch.setattr(w.catalog,'backup',fail)
    w.remove_folder(str(root))
    assert len(notes)==2 and len(w.catalog.photos())==3 and w.catalog.folder_roots()==[str(root)]
    assert not w.maintenance_running and w.current_id in ids and w.catalog.removed_folders()==[]
