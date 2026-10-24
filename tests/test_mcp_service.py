"""mcp_service: GUI-thread calls, the server's lifetime, settings and bridge files, and the settings dialog."""
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLineEdit,QMenu,QMessageBox

from test_studio_ui import app,wait
from mcp_support import make_session,close_session,add_photos,call,post
from luma import mcp_service,mcp_http,mcp_protocol,photo_service
from luma.mcp_dialog import install_commands,install_text,standard_folder
from luma.i18n import set_language,tr

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(mcp_http,'POLL_SECONDS',.05)
    yield
    set_language('ko')


@pytest.fixture
def session(app,tmp_path):
    value=make_session(app,tmp_path)
    yield value
    close_session(app,value)


def in_thread(fn):
    out={}
    def run():
        try:out['value']=fn()
        except BaseException as error:out['error']=error
    thread=threading.Thread(target=run,daemon=True);thread.start()
    return thread,out


# --- GuiCall --------------------------------------------------------------------------------------

def test_guicall_started_call_returns_its_result_once_after_queue_timeout(app,monkeypatch):
    monkeypatch.setattr(mcp_service,'QUEUE_SECONDS',.2)
    gui=mcp_service.GuiCall();runs=[]
    def slow():runs.append(1);time.sleep(.6);return 'done'
    thread,out=in_thread(lambda:gui.call(slow,time.monotonic()+5))
    wait(lambda:not thread.is_alive(),5000)
    assert out=={'value':'done'} and runs==[1]


def test_guicall_unstarted_call_is_cancelled_and_never_runs(app,monkeypatch):
    monkeypatch.setattr(mcp_service,'QUEUE_SECONDS',.2)
    gui=mcp_service.GuiCall();runs=[]
    thread,out=in_thread(lambda:gui.call(lambda:runs.append(1),time.monotonic()+5))
    thread.join(3)                       # the GUI thread (this one) does not process events meanwhile
    assert isinstance(out.get('error'),photo_service.Busy) and 'did not get to this call in time' in str(out['error'])
    wait(lambda:True);app.processEvents();time.sleep(.05);app.processEvents()
    assert runs==[]


def test_guicall_unfinished_after_grace_and_closed_calls_are_busy(app,monkeypatch):
    monkeypatch.setattr(mcp_service,'GRACE_SECONDS',.2)
    gui=mcp_service.GuiCall()
    thread,out=in_thread(lambda:gui.call(lambda:time.sleep(1),time.monotonic()+.2))
    wait(lambda:not thread.is_alive(),5000)
    assert isinstance(out.get('error'),mcp_service.Unfinished)
    waiting,result=in_thread(lambda:gui.call(lambda:'never',time.monotonic()+5))
    time.sleep(.1);gui.close();waiting.join(3)
    assert isinstance(result.get('error'),photo_service.Busy) and 'restarted' in str(result['error'])
    late,result=in_thread(lambda:gui.call(lambda:'never',time.monotonic()+5));late.join(3)
    assert isinstance(result.get('error'),photo_service.Busy)
    wait(lambda:True)


# --- session wiring and settings ------------------------------------------------------------------

def started_session(app,tmp_path,**settings):
    from luma.preferences import Preferences
    from luma.desktop_session import DesktopSession
    preferences=Preferences(tmp_path/'settings.json');preferences.save_language('ko');preferences.save('update_check',False)
    for key,value in settings.items():preferences.save(key,value)
    session=DesktopSession(app,tmp_path/'library',preferences)
    assert session.start()
    return session


def test_settings_menu_entry_is_before_about_and_the_connection_is_on_by_default(app,tmp_path):
    session=started_session(app,tmp_path)
    try:
        actions=session.window.findChild(QMenu,'settingsMenu').actions()
        texts=[a.text() for a in actions]
        assert texts[-1]=='Grainy 정보…' and 'AI 연결 (MCP)…' in texts
        position=texts.index('AI 연결 (MCP)…')
        assert actions[position+1].isSeparator() and session.mcp.running
        assert not hasattr(session.window.mcp, 'indicator') and not session.window.mcp.stop_button.isVisibleTo(session.window)      # nothing on show while idle
    finally:close_session(app,session)


