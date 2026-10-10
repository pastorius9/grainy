import os
import time
import numpy as np
from pathlib import Path
from PIL import Image
from test_studio_ui import app, wait
from luma.folders import path_key
from luma.folder_sync import new_files, EXCLUDED


def photo(path, value=90, age=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.full((30, 40, 3), value, np.uint8)).save(path)
    stamp = time.time()-age; os.utime(path, (stamp, stamp))
    return path


def test_new_files_skips_known_fresh_failed_and_other_files(tmp_path):
    root = tmp_path/'roll'
    known = photo(root/'a.jpg'); new = photo(root/'sub'/'b.JPG', age=90); late = photo(root/'c.jpg', age=30)
    photo(root/'writing.jpg', age=0); bad = photo(root/'bad.jpg'); (root/'notes.txt').write_text('x')
    info = os.stat(bad)
    args = ([str(root), str(tmp_path/'missing')], {path_key(known)})
    assert new_files(*args, {path_key(bad): (info.st_mtime, info.st_size)}) == [str(new), str(late)]   # oldest first
    assert str(bad) in new_files(*args, {path_key(bad): (info.st_mtime-9, info.st_size)})              # changed: offered again


def test_closing_does_not_wait_for_an_automatic_import(app, tmp_path):
    from luma.app import MainWindow
    roll = tmp_path/'roll'
    w = MainWindow(tmp_path/'data')
    w.catalog.add(photo(roll/'1.jpg')); w.refresh_lists()
    for n in range(2, 8):photo(roll/f'{n}.jpg')
    assert w.folder_sync.scan()
    wait(lambda: w.import_busy and len(w.import_pending_paths) > 1)
    assert w.close() and w.closing and not w.import_queue
    again = MainWindow(tmp_path/'data')                                       # the rest arrives next time
    try:
        assert again.folder_sync.scan()
        wait(lambda: len(again.catalog.paths()) == 7 and not again.import_busy)
    finally:
        wait(lambda: not again.render_running); again.close()


def test_registered_folder_imports_new_photos_but_not_removed_or_exported_ones(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from luma.app import MainWindow
    from luma.engine import export_image, defaults
    popups = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *a, **k: popups.append(a))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    roll = tmp_path/'roll'
    w = MainWindow(tmp_path/'data'); errors = []
    monkeypatch.setattr(w, 'show_error', errors.append)                       # a modal error box would hang the test
    try:
        first = w.catalog.add(photo(roll/'1.jpg')); w.refresh_lists()
        names = lambda: sorted(Path(p).name for p in w.catalog.paths())
        idle = lambda: not w.folder_sync.running and not w.import_busy and not w.import_scans
        photo(roll/'new roll'/'2.jpg'); (roll/'broken.jpg').write_bytes(b'not a photo')
        stamp = time.time()-60; os.utime(roll/'broken.jpg', (stamp, stamp))
        assert w.folder_sync.scan()
        wait(lambda: idle() and names() == ['1.jpg', '2.jpg'])
        assert not popups and len(w.folder_sync.skipped) == 1 and w.catalog.folder_roots() == [str(roll)]
        export_image(roll/'1.jpg', roll/'out'/'export.jpg', defaults())
        os.utime(roll/'out'/'export.jpg', (stamp, stamp))
        wait(lambda: not w.library_loading and first in w.photo_model.positions)
        w.activate(first); wait(lambda: w.source is not None and not w.render_running)
        w.manager.remove(False)                                               # "remove from catalog only"
        assert w.folder_sync.scan()
        wait(lambda: not w.folder_sync.running); wait(idle)
        assert names() == ['2.jpg'] and path_key(roll/'out'/'export.jpg') in w.catalog.preference(EXCLUDED)
        w.import_paths([str(roll/'1.jpg')])                                   # asked for explicitly: comes back
        wait(lambda: idle() and names() == ['1.jpg', '2.jpg'])
        assert path_key(roll/'1.jpg') not in w.catalog.preference(EXCLUDED)
        w.folder_sync_action.setChecked(False); photo(roll/'3.jpg')
        assert not w.folder_sync.scan() and not w.catalog.preference('auto_sync_folders')
        assert not errors
    finally:
        wait(lambda: not w.render_running); w.close()
