"""Automatic catalog backup to Google Drive (the "My Drive" folder of Google Drive for desktop) or any
folder. Uploads only consistent SQLite snapshots of the catalog (edits, history, ratings, keywords,
collections, presets); original photos and thumbnails stay local. Unchanged catalogs are not uploaded
again, and old Grainy backups are thinned to the recycle bin, never deleted permanently."""
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import threading
from contextlib import closing
from uuid import uuid4
from .i18n import tr

PREF = 'cloud_backup'
PREFIX = 'Grainy-catalog-'
FOLDER_NAME = 'Grainy 백업'
KEEP = 20                      # newest backups kept; older ones keep one per month
INTERVAL_MS = 30*60*1000
CLOSE_TIMEOUT = 20


def google_drive():
    """"My Drive" of Google Drive for desktop, or None."""
    if os.name == 'nt':
        import ctypes
        k = ctypes.windll.kernel32; mask = k.GetLogicalDrives()
        for i in range(26):
            root = f'{chr(65+i)}:\\'
            if not mask >> i & 1 or k.GetDriveTypeW(root) != 3:continue
            label = ctypes.create_unicode_buffer(261)
            if k.GetVolumeInformationW(root, label, 261, None, None, None, None, 0) and label.value == 'Google Drive':
                for name in ('My Drive', '내 드라이브'):
                    if (Path(root)/name).is_dir():return Path(root)/name
    # macOS: Google Drive for desktop mounts each account under ~/Library/CloudStorage/GoogleDrive-<account>.
    storage = Path.home()/'Library'/'CloudStorage'
    try:accounts = sorted(p for p in storage.iterdir() if p.name.startswith('GoogleDrive-')) if storage.is_dir() else []
    except OSError:accounts = []
    for account in accounts:
        for name in ('My Drive', '내 드라이브'):
            if (account/name).is_dir():return account/name
    for path in (Path.home()/'My Drive', Path.home()/'Google Drive'/'My Drive', Path.home()/'Google Drive'):
        if path.is_dir():return path
    return None


def default_folder():
    drive = google_drive()
    return str(drive/FOLDER_NAME) if drive else ''


def snapshot(catalog):
    """Consistent copy of the catalog in its backups folder (main thread; the catalog connection is not shared)."""
    staged = catalog.backup(catalog.directory/'backups'/'cloud-staging'/f'{uuid4().hex}.sqlite')
    with closing(sqlite3.connect(staged)) as db:db.execute('PRAGMA journal_mode=DELETE')   # one self-contained file
    return staged


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):h.update(block)
    return h.hexdigest()