def test_on_by_default_without_any_question_and_the_off_switch_is_remembered(app,tmp_path,monkeypatch):
    asked=[];monkeypatch.setattr(QMessageBox,'exec',lambda box:asked.append(box.text()))
    session=started_session(app,tmp_path)
    try:
        service=session.mcp
        assert service.running and service.enabled and service.error is None and asked==[]
        stored=json.loads((tmp_path/'settings.json').read_text(encoding='utf-8'))
        assert 'mcp_enabled' not in stored and 'mcp_consent' not in stored and len(stored['mcp_token'])==64
        assert stored['mcp_port']==service.bound_port and session.preferences.path==tmp_path/'settings.json'
        assert {'endpoint','auth-header','grainy-mcp-bridge.ps1','grainy-mcp-bridge.sh'}<={p.name for p in service.folder.iterdir()}
        session.window.mcp.open_dialog();dialog=session.window.mcp.dialog
        assert dialog.enable.isChecked() and dialog.status.text()==f'켜짐 · 포트 {service.bound_port}'
        assert not dialog.advanced.isVisibleTo(dialog) and dialog.tabs.isEnabled()
        dialog.enable.setChecked(False)
        assert not service.running and dialog.status.text()=='꺼짐'
        assert json.loads((tmp_path/'settings.json').read_text(encoding='utf-8'))['mcp_enabled'] is False
        assert not (service.folder/'endpoint').exists() and (service.folder/'offline-call.json').is_file()
        assert dialog.tabs.isEnabled() and token_free(dialog)          # the install texts stay usable while it is off
    finally:close_session(app,session)
    session=started_session(app,tmp_path)                             # the next start: off stays off
    try:
        assert not session.mcp.running and session.mcp.error is None and not (session.mcp.folder/'endpoint').exists()
        session.window.mcp.open_dialog();dialog=session.window.mcp.dialog
        assert not dialog.enable.isChecked() and dialog.status.text()=='꺼짐'
        dialog.enable.setChecked(True)
        assert session.mcp.running and json.loads((tmp_path/'settings.json').read_text(encoding='utf-8'))['mcp_enabled'] is True
        assert post(session.mcp.bound_port,session.mcp.token,{'jsonrpc':'2.0','id':1,'method':'ping'})[0]==200
        assert asked==[]
    finally:close_session(app,session)


def token_free(dialog):
    token=dialog.service.token
    return all(token not in text.toPlainText() and token not in dialog.setup_text(key) for key,(_,text,_) in dialog.setups.items())


def test_settings_of_the_consent_era_start_without_asking(app,tmp_path):
    """mcp_consent is no longer read; mcp_enabled True or a value that is not False is on."""
    for value in (True,None,1,'yes'):
        folder=tmp_path/str(value);folder.mkdir()
        session=started_session(app,folder,mcp_enabled=value,mcp_consent=0)
        try:
            assert session.mcp.running and session.mcp.port==session.mcp.bound_port
            assert post(session.mcp.bound_port,session.mcp.token,{'jsonrpc':'2.0','id':1,'method':'ping'})[0]==200
        finally:close_session(app,session)


def test_start_refuses_without_valid_token_and_generates_hex_token(app,tmp_path,monkeypatch):
    session=make_session(app,tmp_path,start=False)
    try:
        original=session.preferences.save
        def fail(key,value):
            if key=='mcp_token':raise OSError('disk full')
            return original(key,value)
        monkeypatch.setattr(session.preferences,'save',fail)
        assert not session.mcp.start(0) and session.mcp.server is None and session.mcp.gui is None
        assert session.mcp.error=='AI 연결 설정을 저장하지 못했습니다.' and not (session.mcp.folder/'endpoint').exists()
        monkeypatch.setattr(session.preferences,'save',original)
        session.preferences.save('mcp_token','not-hex')
        assert session.mcp.token is None and session.mcp.start(0)
        token=session.mcp.token
        assert len(token)==64 and all(c in '0123456789abcdef' for c in token) and token!='not-hex'
    finally:close_session(app,session)


def test_first_start_picks_another_port_when_default_is_taken(app,tmp_path,monkeypatch):
    with socket.socket() as taken:
        taken.bind(('127.0.0.1',0));taken.listen();port=taken.getsockname()[1]
        monkeypatch.setattr(mcp_service,'DEFAULT_PORT',port)
        session=make_session(app,tmp_path,start=False)
        try:
            assert session.mcp.start() and session.mcp.bound_port!=port and 40000<=session.mcp.bound_port<=48999
            assert session.mcp.port==session.mcp.bound_port
        finally:close_session(app,session)


def test_off_on_and_change_port_then_gui_tools_work(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',1);service=session.mcp;port=service.bound_port
    assert not call(session,'list_photos',{}).error
    service.stop();assert not service.running and service.gui is None or service.gui.closed
    with pytest.raises(OSError):post(port,service.token,{'jsonrpc':'2.0','id':1,'method':'ping'},timeout=2)
    assert service.start(port) and service.bound_port==port
    assert [p['id'] for p in call(session,'list_photos',{}).value['photos']]==ids
    assert service.change_port() and service.bound_port!=port and service.port==service.bound_port and service.port_changed
    assert not call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.2}}]}).error
    with pytest.raises(OSError):post(port,service.token,{'jsonrpc':'2.0','id':1,'method':'ping'},timeout=2)
    assert (service.folder/'endpoint').read_text()==f'http://127.0.0.1:{service.bound_port}/mcp/stdio'


