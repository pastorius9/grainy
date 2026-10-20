"""The stdio bridges (assets/mcp) that Claude Code, Codex and Claude Desktop start: run against a stub server on
this computer only, also exactly as the documented install commands (docs/mcp-install.json) start them."""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from luma import mcp_http,mcp_protocol,mcp_tools
from luma.mcp_dialog import install_commands

ROOT=Path(__file__).resolve().parents[1]/'assets'/'mcp'
DOCUMENTED=json.loads((ROOT.parents[1]/'docs'/'mcp-install.json').read_text(encoding='utf-8'))
TOKEN='9f'*32
WINDOWS=sys.platform=='win32'
POWERSHELL=shutil.which('powershell.exe') if WINDOWS else None
needs_powershell=pytest.mark.skipif(not POWERSHELL,reason='Windows PowerShell is needed')
# What the clients pass on to a stdio server they start: the MCP TypeScript SDK (Claude Desktop) keeps only these
# (DEFAULT_INHERITED_ENV_VARS), Codex clears the rest too (rmcp-client, 0.163); Claude Code passes its own.
DESKTOP_ENV=('APPDATA','HOMEDRIVE','HOMEPATH','LOCALAPPDATA','PATH','PROCESSOR_ARCHITECTURE','SYSTEMDRIVE','SYSTEMROOT','TEMP',
             'USERNAME','USERPROFILE','PROGRAMFILES')
CODEX_ENV=('PATH','PATHEXT','SHELL','COMSPEC','SYSTEMROOT','WINDIR','SYSTEMDRIVE','USERNAME','USERDOMAIN','USERPROFILE','HOMEDRIVE',
           'HOMEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','PROGRAMDATA','LOCALAPPDATA','APPDATA','TEMP','TMP','TMPDIR',
           'POWERSHELL','PWSH')


def client_environment(names,local):
    """Only the variables that client passes on, with LOCALAPPDATA pointing at a temporary folder."""
    env={name:os.environ[name] for name in names if name in os.environ}
    env['LOCALAPPDATA']=str(local)
    return env


def desktop_environment(local):return client_environment(DESKTOP_ENV,local)


def documented_desktop_command(local):
    """The documented Claude Desktop entry as the SDK's cross-spawn starts it: the command found on PATH, the
    arguments as they are."""
    entry=DOCUMENTED['windows']['claude_desktop_json']['mcpServers']['grainy']
    return [shutil.which(entry['command'],path=desktop_environment(local)['PATH']),*entry['args']]


class Tools:
    def definitions(self):return mcp_tools.DEFINITIONS
    def call(self,name,arguments,*,modern,deadline,gui=None):
        return {'content':[{'type':'text','text':json.dumps(arguments,ensure_ascii=False)}],'isError':False}


class Host:
    def __init__(self):self.token=TOKEN;self.calls=threading.BoundedSemaphore(4);self.tools=Tools();self.notes=[]
    def note(self,*args):self.notes.append(args)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(mcp_http,'POLL_SECONDS',.05)
    host=Host();value=mcp_http.start(0,host);value.host=host
    yield value
    value.shutdown();value.close_connections();value.server_close()


def folder_for(tmp_path,server=None,name='mcp',script='grainy-mcp-bridge.ps1',port=None):
    folder=tmp_path/name;folder.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(ROOT/script,folder/script)
    for file,text in mcp_protocol.offline_templates().items():(folder/file).write_text(text,encoding='utf-8')
    if server is not None or port is not None:
        (folder/'endpoint').write_text(f'http://127.0.0.1:{port or server.server_address[1]}/mcp/stdio',encoding='utf-8')
        (folder/'auth-header').write_text(f'Authorization: Bearer {TOKEN}',encoding='utf-8')
    return folder


