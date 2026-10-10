"""Updates from the project's GitHub releases.

Checking: one HTTPS request for the newest release's description; nothing about the user is sent. It
runs only when the user asked for it (the menu command) or switched on the automatic check.
Installing: the release zip is downloaded, verified against the SHA-256 GitHub publishes for it, and
unpacked next to the app. The new version then replaces the old one after the app has closed: the
previous version is moved aside (never deleted here) and restored if anything fails. Photo libraries
are not touched.

Windows replaces the files of the release folder and works in its .update folder. macOS replaces the
whole Grainy.app bundle (Grainy-<version>-macos-<arm64|x64>.zip) and works in Application
Support/Grainy/Update, because a bundle must not be changed from the inside."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from uuid import uuid4
from . import __version__
from .i18n import tr

REPOSITORY = 'pastorius9/grainy'
FEED = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
PAGE = f'https://github.com/{REPOSITORY}/releases/latest'
DOWNLOADS = f'https://github.com/{REPOSITORY}/releases/download/'
ASSET = re.compile(r'^Grainy-(\d+(?:\.\d+)+)-windows-x64\.zip$')
ITEMS = ('Grainy.exe', '_internal', 'THIRD_PARTY_LICENSES', 'LICENSE.txt')     # everything a release folder holds
MAC = sys.platform == 'darwin'
BUNDLE = 'Grainy.app'
PROGRAM = 'Contents/MacOS/Grainy'          # inside the bundle
INTERVAL = 24*3600
TIMEOUT = 20
FREE_BYTES = 1024**3
EXIT_WAIT = 120
# Other systems have no installer here and get no update commands.
SUPPORTED = sys.platform in ('win32', 'darwin')


def asset_pattern():
    """The release file for this system."""
    if not MAC:return ASSET
    machine = {'arm64': 'arm64', 'x86_64': 'x64'}.get(platform.machine(), 'unknown')
    return re.compile(rf'^Grainy-(\d+(?:\.\d+)+)-macos-{machine}\.zip$')


def version_key(text):
    return tuple(int(part) for part in re.findall(r'\d+', str(text)))


def newer(candidate, current=__version__):
    return version_key(candidate) > version_key(current)


def feed():
    """The releases feed; GRAINY_UPDATE_FEED (a URL or a local JSON file) replaces it for testing."""
    return os.environ.get('GRAINY_UPDATE_FEED') or FEED


def fetch(url, opener=urllib.request.urlopen):
    if '://' not in url:return Path(url).read_bytes()
    request = urllib.request.Request(url, headers={'User-Agent': f'Grainy/{__version__}', 'Accept': 'application/vnd.github+json'})
    with opener(request, timeout=TIMEOUT) as response:return response.read()


def parse_release(payload, trusted=DOWNLOADS):
    """{'version','name','url','size','sha256','notes'} of a published release's zip for this system, or None.
    The zip must come from this project's release downloads and carry a SHA-256 digest."""
    if not isinstance(payload, dict) or payload.get('draft') or payload.get('prerelease'):return None
    for asset in payload.get('assets') or []:
        if not isinstance(asset, dict):continue
        name = str(asset.get('name') or ''); match = asset_pattern().match(name)
        digest = re.fullmatch(r'sha256:([0-9a-f]{64})', str(asset.get('digest') or ''))
        url = str(asset.get('browser_download_url') or '')
        if match and digest and (trusted is None or url.startswith(trusted)) and type(asset.get('size')) is int and asset['size'] > 0:
            return dict(version=match.group(1), name=name, url=url, size=asset['size'], sha256=digest.group(1),
                        notes=str(payload.get('body') or ''))
    return None


def latest(opener=urllib.request.urlopen):
    """The newest release (see parse_release) or None; raises OSError/ValueError when it cannot be read."""
    source = feed()
    return parse_release(json.loads(fetch(source, opener).decode('utf-8')), None if source != FEED else DOWNLOADS)