def test_stop_deletes_endpoint_and_auth_header_but_keeps_offline_files(session):
    folder=session.mcp.folder
    names={'endpoint','auth-header','offline-initialize.json','offline-tools.json','offline-call.json',
           'grainy-mcp-bridge.ps1','grainy-mcp-bridge.sh','owner.lock'}
    assert names<={p.name for p in folder.iterdir()}
    session.mcp.stop()
    assert {p.name for p in folder.iterdir()}==names-{'endpoint','auth-header'}
    assert (folder/'grainy-mcp-bridge.sh').read_bytes()==(Path(mcp_service.__file__).parents[1]/'assets'/'mcp'/'grainy-mcp-bridge.sh').read_bytes()


def test_stop_removes_bridge_files_while_it_still_holds_the_port(session,monkeypatch):
    """Another program could take the port the moment it is released: endpoint and auth-header are gone first."""
    server=session.mcp.server;seen=[];original=Path.unlink
    def unlink(path,*args,**kwargs):
        if path.name in ('endpoint','auth-header'):seen.append((path.name,server.socket.fileno()!=-1))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',unlink)
    session.mcp.stop()
    assert seen==[('endpoint',True),('auth-header',True)] and server.socket.fileno()==-1


def other_grainy(tmp_path):
    """A second Grainy process: another library, the same settings file and so the same mcp folder."""
    from luma.preferences import Preferences
    preferences=Preferences(tmp_path/'settings.json')
    return mcp_service.McpService(SimpleNamespace(preferences=preferences,data_dir=tmp_path/'other library',window=None))


def test_second_grainy_never_takes_the_running_ones_bridge_files(session,tmp_path):
    first=session.mcp;folder=first.folder
    files={name:(folder/name).read_text(encoding='utf-8') for name in ('endpoint','auth-header')}
    second=other_grainy(session.preferences.path.parent)
    try:
        assert second.folder==folder
        second.start_if_enabled()                          # on by default, but the first one owns the files: removes nothing
        assert not second.running and second.elsewhere and second.owner is None
        assert not second.start() and not second.running and second.elsewhere
        assert second.error=='이 컴퓨터에서 실행 중인 다른 Grainy가 AI 연결을 쓰고 있습니다.'
        second.stop()                                      # quitting the second Grainy
        assert {name:(folder/name).read_text(encoding='utf-8') for name in files}==files
        assert first.running and post(first.bound_port,first.token,{'jsonrpc':'2.0','id':1,'method':'ping'})[0]==200
        first.stop()
        assert second.start(0) and (folder/'endpoint').read_text(encoding='utf-8')==f'http://127.0.0.1:{second.bound_port}/mcp/stdio'
    finally:second.stop()
    assert not (folder/'endpoint').exists() and not (folder/'auth-header').exists()


def test_bridge_files_left_by_a_crash_are_removed_at_the_next_start(app,tmp_path):
    folder=tmp_path/'mcp';folder.mkdir()
    def left_by_a_crash():
        (folder/'endpoint').write_text('http://127.0.0.1:47821/mcp/stdio',encoding='utf-8')
        (folder/'auth-header').write_text('Authorization: Bearer '+'ab'*32,encoding='utf-8')
    left_by_a_crash()
    service=other_grainy(tmp_path);service.preferences.save('mcp_enabled',False)
    service.start_if_enabled()                             # the person switched the connection off
    assert not (folder/'endpoint').exists() and not (folder/'auth-header').exists() and service.owner is None
    left_by_a_crash()
    with socket.socket() as taken:                         # the saved port is held by another program
        taken.bind(('127.0.0.1',0));taken.listen();port=taken.getsockname()[1]
        for key,value in {'mcp_port':port,'mcp_enabled':True}.items():service.preferences.save(key,value)
        service.start_if_enabled()
        assert not service.running and service.error.startswith(f'포트 {port}을(를) 쓸 수 없습니다') and not service.elsewhere
        assert not (folder/'endpoint').exists() and not (folder/'auth-header').exists() and service.owner is None
        # The installed bridges still answer that Grainy cannot be reached, and how to fix it.
        assert (folder/'grainy-mcp-bridge.ps1').is_file() and (folder/'offline-call.json').is_file()
    try:assert service.start() and (folder/'endpoint').is_file()
    finally:service.stop()


def test_distribution_notes_computers_shared_with_other_accounts():
    text=(Path(mcp_service.__file__).resolve().parents[1]/'DISTRIBUTION.md').read_text(encoding='utf-8')
    limits=text.split('## 알려진 한계',1)[1].split('\n## ',1)[0]
    assert '다른 계정' in limits and '새 키 만들기' in limits


