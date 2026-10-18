import json
import re
from fnmatch import fnmatch
from pathlib import Path
from PySide6.QtWidgets import QDialog, QLabel, QMenu, QMessageBox, QTextBrowser
from test_studio_ui import app
from luma import __version__, feedback, whats_new
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


def sent(*refs):
    return [{'ref': ref, 'version': '0.5.94', 'sent': '2026-10-16'} for ref in refs]


def captured(dialog):
    """What a whats-new dialog shows: its name, title, notes and the thanks line above them (or None)."""
    view = dialog.findChild(QTextBrowser); line = dialog.findChild(QLabel, 'shipped')
    if line is not None:assert dialog.layout().indexOf(line) < dialog.layout().indexOf(view)
    return dialog.objectName(), dialog.windowTitle(), view.toPlainText(), None if line is None else line.text()


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


def test_shipped_feedback_in_the_range_not_thanked_for_yet(tmp_path):
    root = folder(tmp_path)
    done = {'1.2.8': ['AAAAA'], '1.2.9': ['BBBBB', '#ccccc'], '1.2.10': ['DDDDD', 'EEEEE'], '1.3': ['FFFFF'], 'notes': 'x'}
    (root/'assets'/'feedback-done.json').write_text(json.dumps(done), encoding='utf-8')
    preferences = Preferences(tmp_path/'settings.json')
    preferences.save(feedback.SENT, sent('AAAAA', 'BBBBB', 'CCCCC', 'DDDDD', 'EEEEE', 'GGGGG')+['damaged', {'ref': 5}])
    preferences.save(whats_new.THANKED, ['ZZZZZ', 'EEEEE'])
    shipped = lambda previous, current: whats_new.shipped(preferences, previous, current, root=root)
    assert shipped('1.2.8', '1.2.10') == ['BBBBB', 'CCCCC', 'DDDDD']        # 1.2.9 skipped over still counts; EEEEE thanked; GGGGG not addressed
    assert shipped('1.2.9', '1.2.10') == ['DDDDD'] and shipped('1.0', '1.2.8') == ['AAAAA']
    assert shipped('1.2.10', '1.2.10') == [] and shipped('1.2.10', '1.2.9') == []     # the same version, a downgrade
    assert shipped(None, '1.2.9') == ['BBBBB', 'CCCCC']                                 # unknown earlier version: this one only
    assert shipped('1.2.10', '1.3') == []                                               # FFFFF was not sent from this computer
    whats_new.thank(preferences, ['BBBBB', 'DDDDD'])
    assert Preferences(tmp_path/'settings.json').values[whats_new.THANKED] == ['EEEEE', 'BBBBB', 'DDDDD']    # ZZZZZ is no longer recorded
    assert shipped('1.2.8', '1.2.10') == ['CCCCC']
    assert whats_new.shipped(preferences, '1.0', '2.0', root=tmp_path/'missing') == []
    (root/'assets'/'feedback-done.json').write_text('[', encoding='utf-8'); assert shipped('1.0', '2.0') == []


def test_the_shipped_done_file_is_valid_and_both_builds_carry_it():
    data = json.loads((ROOT/'assets'/'feedback-done.json').read_text(encoding='utf-8'))
    assert isinstance(data, dict) and whats_new.done() == data
    for version, refs in data.items():
        assert whats_new.version_key(version) and all(len(ref) == 5 and set(ref) <= set(feedback.ALPHABET) for ref in refs)
    windows = (ROOT/'Build Grainy.ps1').read_text(encoding='utf-8-sig'); mac = (ROOT/'tools'/'build_macos.py').read_text(encoding='utf-8')
    assert '\\assets;assets"' in windows and '{ROOT/"assets"}:assets' in mac                 # the whole folder
    removed = next(line for line in windows.splitlines() if '_internal\\assets' in line and 'Remove-Item' in line)
    assert not any(fnmatch('feedback-done.json', pattern) for pattern in re.findall(r"'([^']+)'", removed))


def test_the_shipped_change_log_describes_this_version_and_both_builds_carry_it():
    assert whats_new.sections((ROOT/'CHANGELOG.md').read_text(encoding='utf-8'))[0][0] == __version__
    assert whats_new.notes(None, lang='ko')[0][0] == __version__
    for script in ('Build Grainy.ps1', 'tools/build_macos.py'):
        text = (ROOT/script).read_text(encoding='utf-8-sig')
        assert 'CHANGELOG.md' in text and 'assets' in text


