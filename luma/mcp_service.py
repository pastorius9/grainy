"""The AI connection (MCP) inside Grainy: the server's lifetime, calls into the GUI thread, readiness checks,
jobs, and the files the stdio bridges read.

The connection is on while Grainy runs unless the person switched it off (Settings › AI Connection). AI
programs reach it through the bridges in the per-user folder (mcp/), which find the port and the key there:
installing Grainy's MCP takes one command with neither in it. McpService lives as long as the app run
(DesktopSession.mcp) and survives the window rebuild after a language change; McpWindowState (window.mcp)
holds what belongs to one MainWindow.
"""
from collections import OrderedDict,deque
import concurrent.futures
from dataclasses import dataclass,field
import errno
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import sys
import threading
import time

from PySide6.QtCore import QObject,QThread,QThreadPool,QTimer,Qt,Signal,Slot
from PySide6.QtWidgets import QApplication

from . import mcp_protocol,photo_service
from .i18n import tr
from .photo_service import Busy,ServiceError

GRACE_SECONDS=8          # GUI work that already started may finish this long after the request deadline
QUEUE_SECONDS=10         # longest wait for the GUI thread to start a call
WAIT_SECONDS=40          # longest a tool waits for its job before returning the job id
RETRY_SECONDS=60         # a finished job waits this long for Grainy to be ready to write
DEFAULT_PORT=47821
TOKEN_PATTERN=re.compile(r'[0-9a-f]{64}')
STOP_SECONDS=3           # stop() waits this long for requests in flight
CLOSED='Grainy is closing or its AI connection was restarted'
BRIDGE_FILES=('grainy-mcp-bridge.ps1','grainy-mcp-bridge.sh')
CONNECTION_FILES=('endpoint','auth-header')     # where and how the bridges reach Grainy: only while it owns them
OWNER_LOCK='owner.lock'  # locked by the Grainy process that owns CONNECTION_FILES (never deleted: see _lock)


def pictures_folder():
    """Pictures/Grainy, the default folder for AI exports (tests patch this)."""
    from PySide6.QtCore import QStandardPaths
    base=QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation) or str(Path.home()/'Pictures')
    return Path(base)/'Grainy'


class Unfinished(Exception):
    """GUI work started but did not finish before the reply deadline; it may still take effect."""


class GuiCall(QObject):
    """Runs functions on the GUI thread for the server's handler threads. A new one per McpService.start(),
    so threads left over from a previous server see a closed one."""
    request=Signal(object)

    def __init__(self):
        super().__init__();self.pending=set();self.lock=threading.Lock();self.closed=False
        self.request.connect(self._run,Qt.ConnectionType.QueuedConnection)

    def call(self,fn,deadline):
        assert threading.current_thread() is not threading.main_thread()
        future=concurrent.futures.Future()
        with self.lock:
            if self.closed:raise Busy(CLOSED)
            self.pending.add(future)
        self.request.emit((fn,future))
        try:
            try:
                return future.result(max(0.,min(QUEUE_SECONDS,deadline-time.monotonic())))
            except concurrent.futures.TimeoutError:
                if future.cancel():                       # never started: it will never run
                    raise Busy('Grainy did not get to this call in time (it may be busy)') from None
            try:                                          # started: wait for the bounded GUI work
                return future.result(max(.1,deadline+GRACE_SECONDS-time.monotonic()))
            except concurrent.futures.TimeoutError:
                raise Unfinished() from None
        finally:
            with self.lock:self.pending.discard(future)

    @Slot(object)
    def _run(self,item):
        fn,future=item
        if future.done() or not future.set_running_or_notify_cancel():return
        try:value=fn()
        except BaseException as error:                    # never let an exception reach Qt
            try:future.set_exception(error)
            except concurrent.futures.InvalidStateError:pass
        else:
            try:future.set_result(value)
            except concurrent.futures.InvalidStateError:pass

    def close(self):
        """GUI thread, so no call is running now: every waiting caller gets Busy."""
        with self.lock:
            self.closed=True
            for future in self.pending:
                if not future.done():
                    try:future.set_exception(Busy(CLOSED))
                    except concurrent.futures.InvalidStateError:pass


# --- readiness ------------------------------------------------------------------------------------

def nested():
    return bool(QApplication.activeModalWidget() or QApplication.activePopupWidget() or QThread.currentThread().loopLevel()>1)