def test_bridge_files_are_private_and_match_port_and_token(app,tmp_path,monkeypatch):
    folder=tmp_path/'settings'/'mcp'
    if os.name!='nt':folder.mkdir(parents=True,mode=0o755);os.chmod(folder,0o755)
    modes=[]
    if os.name!='nt':
        original=os.replace
        def replace(source,target):modes.append(os.stat(source).st_mode&0o777);return original(source,target)
        monkeypatch.setattr(os,'replace',replace)
    session=make_session(app,tmp_path)
    try:
        assert session.mcp.folder==folder
        assert (folder/'endpoint').read_text(encoding='utf-8')==f'http://127.0.0.1:{session.mcp.bound_port}/mcp/stdio'
        assert (folder/'auth-header').read_text(encoding='utf-8')==f'Authorization: Bearer {session.mcp.token}'
        assert not list(folder.glob('*.tmp'))
        if os.name!='nt':
            assert os.stat(folder).st_mode&0o777==0o700 and modes and set(modes)=={0o600}
            assert all(os.stat(p).st_mode&0o777==0o600 for p in folder.iterdir())
    finally:close_session(app,session)


def test_regenerate_token_rewrites_files_and_old_key_fails(session):
    service=session.mcp;old=service.token;port=service.bound_port
    service.regenerate_token()
    assert service.token!=old and len(service.token)==64
    assert (service.folder/'auth-header').read_text()==f'Authorization: Bearer {service.token}'
    assert post(port,old,{'jsonrpc':'2.0','id':1,'method':'ping'})[0]==401
    assert post(port,service.token,{'jsonrpc':'2.0','id':1,'method':'ping'})[0]==200


def test_port_in_use_reports_error_and_another_port_works(app,tmp_path):
    with socket.socket() as taken:
        taken.bind(('127.0.0.1',0));taken.listen();port=taken.getsockname()[1]
        session=make_session(app,tmp_path,start=False,settings={'mcp_port':port})
        try:
            assert not session.mcp.start() and session.mcp.error.startswith(f'포트 {port}을(를) 쓸 수 없습니다: ')
            session.window.mcp.open_dialog();dialog=session.window.mcp.dialog
            # Advanced opens by itself at Use Another Port.
            assert dialog.advanced.isVisibleTo(dialog) and dialog.port_button.isVisibleTo(dialog) and dialog.port_button.isEnabled()
            assert dialog.status.text()==session.mcp.error and not dialog.key.isEnabled()
            dialog.port_button.click()
            assert session.mcp.running and session.mcp.bound_port!=port and dialog.port_note.isVisibleTo(dialog)
            assert session.mcp.port==session.mcp.bound_port and dialog.status.text()==f'켜짐 · 포트 {session.mcp.bound_port}'
            assert (session.mcp.folder/'endpoint').read_text(encoding='utf-8')==f'http://127.0.0.1:{session.mcp.bound_port}/mcp/stdio'
            dialog.advanced_toggle.click();assert not dialog.advanced.isVisibleTo(dialog)
            dialog.refresh();assert not dialog.advanced.isVisibleTo(dialog)      # opened once, not again while it runs
        finally:close_session(app,session)


def test_request_left_from_a_stopped_server_never_writes_after_a_restart(session,tmp_path,monkeypatch):
    """A write still resolving its where filter while the connection is switched off and on again belongs to
    the server it came in on: that server's GuiCall is closed, so nothing is written."""
    from mcp_support import start_call
    monkeypatch.setattr(mcp_service,'STOP_SECONDS',.2)
    w=session.window;ids=add_photos(w,tmp_path/'p',2);service=session.mcp;port=service.bound_port
    entered=threading.Event();release=threading.Event();original=photo_service.query_ids
    def slow(*args,**kwargs):
        entered.set();assert release.wait(20);return original(*args,**kwargs)
    monkeypatch.setattr(photo_service,'query_ids',slow)
    pending=start_call(session,'apply_edits',{'edits':[{'where':{'folder':str(tmp_path/'p')},'add':{'exposure':.5}}]})
    assert entered.wait(10)
    old=service.server
    service.stop();assert service.start(port)
    release.set()
    wait(lambda:old.in_flight==0 and pending.done(),10000)       # the GUI thread keeps running meanwhile
    settled=time.monotonic()+.3;wait(lambda:time.monotonic()>settled)
    assert [w.catalog.photo(i)['settings']['exposure'] for i in ids]==[0,0] and photo_service.batch_index(w.catalog)==[]
    monkeypatch.setattr(photo_service,'query_ids',original)
    assert not call(session,'apply_edits',{'edits':[{'where':{'folder':str(tmp_path/'p')},'add':{'exposure':.5}}]}).error
    assert [w.catalog.photo(i)['settings']['exposure'] for i in ids]==[.5,.5]


