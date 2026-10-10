import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from test_studio_ui import app,wait
from test_folder_recovery import library
from luma.app import MainWindow
from luma.folders import path_key


def test_startup_recovers_legacy_missing_active_photo_without_blocking_dialog(app,tmp_path):
    c,old,ids,copy=library(tmp_path,metadata=False,previews=True);new=tmp_path/'renamed';old.rename(new)
    c.save_preference('folder_panel',dict(selected=str(old),expanded=[path_key(old)],current_photo=ids[0]));c.close()
    w=MainWindow(tmp_path/'catalog');errors=[];w.show_error=errors.append;w.show()
    try:
        wait(lambda:w.current_id==ids[0] and w.source is not None and w.folder_filter==str(new) and not w.library_loading,timeout=20000)
        assert not errors and w.settings['exposure']==.4
        assert w.catalog.photo(copy)['path']==str(new/'0.jpg')
        report=w.catalog.preference('folder_recovery_last')
        assert report['photos']==4 and report['moves'][0]['proof']=='saved_previews'
        assert len(list((w.catalog.directory/'backups').glob('folder-location-*.sqlite')))==1
        assert path_key(old) not in w.folder_items and path_key(new) in w.folder_items
    finally:w.close()
