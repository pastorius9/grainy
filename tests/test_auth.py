import json
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QEventLoop,QTimer,QProcess,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from luma.auth import CodexAuth,usage_lines,valid_login_url
from luma.auth_dialog import AuthDialog
from luma.app import MainWindow,configure_application


@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([])
    configure_application(app)
    return app


def wait_for(predicate,timeout=5000):
    if predicate():
        return
    loop=QEventLoop()
    poll=QTimer();poll.setInterval(5)
    poll.timeout.connect(lambda:loop.quit() if predicate() else None)
    poll.start()
    deadline=QTimer();deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit);deadline.start(timeout)
    loop.exec();poll.stop();deadline.stop()
    assert predicate(),'Auth UI timed out'


@pytest.fixture
def client(app,tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','must-not-inherit')
    monkeypatch.setenv('CODEX_API_KEY','must-not-inherit')
    monkeypatch.setenv('CODEX_ACCESS_TOKEN','must-not-inherit')
    c=CodexAuth(tmp_path/'luma-auth',command=[sys.executable,str(Path(__file__).with_name('fake_codex_auth.py'))])
    yield c
    c.shutdown()
    assert c.process.state()==QProcess.ProcessState.NotRunning


def start(client):
    client.start()
    wait_for(lambda:client.phase in {'signed_out','connected','error'})


def test_account_login_limits_restart_logout_and_isolated_environment(client):
    start(client)
    assert client.phase=='signed_out'
    urls=[];client.browserRequested.connect(urls.append)
    client.login()
    wait_for(lambda:client.connected and bool(client.limits))
    assert len(urls)==1 and valid_login_url(urls[0])
    assert client.account=={'type':'chatgpt','email':'테스트@example.com','planType':'plus'}
    assert '75% 남음' in usage_lines(client.limits)[0]
    assert '40% 남음' in usage_lines(client.limits)[1]
    client.shutdown()
    start(client)
    wait_for(lambda:bool(client.limits))
    assert client.connected
    client.logout()
    wait_for(lambda:client.phase=='signed_out')
    assert client.account is None and client.limits=={}
    calls=[json.loads(line) for line in (client.home/'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert all(not call['api_key_inherited'] for call in calls)
    assert all(call['method'] in {'initialize','initialized','account/read','account/login/start','account/rateLimits/read','account/logout'} for call in calls)
    assert next(c for c in calls if c['method']=='account/login/start')['params']=={'type':'chatgpt'}


def test_cancel_pending_login_ignores_late_completion(client,monkeypatch):
    monkeypatch.setenv('LUMA_FAKE_AUTH','wait')
    start(client);client.login()
    wait_for(lambda:client.phase=='waiting')
    client.cancel_login()
    wait_for(lambda:client.phase=='signed_out')
    client._login_completed({'loginId':'test-login-1','success':True})
    assert not client.connected and not client.login_url and client.login_id is None


@pytest.mark.parametrize('behavior',['login_error','login_failure','bad_url'])
def test_login_failures_do_not_leak_secrets_or_open_untrusted_urls(client,monkeypatch,behavior):
    monkeypatch.setenv('LUMA_FAKE_AUTH',behavior)
    start(client)
    urls=[];client.browserRequested.connect(urls.append)
    client.login();wait_for(lambda:client.phase=='error')
    assert 'private-token' not in client.message and 'must-not' not in client.message
    assert not client.connected and not client.login_url
    if behavior=='bad_url':
        assert urls==[]
        assert client.process.state()==QProcess.ProcessState.NotRunning


def test_startup_error_recovers_on_refresh(client,monkeypatch):
    monkeypatch.setenv('LUMA_FAKE_AUTH','initialize_error')
    start(client)
    assert client.phase=='error'
    assert client.process.state()==QProcess.ProcessState.NotRunning
    monkeypatch.setenv('LUMA_FAKE_AUTH','success')
    client.refresh();wait_for(lambda:client.phase=='signed_out')


def test_timeout_stops_owned_server_and_can_retry(client,monkeypatch):
    monkeypatch.setenv('LUMA_FAKE_AUTH','read_timeout')
    client.start();wait_for(lambda:client.phase=='checking')
    for ident,(_,success,failure) in list(client._pending.items()):
        client._pending[ident]=(0,success,failure)
    client._expire_requests()
    assert client.phase=='error'
    assert client.process.state()==QProcess.ProcessState.NotRunning
    monkeypatch.setenv('LUMA_FAKE_AUTH','success')
    client.refresh();wait_for(lambda:client.phase=='signed_out')


def test_unexpected_exit_does_not_leave_connected_ui(client,monkeypatch):
    monkeypatch.setenv('LUMA_FAKE_AUTH','read_crash')
    start(client)
    assert client.phase=='error' and not client.connected


def test_missing_executable_has_actionable_state(app,tmp_path):
    client=CodexAuth(tmp_path/'auth',executable=str(tmp_path/'missing.exe'))
    client.start()
    assert client.phase=='missing' and not client.busy
    client.shutdown()


def test_usage_unknown_and_multiple_buckets_are_not_shown_as_zero():
    lines=usage_lines({'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':None}},'extra':{
        'primary':{'usedPercent':120},'secondary':{'usedPercent':-10}}}})
    assert '사용량 확인 불가' in lines[0]
    assert '0% 남음' in lines[1] and '100% 남음' in lines[2]
    assert '제공받지 못했습니다' in usage_lines({})[0]