def test_install_commands_are_the_documented_ones_and_hold_no_key_or_port():
    """docs/mcp-install.json (the project page) is exactly what Settings › AI Connection shows on a normal install."""
    documented=json.loads((ROOT/'docs'/'mcp-install.json').read_text(encoding='utf-8'))
    assert documented=={'windows':install_commands(True),'macos':install_commands(False)}
    windows,macos=documented['windows'],documented['macos']
    bridge='-File "$env:LOCALAPPDATA\\Grainy\\mcp\\grainy-mcp-bridge.ps1"'
    assert windows['claude_code']==f"claude mcp add --scope user grainy '--' powershell -NoProfile -ExecutionPolicy Bypass {bridge}"
    assert windows['codex']==f"codex mcp add grainy '--' powershell -NoProfile -ExecutionPolicy Bypass {bridge}"
    assert windows['claude_desktop_json']=={'mcpServers':{'grainy':{'command':'powershell','args':['-NoProfile','-ExecutionPolicy',
        'Bypass','-Command',"& (Join-Path $env:LOCALAPPDATA 'Grainy\\mcp\\grainy-mcp-bridge.ps1')"]}}}
    script='"$HOME/Library/Application Support/Grainy/mcp/grainy-mcp-bridge.sh"'
    assert macos['claude_code']==f'claude mcp add --scope user grainy -- sh {script}'
    assert macos['codex']==f'codex mcp add grainy -- sh {script}'
    assert macos['claude_desktop_json']=={'mcpServers':{'grainy':{'command':'/bin/sh','args':['-c',f'exec /bin/sh {script}']}}}
    text=json.dumps(documented)
    for word in ('Bearer','Authorization','127.0.0.1','47821','token','http','Users',Path.home().name):assert word not in text


def test_readmes_show_the_documented_install_commands_and_no_consent_step():
    documented=json.loads((ROOT/'docs'/'mcp-install.json').read_text(encoding='utf-8'))
    for name in ('README.md','README.ko.md'):
        text=(ROOT/name).read_text(encoding='utf-8')
        for system in documented.values():
            assert system['claude_code'] in text and system['codex'] in text and json.dumps(system['claude_desktop_json']) in text
        for stale in ('off by default','기본은 꺼짐','asks once','확인을 묻습니다','[mcp_servers.grainy]','Lightroom'):
            assert stale not in text.split('## Run from source')[0].split('## 소스에서 실행')[0].split('## Privacy')[-1].split('## 개인정보')[-1],(name,stale)


def test_install_commands_name_another_bridge_folder_as_it_is(tmp_path,monkeypatch):
    """A library run with tools/try_mcp.py (or these tests) keeps its bridges elsewhere: the commands say where."""
    folder=tmp_path/"사용자 O'Neil"/'mcp'
    windows=install_commands(True,folder)
    script=str(folder/'grainy-mcp-bridge.ps1');quoted="'"+script.replace("'","''")+"'"
    assert windows['claude_code'].endswith(f"grainy '--' powershell -NoProfile -ExecutionPolicy Bypass -File {quoted}")
    assert windows['claude_desktop_json']['mcpServers']['grainy']['args']==['-NoProfile','-ExecutionPolicy','Bypass','-File',script]
    posix=install_commands(False,folder)
    sh=str(folder/'grainy-mcp-bridge.sh')
    assert posix['codex']=="codex mcp add grainy -- sh '"+sh.replace("'","'\\''")+"'"
    assert posix['claude_desktop_json']=={'mcpServers':{'grainy':{'command':'/bin/sh','args':[sh]}}}
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    assert standard_folder(True)==tmp_path/'Local'/'Grainy'/'mcp'
    assert install_commands(True,tmp_path/'Local'/'Grainy'/'mcp')==install_commands(True)
    assert '\\u' not in install_text(posix['claude_desktop_json']) and '사용자' in install_text(posix['claude_desktop_json'])


def test_production_settings_folder_is_where_the_install_commands_look(monkeypatch,tmp_path):
    """The packaged app's settings live in user_paths.local_data('Grainy'): its mcp folder is the documented one."""
    from luma import user_paths
    from luma.preferences import Preferences
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    if sys.platform=='win32':
        assert Path(Preferences().path)==tmp_path/'Local'/'Grainy'/'settings.json'
    expected=standard_folder(sys.platform=='win32')
    service=mcp_service.McpService(SimpleNamespace(preferences=SimpleNamespace(path=user_paths.local_data('Grainy')/'settings.json',values={}),
                                                   data_dir=tmp_path,window=None))
    if expected is not None:assert service.folder==expected
    if user_paths.MAC:assert service.folder==Path.home()/'Library'/'Application Support'/'Grainy'/'mcp'


