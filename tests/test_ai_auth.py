from dataclasses import replace
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

from PySide6.QtCore import QTimer
import pytest

from luma import ai_auth as auth
from luma.ai_programs import PROFILES, EventParser
from test_studio_ui import app, wait
from test_ai_programs import alive


PEER = Path(__file__).with_name('fake_ai.py')
PRIVATE = 'private@example.test opaque-secret-key refresh-token-private'


@pytest.mark.parametrize('profile', PROFILES[:2], ids=lambda p: p.key)
@pytest.mark.parametrize('state,expected', [('in', 'signed_in'), ('out', 'signed_out'), ('unknown', 'unknown')])
def test_status_command_with_offline_peer(profile, state, expected, tmp_path, monkeypatch):
    monkeypatch.setattr(auth.Path, 'home', lambda: tmp_path)
    result = auth.check_status(profile, (sys.executable, str(PEER), profile.key, 'status-' + state))
    assert result.state == expected
    assert all(secret not in repr(result) for secret in PRIVATE.split())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('profile', PROFILES, ids=lambda p: p.key)
def test_hung_status_times_out_and_ends_children(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(auth.Path, 'home', lambda: tmp_path)
    # Gemini has no status command; exercise the same timeout containment if a
    # future profile gains one, without launching a real Gemini process.
    profile = replace(profile, status_args=())
    pidfile = tmp_path / 'child.pid'
    start = time.monotonic()
    result = auth.check_status(profile, (sys.executable, str(PEER), profile.key, 'status-hung', str(pidfile)), timeout=.7)
    assert result == auth.AuthStatus('timeout') and time.monotonic() - start < 4
    assert pidfile.is_file()
    child = int(pidfile.read_text()); end = time.monotonic() + 5
    while alive(child) and time.monotonic() < end:time.sleep(.05)     # macOS: a killed child is gone a moment later
    assert not alive(child)


@pytest.mark.parametrize('key,code,output', [
    ('claude', 0, '[]'), ('claude', 0, '{"loggedIn":"yes"}'),
    ('claude', 2, '{"loggedIn":true}'), ('codex', 2, 'Logged in using ChatGPT'),
    ('codex', 0, 'some future account format'), ('gemini', 0, PRIVATE),
])
def test_unrecognized_status_is_unknown(key, code, output):
    assert auth.parse_status(key, code, output) == auth.AuthStatus()


def test_status_never_returns_arbitrary_account_fields():
    result = auth.parse_status('claude', 0, json.dumps({
        'loggedIn': True, 'authMethod': PRIVATE, 'subscriptionType': PRIVATE, 'email': PRIVATE, 'token': PRIVATE}))
    assert result == auth.AuthStatus('signed_in')
    assert auth.parse_status('codex', 0, '', 'Logged in using an API key - ' + PRIVATE) == auth.AuthStatus('signed_in', 'API key')


def test_gemini_presence_only_and_environment_home(tmp_path, monkeypatch):
    alternate = tmp_path / 'other-home'
    credential = alternate / '.gemini/oauth_creds.json'
    credential.parent.mkdir(parents=True); credential.write_text(PRIVATE)
    def no_read(*args, **kwargs):
        raise AssertionError('Must never read credentials/settings/.env/keychain')
    monkeypatch.setattr(Path, 'open', no_read)
    assert auth.gemini_status(home=tmp_path, environ={}) == auth.AuthStatus()
    assert auth.gemini_status(home=tmp_path, environ={'GEMINI_CLI_HOME': str(alternate)}) == auth.AuthStatus('signed_in', 'Google')
    for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        result = auth.gemini_status(home=tmp_path, environ={key: PRIVATE})
        assert result == auth.AuthStatus('signed_in', 'API key')
        assert PRIVATE not in repr(result)
    assert auth.gemini_status(home=tmp_path, environ={'GOOGLE_APPLICATION_CREDENTIALS': PRIVATE}) == auth.AuthStatus()


def test_gemini_does_not_start_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, 'ProcessTree', Mock(side_effect=AssertionError('Gemini must not start')))
    monkeypatch.setattr(auth, 'gemini_status', lambda: auth.AuthStatus())
    assert auth.check_status(PROFILES[2], ('gemini',)) == auth.AuthStatus()


def test_missing_or_unstartable_status_is_unknown(monkeypatch):
    assert auth.check_status(PROFILES[0], None) == auth.AuthStatus()
    monkeypatch.setattr(auth, 'ProcessTree', Mock(side_effect=OSError(PRIVATE)))
    assert auth.check_status(PROFILES[0], ('missing',)) == auth.AuthStatus()


@pytest.mark.parametrize('profile,args', [(PROFILES[0], ['auth', 'login']), (PROFILES[1], ['login']), (PROFILES[2], [])])
def test_windows_sign_in_is_visible_and_uses_normal_environment(profile, args, tmp_path, monkeypatch):
    popen = Mock(); popen.return_value.poll.return_value = None
    monkeypatch.setattr(auth.subprocess, 'Popen', popen)
    monkeypatch.setattr(auth.subprocess, 'CREATE_NEW_CONSOLE', 0x10, raising=False)
    monkeypatch.setattr(auth.Path, 'home', lambda: tmp_path / 'normal-home')
    executable = ('node.exe', 'C:/a & b/gemini.js') if profile.key == 'gemini' else ('C:/a & b/program.exe',)
    session = auth.launch_sign_in(profile, executable, tmp_path, platform='win32')
    argv, options = popen.call_args.args[0], popen.call_args.kwargs
    assert argv == [*executable, *args]
    assert options == {'cwd': tmp_path / 'normal-home', 'close_fds': True, 'creationflags': 0x10}
    assert not session.poll()
    popen.return_value.poll.return_value = 0
    assert session.poll()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('profile', PROFILES, ids=lambda p: p.key)
