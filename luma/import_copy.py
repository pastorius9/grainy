"""Import by copying: copies photos from a card or another folder into a
destination (optionally in dated subfolders, renamed, with a verified second backup copy), then hands
the copies to the normal import with keywords and a develop preset. Sources are never changed or
removed, and existing files are never overwritten."""
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
from .i18n import tr

SUBFOLDERS = ('none', 'year/date', 'date')
FIELDS = {'stem', 'date', 'time', 'n', 'camera'}


def capture_time(path, metadata=None):
    """EXIF capture time, else the file's modification time."""
    value = (metadata or {}).get('date')
    if value:
        try:return datetime.strptime(str(value)[:19], '%Y:%m:%d %H:%M:%S')
        except ValueError:pass
    return datetime.fromtimestamp(Path(path).stat().st_mtime)


def check_template(template):
    """ValueError unless the name template uses only known fields and gives a valid file name."""
    import string
    fields = {f.split('.')[0].split('[')[0] for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    if not template.strip() or fields-FIELDS:
        raise ValueError(tr('이름 규칙에는 {stem}, {date}, {time}, {n}, {camera}만 쓸 수 있습니다.'))
    name = template.format(stem='photo', date='20260925', time='120000', n=1, camera='Camera')
    if re.search(r'[\\/:*?"<>|]', name) or name.strip(' .') != name or not name:
        raise ValueError(tr('이름 규칙이 올바른 파일 이름을 만들지 않습니다.'))


def duplicate_key(path, metadata):
    """Suspected duplicate: same original file name and capture time (else file size)."""
    name = Path(metadata.get('filename') or path).name.lower()
    return (name, str(metadata['date'])) if metadata.get('date') else (name, round(float(metadata.get('size_mb', 0)), 2))


def plan(sources, destination, subfolders='year/date', template='{stem}', known=frozenset(), read=None):
    """[(source, relative target path)] in capture order; suspected duplicates of `known` keys are
    skipped and returned separately."""
    from .engine import read_metadata
    check_template(template)
    if subfolders not in SUBFOLDERS:raise ValueError(subfolders)
    read = read or read_metadata
    items = []
    for source in sources:
        metadata = {**read(source), 'filename': Path(source).name, 'size_mb': round(Path(source).stat().st_size/1024**2, 2)}
        items.append((capture_time(source, metadata), str(source), metadata))
    items.sort(key=lambda item: (item[0], item[1]))
    result, skipped = [], []
    for n, (when, source, metadata) in enumerate(items, 1):
        if duplicate_key(source, metadata) in known:
            skipped.append(source);continue
        name = template.format(stem=Path(source).stem, date=when.strftime('%Y%m%d'), time=when.strftime('%H%M%S'), n=n,
                               camera=re.sub(r'[\\/:*?"<>|]+', '', str(metadata.get('camera') or 'camera')).strip() or 'camera')
        folder = Path() if subfolders == 'none' else Path(when.strftime('%Y'), when.strftime('%Y-%m-%d')) if subfolders == 'year/date' \
            else Path(when.strftime('%Y-%m-%d'))
        result.append((source, folder/(name+Path(source).suffix.lower())))
    return result, skipped


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):h.update(block)
    return h.digest()


def _free(path):
    """path, or path with -1, -2, ... when it exists (never overwrite)."""
    candidate, n = Path(path), 1
    while candidate.exists():
        candidate = Path(path).with_name(f'{Path(path).stem}-{n}{Path(path).suffix}');n += 1
    return candidate


def _copy(source, target):
    """Copy to a new file (exclusive create) and verify the bytes; returns the written path."""
    target = _free(target);target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name+'.grainy-part')
    with open(source, 'rb') as src, open(partial, 'xb') as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    shutil.copystat(source, partial)
    if _digest(partial) != _digest(source):
        partial.unlink(missing_ok=True);raise OSError(tr('복사한 파일이 원본과 다릅니다: {0}', Path(source).name))
    if target.exists():                                  # appeared meanwhile: keep both
        target = _free(target)
    os.replace(partial, target)
    return target


def execute(planned, destination, backup=None, cancel=None, progress=None):
    """Copy every planned file (and its .xmp sidecar) into destination, and to backup when given.
    Returns (copied destination paths, errors)."""
    copied, errors = [], []
    for index, (source, relative) in enumerate(planned):
        if cancel is not None and cancel.is_set():break
        try:
            target = _copy(source, Path(destination)/relative)
            sidecar = Path(source).with_suffix('.xmp')
            if sidecar.is_file():_copy(sidecar, target.with_suffix('.xmp'))
            if backup:
                _copy(source, Path(backup)/relative.parent/target.name)
            copied.append(str(target))
        except OSError as error:
            errors.append(f'{Path(source).name}: {error}')
        if progress:progress(index+1)
    return copied, errors


def known_keys(catalog):
    import json
    keys = set()
    for row in catalog.db.execute('SELECT path,metadata FROM photos WHERE virtual_source IS NULL'):
        try:keys.add(duplicate_key(row[0], json.loads(row[1] or '{}')))
        except (ValueError, TypeError):pass
    return keys