def test_dialog_shows_install_texts_without_the_key_and_hides_the_key_under_advanced(session):
    session.window.mcp.open_dialog();dialog=session.window.mcp.dialog;token=session.mcp.token;port=str(session.mcp.bound_port)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]==['Claude Code','Codex','Claude 데스크톱']
    expected=install_commands(sys.platform=='win32',session.mcp.folder)
    for key,(instruction,text,copy) in dialog.setups.items():
        shown=text.toPlainText()
        assert shown==install_text(expected[key]) and token not in shown and 'Bearer' not in shown and f':{port}' not in shown
        copy.click();assert QGuiApplication.clipboard().text()==shown
    assert str(session.mcp.bridge_path()) in dialog.setups['claude_code'][1].toPlainText() and session.mcp.bridge_path().is_file()
    shell='PowerShell' if sys.platform=='win32' else '터미널'
    assert dialog.setups['codex'][0].text().startswith(f'{shell}에서 아래 한 줄을') and 'claude mcp list' in dialog.setups['claude_code'][0].text()
    desktop=json.loads(dialog.setups['claude_desktop_json'][1].toPlainText())['mcpServers']['grainy']
    assert desktop==expected['claude_desktop_json']['mcpServers']['grainy']
    # Advanced: the key for the HTTP transport, hidden until shown. The window grows by it (on a 1080p screen).
    from PySide6.QtCore import QRect
    dialog.screen=lambda:SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1040));height=dialog.height()
    assert not dialog.advanced.isVisibleTo(dialog)
    dialog.advanced_toggle.click();assert dialog.advanced.isVisibleTo(dialog) and dialog.height()>height
    assert dialog.key.echoMode()==QLineEdit.EchoMode.Password and dialog.key.text()==token and token not in dialog.http.text()
    assert f'http://127.0.0.1:{port}/mcp' in dialog.http.text() and 'Bearer <연결 키>' in dialog.http.text()
    dialog.copy_key.click();assert QGuiApplication.clipboard().text()==token
    dialog.show_key.click()
    assert dialog.key.echoMode()==QLineEdit.EchoMode.Normal and dialog.show_key.text()=='숨기기' and token_free(dialog)
    dialog.show_key.click();assert dialog.key.echoMode()==QLineEdit.EchoMode.Password
    dialog.advanced_toggle.click();assert not dialog.advanced.isVisibleTo(dialog) and dialog.height()==height


def test_dialog_undo_last_ai_task_and_conflict_message(session,tmp_path):
    w=session.window;ids=add_photos(w,tmp_path/'p',2)
    session.window.mcp.open_dialog();dialog=session.window.mcp.dialog
    assert dialog.undo_label.text()=='되돌릴 AI 작업이 없습니다.' and not dialog.undo_button.isEnabled()
    assert not call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.5}}]}).error
    dialog.refresh()
    assert dialog.undo_label.text().startswith('AI 보정 · 2장 · ') and dialog.undo_button.isEnabled()
    dialog.undo_button.click()
    assert all(w.catalog.photo(i)['settings']['exposure']==0 for i in ids) and not dialog.undo_button.isEnabled()
    assert not call(session,'apply_edits',{'edits':[{'ids':ids,'add':{'exposure':.5}}]}).error
    w.catalog.edit(ids[0],{**w.catalog.photo(ids[0])['settings'],'contrast':3});dialog.refresh()
    dialog.undo_button.click()
    assert dialog.undo_message.text()=='이후에 다시 보정한 사진이 있어 이 AI 작업은 되돌릴 수 없습니다. 사진마다 실행 취소를 사용해 주세요.'
    assert w.catalog.photo(ids[1])['settings']['exposure']==.5
    w.maintenance_running=True
    try:
        dialog.undo_button.click()
        assert dialog.undo_message.text()=='지금은 되돌릴 수 없습니다. 열린 창이나 진행 중인 작업을 마친 뒤 다시 눌러 주세요.'
    finally:w.maintenance_running=False
    session.mcp.stop();dialog.refresh();assert dialog.undo_button.isEnabled()      # also while the connection is off


def quiet_start(app,tmp_path,monkeypatch,**settings):
    """A DesktopSession.start() that records every dialog and status-bar message."""
    shown=[];monkeypatch.setattr(QMessageBox,'exec',lambda box:shown.append(box.text()))
    for name in ('warning','information','critical','question'):
        monkeypatch.setattr(QMessageBox,name,lambda *args,**kwargs:shown.append(args[2] if len(args)>2 else ''))
    from luma.preferences import Preferences
    from luma.desktop_session import DesktopSession
    preferences=Preferences(tmp_path/'settings.json');preferences.save_language('ko');preferences.save('update_check',False)
    for key,value in settings.items():preferences.save(key,value)
    session=DesktopSession(app,tmp_path/'library',preferences);messages=[]
    original=session.open_window
    def opened(*args,**kwargs):
        original(*args,**kwargs);session.window.statusBar().messageChanged.connect(messages.append)
    session.open_window=opened
    assert session.start() and session.window is not None and session.window.isVisible()
    return session,shown,messages


def test_start_failure_is_quiet_and_the_bridges_still_explain(app,tmp_path,monkeypatch):
    with socket.socket() as taken:
        taken.bind(('127.0.0.1',0));taken.listen();port=taken.getsockname()[1]
        session,shown,messages=quiet_start(app,tmp_path,monkeypatch,mcp_port=port)
        try:
            service=session.mcp
            assert not service.running and service.error.startswith(f'포트 {port}을(를) 쓸 수 없습니다: ')
            assert shown==[] and not [m for m in messages if 'AI' in m]
            assert ('server','could not start') in [entry[1:] for entry in service.activity]
            assert not (service.folder/'endpoint').exists()
            assert json.loads((service.folder/'offline-call.json').read_text(encoding='utf-8').replace('__ID__','1'))['result']['isError']
        finally:close_session(app,session)