def ready(w,*,write=False,export=False,ids=(),selection=False,job=None):
    """Raise Busy (English reason) unless Grainy can take this request now. GUI thread; nothing is written
    before this passes. job: the MCP job that is finishing (it does not count as running)."""
    if w is None or getattr(w,'closing',True):raise Busy('Grainy is closing or reopening its window')
    if not (write or export):return w
    if nested():raise Busy('a dialog or menu is open in Grainy; ask the person to close it')
    if w.catalog.db.in_transaction:raise Busy('Grainy is saving something else')
    if getattr(w,'command_running',False):raise Busy('a Codex command is running')
    if getattr(w,'maintenance_running',False):raise Busy('a catalog task is running')
    if w.import_busy or w.import_scans:raise Busy('photos are being imported')
    if export and w.export_running:raise Busy('an export is already running')
    state=getattr(w,'mcp',None)
    running=state.service.job if state is not None else None
    if running is not None and running.state=='running' and running is not job:
        raise Busy(f'job {running.id} ({running.tool}) is still running; call get_job')
    if write and w.current_id is not None and w.current_id in ids:
        if w.interacting():raise Busy('the person is adjusting the open photo')
        if w.studio.pending_stroke is not None:raise Busy('the open photo has unfinished brush or healing marks')
        if w.view.crop_mode or w.view.sample_mode:raise Busy('a crop or picker tool is open on the open photo')
    if write and selection and w.library_loading:raise Busy('the photo list is still loading')
    return w


def safe_commit(w):
    """Save the open photo's pending change when nothing is in the middle of it. True when saved (or nothing
    was pending); False leaves the caller with the last saved state."""
    if w.settings==w.last_saved:return True
    if w.interacting() or QThread.currentThread().loopLevel()!=1 or w.catalog.db.in_transaction:return False
    w.commit();return True


# --- jobs -----------------------------------------------------------------------------------------

@dataclass
class Job:
    id:str
    tool:str
    total:int
    generation:int
    done:int=0
    state:str='running'          # running | done | failed | cancelled
    phase:str|None=None          # 'waiting for Grainy' while the finish waits for readiness
    result:dict|None=None
    error:str|None=None
    stopped:bool=False           # cancelled with the status bar's stop button
    cancel:threading.Event=field(default_factory=threading.Event)
    finished:threading.Event=field(default_factory=threading.Event)

    def view(self):
        return {'job':self.id,'tool':self.tool,'state':self.state,'done':self.done,'total':self.total,
                **({'phase':self.phase} if self.phase and self.state=='running' else {}),
                **(self.result or {}),**({'error':self.error} if self.error else {})}


def capped(errors,limit=20):
    """Per-photo error lists: at most 20 entries, plus more_errors."""
    return {'errors':errors[:limit],**({'more_errors':len(errors)-limit} if len(errors)>limit else {})}


# --- files the bridges read -----------------------------------------------------------------------

def _private_folder(folder):
    folder.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':os.chmod(folder,0o700)


def _write_private(path,text):
    """Write a small file atomically; on POSIX readable by this user only (0600 from the start)."""
    temporary=path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    data=text.encode('utf-8')
    try:
        if os.name=='nt':temporary.write_bytes(data)
        else:
            handle=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(handle,'wb') as output:output.write(data)
        os.replace(temporary,path)
    finally:
        try:temporary.unlink()
        except OSError:pass


HELD={errno.EACCES,errno.EAGAIN,errno.EWOULDBLOCK,getattr(errno,'EDEADLK',errno.EACCES),getattr(errno,'EDEADLOCK',errno.EACCES)}


def _lock(path):
    """The open handle of path holding an exclusive lock, or None when another process (or another handle in
    this one) holds it; OSError when the file cannot be opened. The operating system drops the lock when the
    process ends, so a Grainy that crashed never keeps it. The file is never deleted: a process that opened
    it just before a deletion would lock a file the next one no longer sees. On a file system that cannot
    lock at all the handle comes back unlocked (the files are then owned as they were before this lock)."""
    handle=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle,msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in HELD:
            os.close(handle);return None
    return handle


def _unlock(handle):
    try:
        if os.name=='nt':
            import msvcrt
            os.lseek(handle,0,os.SEEK_SET);msvcrt.locking(handle,msvcrt.LK_UNLCK,1)
    except OSError:pass
    finally:os.close(handle)