def app_folder():
    """Folder of the packaged app (on macOS the .app bundle), or None when running from source (which
    cannot update itself)."""
    if not getattr(sys, 'frozen', False):return None
    program = Path(sys.executable).resolve()
    if not MAC:return program.parent
    bundle = program.parents[2]
    return bundle if bundle.suffix == '.app' else None


def work_folder(app):
    """Where downloads, the staged version and previous versions are kept."""
    if not MAC:return Path(app)/'.update'
    from .user_paths import local_data
    return local_data('Grainy')/'Update'


def blocked(app, size=0):
    """Why this copy cannot update itself ('' when it can)."""
    if app is None:return tr('소스에서 실행 중이라 직접 업데이트할 수 없습니다.')
    try:
        # macOS: the bundle is replaced as a whole, so the folder that holds it must be writable (a disk
        # image, or a copy macOS runs from a read-only place until it has been moved, is not).
        probe = (Path(app).parent if MAC else Path(app))/f'.update-probe-{uuid4().hex}'
        probe.write_bytes(b''); probe.unlink()
    except OSError:
        return tr('프로그램 폴더에 쓸 수 없습니다.')
    if shutil.disk_usage(app).free < max(FREE_BYTES, size*6):return tr('디스크 공간이 부족합니다.')
    return ''


def download(release, folder, progress=None, cancel=None, opener=urllib.request.urlopen):
    """Download the release zip into folder and verify size and SHA-256. Returns the path, or None if cancelled."""
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    target = folder/release['name']; part = target.with_name(target.name+'.part'); digest = hashlib.sha256(); done = 0
    source = release['url']
    stream = open(source, 'rb') if '://' not in source else opener(
        urllib.request.Request(source, headers={'User-Agent': f'Grainy/{__version__}'}), timeout=TIMEOUT)
    try:
        with stream, open(part, 'wb') as output:
            while True:
                if cancel is not None and cancel.is_set():return None
                block = stream.read(1 << 20)
                if not block:break
                output.write(block); digest.update(block); done += len(block)
                if done > release['size']:break
                if progress:progress(done, release['size'])
        if done != release['size'] or digest.hexdigest() != release['sha256']:
            raise ValueError(tr('내려받은 파일이 깃헙에 올라간 파일과 다릅니다.'))
        os.replace(part, target)
        return target
    finally:
        part.unlink(missing_ok=True)


def _stage_bundle(archive, work):
    """macOS: unpack to work/'new'/Grainy.app and return the bundle. ditto restores the bundle's symbolic
    links and executable bits, which zipfile does not."""
    work = Path(work); staged = work/'new'
    if staged.exists():raise OSError(tr('이전 업데이트가 정리되지 않았습니다. Grainy를 다시 시작한 뒤 시도해 주세요.'))
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    for name in names:
        parts = Path(name).parts
        if name.startswith('/') or '..' in parts or not parts or parts[0] != BUNDLE:
            raise ValueError(tr('업데이트 파일의 구조가 올바르지 않습니다.'))
    if f'{BUNDLE}/{PROGRAM}' not in names or f'{BUNDLE}/Contents/Info.plist' not in names:
        raise ValueError(tr('업데이트 파일의 구조가 올바르지 않습니다.'))
    scratch = work/f'extract-{uuid4().hex}'; scratch.mkdir(parents=True)
    subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(scratch)], check=True, capture_output=True)
    unpacked = scratch/BUNDLE
    if unpacked.is_symlink() or not (unpacked/PROGRAM).is_file():raise ValueError(tr('업데이트 파일의 구조가 올바르지 않습니다.'))
    os.replace(scratch, staged)
    return staged/BUNDLE