def test_startup_never_fails_because_of_the_ai_connection(app,tmp_path,monkeypatch):
    """Whatever goes wrong in the connection's start (here: an exception in it), Grainy opens, silently."""
    def broken(self,port=None):raise RuntimeError('broken')
    monkeypatch.setattr(mcp_service.McpService,'start',broken)
    session,shown,messages=quiet_start(app,tmp_path,monkeypatch)
    try:
        assert not session.mcp.running and session.mcp.error=='AI 연결을 시작하지 못했습니다: RuntimeError'
        assert shown==[] and not [m for m in messages if 'AI' in m] and session.mcp.server is None
        session.window.mcp.open_dialog();assert session.window.mcp.dialog.status.text()==session.mcp.error
    finally:close_session(app,session)


def test_startup_with_an_unwritable_bridge_folder_still_serves_http(app,tmp_path,monkeypatch):
    def unwritable(folder):raise PermissionError(13,'Access is denied',str(folder))
    monkeypatch.setattr(mcp_service,'_private_folder',unwritable)
    session,shown,messages=quiet_start(app,tmp_path,monkeypatch)
    try:
        service=session.mcp
        assert service.running and service.owner is None and shown==[] and not [m for m in messages if 'AI' in m]
        assert ('files','could not write the bridge files') in [entry[1:] for entry in service.activity]
        assert post(service.bound_port,service.token,{'jsonrpc':'2.0','id':1,'method':'ping'})[0]==200
    finally:close_session(app,session)


def test_startup_next_to_a_grainy_that_owns_the_connection_is_quiet(app,tmp_path,monkeypatch):
    first=other_grainy(tmp_path)
    try:
        assert first.start(0)
        session,shown,messages=quiet_start(app,tmp_path,monkeypatch)
        try:
            assert not session.mcp.running and session.mcp.elsewhere and shown==[] and not [m for m in messages if 'AI' in m]
            assert post(first.bound_port,first.token,{'jsonrpc':'2.0','id':1,'method':'ping'})[0]==200
        finally:close_session(app,session)
    finally:first.stop()


@pytest.mark.skipif(sys.platform!='win32' or not shutil.which('powershell.exe'),reason='Windows PowerShell is needed')
def test_grainy_writes_the_bridge_that_the_documented_desktop_command_starts(app,tmp_path,monkeypatch):
    """The Claude Desktop entry of docs/mcp-install.json, started as Claude Desktop starts it (only the environment
    the MCP SDK passes on, LOCALAPPDATA pointing at a temporary folder), against a running Grainy that keeps its
    settings there: forwarded while the connection is on, the offline answer while it is off, again when it is on."""
    from test_mcp_bridge import Bridge,desktop_environment,documented_desktop_command,message
    local=tmp_path/'사용자 이름'/'AppData'/'Local'
    session=make_session(app,local/'Grainy',settings_path=local/'Grainy'/'settings.json')                                     # settings: <local>/Grainy/settings.json
    try:
        ids=add_photos(session.window,tmp_path/'p',2);service=session.mcp
        assert service.folder==local/'Grainy'/'mcp'
        bridge=Bridge(documented_desktop_command(local),env=desktop_environment(local),replace=True)
        def ask(*args):
            bridge.send(message(*args));wait(lambda:not bridge.lines.empty(),60000)
            return bridge.reply(1)
        assert ask('initialize',1,{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'t','version':'1'}})['result']['instructions']==mcp_protocol.INSTRUCTIONS
        listed=ask('tools/call',2,{'name':'list_photos','arguments':{}})
        assert [p['id'] for p in json.loads(listed['result']['content'][-1]['text'])['photos']]==ids
        service.set_enabled(False)
        offline=ask('tools/call',3,{'name':'list_photos','arguments':{}})['result']
        assert offline['isError'] and 'Grainy did not answer' in offline['content'][0]['text'] and 'Grainy가 응답하지 않았습니다' in offline['content'][0]['text']
        service.set_enabled(True)
        assert not ask('tools/call',4,{'name':'list_photos','arguments':{}})['result']['isError']
        code,rest=bridge.finish()
        assert code==0 and rest==[]
    finally:close_session(app,session)


def test_token_never_in_activity_log(session,tmp_path):
    token=session.mcp.token
    call(session,'list_photos',{});post(session.mcp.bound_port,'0'*64,{'jsonrpc':'2.0','id':1,'method':'ping'})
    session.mcp.regenerate_token();call(session,'get_job',{'job':'j9'})
    session.window.mcp.open_dialog();dialog=session.window.mcp.dialog;dialog.refresh()
    log=repr(list(session.mcp.activity))+' '.join(dialog.activity.item(i).text() for i in range(dialog.activity.count()))
    assert token not in log and session.mcp.token not in log and '0'*64 not in log
    assert 'list_photos · 성공' in log and 'refused 401' in log