def bridge_source():
    return Path(__file__).resolve().parents[1]/'assets'/'mcp'


class McpService(QObject):
    def __init__(self,session):
        super().__init__()
        from .mcp_tools import Tools
        self.session=session;self.server=None;self.running=False;self.error=None
        self.generation=0;self.gui=None;self.snapshotted=set()
        self.job=None;self.jobs=OrderedDict();self.job_count=0;self.activity=deque(maxlen=50)
        self.calls=threading.BoundedSemaphore(4);self.render_lock=threading.BoundedSemaphore(1)
        self.tools=Tools(self);self.port_changed=False
        # owner: the locked handle of mcp/owner.lock while this process owns endpoint and auth-header. Another Grainy
        # (another library, the same settings) may be running; elsewhere: it has the connection on.
        self.owner=None;self.elsewhere=False

    # settings
    @property
    def preferences(self):return self.session.preferences

    @property
    def folder(self):return Path(self.preferences.path).parent/'mcp'

    @property
    def enabled(self):return self.preferences.values.get('mcp_enabled') is not False      # on unless switched off

    @property
    def port(self):
        value=self.preferences.values.get('mcp_port')
        return value if isinstance(value,int) and not isinstance(value,bool) and 1024<=value<=65535 else None

    @property
    def token(self):
        value=self.preferences.values.get('mcp_token')
        return value if isinstance(value,str) and TOKEN_PATTERN.fullmatch(value) else None

    @property
    def bound_port(self):return self.server.server_address[1] if self.server is not None else None

    @property
    def directory(self):return Path(self.session.data_dir)

    def window(self):return self.session.window

    def export_root(self):
        saved=self.preferences.values.get('mcp_export_folder')
        if isinstance(saved,str) and saved and Path(saved).is_dir():return Path(saved)
        return pictures_folder()

    def note(self,name,outcome,ms=0):
        """One line of the dialog's recent activity; never a header value or the key. outcome is a short English
        code ('ok', 'started', 'job j1 done', ...): the dialog shows it in the interface language (mcp_dialog)."""
        self.activity.append((time.strftime('%H:%M:%S'),str(name)[:64],str(outcome)[:200]))

    # lifetime
    def ensure_secrets(self):
        if self.token is None:self.preferences.save('mcp_token',secrets.token_hex(32))

    def start(self,port=None):
        if self.running:return True
        try:self.ensure_secrets()
        except OSError:pass
        if self.token is None:
            self.error=tr('AI 연결 설정을 저장하지 못했습니다.')
            self.note('server','could not start');self.update_windows();return False
        self.elsewhere=not self.claim()          # before binding: copies a crash left behind are gone even if it fails
        if self.elsewhere:
            self.error=tr('이 컴퓨터에서 실행 중인 다른 Grainy가 AI 연결을 쓰고 있습니다.')
            self.note('server','on in another Grainy');self.update_windows();return False
        # Also before binding: when the port cannot be used, the installed bridges still answer that Grainy cannot
        # be reached and how to fix it.
        bridges=self.write_bridges()
        from . import mcp_http
        first=port is None and self.port is None
        chosen=port if port is not None else (self.port or DEFAULT_PORT)
        self.gui=GuiCall()                        # bound to the server: its requests never use a later one
        try:server=mcp_http.start(chosen,self,self.gui)
        except OSError as error:
            server=None;failure=error
            if first:
                for _ in range(20):
                    candidate=random.randint(40000,48999)
                    try:server=mcp_http.start(candidate,self,self.gui);break
                    except OSError as error:failure=error
            if server is None:
                self.gui.close();self.gui=None;self.release()
                self.error=tr('포트 {0}을(를) 쓸 수 없습니다: {1}',chosen,failure.strerror or type(failure).__name__)
                self.note('server','could not start');self.update_windows();return False
        self.server=server;self.running=True;self.generation+=1;self.error=None
        if first:
            try:self.preferences.save('mcp_port',self.bound_port)
            except OSError:self.note('server','port not saved')
        if bridges:self.write_connection()
        self.note('server','started');self.update_windows()
        return True

    def start_if_enabled(self):
        """At start-up: on unless the person switched it off. Quiet, and never in the way of Grainy starting: no
        dialog, no message and no exception; a failure is the status in Settings › AI Connection and a line of
        its recent activity."""
        try:
            if not self.enabled:return self.remove_stale()
            self.start()
        except Exception as error:
            try:self.stop()
            except Exception:pass
            self.error=tr('AI 연결을 시작하지 못했습니다: {0}',type(error).__name__)
            self.note('server','could not start')
            try:self.update_windows()
            except Exception:pass

    def stop(self):
        """Idempotent; GUI thread. A running job is cancelled; waiting calls get Busy. endpoint and auth-header go
        only when this process owns them, and before the port is released: they never name a port that another
        program could take meanwhile."""
        was=self.running or self.server is not None
        self.running=False;self.generation+=1
        if self.job is not None and self.job.state=='running':self.job.cancel.set()
        if self.gui is not None:self.gui.close()
        if self.owner is not None:self.remove_files()
        server=self.server;self.server=None
        if server is not None:
            server.shutdown();server.close_connections();server.server_close()
            deadline=time.monotonic()+STOP_SECONDS
            while server.in_flight and time.monotonic()<deadline:time.sleep(.02)
        self.release()
        if was:self.note('server','stopped')
        self.update_windows()

    # the files the bridges read: owned by one Grainy process at a time
    def claim(self):
        """Own endpoint and auth-header; False when another running Grainy owns them (its connection is on).
        Copies that no running Grainy owns (left by a crash) are removed. True without owning them only when the
        folder cannot be used at all (start() then runs without the bridge files)."""
        if self.owner is not None:return True
        try:
            _private_folder(self.folder);handle=_lock(self.folder/OWNER_LOCK)
        except OSError:return True
        if handle is None:return False
        self.owner=handle;self.remove_files()
        return True

    def release(self):
        handle,self.owner=self.owner,None
        if handle is not None:_unlock(handle)

    def remove_files(self):
        for name in CONNECTION_FILES:
            try:(self.folder/name).unlink()
            except OSError:pass

    def remove_stale(self):
        """Connection off: endpoint and auth-header that no running Grainy owns, left by a crash, are removed."""
        if not any((self.folder/name).exists() for name in CONNECTION_FILES):return
        if self.claim():self.release()

    def set_enabled(self,on):
        """The person's switch in Settings › AI Connection; remembered, so off stays off at the next start."""
        try:self.preferences.save('mcp_enabled',bool(on))
        except OSError:
            self.error=tr('AI 연결 설정을 저장하지 못했습니다.')
            if on:self.update_windows();return False
        else:self.error=None                     # an earlier failure no longer applies
        if not on:self.stop();return True
        return self.start()

    def regenerate_token(self):
        self.preferences.save('mcp_token',secrets.token_hex(32))      # OSError goes to the caller
        self.note('key','new key')
        if self.running and self.write_bridges():self.write_connection()

    def change_port(self):
        old=self.bound_port or self.port
        self.stop()
        for _ in range(20):
            candidate=random.randint(40000,48999)
            if candidate==old:continue
            if self.start(candidate):
                try:self.preferences.save('mcp_port',candidate)
                except OSError:self.error=tr('AI 연결 설정을 저장하지 못했습니다.')
                self.port_changed=True;self.update_windows()
                return True
        return self.start()

    def write_bridges(self):
        """The bridges the AI programs start and their answers while Grainy cannot be reached; on every start of
        the connection, so an update brings its new versions. True when they are in place."""
        folder=self.folder
        try:
            _private_folder(folder)
            source=bridge_source()
            for name in BRIDGE_FILES:
                origin=source/name
                if origin.is_file():
                    target=folder/name
                    data=origin.read_bytes()
                    if not target.is_file() or target.read_bytes()!=data:_write_private(target,data.decode('ascii'))
            for name,text in mcp_protocol.offline_templates().items():
                target=folder/name
                if not target.is_file() or target.read_text(encoding='utf-8')!=text:_write_private(target,text)
            return True
        except OSError:
            self.note('files','could not write the bridge files');return False

    def write_connection(self):
        """While running and owning them: where and how the bridges reach Grainy."""
        if not (self.running and self.server is not None and self.token and self.owner is not None):return False
        try:
            _write_private(self.folder/'endpoint',f'http://127.0.0.1:{self.bound_port}/mcp/stdio')
            _write_private(self.folder/'auth-header',f'Authorization: Bearer {self.token}')
            return True
        except OSError:
            self.note('files','could not write the bridge files');return False

    def bridge_path(self):
        return self.folder/('grainy-mcp-bridge.ps1' if sys.platform=='win32' else 'grainy-mcp-bridge.sh')

    # windows
    def attach(self,window):
        window.mcp=McpWindowState(self,window)

    def update_windows(self):
        w=self.window()
        state=getattr(w,'mcp',None) if w is not None else None
        if state is not None:state.update()

    # jobs
    def new_job(self,tool,total):
        self.job_count+=1
        job=Job(f'j{self.job_count}',tool,total,self.generation)
        self.jobs[job.id]=job
        while len(self.jobs)>20:self.jobs.popitem(last=False)
        self.job=job;self.update_windows()
        return job

    def end_job(self,job,state,*,result=None,error=None):
        """GUI thread. Marks the job finished once."""
        if job.state!='running':return
        job.state=state;job.phase=None
        if result is not None:job.result=result
        if error is not None:job.error=error
        job.finished.set()
        if self.job is job:self.job=None
        self.note(job.tool,f'job {job.id} {state}')
        self.update_windows()

    def stale(self,job):
        """Why a finished computation must not be written, or None."""
        if job.cancel.is_set() and job.stopped:return 'stopped in Grainy; nothing was changed'
        if job.cancel.is_set() or job.generation!=self.generation or not self.running:
            return 'the AI connection was turned off or restarted; nothing was changed'
        return None

    def start_auto(self,w,kind,records,*,label,tool,amount=1.,extra_errors=()):
        """GUI thread, after ready(): compute automatic edits in the window's MCP pool; written when done."""
        job=self.new_job(tool,len(records))
        load,compute=photo_service.auto_job(kind,records,amount)
        directory=w.catalog.directory;state=w.mcp

        def work():
            values,errors=photo_service.auto_values(load,compute,directory,cancel=job.cancel,
                progress=lambda n:setattr(job,'done',n),strict=False)
            if values is None:return None
            if kind=='roll':
                from . import negative
                good=[(r,v) for r,v in zip(records,values) if v is not None]
                if len(good)<2:return {'error':'a roll needs at least 2 photos that Grainy can read; nothing was changed',
                                       'errors':errors}
                converted=negative.converted_together([v for _,v in good],[r['settings'] for r,_ in good])
                results=[(r,s,'applied') for (r,_),s in zip(good,converted)]
            else:
                results=[(r,v[0],(v[1] or {}).get('status','applied')) for r,v in zip(records,values) if v is not None]
            return {'results':results,'errors':errors}

        first_try=[]

        def finish(outcome):
            reason=self.stale(job) if outcome is not None else (self.stale(job) or 'stopped; nothing was changed')
            if reason:return self.end_job(job,'cancelled',error=reason)
            if 'error' in outcome:return self.end_job(job,'failed',error=outcome['error'])
            w=self.window()
            ids=[r['id'] for r,_,_ in outcome['results']]
            if not first_try:first_try.append(time.monotonic())
            try:ready(w,write=True,ids=ids,job=job)
            except Busy as busy:
                # A dialog, a drag or another task: try again every half second for up to a minute.
                if time.monotonic()-first_try[0]<RETRY_SECONDS:
                    job.phase='waiting for Grainy'
                    QTimer.singleShot(500,w if w is not None else self,lambda:guarded(finish,outcome))
                    return
                return self.end_job(job,'failed',error=f'{busy}; nothing was changed. Run it again.')
            if w.current_id in ids:w.commit();w.save_keywords()
            used=[];values=[];meanwhile=[];statuses={}
            for record,settings,status in outcome['results']:
                now=w.catalog.photo(record['id'])
                if now is None or now['settings']!=record['settings'] or now['path']!=record['path']:
                    meanwhile.append(record['id']);continue
                used.append(record);values.append(settings);statuses.setdefault(status,[]).append(record['id'])
            snapshot=tr('AI 작업 전 · {0}',time.strftime('%Y-%m-%d %H:%M'))
            bid,changed=photo_service.apply_batch(w.catalog,used,values,label=label,tool=tool,snapshot_name=snapshot,
                                                  snapshotted=self.snapshotted) if used else (None,[])
            if changed:w.refresh_command_edits(changed,label)
            result={'batch':bid,'changed':len(changed),'unchanged':len(used)-len(changed)}
            if kind=='tone':result['flat']=statuses.get('flat',[])
            if kind=='color':result['skipped']={k:statuses.get(k,[]) for k in ('neutral','monochrome','flat')}
            result['edited_meanwhile']=meanwhile
            result.update(capped(list(extra_errors)+outcome['errors']))
            self.end_job(job,'done',result=result)

        def guarded(step,value):
            try:step(value)
            except Exception as error:
                last=str(error).strip().splitlines()[-1] if str(error).strip() else type(error).__name__
                self.end_job(job,'failed',error=f'could not finish: {last}')

        def failed(error):
            last=str(error).strip().splitlines()[-1] if str(error).strip() else 'unknown error'
            try:self.end_job(job,'failed',error=f'could not read or compute: {last}')
            except Exception:pass

        w.spawn(work,lambda outcome:guarded(finish,outcome),failed,pool=state.pool)
        return job

    def start_export(self,w,ids,folder,options,extra_errors=()):
        """GUI thread, after ready(export=True): the window's export with this job's cancel and progress."""
        job=self.new_job('export_photos',len(ids))

        def done(result):
            try:
                failures=result.get('failures',[])
                outcome={'exported':len(result.get('outputs',[])),'failed':len(failures),'skipped':result.get('skipped',0),
                         'folder':str(folder),'failures':failures[:20]}
                if extra_errors:outcome.update(capped(list(extra_errors)))
                self.end_job(job,'done' if not job.cancel.is_set() else 'cancelled',result=outcome,
                             error=None if not job.cancel.is_set() else 'stopped; files already written stay')
            except Exception as error:
                self.end_job(job,'failed',error=f'could not finish: {type(error).__name__}')

        def failed(error):
            last=str(error).strip().splitlines()[-1] if str(error).strip() else 'unknown error'
            try:self.end_job(job,'failed',error=f'export failed: {last}')
            except Exception:pass

        try:
            started=w.start_export(ids,folder,options['format'],options['quality'],options['longest'],options['color_space'],
                options['keep_metadata'],options['naming'],True,True,remember=False,on_done=done,on_error=failed,
                cancel=job.cancel,progress=lambda n:setattr(job,'done',n))
        except Exception:
            self.end_job(job,'failed',error='the export could not start');raise
        if not started:
            self.end_job(job,'failed',error='an export is already running')
            raise Busy('an export is already running')
        return job