class Bridge:
    """The bridge process as an AI program starts it, with line-by-line I/O. replace: env is the whole environment."""
    def __init__(self,command,env=None,replace=False):
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      env=env if replace else {**os.environ,**(env or {})},
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.lines=queue.Queue()
        threading.Thread(target=self._read,daemon=True).start()

    def _read(self):
        for line in self.process.stdout:self.lines.put(line)
        self.lines.put(None)

    def send(self,message):
        text=message if isinstance(message,str) else json.dumps(message,ensure_ascii=False)
        self.process.stdin.write(text.encode('utf-8')+b'\n');self.process.stdin.flush()

    def reply(self,timeout=60):
        line=self.lines.get(timeout=timeout)
        assert line is not None,self.process.stderr.read().decode(errors='replace')
        assert line.endswith(b'\n') and line.count(b'\n')==1
        return json.loads(line)

    def finish(self,timeout=60):
        self.process.stdin.close()
        code=self.process.wait(timeout)
        rest=[]
        while (line:=self.lines.get(timeout=timeout)) is not None:rest.append(line)
        return code,rest


def desktop_command(folder):
    """The Claude Desktop entry Settings › AI Connection shows for a bridge folder elsewhere (-File and the path)."""
    entry=install_commands(True,folder)['claude_desktop_json']['mcpServers']['grainy']
    return [shutil.which(entry['command']),*entry['args']]


def message(method,ident=None,params=None):
    value={'jsonrpc':'2.0','method':method}
    if ident is not None:value['id']=ident
    if params is not None:value['params']=params
    return value


def test_bridge_scripts_are_ascii_without_secrets_and_sh_has_lf_only():
    for name in ('grainy-mcp-bridge.ps1','grainy-mcp-bridge.sh'):
        data=(ROOT/name).read_bytes()
        assert data.isascii() and b'Bearer' not in data      # the key is read from auth-header, never written here
        assert b'--max-time 56' in data or b'FromSeconds(56)' in data
        text=data.decode()
        for secret in ('token=','password','api_key','127.0.0.1:47821','C:\\Users'):assert secret not in text
        # Their own answer when even the offline files are missing: in English and Korean, how to fix it.
        assert json.dumps(mcp_protocol.OFFLINE_ERROR)[1:-1] in text
    assert 'Grainy가 실행 중이 아니거나' in mcp_protocol.OFFLINE_ERROR and 'Settings > AI Connection' in mcp_protocol.OFFLINE_ERROR
    sh=(ROOT/'grainy-mcp-bridge.sh').read_bytes()
    assert b'\r' not in sh and sh.startswith(b'#!/bin/sh\n') and b'-H @"$DIR/auth-header"' in sh
    assert b'\t' not in (ROOT/'grainy-mcp-bridge.ps1').read_bytes()


@needs_powershell
def test_powershell_bridge_forwards_and_skips_notifications(server,tmp_path):
    bridge=Bridge(desktop_command(folder_for(tmp_path,server)))
    bridge.send(message('initialize',1,{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'t','version':'1'}}))
    bridge.send(message('notifications/initialized'))
    bridge.send(message('tools/list',2))
    bridge.send(message('tools/call','call-3',{'name':'list_photos','arguments':{'text':'현상 사진 "따옴표"'}}))
    first,second,third=bridge.reply(),bridge.reply(),bridge.reply()
    assert first['id']==1 and first['result']['protocolVersion']=='2025-06-18'
    assert second['id']==2 and len(second['result']['tools'])==12
    assert third['id']=='call-3' and json.loads(third['result']['content'][0]['text'])=={'text':'현상 사진 "따옴표"'}
    code,rest=bridge.finish()
    assert code==0 and rest==[]


@needs_powershell
def test_powershell_bridge_passes_a_300kb_line(server,tmp_path):
    bridge=Bridge(desktop_command(folder_for(tmp_path,server)))
    text='가'*50_000+'x'*150_000
    bridge.send(message('tools/call',7,{'name':'list_photos','arguments':{'text':text}}))
    reply=bridge.reply()
    assert reply['id']==7 and json.loads(reply['result']['content'][0]['text'])['text']==text
    assert bridge.finish()[0]==0