def test_activity_list_is_in_the_interface_language(session):
    for name,outcome in (('files','could not write the bridge files'),('key','new key'),('http','refused 401'),
                         ('list_photos','ok'),('auto_tone','job j1 done'),('get_job','error -32602'),('http','error: OSError: x')):
        session.mcp.note(name,outcome)
    session.window.mcp.open_dialog();dialog=session.window.mcp.dialog
    def lines():
        dialog.refresh()
        return [dialog.activity.item(i).text().split(' · ',1)[1] for i in range(dialog.activity.count())]
    assert lines()==['http · 오류: OSError: x','get_job · 오류 -32602','auto_tone · 작업 j1 완료','list_photos · 성공',
        'http · 거부함: 연결 키가 없거나 맞지 않음','연결 키 · 새 키를 만듦',
        '중계 파일 · 쓰지 못함 (설치한 AI 프로그램이 연결할 수 없음)','AI 연결 · 시작함']
    set_language('en')
    assert lines()==['http · Error: OSError: x','get_job · Error -32602','auto_tone · Job j1 done','list_photos · OK',
        'http · Refused: missing or wrong connection key','Connection key · New key made',
        'Bridge files · Could not be written (installed AI programs cannot connect)','AI connection · Started']


def test_failed_key_or_export_folder_save_is_shown_at_once_and_off_shows_off(session,tmp_path,monkeypatch):
    from luma import mcp_dialog
    service=session.mcp;token=service.token;warnings=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *args,**kwargs:QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox,'warning',lambda parent,title,text,*args,**kwargs:warnings.append(text))
    original=service.preferences.save
    def fail(key,value):
        if key in ('mcp_token','mcp_export_folder'):raise OSError('locked by another program')
        return original(key,value)
    monkeypatch.setattr(service.preferences,'save',fail)
    session.window.mcp.open_dialog();dialog=session.window.mcp.dialog
    dialog.regenerate()
    assert warnings==['AI 연결 설정을 저장하지 못했습니다.'] and service.token==token
    chosen=tmp_path/'exports';chosen.mkdir()
    monkeypatch.setattr(mcp_dialog.QFileDialog,'getExistingDirectory',lambda *args,**kwargs:str(chosen))
    dialog.choose_export_folder()
    assert len(warnings)==2 and service.export_root()!=chosen
    assert service.error is None and dialog.status.text()==f'켜짐 · 포트 {service.bound_port}'
    service.error='AI 연결 설정을 저장하지 못했습니다.'        # an earlier failure (the switch could not be saved)
    monkeypatch.setattr(service.preferences,'save',original)
    dialog.toggled(False)                                  # the person turns the connection off
    assert not service.running and dialog.status.text()=='꺼짐'


def test_explanation_names_every_photo_detail_sent_and_says_when_anything_is_read():
    from luma import mcp_dialog
    from luma.engine import defaults
    record={'id':1,'name':'R12_01.NEF','path':'D:/Film/R12_01.NEF','rating':3,'flag':1,'label':'빨강','keywords':'롤12',
            'settings':defaults(),'info':{'width':6048,'height':4024,'format':'NEF','camera':'Z 6','lens':'50mm','iso':400,
            'shutter':'1/250','aperture':2.8,'focal_length':50.,'date':'2026:10:01 10:00:00'}}
    named={'name':'파일 이름','path':'폴더 경로','size':'픽셀 크기','format':'파일 형식','keywords':'키워드','r':'별점',
           'f':'선택·제외 표시','l':'색 라벨','camera':'카메라','lens':'렌즈','iso':'ISO','shutter':'셔터 속도','aperture':'조리개',
           'focal':'초점 거리','date':'촬영 날짜'}
    sent=set(photo_service.summary(record))-{'id','crop','auto_level'}               # edit values, described above
    assert sent==set(named)                                # a new detail needs its words in the explanation
    assert [named[key] for key in sorted(sent) if named[key] not in mcp_dialog.EXPLAIN]==[]
    assert all(words in mcp_dialog.EXPLAIN for words in ('Grainy가 실행 중이면','설치하고 일을 시켰을 때만','그 AI 서비스로 전송',
                                                         '인터넷으로 보내지 않으며','위치 정보는 보내지 않습니다'))
    set_language('en');english=tr(mcp_dialog.EXPLAIN)
    assert english!=mcp_dialog.EXPLAIN and 'While Grainy is running' in english and 'Only when you install' in english
    assert all(word in english for word in ('ratings','pick and reject flags','color labels','ISO','aperture','pixel size'))
    for text in (mcp_dialog.EXPLAIN,english):assert 'Lightroom' not in text and 'Adobe' not in text
