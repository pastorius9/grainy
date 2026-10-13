from pathlib import Path
from PySide6.QtWidgets import QDialog, QMenu, QMessageBox, QTextBrowser
from test_studio_ui import app
from luma import __version__, whats_new
from luma.desktop_session import DesktopSession
from luma.i18n import language, set_language
from luma.preferences import Preferences

ROOT = Path(__file__).resolve().parents[1]
LOG = '''# Log

## 1.2.10 — Ten

- ten

## 1.2.9 — Nine

- nine

## 1.2.8

- eight
'''


def folder(tmp_path, english=''):
    (tmp_path/'assets').mkdir(exist_ok=True)
    (tmp_path/'CHANGELOG.md').write_text(LOG, encoding='utf-8')
    if english:(tmp_path/'assets'/'whats-new-en.md').write_text(english, encoding='utf-8')
    return tmp_path


def test_sections_and_the_range_after_the_version_seen_before(tmp_path):
    root = folder(tmp_path, '## 1.2.10 — Ten in English\n\n- ten!\n')
    assert whats_new.sections(LOG) == [('1.2.10', 'Ten', '- ten'), ('1.2.9', 'Nine', '- nine'), ('1.2.8', '', '- eight')]
    versions = lambda *a, **k: [entry[0] for entry in whats_new.notes(*a, root=root, lang='ko', **k)]
    assert versions('1.2.8', '1.2.10') == ['1.2.10', '1.2.9']               # numbers, not text: 10 > 9
    assert versions('1.2.9', '1.2.10') == ['1.2.10']
    assert versions(None, '1.2.9') == ['1.2.9']                             # unknown earlier version: this one only
    assert versions('1.2.8', '1.2.9') == ['1.2.9']                          # nothing newer than the running version
    assert versions('1.0', '1.3') == ['1.2.10', '1.2.9', '1.2.8']
    english = whats_new.notes('1.2.8', '1.2.10', lang='en', root=root)
    assert english == [('1.2.10', 'Ten in English', '- ten!'), ('1.2.9', 'Nine', '- nine')]    # Korean where no English entry
    assert whats_new.markdown(english).startswith('## 1.2.10 — Ten in English\n\n- ten!\n\n## 1.2.9 — Nine')
    assert whats_new.notes('1', '2', root=tmp_path/'missing') == []


def test_pending_skips_a_first_run_the_same_version_and_a_downgrade(tmp_path):
    preferences = Preferences(tmp_path/'settings.json')
    assert whats_new.pending(preferences, first_run=True) == []
    preferences.save('language', 'ko')
    assert [entry[0] for entry in whats_new.pending(preferences)] == [__version__]      # updated from a version before this feature
    whats_new.remember(preferences)
    assert Preferences(tmp_path/'settings.json').values[whats_new.KEY] == __version__
    assert whats_new.pending(preferences) == []
    preferences.save(whats_new.KEY, '999.0.0')
    assert whats_new.pending(preferences) == []


def test_the_shipped_change_log_describes_this_version_and_both_builds_carry_it():
    assert whats_new.sections((ROOT/'CHANGELOG.md').read_text(encoding='utf-8'))[0][0] == __version__
    assert whats_new.notes(None, lang='ko')[0][0] == __version__
    for script in ('Build Grainy.ps1', 'tools/build_macos.py'):
        text = (ROOT/script).read_text(encoding='utf-8-sig')
        assert 'CHANGELOG.md' in text and 'assets' in text


def test_shown_once_after_an_update_and_again_from_the_settings_menu(app, tmp_path, monkeypatch):
    preferences = Preferences(tmp_path/'settings.json'); preferences.save_language('ko')
    preferences.save('update_check', False); preferences.save(whats_new.KEY, '0.5.90')
    session = DesktopSession(app, tmp_path/'library', preferences)
    shown = []
    monkeypatch.setattr(QDialog, 'exec', lambda dialog: shown.append((dialog.objectName(), dialog.windowTitle(), dialog.findChild(QTextBrowser).toPlainText())))
    monkeypatch.setattr(QMessageBox, 'exec', lambda box: None)
    previous = language()
    try:
        assert session.start()
        session.whats_new()                                                  # running from source: never on its own
        assert shown == []
        session.whats_new(packaged=True)
        (name, title, text), = shown
        assert name == 'whatsNew' and title == f'Grainy {__version__}의 달라진 점'
        assert __version__ in text and '0.5.91' in text and '0.5.90 —' not in text
        assert Preferences(tmp_path/'settings.json').values[whats_new.KEY] == __version__
        session.whats_new(packaged=True)
        assert len(shown) == 1                                               # once
        menu = session.window.findChild(QMenu, 'settingsMenu')
        next(a for a in menu.actions() if a.text() == '이번 버전의 달라진 점…').trigger()
        assert len(shown) == 2 and __version__ in shown[1][2] and '0.5.91' not in shown[1][2]
    finally:
        set_language(previous)
        session.switching = True
        if session.window:session.window.close()
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_a_first_run_only_remembers_the_version(app, tmp_path, monkeypatch):
    preferences = Preferences(tmp_path/'settings.json')
    session = DesktopSession(app, tmp_path/'library', preferences)
    monkeypatch.setattr(whats_new, 'show', lambda *a, **k: (_ for _ in ()).throw(AssertionError('shown on a first run')))
    try:
        session.whats_new(first_run=True, packaged=True)
        assert preferences.values[whats_new.KEY] == __version__
    finally:
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_english_strings_and_notes_exist():
    import json
    english = json.loads((ROOT/'assets'/'i18n'/'en.json').read_text(encoding='utf-8'))
    for source in ('Grainy {0}의 달라진 점', 'Grainy가 {0} 버전으로 업데이트되었습니다.', '이 버전의 변경 기록을 찾지 못했습니다.', '이번 버전의 달라진 점…'):
        assert source in english
    assert whats_new.sections((ROOT/'assets'/'whats-new-en.md').read_text(encoding='utf-8'))
