from dataclasses import replace
from pathlib import Path
import sys

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtTest import QTest
import pytest

from luma.ai_dialog import AiDialog, NOTICE
from luma.ai_programs import PROFILES, Event
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


def test_missing_programs_disabled_with_install_hints(app, tmp_path):
    session = make_session(app, tmp_path, start=False)
    profiles = tuple(replace(p, detect=lambda: None) for p in PROFILES)
    dialog = AiDialog(session.mcp, session.window, profiles=profiles)
    try:
        dialog.show(); dialog.acknowledge()
        assert dialog.program.currentIndex() == -1 and not dialog.send_button.isEnabled()
        assert all(not dialog.program.model().item(i).isEnabled() for i in range(3))
        assert '@openai/codex' in dialog.hints.text() and '@google/gemini-cli' in dialog.hints.text()
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
    finally:
        dialog.close(); close_session(app, session)
        from luma.i18n import set_language
        set_language('ko')
