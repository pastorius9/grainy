import json
from pathlib import Path
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu, QMessageBox
from test_studio_ui import app
from luma import grainy_rs
from luma.desktop_session import DesktopSession
from luma.i18n import language, set_language
from luma.preferences import Preferences

ROOT = Path(__file__).resolve().parents[1]
PAGE = 'https://pastorius9.github.io/grainy/'


def session_for(app, tmp_path, monkeypatch, press):
    """A started session whose notice box is answered by `press` ('get', 'later' or None for closing)."""
    preferences = Preferences(tmp_path/'settings.json'); preferences.save_language('ko'); preferences.save('update_check', False)
    session = DesktopSession(app, tmp_path/'library', preferences)
    boxes, opened = [], []
    monkeypatch.setattr(QTimer, 'singleShot', staticmethod(lambda *a, **k: None))   # no start-up timers left over for other tests
    monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)      # the test run is headless; the notice itself is what is tested
    def answer(box):
        boxes.append((box.objectName(), box.windowTitle(), [button.text() for button in box.buttons()]))
        text = {'get': 'Grainy RS 받기', 'later': '나중에'}.get(press)
        box.setProperty('chosen', text)
        box._clicked = next((b for b in box.buttons() if b.text() == text), None)
    monkeypatch.setattr(QMessageBox, 'exec', answer)
    monkeypatch.setattr(QMessageBox, 'clickedButton', lambda box: getattr(box, '_clicked', None))
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()) or True)
    return session, preferences, boxes, opened


def test_shown_once_and_the_button_opens_the_page(app, tmp_path, monkeypatch):
    session, preferences, boxes, opened = session_for(app, tmp_path, monkeypatch, 'get')
    previous = language()
    try:
        assert session.start()
        assert session.rs_notice(packaged=True) is True
        (name, title, buttons), = boxes
        assert name == 'grainyRsNotice' and title == 'Grainy RS로 옮겨 주세요' and buttons == ['Grainy RS 받기', '나중에']
        assert opened == [PAGE]
        assert Preferences(tmp_path/'settings.json').values[grainy_rs.KEY] is True
        assert session.rs_notice(packaged=True) is False and len(boxes) == 1     # not again
        session.window.close()
    finally:
        set_language(previous)


def test_later_and_closing_also_end_it_without_opening_the_page(app, tmp_path, monkeypatch):
    for index, press in enumerate(('later', None)):
        folder = tmp_path/str(index); folder.mkdir()
        session, preferences, boxes, opened = session_for(app, folder, monkeypatch, press)
        previous = language()
        try:
            assert session.start()
            assert session.rs_notice(packaged=True) is True and opened == []
            assert Preferences(folder/'settings.json').values[grainy_rs.KEY] is True
            assert session.rs_notice(packaged=True) is False
            session.window.close()
        finally:
            set_language(previous)


def test_never_from_source_tests_or_headless_runs(app, tmp_path, monkeypatch):
    session, preferences, boxes, opened = session_for(app, tmp_path, monkeypatch, 'get')
    previous = language()
    try:
        assert session.start()
        assert session.rs_notice() is False                                      # running from source
        monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
        assert session.rs_notice(packaged=True) is False                         # headless
        assert boxes == [] and grainy_rs.KEY not in preferences.values
        session.window.close()
    finally:
        set_language(previous)


def test_the_settings_menu_opens_the_page(app, tmp_path, monkeypatch):
    session, preferences, boxes, opened = session_for(app, tmp_path, monkeypatch, None)
    previous = language()
    try:
        assert session.start()
        action, = [a for a in session.window.findChild(QMenu, 'settingsMenu').actions() if a.text() == 'Grainy RS 받기…']
        action.trigger()
        assert opened == [PAGE] and boxes == []
        session.window.close()
    finally:
        set_language(previous)


def test_english_strings_exist():
    english = json.loads((ROOT/'assets'/'i18n'/'en.json').read_text(encoding='utf-8'))
    for source in ('Grainy RS로 옮겨 주세요', 'Grainy RS 받기', 'Grainy RS 받기…', '나중에'):
        assert source in english
    assert all(key in english for key in english if key.startswith('Grainy는 이제 Grainy RS'))