@needs_powershell
def test_powershell_bridge_runs_from_a_hangul_folder_with_file_and_command_fallback(server,tmp_path):
    folder=folder_for(tmp_path,server,name='사용자 폴더/Grainy/mcp')
    bridge=Bridge(desktop_command(folder))
    bridge.send(message('ping',1));assert bridge.reply()=={'jsonrpc':'2.0','id':1,'result':{}}
    assert bridge.finish()[0]==0
    script=str(folder/'grainy-mcp-bridge.ps1')
    command=[POWERSHELL,'-NoLogo','-NoProfile','-NonInteractive','-Command',
             f"$env:GRAINY_MCP_DIR='{folder}'; & ([scriptblock]::Create((Get-Content -Raw -LiteralPath '{script}')))"]
    bridge=Bridge(command)
    bridge.send(message('tools/call',2,{'name':'get_job','arguments':{'job':'j1'}}))
    assert json.loads(bridge.reply()['result']['content'][0]['text'])=={'job':'j1'}
    assert bridge.finish()[0]==0
    # Where a policy blocks script files: the script's text as a script block finds its folder through LOCALAPPDATA.
    local=tmp_path/'로컬';shutil.copytree(folder,local/'Grainy'/'mcp')
    command=[POWERSHELL,'-NoProfile','-Command',
             "& ([scriptblock]::Create((Get-Content -Raw -LiteralPath (Join-Path $env:LOCALAPPDATA 'Grainy\\mcp\\grainy-mcp-bridge.ps1'))))"]
    bridge=Bridge(command,env={'LOCALAPPDATA':str(local),'GRAINY_MCP_DIR':''})
    bridge.send(message('tools/call',3,{'name':'get_job','arguments':{'job':'j3'}}))
    assert json.loads(bridge.reply()['result']['content'][0]['text'])=={'job':'j3'}
    assert bridge.finish()[0]==0


@needs_powershell
def test_documented_claude_desktop_entry_forwards_and_answers_offline_on_windows(server,tmp_path):
    """docs/mcp-install.json's Windows Claude Desktop entry, started as Claude Desktop starts it: only the
    environment the MCP SDK passes on, with LOCALAPPDATA in a temporary folder whose path has Hangul and spaces."""
    local=tmp_path/'사용자 이름'/'AppData'/'Local'
    folder=folder_for(local,server,name='Grainy/mcp')
    command=documented_desktop_command(local);env=desktop_environment(local)
    assert command[1:]==DOCUMENTED['windows']['claude_desktop_json']['mcpServers']['grainy']['args'] and 'LOCALAPPDATA' in env
    bridge=Bridge(command,env=env,replace=True)
    bridge.send(message('initialize',1,{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'t','version':'1'}}))
    assert bridge.reply()['result']['protocolVersion']=='2025-06-18'
    bridge.send(message('notifications/initialized'))
    bridge.send(message('tools/call',2,{'name':'list_photos','arguments':{'text':'현상 "따옴표"'}}))
    assert json.loads(bridge.reply()['result']['content'][0]['text'])=={'text':'현상 "따옴표"'}
    (folder/'endpoint').unlink()                                    # Grainy quits, or its AI connection is switched off
    bridge.send(message('tools/call',3,{'name':'list_photos','arguments':{}}))
    offline=bridge.reply()['result']
    assert offline['isError'] and offline['content'][0]['text']==mcp_protocol.OFFLINE_TEXT
    code,rest=bridge.finish()
    assert code==0 and rest==[] and not [note for note in server.host.notes if 'refused' in str(note)]


