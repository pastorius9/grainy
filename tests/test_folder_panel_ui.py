import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import sqlite3
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox,QMenu
from test_studio_ui import app,wait
from test_folder_locations import photo
from luma.app import MainWindow
from luma.folders import path_key


@pytest.fixture
def window(app,tmp_path):
    root=tmp_path/'photos';path=photo(root/'one.jpg')
    w=MainWindow(tmp_path/'catalog');w.show_error=lambda text:pytest.fail(text)
    ident=w.catalog.add(path);w.select_folder(root);w.show()
    wait(lambda:w.current_id==ident and w.source is not None and not w.library_loading and not w.folder_panel.running)
    yield w,root,ident
    w.maintenance_running=False;w.close();QApplication.processEvents()


def test_timer_discovers_new_folders_and_expansion_discovers_children(window):
    w,root,ident=window
    assert w.folder_panel.timer.interval()==5000
    w.folder_panel.timer.setInterval(100)
    new=root/'새 촬영';photo(new/'new.jpg');empty=root/'빈 폴더';empty.mkdir()
    wait(lambda:path_key(new) in w.folder_items and path_key(empty) in w.folder_items)
    assert w.folder_items[path_key(new)].text(1)=='0'
    assert len(w.catalog.photos())==1 and w.current_id==ident
    nested=new/'첫 날';nested.mkdir()
    w.folder_items[path_key(new)].setExpanded(True)
    wait(lambda:path_key(nested) in w.folder_items)
    # No original file, folder or catalog entry is removed by directory refresh.
    assert (new/'new.jpg').is_file() and empty.is_dir()


def test_live_rename_reloads_active_photo_and_preserves_edits_and_session(window):
    w,root,ident=window
    w.folder_panel.timer.setInterval(100)
    wait(lambda:path_key(root) in w.catalog.preference('folder_identities',{}))
    w.set_setting('exposure',.65);w.commit();undo=w.catalog.preference(f'undo:{ident}')
    new=root.with_name('사진 이름 변경');root.rename(new)
    wait(lambda:w.catalog.photo(ident)['path']==str(new/'one.jpg') and w.source is not None and not w.library_loading)
    assert path_key(root) not in w.folder_items and path_key(new) in w.folder_items
    assert w.current_id==ident and w.folder_filter==str(new)
    assert w.settings['exposure']==.65 and w.catalog.preference(f'undo:{ident}')==undo
    assert w.catalog.preference('folder_panel')['selected']==str(new)
    backups=list((w.catalog.directory/'backups').glob('folder-location-*.sqlite'));assert len(backups)==1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('SELECT path FROM photos WHERE id=?',(ident,)).fetchone()[0]==str(root/'one.jpg')
    assert (new/'one.jpg').is_file()


def test_manual_location_cancel_and_backup_failure_are_non_destructive(window,monkeypatch):
    w,root,ident=window;w.folder_panel.shutdown()
    new=root.with_name('manual');root.rename(new)
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(new))
    questions=[]
    def reject(*args):questions.append(args[2]);return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox,'question',reject)
    w.folder_panel.locate(str(root))
    assert questions and w.catalog.photo(ident)['path']==str(root/'one.jpg')
    assert not list((w.catalog.directory/'backups').glob('folder-location-*'))
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    real_backup=w.catalog.backup
    def fail(*args):raise OSError('fixture disk full')
    monkeypatch.setattr(w.catalog,'backup',fail)
    with pytest.raises(OSError):w.folder_panel.locate(str(root))
    assert not w.maintenance_running and w.current_id==ident
    assert w.catalog.photo(ident)['path']==str(root/'one.jpg')
    monkeypatch.setattr(w.catalog,'backup',real_backup)
    w.folder_panel.locate(str(root))
    wait(lambda:w.source is not None and not w.library_loading)
    assert w.catalog.photo(ident)['path']==str(new/'one.jpg')


def test_context_menu_offers_refresh_and_location_without_removal(window):
    w,root,_=window;captured=[]
    def inspect():
        menu=QApplication.activePopupWidget()
        if isinstance(menu,QMenu):
            captured.extend(a.text() for a in menu.actions());menu.close()
    QTimer.singleShot(100,inspect)
    item=w.folder_items[path_key(root)];w.folder_tree.scrollToItem(item)
    w.folder_context_menu(w.folder_tree.visualItemRect(item).center())
    assert '폴더 목록 새로 고침' in captured and '폴더 위치 다시 지정…' in captured
    assert not any('제거' in text or '삭제' in text for text in captured)