def test_shown_once_after_an_update_and_again_from_the_settings_menu(app, tmp_path, monkeypatch):
    preferences = Preferences(tmp_path/'settings.json'); preferences.save_language('ko')
    shipped = [version for version, _, _ in whats_new.sections((whats_new.ROOT/'CHANGELOG.md').read_text(encoding='utf-8'))]
    seen, between = shipped[3], shipped[1]                                   # relative to the version being built
    preferences.save('update_check', False); preferences.save(whats_new.KEY, seen)
    preferences.save(feedback.SENT, sent('K7Q2M', 'A3BC4', 'ZZZZZ', 'NNNNN'))
    monkeypatch.setattr(whats_new, 'done', lambda root=None: {__version__: ['K7Q2M'], between: ['A3BC4'], '0.5.80': ['ZZZZZ']})
    session = DesktopSession(app, tmp_path/'library', preferences)
    shown = []
    monkeypatch.setattr(QDialog, 'exec', lambda dialog: shown.append(captured(dialog)))
    monkeypatch.setattr(QMessageBox, 'exec', lambda box: None)
    previous = language()
    try:
        assert session.start()
        session.whats_new()                                                  # running from source: never on its own
        assert shown == []
        session.whats_new(packaged=True)
        (name, title, text, thanks), = shown
        assert name == 'whatsNew' and title == f'Grainy {__version__}의 달라진 점'
        assert __version__ in text and between in text and f'{seen} —' not in text
        assert thanks == '보내 주신 피드백(#K7Q2M, #A3BC4)이 이번 업데이트에 반영되었습니다. 고맙습니다.'     # one sentence, above the notes
        stored = Preferences(tmp_path/'settings.json').values
        assert stored[whats_new.KEY] == __version__ and stored[whats_new.THANKED] == ['K7Q2M', 'A3BC4']
        session.whats_new(packaged=True)
        assert len(shown) == 1                                               # once
        menu = session.window.findChild(QMenu, 'settingsMenu')
        next(a for a in menu.actions() if a.text() == '이번 버전의 달라진 점…').trigger()
        assert len(shown) == 2 and __version__ in shown[1][2] and between not in shown[1][2] and shown[1][3] is None
        preferences.save(whats_new.KEY, seen); session.whats_new(packaged=True)      # back from an older version: no second thanks
        assert len(shown) == 3 and between in shown[2][2] and shown[2][3] is None
    finally:
        set_language(previous)
        session.switching = True
        if session.window:session.window.close()
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_a_first_run_only_remembers_the_version(app, tmp_path, monkeypatch):
    preferences = Preferences(tmp_path/'settings.json'); preferences.save(feedback.SENT, sent('K7Q2M'))
    monkeypatch.setattr(whats_new, 'done', lambda root=None: {__version__: ['K7Q2M']})
    session = DesktopSession(app, tmp_path/'library', preferences)
    monkeypatch.setattr(whats_new, 'show', lambda *a, **k: (_ for _ in ()).throw(AssertionError('shown on a first run')))
    try:
        session.whats_new(first_run=True, packaged=True)
        assert preferences.values[whats_new.KEY] == __version__ and whats_new.THANKED not in preferences.values
        session.whats_new(packaged=True)                                     # the same version
    finally:
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_shipped_feedback_alone_opens_the_dialog_in_english_too(app, tmp_path, monkeypatch):
    preferences = Preferences(tmp_path/'settings.json'); preferences.save(whats_new.KEY, '0.5.90')
    preferences.save(feedback.SENT, sent('K7Q2M', 'A3BC4'))
    monkeypatch.setattr(whats_new, 'done', lambda root=None: {__version__: ['K7Q2M', 'A3BC4']})
    monkeypatch.setattr(whats_new, 'notes', lambda *a, **k: [])               # no change notes for the range
    shown = []; monkeypatch.setattr(QDialog, 'exec', lambda dialog: shown.append(captured(dialog)))
    session = DesktopSession(app, tmp_path/'library', preferences)
    previous = language()
    try:
        set_language('en'); session.whats_new(packaged=True)
        (name, title, text, thanks), = shown
        assert title == f"What's new in Grainy {__version__}" and text == 'The change notes for this version were not found.'
        assert thanks == 'Your feedback (#K7Q2M, #A3BC4) made it into this update. Thank you.'
        assert Preferences(tmp_path/'settings.json').values[whats_new.THANKED] == ['K7Q2M', 'A3BC4']
    finally:
        set_language(previous)
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_english_strings_and_notes_exist():
    import json
    english = json.loads((ROOT/'assets'/'i18n'/'en.json').read_text(encoding='utf-8'))
    for source in ('Grainy {0}의 달라진 점', 'Grainy가 {0} 버전으로 업데이트되었습니다.', '이 버전의 변경 기록을 찾지 못했습니다.', '이번 버전의 달라진 점…',
                   '보내 주신 피드백({0})이 이번 업데이트에 반영되었습니다. 고맙습니다.'):
        assert source in english
    assert whats_new.sections((ROOT/'assets'/'whats-new-en.md').read_text(encoding='utf-8'))