def fake_clients(folder,python,record):
    """claude and codex as two kinds of installs put them on PATH: npm's PowerShell script and a .cmd (an .exe takes its
    arguments the same way). Both only write down the arguments they were given, one JSON line per call, to record."""
    folder.mkdir(parents=True)
    (folder/'argv.py').write_text('import json,os,sys\nwith open(os.environ["GRAINY_TEST_ARGV"],"a",encoding="utf-8") as out:'
                                  'out.write(json.dumps(sys.argv[1:])+"\\n")\n',encoding='utf-8')
    for kind in ('ps1','cmd'):
        (folder/kind).mkdir()
        for name in ('claude','codex'):
            if kind=='ps1':text="& '{0}' '{1}' $args\r\n".format(str(python).replace("'","''"),str(folder/'argv.py').replace("'","''"))
            else:text=f'@"{python}" "{folder/"argv.py"}" %*\r\n'
            (folder/kind/f'{name}.{kind}').write_text(text,encoding='utf-8')
    record.write_text('',encoding='utf-8')
    return {kind:folder/kind for kind in ('ps1','cmd')}


def typing_environment(local,shim,record,home):
    """A PowerShell window's variables, but on PATH only the stand-in clients and Windows itself, and HOME,
    USERPROFILE and CODEX_HOME in a temporary folder."""
    home.mkdir(parents=True,exist_ok=True)
    system=Path(os.environ.get('SystemRoot','C:\\Windows'))/'System32'
    return {**client_environment(DESKTOP_ENV+('COMSPEC','PATHEXT','PSModulePath'),local),
            'PATH':os.pathsep.join([str(shim),str(system),str(system/'WindowsPowerShell'/'v1.0')]),
            'USERPROFILE':str(home),'HOME':str(home),'CODEX_HOME':str(home/'.codex'),'GRAINY_TEST_ARGV':str(record)}


def typed_in_powershell(line,typed,env,record):
    """line as typed at a PowerShell prompt (saved to a file that PowerShell reads): the arguments the client got."""
    typed.write_text(line+'\r\n',encoding='utf-8-sig')
    before=len(record.read_text(encoding='utf-8').splitlines())
    run=subprocess.run([POWERSHELL,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(typed)],capture_output=True,env=env,timeout=120,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    calls=record.read_text(encoding='utf-8').splitlines()
    assert run.returncode==0 and not run.stderr.strip() and len(calls)==before+1,run.stderr.decode(errors='replace')
    return json.loads(calls[-1])


@needs_powershell
def test_documented_windows_install_lines_register_the_bridge_and_it_runs(server,tmp_path):
    """Each documented PowerShell line, run as typed (from a .ps1 file, so PowerShell parses it as in a console), gives
    claude and codex the stdio command that the bridge then answers through. Only stand-in clients are on PATH, and
    HOME, USERPROFILE and CODEX_HOME are temporary folders: nothing reaches the real ones."""
    local=tmp_path/'사용자 이름'/'AppData'/'Local'
    folder=folder_for(local,server,name='Grainy/mcp');script=str(folder/'grainy-mcp-bridge.ps1')
    record=tmp_path/'argv.jsonl';home=tmp_path/'home'
    shims=fake_clients(tmp_path/'clients',sys.executable,record)
    expected={'claude_code':['mcp','add','--scope','user','grainy'],'codex':['mcp','add','grainy']}
    for kind,shim in shims.items():
        for client,line in ((c,DOCUMENTED['windows'][c]) for c in ('claude_code','codex')):
            argv=typed_in_powershell(line,tmp_path/f'{client}-{kind}.ps1',typing_environment(local,shim,record,home),record)
            assert argv==[*expected[client],'--','powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',script],(kind,argv)
    assert not (home/'.claude.json').exists() and not (home/'.codex').exists()
    # What Codex then starts (with only the variables it passes on), and Claude Code (with its own environment).
    stdio=argv[argv.index('--')+1:]
    for env in (client_environment(CODEX_ENV,local),{**os.environ,'LOCALAPPDATA':str(local)}):
        bridge=Bridge([shutil.which(stdio[0],path=env['PATH']),*stdio[1:]],env=env,replace=True)
        bridge.send(message('initialize',1,{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'t','version':'1'}}))
        assert bridge.reply()['result']['protocolVersion']=='2025-06-18'
        bridge.send(message('tools/call',2,{'name':'get_job','arguments':{'job':'j2'}}))
        assert json.loads(bridge.reply()['result']['content'][0]['text'])=={'job':'j2'}
        assert bridge.finish()[0]==0
    (folder/'endpoint').unlink()
    bridge=Bridge([shutil.which(stdio[0]),*stdio[1:]],env=client_environment(CODEX_ENV,local),replace=True)
    bridge.send(message('tools/list',3));assert len(bridge.reply()['result']['tools'])==len(mcp_tools.DEFINITIONS)
    bridge.send(message('tools/call',4,{'name':'list_photos','arguments':{}}))
    assert bridge.reply()['result']['content'][0]['text']==mcp_protocol.OFFLINE_TEXT
    assert bridge.finish()[0]==0


