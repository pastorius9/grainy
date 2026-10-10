import json
from copy import deepcopy
from pathlib import Path
import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QCheckBox, QComboBox
from PySide6.QtCore import Qt
from test_studio_ui import app, wait
from luma.i18n import language, set_language, tr
from luma.preferences import Preferences
from luma.language_dialog import LanguageDialog
from luma.desktop_session import DesktopSession
from luma.photo_actions import build_menu


@pytest.fixture(autouse=True)
def restore_language():
    previous=language()
    yield
    set_language(previous)


def test_preferences_first_run_persistence_and_corrupt_setting(tmp_path,monkeypatch):
    path=tmp_path/'settings.json';settings=Preferences(path)
    assert settings.language is None
    path.write_text('{"other": 7, "language": "fr"}',encoding='utf-8')
    settings=Preferences(path);assert settings.language is None
    settings.save_language('en')
    assert Preferences(path).language=='en' and json.loads(path.read_text())['other']==7
    import os
    def fail(*args):raise OSError('disk full')
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError):settings.save_language('ko')
    assert Preferences(path).language=='en' and not list(tmp_path.glob('*.tmp'))
    path.write_text('{invalid',encoding='utf-8');assert Preferences(path).language is None


def test_first_launch_asks_once_and_cancel_does_not_save(app,tmp_path,monkeypatch):
    calls=[]
    def cancel(dialog):calls.append('cancel');return QDialog.DialogCode.Rejected
    monkeypatch.setattr(LanguageDialog,'exec',cancel)
    preferences=Preferences(tmp_path/'settings.json')
    session=DesktopSession(app,tmp_path/'library',preferences)
    try:
        assert not session.start() and not preferences.path.exists()
        def accept(dialog):
            calls.append('accept');dialog.languages.setCurrentIndex(dialog.languages.findData('en'))
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(LanguageDialog,'exec',accept)
        assert session.start() and language()=='en'
        session.switching=True;session.window.close()
        assert session.start() and calls==['cancel','accept']
        assert session.window.library_button.text()=='Library'
    finally:
        session.switching=True
        if session.window:session.window.close()
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_switch_reopens_preserves_edits_keywords_user_names_and_blocks_busy(app,tmp_path,monkeypatch):
    preferences=Preferences(tmp_path/'settings.json');preferences.save_language('ko')
    session=DesktopSession(app,tmp_path/'library',preferences)
    try:
        assert session.start();w=session.window
        path=tmp_path/'현상.png';Image.new('RGB',(180,120),(123,130,145)).save(path)
        ident=w.catalog.add(path)
        w.catalog.save_preset('현상',{'exposure':.3})
        collection=w.catalog.add_collection('현상')
        w.refresh_lists();w.activate(ident)
        wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
        w.set_setting('exposure',1.25);w.keywords.setText('현상, 가족');w.tabs.setCurrentIndex(1)
        w.export_running=True
        assert not session.change_language('en') and session.window is w
        assert Preferences(preferences.path).language=='ko'
        w.export_running=False
        def accept_english(dialog):
            dialog.languages.setCurrentIndex(dialog.languages.findData('en'))
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(LanguageDialog,'exec',accept_english)
        w.language_action.trigger()
        assert language()=='en'
        w=session.window
        wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
        assert w.library_button.text()=='Library' and w.edit_button.text()=='Develop'
        assert w.current_id==ident and w.catalog.photo(ident)['settings']['exposure']==1.25
        assert w.catalog.photo(ident)['keywords']=='현상, 가족'
        assert w.catalog.photo(ident)['name']=='현상.png'
        assert any(c['id']==collection and c['name']=='현상' for c in w.catalog.collections())
        assert '현상' in w.all_presets
        assert w.adjustments['exposure'].label.text()=='Exposure'
        assert w.windowTitle()=='Grainy — Photo Studio'
        assert w.tabs.currentIndex()==1
        before=deepcopy(w.catalog.photo(ident)['settings'])
        # English label text must not be used as the persisted color identifier.
        menu=build_menu(w)
        def actions(menu):
            for action in menu.actions():
                yield action
                if action.menu():yield from actions(action.menu())
        import re
        assert not [a.text() for a in actions(w.menuBar()) if a is not w.language_action and re.search('[가-힣]',a.text())]
        assert not [a.text() for a in actions(menu) if re.search('[가-힣]',a.text())]
        red=next(a for a in actions(menu) if a.data()=='label-빨강')
        assert red.text()=='Red';red.trigger()
        assert w.catalog.photo(ident)['label']=='빨강'
        assert w.catalog.photo(ident)['settings']==before
        assert session.change_language('ko')
        assert session.window.library_button.text()=='라이브러리'
        assert Preferences(preferences.path).language=='ko'
    finally:
        session.switching=True
        if session.window:session.window.close()
        app.lastWindowClosed.disconnect(session.last_window_closed)


def test_translation_formats_values_only_after_lookup():
    set_language('en')
    assert tr('{0}장의 사진','1,234')=='1,234 photos'
    assert tr('“{0}” 프리셋을 추가합니다.\n','현상 {0}')=='Add the “현상 {0}” preset.\n'
    assert '{stem}' in tr('이름 규칙: {stem} 원본 이름 / {n:03d} 순번\nICC 프로파일 포함 · 같은 이름이 있으면 번호를 붙여 저장합니다.')
    set_language('ko');assert tr('현상')=='현상'


def test_all_marked_ui_strings_have_english_translations():
    import ast
    root=Path(__file__).resolve().parents[1]
    translations=json.loads((root/'assets/i18n/en.json').read_text(encoding='utf-8'))
    missing=[]
    for path in (root/'luma').glob('*.py'):
        for call in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=='tr' and call.args and isinstance(call.args[0],ast.Constant):
                source=call.args[0].value
                if source not in translations:missing.append((path.name,call.lineno,source))
    assert not missing
