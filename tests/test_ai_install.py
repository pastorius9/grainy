from dataclasses import replace
import base64
from pathlib import Path
import os
import shlex
from unittest.mock import Mock

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
import pytest

from luma import ai_auth, ai_dialog, ai_install as install, ai_paths, ai_programs, auth
from luma.ai_programs import PROFILES
from luma.i18n import set_language, tr
from mcp_support import make_session, close_session
from test_ai_dialog import Peer
from test_studio_ui import app, wait


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    # A forgotten mock must fail, never install or sign in on the test computer.
    monkeypatch.setattr(install.subprocess, 'Popen', Mock(side_effect=AssertionError('Real console forbidden')))
    monkeypatch.setattr(ai_dialog.QDesktopServices, 'openUrl', Mock(side_effect=AssertionError('Real browser forbidden')))
    monkeypatch.setattr(ai_auth, 'check_status', lambda *args: ai_auth.AuthStatus('signed_out'))
    monkeypatch.setattr(ai_paths.shutil, 'which', lambda name: None)
    monkeypatch.setattr(ai_paths, 'tool_folders', lambda platform=None: [tmp_path / 'bin'])


@pytest.mark.parametrize('key,platform,command', [
    ('claude', 'win32', 'irm https://claude.ai/install.ps1 | iex'),
    ('claude', 'darwin', 'curl -fsSL https://claude.ai/install.sh | bash'),
    ('codex', 'win32', 'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"'),
    ('codex', 'darwin', 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'),
    ('gemini', 'win32', 'npm install -g @google/gemini-cli'),
    ('gemini', 'darwin', 'npm install -g @google/gemini-cli'),
])
def test_official_profile_table(key, platform, command):
    profile = next(p for p in PROFILES if p.key == key)
    spec = profile.installer(platform)
    assert spec.command == command and spec.source.startswith('https://')
    assert spec.needs_node == (key == 'gemini')
    assert profile.installer('unsupported') is None


@pytest.mark.parametrize('spec', [*(p.installer('win32') for p in PROFILES), install.NODE_WINDOWS], ids=lambda s: s.name)
def test_windows_console_runs_exact_command_and_pauses_even_after_failure(spec, tmp_path, monkeypatch):
    popen = Mock(); popen.return_value.poll.return_value = None
    monkeypatch.setattr(install.subprocess, 'Popen', popen)
    monkeypatch.setattr(install.subprocess, 'CREATE_NEW_CONSOLE', 0x10, raising=False)
    session = install.launch_install(spec, tmp_path, platform='win32')
    argv = popen.call_args.args[0]; options = popen.call_args.kwargs
    assert argv[:3] == ['powershell.exe', '-NoProfile', '-Command']
    wrapper = argv[-1]
    if spec.shell == 'cmd':
        assert '& cmd.exe /d /c ' + install.ps_quote(spec.command) in wrapper
    else:
        encoded = wrapper.split('-EncodedCommand ', 1)[1].split()[0]
        assert base64.b64decode(encoded).decode('utf-16-le') == spec.command
    assert 'finally {' in wrapper and "ReadKey('NoEcho,IncludeKeyDown')" in wrapper
    assert options['creationflags'] == 0x10 and options['close_fds']
    assert options['cwd'] == tmp_path / 'ai-install'
    assert not any(k in options for k in ('stdin', 'stdout', 'stderr', 'shell', 'startupinfo'))
    assert not session.poll()
    popen.return_value.poll.return_value = 1
    assert session.poll()


@pytest.mark.parametrize('profile', PROFILES, ids=lambda p: p.key)
def test_macos_script_is_private_quoted_pauses_and_tracks_terminal(profile, tmp_path, monkeypatch):
    popen = Mock(); popen.return_value.poll.return_value = 0
    monkeypatch.setattr(install.subprocess, 'Popen', popen)
    folder = tmp_path / "Grainy's 설정 & space"
    spec = profile.installer('darwin')
    session = install.launch_install(spec, folder, platform='darwin')
    argv = popen.call_args.args[0]; script = Path(argv[-1]); content = script.read_text(encoding='utf-8')
    assert argv[:-1] == ['open', '-a', 'Terminal']
    assert script.is_relative_to(folder / 'ai-install') and script.suffix == '.command'
    assert '\n' + spec.command + '\n' in content
    assert 'set -o pipefail' in content and 'read -r -n 1' in content
    assert 'HUP INT TERM' in content and ' EXIT' in content
    cd = next(line for line in content.splitlines() if line.startswith('cd '))
    assert shlex.split(cd) == ['cd', str(folder / 'ai-install'), '||', 'exit', '1']
    assert not session.poll()  # `open` has exited; the Terminal session has not.
    session.done.touch(); assert session.poll()
    session.cleanup(); assert not script.exists()


def test_macos_launcher_failure_removes_own_script_only(tmp_path, monkeypatch):
    sentinel = tmp_path / 'settings.json'; sentinel.write_text('unchanged')
    monkeypatch.setattr(install.subprocess, 'Popen', Mock(side_effect=OSError('Terminal missing')))
    with pytest.raises(OSError):
        install.launch_install(PROFILES[0].installer('darwin'), tmp_path, platform='darwin')
    assert not list((tmp_path / 'ai-install').iterdir())
    assert sentinel.read_text() == 'unchanged'


def test_unsupported_terminal_does_not_create_files(tmp_path):
    with pytest.raises(OSError):
        install.launch_install(install.NODE_WINDOWS, tmp_path, platform='linux')
    assert not list(tmp_path.iterdir())


def test_controller_serializes_installers_and_detects_close(app, tmp_path, monkeypatch):
    session = Mock(); session.poll.return_value = False
    launch = Mock(return_value=session); monkeypatch.setattr(install, 'launch_install', launch)
    controller = install.InstallController(); closed = []; controller.closed.connect(closed.append)
    assert controller.start('gemini', install.NODE_WINDOWS, tmp_path)
    assert not controller.start('claude', PROFILES[0].installer('win32'), tmp_path)
    controller.poll(); assert not closed and launch.call_count == 1
    session.poll.return_value = True
    wait(lambda: bool(closed))
    assert closed == ['gemini'] and not controller.sessions and not controller.timer.isActive()
    session.cleanup.assert_called_once()


@pytest.mark.parametrize('node,npm,ready', [(False, False, False), (True, False, False), (False, True, False), (True, True, True)])
def test_both_node_and_npm_required(node, npm, ready, monkeypatch):
    monkeypatch.setattr(install, 'find_tool', lambda name: {'node': node, 'npm': npm}[name])
    assert install.node_ready() == ready


@pytest.mark.parametrize('platform,name', [('win32', 'node.exe'), ('win32', 'npm.cmd'), ('darwin', 'node'), ('darwin', 'npm')])
def test_find_new_tools_without_inherited_path(platform, name, tmp_path):
    binary = tmp_path / 'bin' / name; binary.parent.mkdir(); binary.touch(); binary.chmod(0o700)
    assert ai_paths.find_tool(name.split('.')[0], platform=platform) == str(binary)


@pytest.mark.skipif(os.name != 'nt', reason='Windows npm command discovery')
def test_detect_new_npm_gemini_and_node_without_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_paths.sys, 'platform', 'win32')
    root = tmp_path / 'bin'; root.mkdir()
    script = root / 'node_modules/@google/gemini-cli/dist/index.js'
    script.parent.mkdir(parents=True); script.touch()
    (root / 'gemini.cmd').touch(); (root / 'node.exe').touch()
    assert ai_programs.find_program('gemini') == (str(root / 'node.exe'), str(script))