def stage(archive, work):
    """Unpack a verified release zip to work/'new' and return that folder (which then holds Grainy.exe);
    on macOS the unpacked bundle, work/'new'/Grainy.app."""
    if MAC:return _stage_bundle(archive, work)
    work = Path(work); staged = work/'new'
    if staged.exists():raise OSError(tr('이전 업데이트가 정리되지 않았습니다. Grainy를 다시 시작한 뒤 시도해 주세요.'))
    scratch = work/f'extract-{uuid4().hex}'
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        for name in names:
            parts = Path(name).parts
            if name.startswith(('/', '\\')) or ':' in name or '..' in parts or not parts or parts[0] != 'Grainy':
                raise ValueError(tr('업데이트 파일의 구조가 올바르지 않습니다.'))
        if 'Grainy/Grainy.exe' not in names or not any(n.startswith('Grainy/_internal/') for n in names):
            raise ValueError(tr('업데이트 파일의 구조가 올바르지 않습니다.'))
        bundle.extractall(scratch)
    os.replace(scratch/'Grainy', staged)
    try:scratch.rmdir()
    except OSError:pass
    return staged


def launch(staged, app, arguments):
    """Start the new version's own installer step; it waits for this process to exit."""
    command = ['--finish-update', str(app), '--wait-pid', str(os.getpid()), '--', *arguments]
    if MAC:
        subprocess.Popen([str(Path(staged)/PROGRAM), *command], cwd=str(Path(staged).parent), close_fds=True, start_new_session=True)
        return
    flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen([str(Path(staged)/'Grainy.exe'), *command], cwd=str(staged), close_fds=True, creationflags=flags)


def restart_arguments(argv=None):
    """The arguments the app was started with, without update commands."""
    return [a for a in (sys.argv[1:] if argv is None else argv) if a != '--update-now']


def wait_for_exit(pid, timeout=EXIT_WAIT):
    """True once the process is gone; False if it is still running after timeout seconds."""
    if not pid:return True
    if MAC:
        deadline = time.monotonic()+timeout
        while True:
            try:os.kill(int(pid), 0)
            except ProcessLookupError:return True
            except PermissionError:pass                                           # exists, not ours
            if time.monotonic() >= deadline:return False
            time.sleep(.1)
    if os.name != 'nt':return True
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p; kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, int(pid))                  # SYNCHRONIZE
    if not handle:return True
    try:return kernel.WaitForSingleObject(handle, int(timeout*1000)) == 0
    finally:kernel.CloseHandle(handle)


def _move(source, target, attempts=40):
    """Rename within the folder; retried while a scanner or the exiting app still holds the file."""
    for attempt in range(attempts):
        try:os.replace(source, target); return
        except PermissionError:
            if attempt == attempts-1:raise
            time.sleep(.25)


def _place(source, target):
    """Move a bundle; across volumes (the work folder and the app are usually on the same one) copy it."""
    try:os.replace(source, target)
    except OSError as error:
        import errno
        if error.errno != errno.EXDEV:raise
        subprocess.run(['/usr/bin/ditto', str(source), str(target)], check=True, capture_output=True)


def _finish_bundle(app, staged, pid, note):
    """macOS: move the old bundle to previous-*/ and the staged one into its place."""
    work = work_folder(app); stamp = time.strftime('%Y%m%d-%H%M%S')
    if not (staged/PROGRAM).is_file():return 'staged version is incomplete'
    if not wait_for_exit(pid):
        note('the app did not close'); return tr('Grainy가 닫히지 않아 업데이트하지 않았습니다.')
    backup = work/f'previous-{stamp}'; backup.mkdir(parents=True, exist_ok=True); moved = False
    try:
        _place(app, backup/app.name); moved = True
        if app.exists():shutil.rmtree(app)                                       # a copy across volumes leaves the original
        note(f'previous version moved to {backup.name}')
        _place(staged, app)
        if not (app/PROGRAM).is_file():raise OSError('Grainy.app is incomplete after the move')
        note('new version in place')
        return ''
    except Exception as error:
        note(f'failed: {error!r}; restoring')
        try:
            if app.exists():
                failed = work/f'failed-{stamp}'; failed.mkdir(parents=True, exist_ok=True); _place(app, failed/app.name)
            if moved and not app.exists():_place(backup/app.name, app)
        except OSError:pass
        return str(error) or type(error).__name__


