"""ChatGPT account access through an isolated, local Codex app-server.

Credentials belong to Codex; Luma never
reads auth.json, copies another client's login, or sends photos during login.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from . import __version__


def default_auth_home():
    from .user_paths import local_data
    return local_data('Luma', Path.home() / '.local' / 'share') / 'Codex'


def _find_codex_posix(configured=None):
    """macOS: the native program inside the npm package (its `codex` command is a Node script, which an app
    started from Finder cannot run: its PATH has no node), or a program installed as `codex` itself."""
    def usable(path):return path.is_file() and os.access(path,os.X_OK) and path.suffix not in ('.js','.mjs')
    if configured:
        path=Path(configured)
        return str(path.resolve()) if usable(path) else None
    # Finder gives apps a short PATH; npm's and Homebrew's usual places are checked as well.
    places=[shutil.which('codex'),'/opt/homebrew/bin/codex','/usr/local/bin/codex',str(Path.home()/'.npm-global'/'bin'/'codex')]
    for place in dict.fromkeys(p for p in places if p):
        command=Path(place)
        if not command.exists():continue
        real=command.resolve()
        for folder in real.parents:
            if folder.name=='codex' and folder.parent.name=='@openai':
                for native in sorted(folder.glob('vendor/*/codex/codex'))+sorted(folder.glob('node_modules/@openai/codex-darwin-*/vendor/*/codex/codex')):
                    if usable(native):return str(native.resolve())
        with real.open('rb') as handle:script=handle.read(2)==b'#!'
        if usable(real) and not script:return str(real)
    return None


def find_codex(configured=None):
    if os.name!='nt':return _find_codex_posix(configured)
    if configured:
        path=Path(configured)
        return str(path.resolve()) if path.is_file() and path.suffix.lower()=='.exe' else None
    native=shutil.which('codex.exe')
    if native:
        return native
    roots=[Path(os.environ.get('APPDATA',Path.home()))/'npm']
    for name in ('codex','codex.cmd','codex.ps1'):
        found=shutil.which(name)
        if found:
            roots.append(Path(found).parent)
    for root in dict.fromkeys(roots):
        package=root/'node_modules'/'@openai'/'codex'
        for pattern in ('node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe',
                        'vendor/*/codex/codex.exe'):
            for path in sorted(package.glob(pattern)):
                if path.is_file():
                    return str(path.resolve())
    return None


def valid_login_url(value):
    try:
        url=urlsplit(value)
        return (url.scheme=='https' and url.hostname in {'auth.openai.com','chatgpt.com','auth0.openai.com'}
                and url.port in (None,443) and not url.username and not url.password)
    except (ValueError,TypeError):
        return False


def usage_lines(payload):
    """Unknown usage stays unknown; usedPercent is consumed, not remaining."""
    buckets=payload.get('rateLimitsByLimitId') or {}
    if not buckets:
        legacy=payload.get('rateLimits')
        buckets={'codex':legacy} if isinstance(legacy,dict) else {}
    lines=[]
    for ident,bucket in buckets.items():
        if not isinstance(bucket,dict):
            continue
        name=bucket.get('limitName') or ('Codex' if ident=='codex' else str(ident))
        for key,label in (('primary','기본 한도'),('secondary','추가 한도')):
            window=bucket.get(key)
            if not isinstance(window,dict):
                continue
            duration=window.get('windowDurationMins')
            if isinstance(duration,(int,float)) and duration>0:
                label=f'{duration/1440:g}일' if duration>=1440 else (f'{duration/60:g}시간' if duration>=60 else f'{duration:g}분')
            used=window.get('usedPercent')
            remaining=f'{max(0,min(100,100-used)):g}% 남음' if isinstance(used,(int,float)) and math.isfinite(used) else '사용량 확인 불가'
            reset=''
            stamp=window.get('resetsAt')
            if isinstance(stamp,(int,float)):
                try:
                    reset=' · '+datetime.fromtimestamp(stamp).strftime('%m/%d %H:%M')+' 초기화'
                except (ValueError,OverflowError,OSError):
                    pass
            lines.append(f'{name} · {label}: {remaining}{reset}')
        if bucket.get('rateLimitReachedType'):
            lines.append(f'{name} · 사용 한도에 도달했습니다.')
    return lines or ['사용량 정보를 아직 제공받지 못했습니다.']


class CodexAuth(QObject):
    changed=Signal()
    browserRequested=Signal(str)
    notification=Signal(str,object)

    def __init__(self,home=None,executable=None,parent=None,command=None):
        super().__init__(parent)
        self.home=Path(home or default_auth_home()).resolve()
        self.executable=executable
        self._command=command  # Injectable transport for protocol tests.
        self.phase='stopped'
        self.message='ChatGPT 계정을 연결해 주세요.'
        self.account=None
        self.limits={}
        self.limits_error=''
        self.login_id=None
        self.login_url=''
        self._buffer=b''
        self._pending={}
        self._next_id=0
        self._account_revision=0
        self._ready=False
        self._closing=False
        self._cancel_on_start=False
        self._early_login=None
        self.process=QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.started.connect(self._initialize)
        self.process.readyReadStandardOutput.connect(self._read)
        # Never persist or expose the server's raw diagnostics or OAuth URLs.
        self.process.readyReadStandardError.connect(lambda:self.process.readAllStandardError())
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._exited)
        self.watchdog=QTimer(self)
        self.watchdog.setInterval(500)
        self.watchdog.timeout.connect(self._expire_requests)
        self.login_timer=QTimer(self)
        self.login_timer.setSingleShot(True)
        self.login_timer.setInterval(180000)
        self.login_timer.timeout.connect(self._login_expired)

    @property
    def connected(self):
        return bool(self.account and self.account.get('type')=='chatgpt')

    @property
    def busy(self):
        return self.phase in {'starting','checking','login_start','waiting','cancelling','logging_out'}

    def _state(self,phase,message):
        self.phase,self.message=phase,message
        self.changed.emit()

    def start(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            return
        executable=find_codex(self.executable) if self._command is None else None
        if self._command is None and not executable:
            self._state('missing','Codex 실행 파일을 찾지 못했습니다. 설치된 codex.exe를 선택해 주세요.')
            return
        try:
            self.home.mkdir(parents=True,exist_ok=True)
        except OSError:
            self._state('error','로그인 저장소를 열지 못했습니다. 폴더 접근 권한을 확인해 주세요.')
            return
        self._closing=False
        self._buffer=b''
        self._pending.clear()
        self._ready=False
        self._clear_login()
        self.account=None
        self.limits={}
        env=QProcessEnvironment.systemEnvironment()
        # This home is independent from the user's desktop/CLI Codex session.
        env.insert('CODEX_HOME',str(self.home))
        for key in ('OPENAI_API_KEY','CODEX_API_KEY','OPENAI_BASE_URL','CODEX_ACCESS_TOKEN'):
            env.remove(key)
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(self.home))
        command=self._command or [executable,'app-server','--listen','stdio://',
            '-c','forced_login_method="chatgpt"','-c','cli_auth_credentials_store="file"']
        self._state('starting','연결 준비 중…')
        self.watchdog.start()
        self.process.start(command[0],command[1:])

    def _send(self,message):
        self.process.write((json.dumps(message,ensure_ascii=False)+'\n').encode('utf-8'))

    def _request(self,method,params,success,failure=None,timeout=30):
        self._next_id+=1
        self._pending[self._next_id]=(time.monotonic()+timeout,success,failure or self._request_failed)
        self._send({'id':self._next_id,'method':method,'params':params})

    def _initialize(self):
        self._request('initialize',{'clientInfo':{'name':'luma_photo_studio','title':'Luma Photo Studio','version':__version__}},self._initialized)

    def _initialized(self,_):
        self._send({'method':'initialized','params':{}})
        self._ready=True
        self.refresh()

    def refresh(self):
        if not self._ready:
            self.start()
        elif not self.busy or self.phase=='starting':
            self.limits={}
            self.limits_error=''
            self._state('checking','계정 확인 중…')
            self._request('account/read',{'refreshToken':False},self._account_read,self._account_failed)

    def _account_read(self,result):
        account=result.get('account')
        # Keep only display metadata, never arbitrary server fields or tokens.
        self.account={k:account.get(k) for k in ('type','email','planType')} if isinstance(account,dict) and account.get('type')=='chatgpt' else None
        self._account_revision+=1
        revision=self._account_revision
        if self.connected:
            self._state('connected','ChatGPT 계정이 연결되었습니다.')
            self._request('account/rateLimits/read',{},
                lambda result:self._limits_read(result) if revision==self._account_revision else None,
                lambda:self._limits_failed() if revision==self._account_revision else None)
        else:
            self.limits={}
            self._state('signed_out','ChatGPT 계정을 연결해 주세요.')

    def _account_failed(self):
        self._account_revision+=1
        self.account=None
        self.limits={}
        self._state('error','계정을 확인하지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요.')

    def _limits_read(self,result):
        if self.connected:
            self.limits=result
            self.limits_error=''
            self.changed.emit()

    def _limits_failed(self):
        if self.connected:
            self.limits={}
            self.limits_error='사용량을 확인하지 못했습니다. 잠시 후 새로고침해 주세요.'
            self.changed.emit()

    def login(self):
        if not self._ready or self.busy or self.connected:
            return
        self._cancel_on_start=False
        self._early_login=None
        self._state('login_start','로그인 페이지 준비 중…')
        self._request('account/login/start',{'type':'chatgpt'},self._login_started)

    def _login_started(self,result):
        if result.get('type')!='chatgpt' or not result.get('loginId') or not valid_login_url(result.get('authUrl')):
            self.shutdown()
            self._state('error','올바른 OpenAI 로그인 주소를 받지 못했습니다. 다시 연결해 주세요.')
            return
        self.login_id=result['loginId']
        self.login_url=result['authUrl']
        if self._cancel_on_start:
            self.cancel_login()
            return
        self._state('waiting','브라우저에서 로그인을 완료해 주세요.')
        self.login_timer.start()
        if self._early_login:
            self._login_completed(self._early_login)
            self._early_login=None
        if self.phase=='waiting':
            self.browserRequested.emit(self.login_url)

    def _login_completed(self,params):
        if self.phase=='login_start' and not self.login_id:
            self._early_login=params
            return
        if not self.login_id or params.get('loginId')!=self.login_id:
            return
        cancelling=self.phase=='cancelling'
        self._clear_login()
        if params.get('success') or cancelling:
            self.phase='signed_out'
            self.refresh()
        else:
            self._state('error','로그인이 완료되지 않았습니다. 다시 로그인해 주세요.')

    def _clear_login(self):
        self.login_timer.stop()
        self.login_id=None
        self.login_url=''

    def cancel_login(self):
        if self.phase=='login_start' and not self.login_id:
            self._cancel_on_start=True
            self._state('login_start','로그인 취소 중…')
        elif self.login_id and self.phase!='cancelling':
            ident=self.login_id
            self._state('cancelling','로그인 취소 중…')
            self._request('account/login/cancel',{'loginId':ident},self._cancelled,self._cancel_failed)

    def _cancelled(self,_):
        self._clear_login()
        self.phase='signed_out'
        self.refresh()

    def _cancel_failed(self):
        # Stopping this dedicated server also closes its local OAuth callback.
        self.shutdown()
        self._state('error','로그인 연결을 닫았습니다. 새로고침 후 다시 연결할 수 있습니다.')

    def _login_expired(self):
        self.shutdown()
        self._state('error','로그인 대기 시간이 지났습니다. 새로고침 후 다시 로그인해 주세요.')

    def logout(self):
        if not self._ready or self.busy or not self.connected:
            return
        self._account_revision+=1
        self.limits={}
        self._state('logging_out','연결 해제 중…')
        self._request('account/logout',{},self._logged_out)

    def _logged_out(self,_):
        self.account=None
        self.limits={}
        self._state('signed_out','Luma의 ChatGPT 연결을 해제했습니다.')

    def _request_failed(self):
        if not self._ready:
            self.shutdown()
        self._clear_login()
        self._state('error','요청을 완료하지 못했습니다. 네트워크를 확인하고 다시 시도해 주세요.')

    def _read(self):
        self._buffer+=bytes(self.process.readAllStandardOutput())
        if len(self._buffer)>2_000_000:
            self.shutdown()
            self._state('error','연결 응답을 읽지 못했습니다. 다시 연결해 주세요.')
            return
        while b'\n' in self._buffer:
            line,self._buffer=self._buffer.split(b'\n',1)
            try:
                message=json.loads(line)
            except (ValueError,UnicodeDecodeError):
                continue
            if not isinstance(message,dict):
                continue
            if 'method' in message:
                method,params=message['method'],message.get('params') or {}
                if 'id' in message:
                    # The command window only accepts structured text proposals.
                    # Requests to execute tools or change files are not approved.
                    self._send({'id':message['id'],'error':{'code':-32601,'message':'Unsupported method'}})
                elif method=='account/login/completed':
                    self._login_completed(params)
                elif method=='account/updated' and not self.busy:
                    self.refresh()
                elif method=='account/rateLimits/updated':
                    self._limits_read(params)
                else:
                    self.notification.emit(method,params)
            elif message.get('id') in self._pending:
                _,success,failure=self._pending.pop(message['id'])
                if 'error' in message:
                    failure()
                elif isinstance(message.get('result'),dict):
                    success(message['result'])
                else:
                    failure()

    def _expire_requests(self):
        expired=[key for key,value in self._pending.items() if value[0]<time.monotonic()]
        if expired:
            # Login-start may have created a callback even without a response.
            self.shutdown()
            self._state('error','연결 응답 시간이 초과되었습니다. 새로고침 후 다시 시도해 주세요.')

    def _process_error(self,_):
        if not self._closing:
            self._ready=False
            self._pending.clear()
            self.watchdog.stop()
            self._clear_login()
            self.account=None
            self.limits={}
            self._state('error','Codex 연결을 실행하지 못했습니다. 실행 파일을 확인하고 다시 시도해 주세요.')

    def _exited(self,*_):
        self._ready=False
        self._pending.clear()
        self.watchdog.stop()
        self._clear_login()
        if not self._closing:
            self.account=None
            self.limits={}
            self._state('error','ChatGPT 연결이 종료되었습니다. 새로고침으로 다시 연결해 주세요.')

    def shutdown(self):
        self._account_revision+=1
        self._closing=True
        self._ready=False
        self.watchdog.stop()
        self._pending.clear()
        self._clear_login()
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            self.process.closeWriteChannel()
            if not self.process.waitForFinished(1000):
                self.process.terminate()
                if not self.process.waitForFinished(1000):
                    self.process.kill()
                    self.process.waitForFinished(1000)
        self.process.close()
        self.account=None
        self.limits={}
        self._state('stopped','ChatGPT 연결이 닫혔습니다.')