@needs_powershell
def test_policy_fallback_in_the_distribution_notes_runs_the_bridge(server,tmp_path):
    """DISTRIBUTION.md's way round a policy that blocks script files (the script's text as a script block): the
    Claude Desktop args and the PowerShell line for Claude Code, as written there."""
    import re
    text=(ROOT.parents[1]/'DISTRIBUTION.md').read_text(encoding='utf-8')
    args=json.loads(re.search(r'`(\["-NoProfile", "-Command", "& \(\[scriptblock\][^`]*\])`',text)[1])
    line=re.search(r"`(claude mcp add --scope user grainy '--' powershell -NoProfile -Command '[^`]*')`",text)[1]
    local=tmp_path/'사용자 이름'/'AppData'/'Local';folder=folder_for(local,server,name='Grainy/mcp')
    record=tmp_path/'argv.jsonl';shim=fake_clients(tmp_path/'clients',sys.executable,record)['ps1']
    argv=typed_in_powershell(line,tmp_path/'line.ps1',typing_environment(local,shim,record,tmp_path/'home'),record)
    assert argv[argv.index('--')+1:]==['powershell',*args]
    for env in (desktop_environment(local),client_environment(CODEX_ENV,local)):
        bridge=Bridge([shutil.which('powershell',path=env['PATH']),*args],env=env,replace=True)
        bridge.send(message('tools/call',1,{'name':'get_job','arguments':{'job':'j1'}}))
        assert json.loads(bridge.reply()['result']['content'][0]['text'])=={'job':'j1'}
        assert bridge.finish()[0]==0


@needs_powershell
@pytest.mark.parametrize('endpoint',['none','closed'])
def test_powershell_bridge_answers_offline_from_templates(tmp_path,endpoint):
    import socket
    if endpoint=='closed':
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        folder=folder_for(tmp_path,port=port)
    else:folder=folder_for(tmp_path)
    bridge=Bridge(desktop_command(folder))
    for ident in (11,'s-12'):
        bridge.send(message('initialize',ident,{'protocolVersion':'2025-06-18'}))
        reply=bridge.reply();assert reply['id']==ident and reply['result']['protocolVersion']==mcp_protocol.OFFLINE_VERSION
        bridge.send(message('tools/list',ident))
        reply=bridge.reply();assert reply['id']==ident and len(reply['result']['tools'])==12
    bridge.send(message('notifications/initialized'))
    bridge.send(message('tools/call',13,{'name':'list_photos','arguments':{}}))
    reply=bridge.reply();assert reply['id']==13 and reply['result']['isError']
    text=reply['result']['content'][0]['text']
    assert text.startswith('Grainy did not answer: it is not running') and 'Grainy가 응답하지 않았습니다' in text
    assert "tick 'Turn on the AI connection'" in text and "'AI 연결 켜기'를 켜고" in text
    bridge.send(message('ping','p"q'));assert bridge.reply()=={'jsonrpc':'2.0','id':'p"q','result':{}}
    bridge.send(message('frobnicate',14));assert bridge.reply()['error']['code']==-32601
    bridge.send('not json at all');bridge.send('')
    code,rest=bridge.finish()
    assert code==0 and rest==[]


