"""Screens of the app for the interactive tutorial (docs/learn), made only when GRAINY_LEARN_SCREENS names a folder:

    set GRAINY_LEARN_SCREENS=docs\\learn\\img && python -m pytest tests/test_learn_screens.py

For each language: the Develop screen with the tutorial's garden photo, the Library, and where the tour's parts are
(screens-<lang>.json, rectangles as fractions of the screen). Without the variable the test only checks that the
parts the tour points at exist.
"""
import json
import os
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint

from test_studio_ui import app, wait
from luma import i18n
from luma.app import MainWindow

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT/'docs'/'learn'/'img'
PHOTOS = [IMG/'garden.jpg', ROOT/'docs'/'site'/'negative-scan.jpg'] + [IMG/f'film-{n}.jpg' for n in
          ('kodak-portra-400', 'fuji-velvia-50', 'kodak-kodachrome-64', 'kodak-tri-x-400', 'cinestill-800t')]
PARTS = {'library_button': 'library_button', 'develop_button': 'edit_button', 'import': None, 'export_button': 'export_button',
         'feedback': 'feedback_button', 'folders': 'folder_tree', 'presets': 'preset_tree', 'photo': 'view', 'filmstrip': 'filmstrip',
         'histogram': 'histogram', 'panels': 'tabs', 'compare': 'compare_button', 'before': 'before_button'}


def rect(w, widget):
    top_left = widget.mapTo(w, QPoint(0, 0))
    return [round(top_left.x()/w.width(), 4), round(top_left.y()/w.height(), 4),
            round(widget.width()/w.width(), 4), round(widget.height()/w.height(), 4)]


@pytest.mark.parametrize('language', ['ko', 'en'])
def test_learn_screens(app, tmp_path, language, monkeypatch):
    folder = os.environ.get('GRAINY_LEARN_SCREENS')
    from luma import engine
    monkeypatch.setattr(engine, 'HDR_FEATURE', False)          # as shipped (tests switch it on)
    previous = i18n.language()
    i18n.set_language(language)
    try:
        photos = tmp_path/'photos'; photos.mkdir()
        for path in PHOTOS:
            if path.exists():shutil.copy(path, photos/path.name)
        w = MainWindow(tmp_path/'data'); w.resize(1600, 1000)
        for path in sorted(photos.iterdir()):w.catalog.add(path)
        w.refresh_lists()
        garden = next(p['id'] for p in w.catalog.photos() if p['name'] == 'garden.jpg') if hasattr(w.catalog, 'photos') else None
        ident = garden or w.catalog.add(photos/'garden.jpg')
        w.show(); w.command_button.hide()
        for other in [p['id'] for p in w.catalog.photos()] if hasattr(w.catalog, 'photos') else []:   # each opened once: its thumbnail
            w.activate(other); wait(lambda: w.source is not None and not w.render_running)
        w.activate(ident)
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        import_button = next(b for b in w.findChildren(type(w.export_button)) if b.text() in ('사진 가져오기', 'Import photos'))
        parts = {}
        for name, attribute in PARTS.items():
            widget = import_button if attribute is None else getattr(w, attribute)
            assert widget is not None, name
            parts[name] = rect(w, widget)
        w.tabs.sections['basic'].set_expanded(True)
        if folder:
            out = Path(folder); out.mkdir(parents=True, exist_ok=True)
            wait(lambda: not w.render_running)
            w.grab().save(str(out/f'screen-develop-{language}.jpg'), 'JPG', 86)
            from PySide6.QtTest import QTest
            w.set_mode(1); wait(lambda: not w.library_loading)
            for _ in range(60):                                     # thumbnails are built in the background
                QTest.qWait(250)
                if not w.library_loading and not getattr(w.grid.model(), 'pending_thumbs', None):break
            QTest.qWait(1500)
            w.grab().save(str(out/f'screen-library-{language}.jpg'), 'JPG', 86)
            (out/f'screens-{language}.json').write_text(json.dumps({'size': [w.width(), w.height()], 'parts': parts}, indent=1), 'utf-8')
        wait(lambda: not w.render_running); w.close()
    finally:
        i18n.set_language(previous)
