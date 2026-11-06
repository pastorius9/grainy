import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from test_studio_ui import app, wait
from luma import safe_start, updater


def test_marker_tells_whether_the_run_before_closed(tmp_path):
    assert safe_start.begin(tmp_path) is None                          # first start
    marker = json.loads((tmp_path/safe_start.NAME).read_text(encoding='utf-8'))
    assert marker['pid'] == os.getpid()
    safe_start.end(tmp_path)
    assert not (tmp_path/safe_start.NAME).exists() and safe_start.begin(tmp_path) is None   # closed normally
    gone = subprocess.Popen([sys.executable, '-c', 'pass']); gone.wait()
    (tmp_path/safe_start.NAME).write_text(json.dumps({'pid': gone.pid, 'version': '0.6.6'}), encoding='utf-8')
    assert safe_start.begin(tmp_path)['version'] == '0.6.6'           # its process is gone without closing: a crash
    running = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        (tmp_path/safe_start.NAME).write_text(json.dumps({'pid': running.pid}), encoding='utf-8')
        assert safe_start.begin(tmp_path) is None                      # another Grainy still running: not a crash
    finally:
        running.kill(); running.wait()
    (tmp_path/safe_start.NAME).write_text(json.dumps({'pid': -5}), encoding='utf-8')
    safe_start.end(tmp_path)                                           # not this run's marker: left alone
    assert (tmp_path/safe_start.NAME).exists()
    (tmp_path/safe_start.NAME).write_text('not json', encoding='utf-8')
    assert safe_start.begin(tmp_path) is None


def session_after(app, tmp_path, crashed, monkeypatch):
    """A started session whose library remembers an open photo; crashed: the marker of a run that did not close."""
    from luma.desktop_session import DesktopSession
    from luma.preferences import Preferences
    from luma.catalog import Catalog
    photo = tmp_path/'roll'/'a.png'; photo.parent.mkdir()
    Image.fromarray(np.full((30, 40, 3), 120, np.uint8)).save(photo)
    catalog = Catalog(tmp_path/'library'); ident = catalog.add(photo); catalog.register_folder(photo.parent)
    catalog.save_preference('folder_panel', {'selected': str(photo.parent.resolve()), 'include_subfolders': True,
                                             'expanded': [], 'current_photo': ident})
    catalog.close()
    prefs = Preferences(tmp_path/'settings'/'settings.json'); prefs.save_language('ko'); prefs.save('update_check', False)
    if crashed:
        gone = subprocess.Popen([sys.executable, '-c', 'pass']); gone.wait()
        (tmp_path/'settings'/safe_start.NAME).write_text(json.dumps({'pid': gone.pid}), encoding='utf-8')
    offered = []
    monkeypatch.setattr(updater.UpdateController, 'after_crash', lambda self: offered.append('after_crash'))
    monkeypatch.setattr(updater.UpdateController, 'startup', lambda self: offered.append('startup'))
    monkeypatch.setattr('luma.desktop_session.DesktopSession.check_folders', lambda self: None)
    session = DesktopSession(app, tmp_path/'library', prefs)
    assert session.start()
    return session, ident, offered


@pytest.mark.parametrize('crashed', [False, True])
def test_after_a_crash_no_photo_opens_and_the_update_offer_comes_first(app, tmp_path, monkeypatch, crashed):
    session, ident, offered = session_after(app, tmp_path, crashed, monkeypatch)
    w = session.window
    try:
        wait(lambda: not w.library_loading and offered, 10000)
        assert w.folder_filter and (w.current_id is None) == crashed     # the folder comes back; its photo only normally
        assert offered[0] == ('after_crash' if crashed else 'startup') and 'startup' not in offered[1:]
        assert getattr(w, 'safe_start', False) == crashed
        assert json.loads((tmp_path/'settings'/safe_start.NAME).read_text(encoding='utf-8'))['pid'] == os.getpid()
    finally:
        session.switching = True; w.close()


@pytest.mark.parametrize('automatic', [True, False])
def test_after_crash_checks_at_once_with_automatic_checks_and_asks_otherwise(app, tmp_path, monkeypatch, automatic):
    from PySide6.QtWidgets import QMessageBox
    from luma.app import MainWindow
    monkeypatch.setattr(updater, 'cleanup', lambda *a: None); monkeypatch.setattr(updater, 'SUPPORTED', True)
    asked = []
    def answer(self):
        asked.append(self.text()); self.clickedButton = lambda: next(b for b in self.buttons() if b.text() == '새 버전 확인'); return 0
    monkeypatch.setattr(QMessageBox, 'exec', answer)
    offered = []; monkeypatch.setattr(updater.UpdateController, 'offer', lambda self, release: offered.append(release['version']))

    class Prefs:
        def __init__(self, **values):self.values = dict(values)
        def save(self, key, value):self.values[key] = value
    release = dict(version='99.0.0', name='Grainy-99.0.0-windows-x64.zip', url='x', size=10, sha256='0'*64, notes='n')
    w = MainWindow(tmp_path/'data')
    try:
        w.show()
        notes = []; show = w.statusBar().showMessage
        w.statusBar().showMessage = lambda text, *a: (notes.append(text), show(text, *a))
        controller = updater.UpdateController(w, Prefs(update_check=automatic), fetcher=lambda: release, packaged=True)
        controller.after_crash()
        wait(lambda: controller.thread is None and offered)
        assert offered == ['99.0.0'] and len(asked) == (0 if automatic else 1)
        assert any('닫히지 않아' in text for text in notes)
        declined = updater.UpdateController(w, Prefs(update_check=False), fetcher=lambda: pytest.fail('no check'), packaged=True)
        monkeypatch.setattr(QMessageBox, 'exec', lambda self: setattr(self, 'clickedButton', lambda: next(b for b in self.buttons() if b.text() == '계속')) or 0)
        declined.after_crash(); app.processEvents()
        assert declined.thread is None                                  # "Continue": nothing goes to GitHub
    finally:
        w.close()