def test_macos_terminal_quotes_command_and_waits_for_cli(profile, tmp_path, monkeypatch):
    popen = Mock(); popen.return_value.poll.return_value = 0
    monkeypatch.setattr(auth.subprocess, 'Popen', popen)
    folder = tmp_path / "Grainy's settings & space"
    executable = ('/Applications/a "quoted" & name/cli',)
    session = auth.launch_sign_in(profile, executable, folder, platform='darwin')
    script = Path(popen.call_args.args[0][-1])
    assert popen.call_args.args[0][:-1] == ['open', '-a', 'Terminal']
    assert script.is_relative_to(folder / 'ai-login') and script.suffix == '.command'
    content = script.read_text()
    assert shlex.split(content.splitlines()[-1]) == [*executable, *profile.login_args]
    assert 'HUP INT TERM' in content and ' EXIT' in content
    assert 'env' not in popen.call_args.kwargs and not session.poll()
    session.done.touch(); assert session.poll()
    session.cleanup(); assert not script.exists()


def test_macos_launch_failure_cleans_temporary_script(tmp_path, monkeypatch):
    monkeypatch.setattr(auth.subprocess, 'Popen', Mock(side_effect=OSError('missing Terminal')))
    with pytest.raises(OSError):
        auth.launch_sign_in(PROFILES[0], ('claude',), tmp_path, platform='darwin')
    assert list((tmp_path / 'ai-login').iterdir()) == []


def test_login_monitor_does_not_start_twice_and_reports_close(app, tmp_path, monkeypatch):
    session = Mock(); session.poll.return_value = False
    launch = Mock(return_value=session); monkeypatch.setattr(auth, 'launch_sign_in', launch)
    controller = auth.AuthController(); closed = []; controller.login_closed.connect(closed.append)
    controller.login(PROFILES[0], ('claude',), tmp_path)
    controller.login(PROFILES[0], ('claude',), tmp_path)
    assert launch.call_count == 1
    controller.poll_logins(); assert closed == []
    session.poll.return_value = True
    wait(lambda: bool(closed))
    assert closed == ['claude'] and not controller.logins and not controller.timer.isActive()
    session.cleanup.assert_called_once()


def test_status_worker_keeps_gui_responsive_and_coalesces_refresh(app, monkeypatch):
    gate = threading.Event(); calls = []; results = []; ticks = []
    main_thread = threading.get_ident()
    def slow_check(profile, executable):
        calls.append(threading.get_ident())
        assert gate.wait(3)
        return auth.AuthStatus('signed_in')
    monkeypatch.setattr(auth, 'check_status', slow_check)
    controller = auth.AuthController(); controller.status_ready.connect(lambda *args: results.append(args))
    timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(1)); timer.start()
    try:
        controller.check(PROFILES[0], ('standin',))
        for _ in range(5): controller.check(PROFILES[0], ('standin',))
        wait(lambda: len(ticks) >= 3)
        assert calls and all(t != main_thread for t in calls) and not results
        gate.set(); wait(lambda: bool(results))
        assert len(calls) == 2 and results == [('claude', auth.AuthStatus('signed_in'))]
        assert not controller.active
    finally:
        gate.set(); timer.stop()


@pytest.mark.parametrize('key,data', [
    ('claude', {'type': 'result', 'is_error': True, 'errors': ['HTTP 401 ' + PRIVATE]}),
    ('claude', {'type': 'assistant', 'error': 'authentication_failed', 'message': {'content': [{'type': 'text', 'text': PRIVATE}]}}),
    ('claude', {'type': 'stream_event', 'event': {'type': 'error', 'error': {'type': 'authentication_error'}}}),
    ('codex', {'type': 'error', 'message': 'Not logged in ' + PRIVATE}),
    ('codex', {'type': 'turn.failed', 'error': {'message': 'Unexpected status 401 Unauthorized ' + PRIVATE}}),
    ('gemini', {'type': 'error', 'message': 'Failed to authenticate ' + PRIVATE}),
    ('gemini', {'type': 'result', 'status': 'error', 'error': {'type': 'FatalAuthenticationError', 'message': PRIVATE}}),
])
def test_auth_event_formats_only_return_fixed_error(key, data):
    parser = EventParser(); events = getattr(parser, key)(data)
    assert parser.failed and [e.kind for e in events] == ['auth_error']
    assert all(not e.text for e in events)


@pytest.mark.parametrize('message', [
    'OAuth access token is invalid', 'Failed to authenticate', 'API_KEY_INVALID',
    'API key not valid. Please pass a valid API key.', 'invalid_grant',
    'Your access token could not be refreshed because your refresh token was already used',
    'Please set an Auth method', 'Could not load the default credentials',
])
def test_auth_diagnostic_patterns(message):
    assert auth.authentication_failed(message)


@pytest.mark.parametrize('message', ['quota exceeded', 'HTTP 429', 'HTTP 403 permission denied', 'connection refused', 'model not found'])
def test_other_failures_are_not_login_failures(message):
    assert not auth.authentication_failed(message)