def upload(staged, folder, last_hash=None, keep=KEEP, now=None):
    """Verify the snapshot and copy it into folder unless it matches last_hash. Returns (path or None, hash).
    The staged file is always removed; a partial upload never gets the .sqlite name."""
    staged = Path(staged)
    try:
        with closing(sqlite3.connect(f'{staged.resolve().as_uri()}?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':raise ValueError(tr('백업 사본 검사에 실패했습니다.'))
        value = digest(staged)
        if value == last_hash:return None, value
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        stamp = (now or datetime.now()).strftime('%Y%m%d-%H%M%S')
        target = folder/f'{PREFIX}{stamp}.sqlite'
        n = 1
        while target.exists():target = folder/f'{PREFIX}{stamp}-{n}.sqlite'; n += 1
        part = target.with_name(target.name+'.part')
        shutil.copyfile(staged, part)
        if digest(part) != value:raise OSError(tr('업로드한 사본이 원본과 다릅니다.'))
        os.replace(part, target)
        trim(folder, keep)
        return target, value
    finally:
        staged.unlink(missing_ok=True)


def surplus(names, keep=KEEP):
    """Backup names to retire: all but the newest `keep`, except the newest of each older month."""
    names = sorted(names, reverse=True)
    months = set(); out = []
    for name in names[keep:]:
        month = name[len(PREFIX):len(PREFIX)+6]
        if month in months:out.append(name)
        else:months.add(month)
    return out


def trim(folder, keep=KEEP):
    folder = Path(folder)
    names = [p.name for p in folder.glob(PREFIX+'*.sqlite') if p.is_file()]
    for name in surplus(names, keep):
        try:
            from send2trash import send2trash
            send2trash(str(folder/name))
        except Exception:
            pass                                   # never fall back to permanent deletion


class CloudBackup:
    """Periodic and on-close backup for a MainWindow."""

    def __init__(self, w):
        from PySide6.QtCore import QTimer
        self.w = w; self.thread = None; self.result = None
        self.timer = QTimer(w); self.timer.setInterval(INTERVAL_MS); self.timer.timeout.connect(lambda: self.start(False))
        self.poll = QTimer(w); self.poll.setInterval(200); self.poll.timeout.connect(self.check)
        self.timer.start()

    def settings(self):
        saved = self.w.catalog.preference(PREF, {}) or {}
        return {'enabled': False, 'folder': '', 'last': '', 'hash': '', 'error': '', **saved}

    def save(self, **changes):
        self.w.catalog.save_preference(PREF, {**self.settings(), **changes})

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, manual=True, notify=None):
        """Back up now (manual) or when enabled (automatic). notify(message) is called on completion."""
        s = self.settings()
        if self.busy or getattr(self.w, 'closing', False) or not s['folder'] or not (manual or s['enabled']):return False
        try:
            if manual:self.w.commit(); self.w.save_keywords()
            staged = snapshot(self.w.catalog)
        except Exception as error:
            self.finish(None, None, error, manual, notify); return False
        box = {}
        def work():
            try:box['result'] = upload(staged, s['folder'], None if manual else s['hash'])
            except Exception as error:box['error'] = error
        self.result = (box, manual, notify)
        self.thread = threading.Thread(target=work, daemon=True); self.thread.start(); self.poll.start()
        return True

    def check(self):
        if self.busy or self.result is None:return
        self.poll.stop(); box, manual, notify = self.result; self.result = None
        target, value = box.get('result', (None, None))
        self.finish(target, value, box.get('error'), manual, notify)

    def finish(self, target, value, error, manual, notify):
        if error:
            self.save(error=str(error)); message = tr('구글 드라이브 백업 실패: {0}', str(error))
        else:
            changes = {'error': '', 'hash': value} if value else {'error': ''}
            if target:changes['last'] = datetime.now().isoformat(timespec='seconds')
            self.save(**changes)
            message = tr('백업했습니다: {0}', str(target)) if target else tr('바뀐 내용이 없어 백업을 건너뛰었습니다.')
        if notify:notify(message)
        elif manual or error:self.w.statusBar().showMessage(message, 8000)

    def shutdown(self):
        """On close (catalog still open): wait for a running backup, then back up the final state."""
        self.timer.stop(); self.poll.stop()
        if self.busy:self.thread.join(CLOSE_TIMEOUT)
        if self.result is not None and not self.busy:self.check()
        s = self.settings()
        if not s['enabled'] or not s['folder'] or self.busy:return
        try:
            staged = snapshot(self.w.catalog)
        except Exception as error:
            self.save(error=str(error)); return
        box = {}
        def work():
            try:box['result'] = upload(staged, s['folder'], s['hash'])
            except Exception as error:box['error'] = error
        thread = threading.Thread(target=work, daemon=True); thread.start(); thread.join(CLOSE_TIMEOUT)
        if thread.is_alive():
            self.save(error=tr('종료할 때 백업이 시간 안에 끝나지 않았습니다.')); return
        if 'error' in box:self.save(error=str(box['error'])); return
        target, value = box['result']
        changes = {'error': '', 'hash': value}
        if target:changes['last'] = datetime.now().isoformat(timespec='seconds')
        self.save(**changes)


def dialog(w):
    """Google Drive backup settings for MainWindow w."""
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QCheckBox, QLineEdit, QPushButton,
                                   QLabel, QDialogButtonBox, QFileDialog)
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtCore import QUrl
    backup = w.cloud_backup; s = backup.settings()
    d = QDialog(w); d.setWindowTitle(tr('구글 드라이브 백업')); box = QVBoxLayout(d); form = QFormLayout()
    info = QLabel(tr('보정값·편집 이력·별점·키워드·컬렉션·프리셋이 담긴 카탈로그를 백업합니다. 원본 사진은 올리지 않습니다.'))
    info.setWordWrap(True); box.addWidget(info)
    if not google_drive():
        missing = QLabel(tr('Google Drive 데스크톱 앱을 찾지 못했습니다. 설치하고 로그인하면 내 드라이브 폴더를 고를 수 있습니다.'))
        missing.setWordWrap(True); box.addWidget(missing)
    enabled = QCheckBox(tr('자동 백업 (30분마다 바뀐 내용이 있으면, 그리고 종료할 때)')); enabled.setChecked(bool(s['enabled']))
    form.addRow(enabled)
    folder = QLineEdit(s['folder'] or default_folder()); row = QHBoxLayout(); row.addWidget(folder)
    choose = QPushButton(tr('폴더 선택')); row.addWidget(choose)
    choose.clicked.connect(lambda: folder.setText(QFileDialog.getExistingDirectory(d, tr('백업 폴더'), folder.text()) or folder.text()))
    form.addRow(tr('백업 폴더'), row)
    def status_text():
        now = backup.settings(); last = now['last'].replace('T', ' ') if now['last'] else tr('없음')
        text = tr('마지막 백업: {0}', last)
        return text+('\n'+tr('최근 오류: {0}', now['error']) if now['error'] else '')
    status = QLabel(status_text()); status.setWordWrap(True); form.addRow(status)
    form.addRow(QLabel(tr('최근 {0}개는 모두, 그보다 오래된 것은 달마다 1개씩 남기고 나머지는 휴지통으로 옮깁니다. 복원: 파일 관리 › 카탈로그 복원', f'{KEEP}')))
    box.addLayout(form)
    actions = QHBoxLayout(); now = QPushButton(tr('지금 백업')); open_folder = QPushButton(tr('폴더 열기'))
    actions.addWidget(now); actions.addWidget(open_folder); actions.addStretch(); box.addLayout(actions)
    def store():
        backup.save(enabled=enabled.isChecked() and bool(folder.text().strip()), folder=folder.text().strip())
    def run_now():
        if not folder.text().strip():status.setText(tr('백업 폴더를 선택하세요.')); return
        store(); now.setEnabled(False); status.setText(tr('백업하는 중…'))
        def done(message):
            now.setEnabled(True); status.setText(message+'\n'+status_text())
        if not backup.start(True, done):now.setEnabled(True); status.setText(status_text())
    now.clicked.connect(run_now)
    open_folder.clicked.connect(lambda: Path(folder.text()).is_dir() and QDesktopServices.openUrl(QUrl.fromLocalFile(folder.text())))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(d.accept); buttons.rejected.connect(d.reject); box.addWidget(buttons)
    if d.exec() == QDialog.DialogCode.Accepted:
        store()
        if enabled.isChecked() and folder.text().strip() and not backup.settings()['last']:backup.start(False)