@pytest.mark.parametrize('platform,name', [('win32', 'codex.exe'), ('darwin', 'codex')])
def test_detect_standalone_codex_without_restart(platform, name, tmp_path, monkeypatch):
    if platform == 'win32' and os.name != 'nt':
        pytest.skip('Windows native executable discovery')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setattr(auth, 'tool_folders', ai_paths.tool_folders)
    native = tmp_path / 'bin' / name; native.parent.mkdir(); native.write_bytes(b'native'); native.chmod(0o700)
    found = auth.find_codex() if platform == 'win32' else auth._find_codex_posix()
    assert found == str(native.resolve())


@pytest.fixture
def missing(app, tmp_path):
    session = make_session(app, tmp_path, start=False)
    found = {}
    profiles = tuple(replace(p, detect=lambda key=p.key: found.get(key)) for p in PROFILES)
    dialog = ai_dialog.AiDialog(session.mcp, session.window, profiles=profiles, runner=Peer())
    dialog.show(); dialog.acknowledge()
    yield dialog, found
    dialog.shutdown(); close_session(app, session); set_language('ko')


def answer_box(action, seen):
    """Click the real confirmation UI in the nested Qt event loop, never mock consent."""
    def click():
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox)
        seen.append(box)
        button = next(b for b in box.buttons() if b.text() == action)
        button.click()
    QTimer.singleShot(0, click)


@pytest.mark.parametrize('index', range(3))
def test_install_confirmation_cancel_is_noop_and_accept_launches_once(missing, monkeypatch, index):
    dialog, _ = missing
    monkeypatch.setattr(ai_dialog, 'node_ready', lambda: True)
    monkeypatch.setattr(ai_dialog.sys, 'platform', 'win32')
    session = Mock(); session.poll.return_value = False
    launch = Mock(return_value=session); monkeypatch.setattr(install, 'launch_install', launch)
    profile = dialog.profiles[index]; spec = profile.installer()
    seen = []; answer_box(tr('취소'), seen); dialog.install_buttons[profile.key].click()
    assert not launch.called
    box = seen[-1]
    assert box.textFormat() == Qt.TextFormat.PlainText
    assert spec.name in box.text() and spec.vendor in box.text() and spec.command in box.text() and spec.source in box.text()
    assert 'Terminal' in box.informativeText() and box.defaultButton().text() == tr('취소')
    answer_box(tr('설치'), seen); dialog.install_buttons[profile.key].click()
    launch.assert_called_once_with(spec, dialog.preferences.path.parent)
    assert all(not b.isEnabled() for b in dialog.install_buttons.values())
    dialog.install_program(index); assert launch.call_count == 1
    session.poll.return_value = True; dialog.installs.poll()
    assert not dialog.executables[index] and dialog.install_buttons[profile.key].isEnabled()
    assert not dialog.send_button.isEnabled()


