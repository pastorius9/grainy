"""Read-only status checks and visible, official CLI sign-in.

Only fixed state/kind values cross the GUI boundary. Credential files are never
opened, and login inherits the normal environment without per-run overrides.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from .ai_process import ProcessTree


@dataclass(frozen=True)
class AuthStatus:
    state: str = 'unknown'
    kind: str = ''


def parse_status(key, code, stdout, stderr=''):
    """Consume CLI output, returning only allowlisted labels, never account data."""
    if key == 'claude':
        try:
            data = json.loads(stdout)
        except (ValueError, TypeError):
            return AuthStatus()
        if not isinstance(data, dict):
            return AuthStatus()
        if data.get('loggedIn') is False and code in (0, 1):
            return AuthStatus('signed_out')
        if data.get('loggedIn') is True and code == 0:
            kinds = {'claude.ai': 'Claude', 'oauth_token': 'Claude',
                     'api_key': 'API key', 'api_key_helper': 'API key', 'third_party': 'Cloud'}
            return AuthStatus('signed_in', kinds.get(str(data.get('authMethod')), ''))
    elif key == 'codex':
        output = stdout + '\n' + stderr  # Codex reports login status on stderr.
        if re.search(r'(?im)^not logged in\s*$', output) and code in (0, 1):
            return AuthStatus('signed_out')
        if code == 0:
            if re.search(r'(?im)^logged in using chatgpt\s*$', output):
                return AuthStatus('signed_in', 'ChatGPT')
            if re.search(r'(?im)^logged in using an? api key\b', output):
                return AuthStatus('signed_in', 'API key')
    return AuthStatus()


def gemini_status(*, home=None, environ=None):
    """Presence is a hint, not a claim that the cached credential is still valid.

    Gemini can also use a keychain, .env or Vertex credentials. Their absence
    cannot be established without inspecting secrets, so absence means unknown.
    """
    env = os.environ if environ is None else environ
    if env.get('GEMINI_API_KEY') or env.get('GOOGLE_API_KEY'):
        return AuthStatus('signed_in', 'API key')
    root = Path(env.get('GEMINI_CLI_HOME') or home or Path.home())
    try:
        if (root / '.gemini' / 'oauth_creds.json').is_file():
            return AuthStatus('signed_in', 'Google')
    except OSError:
        pass
    return AuthStatus()


def check_status(profile, executable, *, timeout=5):
    if not executable:
        return AuthStatus()
    if profile.status_args is None:
        return gemini_status() if profile.key == 'gemini' else AuthStatus()
    tree = None
    try:
        tree = ProcessTree([*executable, *profile.status_args], Path.home())
        try:
            out, err = tree.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            tree.stop()
            tree.process.communicate(timeout=2)
            return AuthStatus('timeout')
        return parse_status(profile.key, tree.process.returncode,
                            out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace'))
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return AuthStatus()
    finally:
        if tree:
            tree.close()


def authentication_failed(value):
    """Classify error envelopes/exit diagnostics only; never display their text."""
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return bool(re.search(
        r'(?i)\b401\b|\bunauthenticated\b|authentication[_ ]?(?:failed|required|error)|'
        r'failed to authenticate|not (?:logged|signed) in|(?:log|sign)[ -]?in (?:is )?required|'
        r'oauth access token is invalid|invalid[_ ]grant|invalid[_ ](?:api[_ ]key|credentials)|'
        r'api[_ ]key[_ ](?:invalid|not[_ ]valid)|api key (?:is )?not valid|'
        r'(?:access|refresh|oauth) token.{0,60}(?:expired|invalid|revoked|refresh)|'
        r'(?:missing|no|could not load).{0,40}(?:credentials|api[_ ]key)|'
        r'please (?:log|sign) in|please set an auth method|failed to (?:refresh|sign in)|'
        r'authentication (?:is )?required|no authentication is configured|re-authenticate', text))


@dataclass
class SignInSession:
    process: object
    directory: object = None
    done: Path | None = None

    def poll(self):
        code = self.process.poll()
        # `open` exits as soon as Terminal starts, not when the CLI finishes.
        return (self.done.is_file() or code not in (None, 0)) if self.done else code is not None

    def cleanup(self):
        if self.directory:
            self.directory.cleanup()


def launch_sign_in(profile, executable, folder, *, platform=None):
    if not executable:
        raise ValueError('Program is not installed')
    platform = sys.platform if platform is None else platform
    argv = [*executable, *profile.login_args]
    if platform == 'win32':
        # No pipes or inherited redirected handles: the person owns this console.
        process = subprocess.Popen(argv, cwd=Path.home(), close_fds=True,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE)
        return SignInSession(process)
    if platform != 'darwin':
        raise OSError('No supported sign-in terminal')
    root = Path(folder) / 'ai-login'
    root.mkdir(parents=True, exist_ok=True)
    directory = tempfile.TemporaryDirectory(prefix='signin-', dir=root)
    script = Path(directory.name) / 'signin.command'
    done = Path(directory.name) / 'closed'
    try:
        script.write_text('#!/bin/sh\n'
                          + 'trap ' + shlex.quote('touch ' + shlex.quote(str(done))) + ' EXIT\n'
                          + "trap 'exit 0' HUP INT TERM\n"
                          + 'cd ' + shlex.quote(str(Path.home())) + ' || exit 1\n'
                          + shlex.join(argv) + '\n', encoding='utf-8', newline='\n')
        script.chmod(0o700)
        process = subprocess.Popen(['open', '-a', 'Terminal', str(script)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        return SignInSession(process, directory, done)
    except BaseException:
        directory.cleanup()
        raise


class AuthController(QObject):
    status_ready = Signal(str, object)
    checked = Signal(str, object)
    login_closed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = set()
        self.pending = {}
        self.logins = {}
        self.checked.connect(self._checked)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.poll_logins)

    def check(self, profile, executable):
        if profile.key in self.active:
            self.pending[profile.key] = (profile, executable)
            return
        self.active.add(profile.key)

        def work():
            try:
                status = check_status(profile, executable)
            except Exception:
                status = AuthStatus()  # Never propagate an exception containing CLI output.
            try:
                self.checked.emit(profile.key, status)
            except RuntimeError:
                pass  # The owning window was destroyed; the bounded worker is done.

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, key, status):
        self.active.discard(key)
        if key in self.pending:
            self.check(*self.pending.pop(key))
        else:
            self.status_ready.emit(key, status)

    def login(self, profile, executable, folder):
        if profile.key not in self.logins:
            self.logins[profile.key] = launch_sign_in(profile, executable, folder)
            self.timer.start()

    def poll_logins(self):
        for key, session in tuple(self.logins.items()):
            if session.poll():
                session.cleanup()
                del self.logins[key]
                self.login_closed.emit(key)
        if not self.logins:
            self.timer.stop()