def dialog(w):
    """Copy-import dialog and run (MainWindow w)."""
    from threading import Event
    from PySide6.QtWidgets import (QDialog, QFormLayout, QLineEdit, QPushButton, QHBoxLayout, QVBoxLayout, QComboBox,
                                   QCheckBox, QDialogButtonBox, QFileDialog, QLabel, QMessageBox, QProgressDialog)
    from PySide6.QtCore import Qt
    from .engine import IMAGE_EXTENSIONS
    if w.import_busy or w.import_scans:
        w.statusBar().showMessage(tr('진행 중인 가져오기가 끝난 뒤 다시 시도하세요.'), 5000);return
    saved = w.catalog.preference('import_copy', {}) or {}
    d = QDialog(w);d.setWindowTitle(tr('사진 복사해서 가져오기'));box = QVBoxLayout(d);form = QFormLayout()
    def folder_row(edit, title):
        row = QHBoxLayout();row.addWidget(edit);b = QPushButton(tr('폴더 선택'))
        b.clicked.connect(lambda: edit.setText(QFileDialog.getExistingDirectory(d, tr(title)) or edit.text()));row.addWidget(b);return row
    source = QLineEdit();form.addRow(tr('가져올 곳 (카드·폴더)'), folder_row(source, '가져올 폴더'))
    destination = QLineEdit(saved.get('destination', ''));form.addRow(tr('복사할 곳'), folder_row(destination, '복사할 폴더'))
    subfolders = QComboBox()
    for key, label in (('year/date', '연도/날짜 폴더 (2026/2026-09-25)'), ('date', '날짜 폴더 (2026-09-25)'), ('none', '하위 폴더 없음')):
        subfolders.addItem(tr(label), key)
    subfolders.setCurrentIndex(max(0, subfolders.findData(saved.get('subfolders', 'year/date'))));form.addRow(tr('정리'), subfolders)
    template = QLineEdit(saved.get('template', '{stem}'));form.addRow(tr('파일 이름'), template)
    form.addRow(QLabel(tr('{stem} 원래 이름 · {date} 촬영일 · {time} 촬영 시각 · {n:04d} 순번 · {camera} 카메라')))
    use_backup = QCheckBox(tr('두 번째 사본(백업)도 만들기'));backup = QLineEdit(saved.get('backup', ''))
    form.addRow(use_backup);form.addRow(tr('백업 위치'), folder_row(backup, '백업 폴더'))
    keywords = QLineEdit();keywords.setPlaceholderText(tr('쉼표로 구분'));form.addRow(tr('키워드'), keywords)
    preset = QComboBox();preset.addItem(tr('없음'), '')
    for name in w.all_presets:preset.addItem(name, name)
    form.addRow(tr('현상 프리셋'), preset)
    skip = QCheckBox(tr('중복으로 보이는 사진은 가져오지 않기 (같은 이름·촬영 시각)'));skip.setChecked(True);form.addRow(skip)
    box.addLayout(form)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr('복사하고 가져오기'))
    buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject);box.addWidget(buttons)
    while True:
        if d.exec() != QDialog.DialogCode.Accepted:return
        src, dst = Path(source.text()), Path(destination.text())
        problem = None
        try:check_template(template.text())
        except ValueError as error:problem = str(error)
        if not problem and not src.is_dir():problem = tr('가져올 폴더를 선택하세요.')
        elif not problem and not destination.text().strip():problem = tr('복사할 폴더를 선택하세요.')
        elif not problem and (dst.resolve() == src.resolve() or src.resolve() in dst.resolve().parents):
            problem = tr('복사할 곳은 가져올 폴더 밖이어야 합니다.')
        elif not problem and use_backup.isChecked() and (not backup.text().strip() or Path(backup.text()).resolve() == dst.resolve()):
            problem = tr('백업 위치는 복사할 곳과 다른 폴더여야 합니다.')
        if not problem:break
        QMessageBox.information(w, tr('사진 복사해서 가져오기'), problem)
    w.catalog.save_preference('import_copy', {'destination': str(dst), 'subfolders': subfolders.currentData(),
                                              'template': template.text(), 'backup': backup.text()})
    files = sorted(str(p) for p in src.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    known = known_keys(w.catalog) if skip.isChecked() else frozenset()
    options = dict(subfolders=subfolders.currentData(), template=template.text())
    extras = {'keywords': keywords.text().strip(), 'preset': preset.currentData()}
    cancel = Event()
    progress = QProgressDialog(tr('사진을 복사하는 중…'), tr('취소'), 0, max(1, len(files)), w)
    progress.setWindowModality(Qt.WindowModality.WindowModal);progress.setMinimumDuration(0);progress.canceled.connect(cancel.set)
    from PySide6.QtCore import QTimer
    state = {'done': 0}
    ticker = QTimer(w);ticker.setInterval(100);ticker.timeout.connect(lambda: progress.setValue(state['done']));ticker.start()
    w.import_scans += 1
    def work():
        planned, skipped = plan(files, dst, known=known, **options)
        copied, errors = execute(planned, dst, backup.text() if use_backup.isChecked() else None, cancel,
                                 lambda n: state.__setitem__('done', n))
        return copied, skipped, errors
    def done(result):
        w.import_scans -= 1;ticker.stop();progress.close()
        copied, skipped, errors = result
        from .folders import path_key
        for path in copied:w.import_extras[path_key(str(Path(path).resolve()))] = extras
        w.import_paths(copied)
        summary = tr('{0}장 복사 · 중복으로 건너뜀 {1}장', f'{len(copied)}', f'{len(skipped)}')
        if errors:QMessageBox.information(w, tr('복사하지 못한 사진'), summary+'\n'+'\n'.join(errors[:10]))
        else:w.statusBar().showMessage(summary, 8000)
    def failed(error):
        w.import_scans -= 1;ticker.stop();progress.close();w.show_error(error)
    w.spawn(work, done, failed)