@pytest.mark.parametrize('url',['http://auth.openai.com/x','https://auth.openai.com.evil.test',
    'file:///x','https://evil.test@auth.openai.com/x','https://auth.openai.com:123/x'])
def test_login_link_rejects_non_openai_destinations(url):
    assert not valid_login_url(url)


def test_real_buttons_update_account_and_logout(client,monkeypatch,tmp_path):
    monkeypatch.setattr('luma.auth_dialog.QDesktopServices.openUrl',lambda url:True)
    dialog=AuthDialog(client);dialog.show()
    start(client)
    assert dialog.login_button.isEnabled()
    QTest.mouseClick(dialog.login_button,Qt.MouseButton.LeftButton)
    wait_for(lambda:client.connected and bool(client.limits))
    assert dialog.logout_button.isVisible()
    assert '테스트@example.com' in dialog.account_label.text()
    assert '75% 남음' in dialog.usage.text()
    out=Path(__file__).resolve().parents[1]/'validation'
    dialog.grab().save(str(out/'08-auth-connected-test.png'))
    QTest.mouseClick(dialog.logout_button,Qt.MouseButton.LeftButton)
    wait_for(lambda:client.phase=='signed_out')
    assert dialog.login_button.isVisible() and not dialog.account_label.text()
    dialog.reject()


def test_closing_dialog_during_start_cancels_before_opening_browser(client,monkeypatch):
    monkeypatch.setenv('LUMA_FAKE_AUTH','wait')
    urls=[];client.browserRequested.connect(urls.append)
    dialog=AuthDialog(client);dialog.show()
    start(client)
    client.login();dialog.reject()
    wait_for(lambda:client.phase=='signed_out')
    assert not client.login_url and urls==[]


def test_main_window_header_and_shutdown(app,client,tmp_path):
    window=MainWindow(tmp_path/'catalog')
    window.auth.deleteLater()
    window.auth=client
    client.changed.connect(window.update_codex_button)
    window.show()
    assert not hasattr(window,'auth_button') and window.command_button.text()=='Codex'
    QTest.mouseClick(window.command_button,Qt.MouseButton.LeftButton)
    wait_for(lambda:client.phase=='signed_out')
    assert window.auth_dialog.isVisible()
    window.close()
    assert client.process.state()==QProcess.ProcessState.NotRunning


@pytest.mark.skipif(os.name=='nt',reason='the macOS search; Windows looks for codex.exe')
def test_macos_finds_the_native_program_behind_the_npm_command(tmp_path,monkeypatch):
    from luma import auth
    package=tmp_path/'lib'/'node_modules'/'@openai'/'codex'
    native=package/'vendor'/'aarch64-apple-darwin'/'codex'/'codex';native.parent.mkdir(parents=True)
    native.write_bytes(b'\xcf\xfa\xed\xfe native');native.chmod(0o755)
    script=package/'bin'/'codex.js';script.parent.mkdir();script.write_text('#!/usr/bin/env node\n');script.chmod(0o755)
    command=tmp_path/'bin'/'codex';command.parent.mkdir();command.symlink_to(script)
    monkeypatch.setattr(auth.shutil,'which',lambda name:str(command) if name=='codex' else None)
    assert auth.find_codex()==str(native.resolve())                    # not the Node script the command points to
    native.unlink()
    monkeypatch.setattr(auth.Path,'home',lambda:tmp_path/'nobody')
    found=auth.find_codex()
    assert found is None or 'node_modules' not in found and found!=str(script)   # only a program installed on this Mac itself
    assert auth.find_codex(str(script)) is None and auth.find_codex(str(tmp_path/'missing')) is None
    program=tmp_path/'codex';program.write_bytes(b'\xcf\xfa\xed\xfe');program.chmod(0o755)
    assert auth.find_codex(str(program))==str(program.resolve())
    monkeypatch.setattr(auth.shutil,'which',lambda name:str(program) if name=='codex' else None)
    assert auth.find_codex()==str(program.resolve())                   # a native program installed as `codex`
