from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import Mock

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtTest import QTest
import pytest

from luma.ai_dialog import AiDialog, NOTICE
from luma.ai_programs import PROFILES, Event
from luma import ai_auth
from luma.i18n import tr
from mcp_support import make_session, close_session, add_photos, call
from test_studio_ui import app, wait


class Peer(QObject):
    event = Signal(object)
    finished = Signal(str)

    def __init__(self):
        super().__init__(); self.running = False; self.session_id = ''; self.calls = []; self.stopped = False

    def start(self, profile, request, **kwargs):
        self.calls.append((profile, request)); self.running = True

    def stop(self):
        self.stopped = True; self.running = False; self.finished.emit('stopped')

    def shutdown(self):
        if self.running: self.stop()

    def complete(self):
        self.event.emit(Event('session', 'session-1')); self.event.emit(Event('text', 'Done'))
        self.running = False; self.finished.emit('done')


@pytest.fixture(autouse=True)
def offline_status(monkeypatch):
    monkeypatch.setattr(ai_auth, 'check_status', lambda *args: ai_auth.AuthStatus())


@pytest.fixture
def setup(app, tmp_path):
    session = make_session(app, tmp_path, start=False)
    peer = Peer()
    profiles = tuple(replace(p, detect=lambda: ('standin',)) for p in PROFILES)
    dialog = AiDialog(session.mcp, session.window, profiles=profiles, runner=peer)
    session.window.ai_dialog = dialog; dialog.show()
    yield session, dialog, peer
    dialog.shutdown(); close_session(app, session)


def test_first_use_notice_is_required_and_remembered(setup):
    session, dialog, peer = setup
    assert dialog.notice.isVisible() and not dialog.send_button.isEnabled()
    assert not dialog.isModal()
    dialog.acknowledge()
    assert session.preferences.values['ai_notice_seen'] and dialog.notice.isHidden()
    second = AiDialog(session.mcp, session.window, profiles=dialog.profiles, runner=Peer())
    second.show()
    try:
        assert second.notice.isHidden()
    finally:
        second.close()


def test_mcp_off_stays_off_until_enabled_then_creates_bridge(setup):
    session, dialog, peer = setup
    session.mcp.set_enabled(False); dialog.acknowledge(); dialog.refresh()
    assert dialog.enable.isVisible() and not dialog.send_button.isEnabled()
    dialog.input.setPlainText('request'); dialog.send(); assert not peer.calls
    dialog.enable_connection(); dialog.send()
    assert session.mcp.running and session.mcp.bridge_path().is_file()
    assert peer.calls[0][1].server['command'] in ('powershell', '/bin/sh')
    assert peer.calls[0][1].workspace.is_relative_to(session.preferences.path.parent)