@pytest.mark.parametrize('automatic', [True, False])
def test_redetection_enables_sign_in_and_send_after_install(missing, monkeypatch, automatic):
    dialog, found = missing
    assert not dialog.send_button.isEnabled()
    dialog.program.setCurrentIndex(1)
    found['codex'] = ('new-codex.exe',)
    if automatic:
        dialog.installs.closed.emit('codex')
    else:
        dialog.recheck_buttons['codex'].click()
    wait(lambda: not dialog.auth.active)
    assert dialog.executables[1] == found['codex']
    assert dialog.login_buttons['codex'].isVisible() and dialog.install_buttons['codex'].isHidden()
    assert tr('로그인되지 않음') in dialog.auth_labels['codex'].text()
    assert dialog.send_button.isEnabled()
    login = Mock(); monkeypatch.setattr(dialog.auth, 'login', login)
    dialog.login_buttons['codex'].click(); assert login.call_args.args[:2] == (dialog.profiles[1], found['codex'])
    found.clear(); dialog.recheck_buttons['codex'].click()
    assert dialog.install_buttons['codex'].isVisible() and not dialog.send_button.isEnabled()


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_missing_node_offers_official_browser_without_install(missing, monkeypatch, platform):
    dialog, _ = missing
    monkeypatch.setattr(ai_dialog.sys, 'platform', platform)
    monkeypatch.setattr(ai_dialog, 'node_ready', lambda: False)
    browser = Mock(return_value=True); monkeypatch.setattr(ai_dialog.QDesktopServices, 'openUrl', browser)
    seen = []; answer_box(tr('Node.js 공식 페이지 열기'), seen)
    dialog.install_buttons['gemini'].click()
    assert browser.call_args.args[0].toString() == install.NODE_URL
    assert not dialog.installs.sessions and 'Node.js' in seen[0].text()
    assert any('winget' in b.text() for b in seen[0].buttons()) == (platform == 'win32')


def test_node_dialog_cancel_does_nothing(missing, monkeypatch):
    dialog, _ = missing
    monkeypatch.setattr(ai_dialog, 'node_ready', lambda: False)
    seen = []; answer_box(tr('취소'), seen)
    dialog.install_buttons['gemini'].click()
    assert len(seen) == 1 and not dialog.installs.sessions


@pytest.mark.parametrize('accept', [False, True])
def test_node_winget_needs_separate_confirmation_and_never_chains(missing, monkeypatch, accept):
    dialog, _ = missing
    monkeypatch.setattr(ai_dialog.sys, 'platform', 'win32')
    monkeypatch.setattr(ai_dialog, 'node_ready', lambda: False)
    session = Mock(); session.poll.return_value = False
    launch = Mock(return_value=session); monkeypatch.setattr(install, 'launch_install', launch)
    seen = []
    def prerequisite():
        answer_box(tr('Node.js 설치… (winget)'), seen)
        result = ai_dialog.AiDialog.node_prerequisite(dialog)
        answer_box(tr('설치') if accept else tr('취소'), seen)
        return result
    monkeypatch.setattr(dialog, 'node_prerequisite', prerequisite)
    dialog.install_buttons['gemini'].click()
    assert install.NODE_WINDOWS.command in seen[-1].text()
    assert launch.call_count == int(accept)
    if accept:
        assert launch.call_args.args[0] == install.NODE_WINDOWS
        monkeypatch.setattr(ai_dialog, 'node_ready', lambda: True)
        session.poll.return_value = True; dialog.installs.poll()
        assert launch.call_count == 1 and dialog.install_buttons['gemini'].isVisible()
        answer_box(tr('설치'), seen); dialog.install_buttons['gemini'].click()
        assert launch.call_count == 2 and launch.call_args.args[0] == dialog.profiles[2].installer()
        dialog.installs.poll()


def test_launch_error_and_english_translation(missing, monkeypatch):
    dialog, _ = missing
    set_language('en')
    monkeypatch.setattr(ai_dialog.sys, 'platform', 'win32')
    monkeypatch.setattr(install, 'launch_install', Mock(side_effect=OSError('private failure detail')))
    seen = []; answer_box('Install', seen); dialog.install_program(0)
    assert seen[0].windowTitle() == 'Confirm installation'
    assert 'may ask for confirmation' in seen[0].informativeText()
    assert dialog.login_help.text().startswith('Could not open the installer')
    assert 'private' not in dialog.login_help.text() and not dialog.installs.sessions
    assert dialog.install_buttons['claude'].isEnabled()


def test_in_flight_request_cannot_install(missing):
    dialog, _ = missing
    dialog.in_flight = True; dialog.refresh()
    assert all(not b.isEnabled() for b in dialog.install_buttons.values())
    dialog.install_program(0)  # Must not open a confirmation or a process.
