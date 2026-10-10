from PySide6.QtWidgets import QMenu, QMessageBox
from test_studio_ui import app
from luma import __version__
from luma.desktop_session import DesktopSession
from luma.preferences import Preferences


def test_about_names_the_version_and_says_grainy_is_unrelated_to_adobe(app, tmp_path, monkeypatch):
    preferences = Preferences(tmp_path/'settings.json'); preferences.save_language('ko')
    preferences.save('update_check', False)
    session = DesktopSession(app, tmp_path/'library', preferences)
    shown = []
    monkeypatch.setattr(QMessageBox, 'exec', lambda box: shown.append((box.text(), box.informativeText())))
    try:
        assert session.start()
        menu = session.window.findChild(QMenu, 'settingsMenu')
        action = next(a for a in menu.actions() if a.text() == 'Grainy 정보…')
        assert action is menu.actions()[-1]
        action.trigger()
        (title, text), = shown
        assert title == f'Grainy {__version__}'
        assert 'Adobe Inc.의 상표' in text and 'Adobe와 관련이 없습니다' in text and 'MIT' in text
    finally:
        session.switching = True
        if session.window:session.window.close()
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_about_text_has_an_english_translation():
    import json
    from pathlib import Path
    table = json.loads((Path(__file__).resolve().parents[1]/'assets/i18n/en.json').read_text(encoding='utf-8'))
    about = next(value for key, value in table.items() if key.startswith('원본을 건드리지 않는 사진 보정'))
    assert 'trademarks of Adobe Inc.' in about and 'not affiliated' in about
    assert table['Grainy 정보…'] == 'About Grainy…'