def finish(app, staged, pid=0, log=None):
    """Replace the app's files with the staged version. The previous version is kept in .update/previous-*.
    On any failure the previous version is put back. Returns '' or the reason it failed."""
    app, staged = Path(app), Path(staged); work = work_folder(app); stamp = time.strftime('%Y%m%d-%H%M%S')
    note = (lambda text: log.write(time.strftime('%H:%M:%S ')+text+'\n') or log.flush()) if log else (lambda text: None)
    if MAC:return _finish_bundle(app, staged, pid, note)
    if not (staged/'Grainy.exe').is_file():return 'staged version is incomplete'
    if not wait_for_exit(pid):
        note('the app did not close'); return tr('Grainy가 닫히지 않아 업데이트하지 않았습니다.')
    backup = work/f'previous-{stamp}'; backup.mkdir(parents=True, exist_ok=True); moved = []; copied = []
    try:
        for name in ITEMS:
            if (app/name).exists():
                _move(app/name, backup/name); moved.append(name)
        note(f'previous version moved to {backup.name}: {moved}')
        for name in ITEMS:
            source = staged/name
            if source.is_dir():
                copied.append(name); shutil.copytree(source, app/name)
            elif source.is_file():
                copied.append(name); shutil.copy2(source, app/name)
        if not (app/'Grainy.exe').is_file():raise OSError('Grainy.exe missing after the copy')
        note(f'new version copied: {copied}')
        return ''
    except Exception as error:
        note(f'failed: {error!r}; restoring')
        failed = work/f'failed-{stamp}'; failed.mkdir(parents=True, exist_ok=True)
        for name in copied:
            if (app/name).exists():
                try:_move(app/name, failed/name, 8)
                except OSError:pass
        for name in moved:
            if not (app/name).exists():
                try:_move(backup/name, app/name, 8)
                except OSError:pass
        return str(error) or type(error).__name__


def cleanup(app=None):
    """Tidy what an update left behind, through the recycle bin: the staged copy, downloads, failed
    attempts, and all but the newest previous version."""
    app = Path(app) if app is not None else app_folder()
    if app is None:return
    work = work_folder(app)
    if not work.is_dir() or (work/'new').resolve() in Path(sys.executable).resolve().parents:return
    from send2trash import send2trash
    previous = sorted(p for p in work.glob('previous-*') if p.is_dir())
    for path in [work/'new', work/'download', *work.glob('failed-*'), *work.glob('extract-*'), *previous[:-1]]:
        if path.exists():
            try:send2trash(str(path))
            except Exception:pass


def update_now(arguments=None):
    """Check, download and start the installer step without any window (the --update-now command)."""
    app = app_folder(); release = latest()
    if release is None or not newer(release['version']) and not os.environ.get('GRAINY_UPDATE_FEED'):return 'up to date'
    reason = blocked(app, release['size'])
    if reason:return reason
    work = work_folder(app); archive = download(release, work/'download')
    launch(stage(archive, work), app, restart_arguments(arguments)); return ''


def run_finish(app, pid, arguments):
    """The --finish-update command, run by the new version from its staged folder."""
    from PySide6.QtWidgets import QApplication, QProgressDialog, QMessageBox
    from PySide6.QtCore import Qt, QTimer
    app = Path(app); staged = Path(sys.executable).resolve().parents[2] if MAC else Path(sys.executable).resolve().parent
    work = work_folder(app)
    quiet = os.environ.get('QT_QPA_PLATFORM') == 'offscreen'
    qt = QApplication.instance() or QApplication([])
    dialog = QProgressDialog(tr('Grainy를 업데이트하는 중입니다…'), '', 0, 0)
    dialog.setCancelButton(None); dialog.setWindowTitle('Grainy'); dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    dialog.setMinimumDuration(0); dialog.show()
    work.mkdir(parents=True, exist_ok=True); result = {}
    def job():
        with open(work/'update.log', 'a', encoding='utf-8') as log:
            try:result['error'] = finish(app, staged, pid, log)
            except Exception as error:result['error'] = str(error) or type(error).__name__
    thread = threading.Thread(target=job); thread.start()
    timer = QTimer(); timer.setInterval(100); timer.timeout.connect(lambda: qt.quit() if not thread.is_alive() else None); timer.start()
    qt.exec(); thread.join(); dialog.close()
    error = result.get('error', '')
    if error and not quiet:
        QMessageBox.warning(None, 'Grainy', tr('업데이트하지 못했습니다. 이전 버전을 그대로 사용합니다.\n{0}', error))
    if MAC:
        # Through LaunchServices, so the new copy starts as a normal app of its own rather than as this helper's child.
        if (app/PROGRAM).is_file():subprocess.Popen(['/usr/bin/open', '-n', str(app), *(['--args', *arguments] if arguments else [])], close_fds=True)
        return 1 if error else 0
    target = app/'Grainy.exe'
    if target.is_file():subprocess.Popen([str(target), *arguments], cwd=str(app), close_fds=True)
    return 1 if error else 0