@needs_powershell
def test_powershell_bridge_rereads_key_files(server,tmp_path):
    folder=folder_for(tmp_path,server);bridge=Bridge(desktop_command(folder))
    bridge.send(message('tools/call',1,{'name':'get_job','arguments':{'job':'j1'}}))
    assert bridge.reply()['result']['isError'] is False
    server.host.token='a1'*32                                   # a new key: the old one gets 401, so the answer is offline
    bridge.send(message('tools/call',2,{'name':'get_job','arguments':{'job':'j2'}}))
    assert bridge.reply()['result']['isError'] is True
    (folder/'auth-header').write_text('Authorization: Bearer '+'a1'*32,encoding='utf-8')
    bridge.send(message('tools/call',3,{'name':'get_job','arguments':{'job':'j3'}}))
    reply=bridge.reply();assert reply['id']==3 and reply['result']['isError'] is False
    assert bridge.finish()[0]==0


def posix_tools():
    """sh and awk, also those of Git for Windows; None when this computer has none."""
    sh,awk=shutil.which('sh'),shutil.which('awk')
    if sh and awk:return sh,str(Path(awk).parent)
    git=shutil.which('git')
    for base in ([Path(git).resolve().parents[1]] if git else [])+[Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Git']:
        if (base/'usr'/'bin'/'sh.exe').is_file() and (base/'usr'/'bin'/'awk.exe').is_file():
            return str(base/'usr'/'bin'/'sh.exe'),str(base/'usr'/'bin')
    return None


@pytest.mark.skipif(posix_tools() is None,reason='sh and awk are needed')
def test_sh_field_parser_finds_top_level_id_and_method(tmp_path):
    sh,bin_folder=posix_tools();folder=folder_for(tmp_path,script='grainy-mcp-bridge.sh')
    lines=['{"params":{"arguments":{"id":99,"method":"x"},"name":"list_photos"},"method":"tools/call","jsonrpc":"2.0","id":5}',
           '{"jsonrpc":"2.0","method":"ping","id":"quote \\" and \\\\ backslash"}',
           '{ "jsonrpc" : "2.0" , "id" : 17 , "method" : "ping" }',
           '{"jsonrpc":"2.0","id":[1,2],"method":"ping"}',
           '{"jsonrpc":"2.0","method":"notifications/initialized"}',
           '{"jsonrpc":"2.0","id":8,"method":"tools/list","params":{"cursor":"{\\"id\\":1}"}}',
           '{"jsonrpc":"2.0","id":"a|b&c","method":"initialize"}',
           '{"jsonrpc":"2.0","id":9,"method":"other/thing"}']
    env={**os.environ,'GRAINY_MCP_DIR':str(folder),'PATH':bin_folder+os.pathsep+os.environ.get('PATH','')}
    run=subprocess.run([sh,str(folder/'grainy-mcp-bridge.sh')],input=('\n'.join(lines)+'\n').encode(),capture_output=True,
                       env=env,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert run.returncode==0,run.stderr
    replies=[json.loads(line) for line in run.stdout.decode().splitlines()]
    # An array is no JSON-RPC id: that line, like the notification, gets no reply.
    assert [r['id'] for r in replies]==[5,'quote " and \\ backslash',17,8,'a|b&c',9]
    assert replies[0]['result']['isError'] and replies[1]['result']=={} and len(replies[3]['result']['tools'])==12
    assert replies[4]['result']['protocolVersion']==mcp_protocol.OFFLINE_VERSION and replies[5]['error']['code']==-32601


@pytest.mark.skipif(posix_tools() is None,reason='sh and awk are needed')
def test_documented_macos_lines_start_the_bridge_from_home(tmp_path):
    """docs/mcp-install.json's macOS lines in a POSIX shell (Git's on Windows) with HOME in a temporary folder whose
    path has Hangul and spaces: claude and codex get sh and the bridge's path, and Claude Desktop's /bin/sh -c line
    starts it; Grainy is not running, so it answers from the offline files."""
    sh,bin_folder=posix_tools()
    home=tmp_path/'사용자 이름'
    folder=folder_for(home,name='Library/Application Support/Grainy/mcp',script='grainy-mcp-bridge.sh')
    env={**os.environ,'HOME':str(home),'PATH':bin_folder+os.pathsep+os.environ.get('PATH','')}
    env.pop('GRAINY_MCP_DIR',None)
    stand_in='claude() { for a; do printf "%s\\n" "$a"; done; }; codex() { for a; do printf "%s\\n" "$a"; done; }; '
    macos=DOCUMENTED['macos'];windows_flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    for client,head in (('claude_code',['mcp','add','--scope','user','grainy']),('codex',['mcp','add','grainy'])):
        run=subprocess.run([sh,'-c',stand_in+macos[client]],capture_output=True,env=env,timeout=60,creationflags=windows_flags)
        argv=run.stdout.decode('utf-8').splitlines()
        assert run.returncode==0 and argv[:-1]==[*head,'--','sh'],(argv,run.stderr)
        assert argv[-1].endswith('/사용자 이름/Library/Application Support/Grainy/mcp/grainy-mcp-bridge.sh')
    entry=macos['claude_desktop_json']['mcpServers']['grainy']
    command=entry['command'] if Path(entry['command']).is_file() and not WINDOWS else sh
    lines=[message('initialize',1,{'protocolVersion':'2025-06-18'}),message('notifications/initialized'),message('tools/list',2),
           message('tools/call',3,{'name':'list_photos','arguments':{}})]
    for start in ([command,*entry['args']],[sh,argv[-1]]):
        run=subprocess.run(start,input='\n'.join(json.dumps(m) for m in lines).encode()+b'\n',capture_output=True,env=env,timeout=120,
                           creationflags=windows_flags)
        replies=[json.loads(line) for line in run.stdout.decode().splitlines()]
        assert run.returncode==0 and [r['id'] for r in replies]==[1,2,3],run.stderr
        assert replies[0]['result']['protocolVersion']==mcp_protocol.OFFLINE_VERSION and len(replies[1]['result']['tools'])==12
        assert replies[0]['result']['instructions'].endswith(mcp_protocol.OFFLINE_NOTE)
        assert replies[2]['result']['content'][0]['text']==mcp_protocol.OFFLINE_TEXT
    assert folder.is_dir()


@pytest.mark.skipif(WINDOWS or not shutil.which('curl') or not shutil.which('sh'),reason='macOS or Linux with curl')
def test_sh_bridge_forwards_and_answers_offline(server,tmp_path):
    folder=folder_for(tmp_path,server,script='grainy-mcp-bridge.sh')
    env={**os.environ,'GRAINY_MCP_DIR':str(folder),'GRAINY_MCP_CURL':shutil.which('curl')}
    lines=[message('initialize',1,{'protocolVersion':'2025-06-18'}),message('notifications/initialized'),
           message('tools/call',2,{'name':'list_photos','arguments':{'text':'현상'}})]
    run=subprocess.run(['/bin/sh',str(folder/'grainy-mcp-bridge.sh')],input='\n'.join(json.dumps(m) for m in lines).encode()+b'\n',
                       capture_output=True,env=env,timeout=120)
    replies=[json.loads(line) for line in run.stdout.decode().splitlines()]
    assert run.returncode==0 and [r['id'] for r in replies]==[1,2]
    assert json.loads(replies[1]['result']['content'][0]['text'])=={'text':'현상'}
    (folder/'endpoint').unlink()
    run=subprocess.run(['/bin/sh',str(folder/'grainy-mcp-bridge.sh')],input=json.dumps(message('tools/list',3)).encode()+b'\n',
                       capture_output=True,env=env,timeout=120)
    assert json.loads(run.stdout)['id']==3 and len(json.loads(run.stdout)['result']['tools'])==12
