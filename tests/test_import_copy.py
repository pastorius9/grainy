import os
import numpy as np
import pytest
from datetime import datetime
from pathlib import Path
from PIL import Image
from test_studio_ui import app, wait
from luma.import_copy import plan, execute, check_template, duplicate_key, known_keys


def photo(path, value, when=None):
    Image.fromarray(np.full((30, 40, 3), value, np.uint8)).save(path)
    if when:
        stamp = datetime.strptime(when, '%Y-%m-%d %H:%M').timestamp(); os.utime(path, (stamp, stamp))
    return path


def test_plan_orders_by_capture_time_renames_and_skips_suspected_duplicates(tmp_path):
    card = tmp_path/'card'; card.mkdir()
    a = photo(card/'IMG_2.jpg', 10, '2026-09-24 10:00'); b = photo(card/'IMG_1.jpg', 20, '2026-09-25 09:30')
    read = lambda p: {}
    planned, skipped = plan([a, b], tmp_path/'dest', 'year/date', '{date}_{n:03d}_{stem}', read=read)
    assert [(Path(s).name, str(r).replace(os.sep, '/')) for s, r in planned] == [
        ('IMG_2.jpg', '2026/2026-09-24/20260924_001_IMG_2.jpg'), ('IMG_1.jpg', '2026/2026-09-25/20260925_002_IMG_1.jpg')]
    exif = lambda p: {'date': '2026:09:25 09:30:00'} if Path(p).name == 'IMG_1.jpg' else {}
    known = {('img_1.jpg', '2026:09:25 09:30:00')}
    planned, skipped = plan([a, b], tmp_path/'dest', 'none', '{stem}', known=known, read=exif)
    assert [Path(s).name for s, _ in planned] == ['IMG_2.jpg'] and [Path(s).name for s in skipped] == ['IMG_1.jpg']
    for bad in ('{stem}/x', '{title}', '', '{stem}:'):
        with pytest.raises(ValueError):check_template(bad)


def test_execute_copies_verifies_keeps_sources_and_never_overwrites(tmp_path):
    card = tmp_path/'card'; card.mkdir()
    a = photo(card/'A.jpg', 50); (card/'A.xmp').write_text('<x/>')
    before = a.read_bytes()
    dest, backup = tmp_path/'dest', tmp_path/'backup'
    (dest/'2026').mkdir(parents=True); (dest/'2026'/'A.jpg').write_bytes(b'someone else')     # existing file with the name
    copied, errors = execute([(str(a), Path('2026')/'A.jpg')], dest, backup)
    assert not errors and Path(copied[0]).name == 'A-1.jpg' and Path(copied[0]).read_bytes() == before
    assert (dest/'2026'/'A.jpg').read_bytes() == b'someone else'                 # not overwritten
    assert (dest/'2026'/'A-1.xmp').read_text() == '<x/>' and (backup/'2026'/'A-1.jpg').read_bytes() == before
    assert a.read_bytes() == before and (card/'A.xmp').exists()                  # sources untouched
    assert not list(dest.rglob('*.grainy-part'))
    from threading import Event
    stop = Event(); stop.set()
    assert execute([(str(a), Path('B.jpg'))], dest, cancel=stop) == ([], [])


def test_copied_photos_import_with_keywords_and_preset(app, tmp_path):
    from luma.app import MainWindow
    from luma.folders import path_key
    card = tmp_path/'card'; card.mkdir(); photo(card/'A.jpg', 90)
    w = MainWindow(tmp_path/'data')
    try:
        w.catalog.add(photo(tmp_path/'old.jpg', 30))
        keys = known_keys(w.catalog)
        assert duplicate_key(str(tmp_path/'old.jpg'), w.catalog.photo(1)['info']) in keys
        copied, _ = execute(plan([card/'A.jpg'], tmp_path/'lib', 'none', '{stem}', read=lambda p: {})[0], tmp_path/'lib')
        preset = next(name for name, values in w.all_presets.items() if values.get('monochrome'))
        w.import_extras[path_key(str(Path(copied[0]).resolve()))] = {'keywords': 'card, trip', 'preset': preset}
        w.import_paths(copied)
        wait(lambda: not w.import_busy and not w.import_scans and len(w.catalog.photos()) == 2)
        record = [r for r in w.catalog.photos() if r['path'].endswith('A.jpg')][0]
        full = w.catalog.photo(record['id'])
        assert full['keywords'] == 'card, trip' and full['settings']['monochrome'] and not w.import_extras
    finally:
        wait(lambda: not w.render_running); w.close()


def test_copy_import_dialog_builds_and_cancels(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from luma.app import MainWindow
    from luma.import_copy import dialog
    seen = []
    monkeypatch.setattr(QDialog, 'exec', lambda self: seen.append(self.windowTitle()) or QDialog.DialogCode.Rejected)
    w = MainWindow(tmp_path/'data')
    try:
        dialog(w)
        assert seen and not w.import_scans
        titles = [a.text() for m in w.menuBar().findChildren(type(w.menuBar().actions()[0].menu())) for a in m.actions()]
        assert any('복사해서 가져오기' in t for t in titles)
    finally:
        w.close()