class UpdateController:
    """Menu commands, the one-time question about automatic checks, and the update dialogs for a window."""

    def __init__(self, window, preferences, fetcher=latest, packaged=None):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMenu
        self.w, self.preferences, self.fetcher = window, preferences, fetcher
        self.packaged = getattr(sys, 'frozen', False) if packaged is None else packaged
        self.thread = None; self.result = None; self.manual = False; self.release = None
        self.poll = QTimer(window); self.poll.setInterval(150); self.poll.timeout.connect(self.checked)
        menu = window.findChild(QMenu, 'settingsMenu')
        self.auto_action = menu.addAction(tr('새 버전 자동 확인'))
        self.auto_action.setCheckable(True); self.auto_action.setChecked(self.preferences.values.get('update_check') is True)
        self.auto_action.toggled.connect(lambda value: self.save('update_check', bool(value)))
        self.check_action = menu.addAction(tr('업데이트 확인…')); self.check_action.triggered.connect(lambda: self.check(True))
        window.update_button.clicked.connect(lambda: self.offer(self.release) if self.release else self.check(True))
        if not SUPPORTED:
            self.auto_action.setVisible(False); self.check_action.setVisible(False)

    def save(self, key, value):
        try:self.preferences.save(key, value)
        except OSError:pass

    def startup(self):
        """Once per start of the packaged app: ask about automatic checks the first time, then check if due."""
        if not self.packaged or not SUPPORTED:return
        cleanup()
        if self.preferences.values.get('update_check') is None:
            from PySide6.QtWidgets import QMessageBox
            box = QMessageBox(self.w); box.setWindowTitle(tr('업데이트')); box.setIcon(QMessageBox.Icon.Question)
            box.setText(tr('새 버전이 나오면 알려 드릴까요?'))
            box.setInformativeText(tr('켜 두면 Grainy를 시작할 때 하루에 한 번 GitHub에서 최신 버전 번호만 확인합니다. 사진이나 사용 정보는 보내지 않으며, 설정 메뉴에서 언제든 끌 수 있습니다.'))
            yes = box.addButton(tr('자동 확인 켜기'), QMessageBox.ButtonRole.AcceptRole); box.addButton(tr('끄기'), QMessageBox.ButtonRole.RejectRole)
            box.exec(); chosen = box.clickedButton() is yes
            self.save('update_check', chosen); self.auto_action.blockSignals(True); self.auto_action.setChecked(chosen); self.auto_action.blockSignals(False)
        last = self.preferences.values.get('update_checked')
        if self.preferences.values.get('update_check') is True and (type(last) not in (int, float) or time.time()-last >= INTERVAL):self.check(False)

    def check(self, manual):
        if self.thread is not None:return
        self.manual = manual; box = {}
        def work():
            try:box['release'] = self.fetcher()
            except Exception as error:box['error'] = str(getattr(error, 'reason', error)) or type(error).__name__
        self.result = box; self.thread = threading.Thread(target=work, daemon=True); self.thread.start(); self.poll.start()
        if manual:self.w.statusBar().showMessage(tr('새 버전을 확인하는 중…'))

    def checked(self):
        if self.thread is None or self.thread.is_alive():return
        from PySide6.QtWidgets import QMessageBox
        self.poll.stop(); self.thread = None; box = self.result; manual = self.manual
        if getattr(self.w, 'closing', False):return
        if 'error' in box:
            if manual:QMessageBox.information(self.w, tr('업데이트'), tr('새 버전을 확인하지 못했습니다 ({0}). 인터넷 연결을 확인해 주세요.', box['error']))
            return
        self.save('update_checked', time.time()); release = box.get('release')
        if release and newer(release['version']):
            self.release = release
            self.w.update_button.setText(tr('새 버전 {0}', release['version'])); self.w.update_button.show()
            if manual:self.offer(release)
            else:self.w.statusBar().showMessage(tr('새 버전 {0}이 있습니다. 위쪽의 버튼을 누르면 업데이트합니다.', release['version']), 15000)
        else:
            self.release = None; self.w.update_button.hide()
            if manual:QMessageBox.information(self.w, tr('업데이트'), tr('최신 버전을 쓰고 있습니다 ({0}).', __version__))

    def offer(self, release):
        from PySide6.QtWidgets import QMessageBox
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        reason = blocked(app_folder() if self.packaged else None, release['size'])
        box = QMessageBox(self.w); box.setWindowTitle(tr('업데이트')); box.setIcon(QMessageBox.Icon.Information)
        box.setText(tr('새 버전 {0}이 있습니다. (지금 버전 {1})', release['version'], __version__))
        notes = release.get('notes', '').strip()
        box.setInformativeText((reason+'\n' if reason else tr('업데이트하면 Grainy가 닫혔다가 새 버전으로 다시 열립니다. 사진과 보정 기록은 그대로입니다.')))
        if notes:box.setDetailedText(notes[:4000])
        install = None if reason else box.addButton(tr('지금 업데이트'), QMessageBox.ButtonRole.AcceptRole)
        page = box.addButton(tr('내려받기 페이지 열기'), QMessageBox.ButtonRole.ActionRole)
        box.addButton(tr('나중에'), QMessageBox.ButtonRole.RejectRole)
        box.exec(); clicked = box.clickedButton()
        if clicked is page:QDesktopServices.openUrl(QUrl(PAGE))
        elif install is not None and clicked is install:self.install(release)

    def install(self, release):
        """Download with a progress dialog, then hand over to the new version and close this window."""
        from threading import Event
        from PySide6.QtWidgets import QMessageBox, QProgressDialog
        from PySide6.QtCore import Qt, QEventLoop, QTimer
        w = self.w
        if w.export_running or w.import_busy or w.import_scans or getattr(w, 'maintenance_running', False) or getattr(w, 'command_running', False):
            QMessageBox.information(w, tr('업데이트'), tr('가져오기·내보내기 같은 작업이 끝난 뒤 다시 시도해 주세요.')); return False
        app = app_folder(); work = work_folder(app); cancel = Event(); state = {'done': 0}; box = {}
        progress = QProgressDialog(tr('새 버전을 내려받는 중…'), tr('취소'), 0, 100, w)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.setMinimumDuration(0); progress.setAutoClose(False); progress.setAutoReset(False)
        progress.canceled.connect(cancel.set)
        def job():
            try:
                archive = download(release, work/'download', lambda done, total: state.__setitem__('done', done*100//max(1, total)), cancel)
                box['staged'] = stage(archive, work) if archive else None
            except Exception as error:box['error'] = str(getattr(error, 'reason', error)) or type(error).__name__
        thread = threading.Thread(target=job, daemon=True); thread.start(); loop = QEventLoop()
        timer = QTimer(); timer.setInterval(100)
        timer.timeout.connect(lambda: (progress.setValue(min(99, state['done'])), loop.quit() if not thread.is_alive() else None)); timer.start()
        loop.exec(); timer.stop(); progress.close()
        if 'error' in box:
            QMessageBox.warning(w, tr('업데이트'), tr('업데이트를 준비하지 못했습니다.\n{0}', box['error'])); return False
        if not box.get('staged'):return False
        launch(box['staged'], app, restart_arguments()); w.close()
        return True