class McpWindowState:
    """What the AI connection adds to one MainWindow: its menu entry, status-bar widgets, worker pool, dialog."""
    def __init__(self,service,window):
        from PySide6.QtWidgets import QMenu
        from .app import button
        self.service=service;self.window=window;self.dialog=None
        self.pool=QThreadPool(window);self.pool.setMaxThreadCount(1)
        menu=window.findChild(QMenu,'settingsMenu')
        self.action=menu.addAction(tr('AI 연결 (MCP)…'))
        self.action.triggered.connect(lambda checked=False:self.open_dialog())
        # Nothing shows while the connection merely waits (as with other apps' MCPs); a running AI task shows its Stop button.
        self.stop_button=button(tr('AI 작업 중단'),self.stop_job)
        window.statusBar().addPermanentWidget(self.stop_button)
        self.update()

    def update(self):
        job=self.service.job
        self.stop_button.setVisible(job is not None and job.state=='running')
        if self.dialog is not None and self.dialog.isVisible():self.dialog.refresh()

    def stop_job(self):
        job=self.service.job
        if job is not None and job.state=='running':
            job.stopped=True;job.cancel.set();self.service.note(job.tool,f'job {job.id} stopped in Grainy')

    def open_dialog(self):
        from .mcp_dialog import McpDialog
        if self.dialog is None:self.dialog=McpDialog(self.service,self.window)
        self.dialog.refresh();self.dialog.show();self.dialog.raise_();self.dialog.activateWindow()

    def shutdown(self):
        """From closeEvent: a running job is cancelled (it has written nothing) and the pool emptied."""
        job=self.service.job
        if job is not None and job.state=='running':
            job.cancel.set()
            self.service.end_job(job,'cancelled',error='Grainy closed or reopened its window; nothing was changed')
        self.pool.clear();self.pool.waitForDone(10000)
        if self.dialog is not None:self.dialog.close()
