"""Explicitly confirmed official installers, in consoles owned by the user.

Never run at discovery/startup. Call launch_install only after UI confirmation.
Commands are fixed vendor instructions, not user input. See DEVELOPMENT.md.
"""
from dataclasses import dataclass
import base64
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from PySide6.QtCore import QObject, QTimer, Signal

from .ai_auth import SignInSession
from .ai_paths import find_tool, install_environment
from .i18n import tr


@dataclass(frozen=True)
class Installer:
    name: str
    vendor: str
    command: str
    source: str
    shell: str = 'powershell'
    needs_node: bool = False


CLAUDE_SOURCE = 'https://code.claude.com/docs/en/setup'
CODEX_SOURCE = 'https://github.com/openai/codex#installing-and-running-codex-cli'
GEMINI_SOURCE = 'https://github.com/google-gemini/gemini-cli#installation'
NODE_URL = 'https://nodejs.org/en/download'
INSTALLERS = {
    'claude': {
        'win32': Installer('Claude Code', 'Anthropic', 'irm https://claude.ai/install.ps1 | iex', CLAUDE_SOURCE),
        'darwin': Installer('Claude Code', 'Anthropic', 'curl -fsSL https://claude.ai/install.sh | bash', CLAUDE_SOURCE),
    },
    'codex': {
        'win32': Installer('Codex', 'OpenAI',
                           'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"', CODEX_SOURCE),
        'darwin': Installer('Codex', 'OpenAI', 'curl -fsSL https://chatgpt.com/codex/install.sh | sh', CODEX_SOURCE),
    },
    'gemini': {
        platform: Installer('Gemini CLI', 'Google (npm)', 'npm install -g @google/gemini-cli',
                            GEMINI_SOURCE, shell='cmd', needs_node=True)
        for platform in ('win32', 'darwin')
    },
}
NODE_WINDOWS = Installer('Node.js LTS', 'OpenJS Foundation (winget)',
                         'winget install OpenJS.NodeJS.LTS', NODE_URL, shell='cmd')


def node_ready():
    return bool(find_tool('node') and find_tool('npm'))


def ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


def launch_install(installer, folder, *, platform=None):
    platform = sys.platform if platform is None else platform
    if platform not in ('win32', 'darwin'):
        raise OSError('No supported installation terminal')
    root = Path(folder) / 'ai-install'
    root.mkdir(parents=True, exist_ok=True)
    env = install_environment(platform)
    pause = tr('결과를 확인한 뒤 아무 키나 누르면 이 설치 실행을 마칩니다.')
    if platform == 'win32':
        # Run the installer in a child shell so even an explicit `exit` cannot
        # skip the outer console's pause. cmd avoids npm.ps1 execution policy.
        if installer.shell == 'powershell':
            # Windows PowerShell's native argument forwarding can strip embedded
            # quotes (notably Codex's -c "..."). Encode only this fixed command.
            encoded = base64.b64encode(installer.command.encode('utf-16-le')).decode('ascii')
            child = '& powershell.exe -NoProfile -EncodedCommand ' + encoded
        else:
            child = '& cmd.exe /d /c ' + ps_quote(installer.command)
        script = ('try { ' + child + ' } finally { '
                  + 'Write-Host ' + ps_quote('\n' + pause) + '; '
                  + "$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') }")
        process = subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', script],
                                   cwd=root, env=env, close_fds=True,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE)
        return SignInSession(process)
    directory = tempfile.TemporaryDirectory(prefix='install-', dir=root)
    script = Path(directory.name) / 'install.command'
    done = Path(directory.name) / 'closed'
    try:
        script.write_text('#!/bin/bash\nset -o pipefail\n'
                          + 'trap ' + shlex.quote('touch ' + shlex.quote(str(done))) + ' EXIT\n'
                          + "trap 'exit 0' HUP INT TERM\n"
                          # Terminal may already be running with its own PATH.
                          + 'export PATH=' + shlex.quote(env['PATH']) + '\n'
                          + 'cd ' + shlex.quote(str(root)) + ' || exit 1\n'
                          + '(\n' + installer.command + '\n)\nresult=$?\n'
                          + "printf '\\n%s\\n' " + shlex.quote(pause) + '\n'
                          + 'read -r -n 1\nexit "$result"\n', encoding='utf-8', newline='\n')
        script.chmod(0o700)
        process = subprocess.Popen(['open', '-a', 'Terminal', str(script)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        return SignInSession(process, directory, done)
    except BaseException:
        directory.cleanup()
        raise


class InstallController(QObject):
    closed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sessions = {}
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.poll)

    def start(self, key, installer, folder):
        if self.sessions:
            return False  # In particular, never run two global npm installs.
        self.sessions[key] = launch_install(installer, folder)
        self.timer.start()
        return True

    def poll(self):
        for key, session in tuple(self.sessions.items()):
            if session.poll():
                session.cleanup()
                del self.sessions[key]
                self.closed.emit(key)
        if not self.sessions:
            self.timer.stop()
