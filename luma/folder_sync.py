"""Automatic import of new photos that appear in registered folders: checked shortly after start,
whenever Grainy becomes the active application, and every two minutes. Only adds catalog entries;
never touches the files. Photos the user removed from the catalog are not imported again, files
still being written are left for the next check, and unreadable files are skipped quietly until
they change."""
import os
import time
from pathlib import Path
from threading import Event
from .folders import path_key
from .i18n import tr

PREFERENCE = 'auto_sync_folders'
EXCLUDED = 'sync_excluded'
INTERVAL_MS = 120000
START_MS = 4000
ACTIVATE_GAP = 20          # seconds between checks triggered by returning to the app
SETTLE = 5                 # seconds a file must be unchanged (still copying / downloading otherwise)
OUTPUTS = set()            # files Grainy wrote itself (exports): not new photos


def note_output(path):
    OUTPUTS.add(path_key(str(Path(path).resolve())))


def new_files(roots, known, skipped=None, cancel=None, settle=SETTLE, now=None):
    """Image files under roots whose path_key is not in known, oldest first. skipped maps a key to the
    (mtime, size) of a file that failed to import; it is offered again only after it changes."""
    from .engine import IMAGE_EXTENSIONS
    skipped = skipped or {}; now = time.time() if now is None else now
    found = {}
    for root in roots:
        if not os.path.isdir(root):continue
        for folder, _, names in os.walk(root):
            if cancel is not None and cancel.is_set():return []
            for name in names:
                if os.path.splitext(name)[1].lower() not in IMAGE_EXTENSIONS:continue
                path = os.path.join(folder, name)
                if path_key(path) in known:continue
                key = path_key(str(Path(path).resolve()))
                if key in known or key in found:continue
                try:info = os.stat(path)
                except OSError:continue
                if now-info.st_mtime < settle or skipped.get(key) == (info.st_mtime, info.st_size):continue
                found[key] = (info.st_mtime, path)
    return [path for _, path in sorted(found.values())]


class FolderSync:
    def __init__(self, w):
        from PySide6.QtCore import QTimer, QThreadPool
        from PySide6.QtWidgets import QApplication
        self.w = w; self.running = False; self.cancel = Event(); self.last = 0.
        self.auto = set()                               # keys queued here: their import errors stay quiet
        self.skipped = {}
        self.excluded = set(w.catalog.preference(EXCLUDED, []) or [])
        self.pool = QThreadPool(w); self.pool.setMaxThreadCount(1)
        self.timer = QTimer(w); self.timer.setInterval(INTERVAL_MS); self.timer.timeout.connect(self.scan); self.timer.start()
        QTimer.singleShot(START_MS, self.scan)
        QApplication.instance().applicationStateChanged.connect(self.activated)

    def enabled(self):
        return bool(self.w.catalog.preference(PREFERENCE, True))

    def set_enabled(self, value):
        self.w.catalog.save_preference(PREFERENCE, bool(value))
        if value:self.scan()

    def activated(self, state):
        from PySide6.QtCore import Qt
        if state == Qt.ApplicationState.ApplicationActive and time.monotonic()-self.last > ACTIVATE_GAP:self.scan()

    def idle(self):
        w = self.w
        return not (getattr(w, 'closing', False) or w.import_busy or w.import_scans or w.export_running
                    or getattr(w, 'maintenance_running', False) or getattr(w, 'command_running', False)
                    or w.import_cancel.is_set())          # after "cancel import": wait for the next manual import

    def scan(self):
        if self.running or not self.idle() or not self.enabled():return False
        w = self.w
        self.keep_outputs()
        roots = w.catalog.folder_roots()
        known = {path_key(p) for p in w.catalog.paths()} | self.excluded
        skipped = dict(self.skipped); cancel = self.cancel
        self.running = True; self.last = time.monotonic()
        w.spawn(lambda: new_files(roots, known, skipped, cancel), self.found, self.failed, self.pool)
        return True

    def failed(self, error):
        self.running = False

    def found(self, files):
        self.running = False
        w = self.w
        if not files or not self.idle() or not self.enabled():return
        known = {path_key(p) for p in w.catalog.paths()} | w.import_pending_paths | self.excluded
        self.auto &= w.import_pending_paths
        added = 0
        for file in files:
            key = path_key(str(Path(file).resolve()))
            if key in known:continue
            w.import_queue.append(file); w.import_pending_paths.add(key); known.add(key); self.auto.add(key); added += 1
        if added and not w.import_busy:
            w.import_errors = []
            w.statusBar().showMessage(tr('등록한 폴더에서 새 사진 {0}장을 찾았습니다.', f'{added}'))
            w.import_cancel_button.show()
            w.import_next()

    def import_failed(self, path):
        """True when the failed file was queued automatically (the caller then reports nothing)."""
        key = path_key(str(Path(path).resolve()))
        if key not in self.auto:return False
        self.auto.discard(key)
        try:
            info = os.stat(path); self.skipped[key] = (info.st_mtime, info.st_size)
        except OSError:
            pass
        return True

    def imported(self, path):
        key = path_key(str(Path(path).resolve()))
        self.auto.discard(key)
        if key in self.excluded:
            self.excluded.discard(key); self.w.catalog.save_preference(EXCLUDED, sorted(self.excluded))

    def exclude(self, paths):
        """Photos removed from the catalog while the files stay: do not bring them back automatically."""
        self.excluded |= {path_key(str(Path(p).resolve())) for p in paths}
        self.w.catalog.save_preference(EXCLUDED, sorted(self.excluded))

    def release(self):
        """Closing the window: drop an import that only this class queued (found again at the next start)."""
        w = self.w
        if w.import_scans or not w.import_pending_paths or not w.import_pending_paths <= self.auto:return
        w.import_queue.clear(); w.import_pending_paths.clear(); self.auto.clear(); w.import_busy = False

    def keep_outputs(self):
        """Remember exports across restarts so they are not imported as new photos later."""
        known = {path_key(p) for p in self.w.catalog.paths()}
        roots = [path_key(r)+os.sep for r in self.w.catalog.folder_roots()]
        new = {k for k in OUTPUTS-self.excluded-known if any(k.startswith(r) for r in roots)}
        if new:
            self.excluded |= new; self.w.catalog.save_preference(EXCLUDED, sorted(self.excluded))

    def shutdown(self):
        self.timer.stop(); self.cancel.set(); self.pool.waitForDone(); self.keep_outputs()