def test_send_shortcut_stream_tools_followup_new_and_last_program(setup):
    session, dialog, peer = setup
    dialog.acknowledge(); dialog.input.setPlainText('first')
    dialog.input.setFocus(); QTest.keyClick(dialog.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    wait(lambda: bool(peer.calls))
    assert peer.running and not dialog.program.isEnabled() and dialog.stop_button.isEnabled()
    peer.event.emit(Event('tool', 'apply_edits', 24)); peer.complete()
    assert '24' in dialog.transcript.toPlainText() and dialog.session_id == 'session-1'
    dialog.input.setPlainText('next'); dialog.send()
    assert 'first' in peer.calls[-1][1].prompt and 'next' in peer.calls[-1][1].prompt
    peer.complete(); dialog.new_conversation()
    assert not dialog.history and not dialog.session_id and not dialog.transcript.toPlainText()
    dialog.program.setCurrentIndex(2)
    assert session.preferences.values['ai_program'] == 'gemini'
    dialog.input.setPlainText('first native'); dialog.send(); peer.complete()
    dialog.input.setPlainText('native followup'); dialog.send()
    assert peer.calls[-1][1].session_id == 'session-1'
    assert peer.calls[-1][1].prompt == 'native followup'


def test_changed_model_starts_new_conversation(setup):
    _, dialog, peer = setup
    dialog.acknowledge(); dialog.input.setPlainText('first'); dialog.send(); peer.complete()
    dialog.model.setText('another'); dialog.choice_changed()
    assert not dialog.session_id and not dialog.history


def test_missing_programs_selectable_but_cannot_send(app, tmp_path):
    session = make_session(app, tmp_path, start=False)
    profiles = tuple(replace(p, detect=lambda: None) for p in PROFILES)
    dialog = AiDialog(session.mcp, session.window, profiles=profiles)
    try:
        dialog.show(); dialog.acknowledge()
        assert dialog.program.currentIndex() == 0 and not dialog.send_button.isEnabled()
        assert all(dialog.program.model().item(i).isEnabled() for i in range(3))
        assert all(button.isVisible() for button in dialog.install_buttons.values())
        assert all(button.isVisible() for button in dialog.recheck_buttons.values())
        assert all(button.isHidden() for button in dialog.login_buttons.values())
    finally:
        dialog.close(); close_session(app, session)


def test_stop_and_editor_close_end_runner(setup):
    session, dialog, peer = setup
    dialog.acknowledge(); dialog.input.setPlainText('first'); dialog.send(); dialog.stop()
    assert peer.stopped and not peer.running
    peer.stopped = False; dialog.input.setPlainText('second'); dialog.send()
    session.window.close()
    assert peer.stopped and not peer.running


def test_undo_uses_existing_ai_batch_even_when_mcp_off(app, tmp_path):
    session = make_session(app, tmp_path)
    ids = add_photos(session.window, tmp_path / 'photos', 2)
    result = call(session, 'apply_edits', {'edits': [{'ids': [ids[0]], 'set': {'exposure': .3}}]})
    assert not result.error
    dialog = AiDialog(session.mcp, session.window)
    try:
        session.mcp.set_enabled(False); dialog.show(); dialog.refresh()
        assert dialog.undo_button.isEnabled()
        dialog.undo_last()
        assert session.window.catalog.photo(ids[0])['settings']['exposure'] == 0
    finally:
        dialog.close(); close_session(app, session)


def test_english_ui_and_privacy_details(app, tmp_path):
    session = make_session(app, tmp_path, start=False, language='en')
    dialog = AiDialog(session.mcp, session.window)
    try:
        assert dialog.windowTitle() == 'Ask AI'
        assert tr(NOTICE).startswith('Sending from this window')
        assert dialog.stop_button.text() == 'Stop'
        assert dialog.login_buttons['claude'].text() == 'Sign in…'
        assert dialog.recheck_buttons['claude'].text() == 'Check again'
        dialog.auth_status_ready('codex', ai_auth.AuthStatus('signed_in', 'ChatGPT'))
        assert dialog.auth_labels['codex'].text() == 'Codex: Signed in (ChatGPT)'
    finally:
        dialog.close(); close_session(app, session)
        from luma.i18n import set_language
        set_language('ko')


def test_status_refreshes_on_show_choice_recheck_and_login_close(setup, monkeypatch):
    _, dialog, _ = setup
    wait(lambda: not dialog.auth.active)
    checks = []
    monkeypatch.setattr(ai_auth, 'check_status', lambda p, exe: checks.append(p.key) or ai_auth.AuthStatus('signed_out'))
    dialog.hide(); dialog.show()
    wait(lambda: len(checks) == 3 and not dialog.auth.active)
    assert set(checks) == {p.key for p in PROFILES}
    dialog.program.setCurrentIndex(1)
    wait(lambda: checks.count('codex') == 2 and not dialog.auth.active)
    dialog.recheck_buttons['codex'].click()
    wait(lambda: checks.count('codex') == 3 and not dialog.auth.active)
    dialog.auth.login_closed.emit('codex')
    wait(lambda: checks.count('codex') == 4 and not dialog.auth.active)
    dialog.acknowledge()
    assert dialog.program.model().item(1).isEnabled() and dialog.send_button.isEnabled()
    assert tr('로그인되지 않음') in dialog.auth_labels['codex'].text()


@pytest.mark.parametrize('index', range(3))
def test_sign_in_button_launches_official_command_without_real_console(setup, monkeypatch, index):
    session, dialog, _ = setup
    popen = Mock(); popen.return_value.poll.return_value = None
    monkeypatch.setattr(ai_auth.subprocess, 'Popen', popen)
    monkeypatch.setattr(ai_auth.subprocess, 'CREATE_NEW_CONSOLE', 0x10, raising=False)
    monkeypatch.setattr(ai_auth.sys, 'platform', 'win32')
    profile = PROFILES[index]
    dialog.login_buttons[profile.key].click()
    assert popen.call_args.args[0] == ['standin', *profile.login_args]
    assert popen.call_args.kwargs['creationflags'] == 0x10
    assert 'env' not in popen.call_args.kwargs
    assert not dialog.login_buttons[profile.key].isEnabled()
    if profile.key == 'gemini':
        assert 'Login with Google' in dialog.login_help.text()
    popen.return_value.poll.return_value = 0
    wait(lambda: not dialog.auth.logins)
    assert dialog.login_buttons[profile.key].isEnabled()


@pytest.mark.parametrize('index', range(3))
def test_auth_failure_highlights_right_button_without_raw_diagnostics(setup, index):
    _, dialog, peer = setup
    dialog.program.setCurrentIndex(index); dialog.acknowledge()
    dialog.input.setPlainText('request'); dialog.send()
    peer.event.emit(Event('auth_error', 'private@example.test opaque-secret-key'))
    peer.running = False; peer.finished.emit('auth_required')
    text = dialog.transcript.toPlainText()
    assert tr("로그인이 필요합니다. '로그인…'을 눌러 {0}에 다시 로그인하세요.", PROFILES[index].name) in text
    assert 'private@example.test' not in text and 'opaque-secret-key' not in text
    assert '설치·로그인·사용 한도' not in text
    assert dialog.login_buttons[PROFILES[index].key].styleSheet()
    assert all(not b.styleSheet() for key, b in dialog.login_buttons.items() if key != PROFILES[index].key)
    dialog.input.setPlainText('retry'); dialog.send(); peer.complete()
    assert not dialog.login_buttons[PROFILES[index].key].styleSheet()


def test_unknown_timeout_and_launch_error_never_show_secrets(setup, monkeypatch):
    _, dialog, peer = setup
    dialog.auth_status_ready('claude', ai_auth.AuthStatus('timeout', 'private@example.test'))
    assert tr('응답 없음') in dialog.auth_labels['claude'].text()
    assert 'private' not in dialog.auth_labels['claude'].text()
    monkeypatch.setattr(ai_auth, 'launch_sign_in', Mock(side_effect=OSError('opaque-secret-key')))
    dialog.login_buttons['claude'].click()
    peer.event.emit(Event('error', 'private@example.test opaque-secret-key'))
    assert 'opaque-secret-key' not in dialog.login_help.text() + dialog.transcript.toPlainText()
