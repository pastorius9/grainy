from __future__ import annotations
from .i18n import tr, keys

import json
import os
import traceback
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from threading import Event, Thread
from collections import deque

import numpy as np
from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool, QTimer, QSize, QItemSelectionModel
from PySide6.QtGui import QIcon, QPixmap, QImage, QShortcut, QKeySequence, QDesktopServices, QFontDatabase, QFont
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QListWidget, QListWidgetItem, QAbstractItemView, QStackedWidget,
    QScrollArea, QTabWidget, QComboBox, QCheckBox, QFileDialog, QMessageBox, QInputDialog,
    QDialog, QFormLayout, QSpinBox, QDialogButtonBox, QFrame, QSplitter,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QToolButton, QMenu, QStyle,QProgressDialog)

from .catalog import Catalog
from .engine import defaults, normalized, load_image, develop, resize_float, to_srgb, thumbnail, export_image, auto_tone_settings, PRESETS, IMAGE_EXTENSIONS
from .widgets import Adjustment, Histogram, ToneCurve, PhotoView, qimage, histogram_bins, clipping_channels
from .render_cache import DevelopmentCache
from .folders import folder_nodes, in_folder, path_key
from .auth import CodexAuth
from .auth_dialog import AuthDialog
from .command_dialog import CommandDialog
from .photo_browser import PhotoModel,PhotoListView
from .library_query import query as library_query,QueryCancelled
from . import __version__
from .user_paths import MAC


# Windows' Korean UI font; macOS has its own, and naming a missing family costs Qt a slow alias search.
UI_FONT='Apple SD Gothic Neo' if MAC else 'Malgun Gothic'

STYLE = '''
QWidget { background:#1b1f24; color:#dce0e4; font-family:"Malgun Gothic"; font-size:12px; }
QToolButton, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser, QGroupBox, QMenu, QTabBar::tab { border-radius:0px; }
QMainWindow { background:#101215; }
QLabel { background:transparent; }
QLabel#brand { color:#f4e7d4; font-size:28px; font-weight:600; letter-spacing:3px; }
QLabel#muted { color:#89929d; }
QLabel#section { color:#d2b991; font-size:10px; font-weight:600; letter-spacing:2px; padding-top:12px; padding-bottom:6px; }
QPushButton { background:#272d34; border:1px solid #373e47; padding:7px 11px; border-radius:0px; }
QPushButton:hover { background:#363e47; border-color:#68717a; }
QPushButton:pressed, QPushButton:checked { background:#4d453a; border-color:#b89c76; color:#ffe7c8; }
QPushButton:disabled { color:#59616a; border-color:#2c3239; }
QPushButton#primary { background:#dec49f; color:#211d17; border:none; font-weight:600; padding:9px 17px; }
QPushButton#primary:hover { background:#f0d8b5; }
QPushButton#nav { text-align:left; padding:10px; }
QLineEdit, QComboBox, QSpinBox { background:#15191e; border:1px solid #39414b; border-radius:0px; padding:6px; }
QDoubleSpinBox#adjustmentValue { background:transparent; border:1px solid transparent; border-radius:0px; padding:0px 2px; color:#aeb6bf; }
QDoubleSpinBox#adjustmentValue:focus { background:#15191e; border-color:#b89c76; color:#f4e7d4; }
QLineEdit:focus { border-color:#dec49f; }
QComboBox QAbstractItemView { background:#252b32; selection-background-color:#554838; }
QListWidget { background:#15181d; border:none; outline:0; padding:4px; }
QListWidget::item { border:1px solid transparent; border-radius:0px; padding:5px; color:#aeb6bf; }
QListWidget::item:selected { background:#35322e; border:1px solid #d3b68d; color:#f4e7d4; }
QListWidget::item:hover { background:#29313a; }
QTreeWidget { background:#15181d; border:0; outline:0; padding:3px; }
QTreeWidget::item { height:28px; padding:1px; border:1px solid transparent; }
QTreeWidget::item:selected { background:#3d3831; border-color:#907b60; color:#f4e7d4; }
QTreeWidget::item:hover { background:#29313a; }
QToolButton#foldout { border:0; background:transparent; text-align:left; color:#d2b991; padding:9px 0; font-weight:600; }
QToolButton#developPanelHeader { background:#25292e; border:0; border-top:1px solid #3a3e43; color:#d9dcdf; padding:10px 8px; text-align:left; font-weight:600; }
QToolButton#developPanelHeader:hover { background:#30353b; }
QWidget#developPanelBody { background:#1d2126; }
QToolButton#developTool { background:transparent; border:0; border-bottom:2px solid transparent; padding:9px 6px; color:#a4aab2; }
QToolButton#developTool:checked { color:#f0d6b2; border-bottom-color:#dec49f; }
QToolButton#developTool:hover { background:#30353b; }
QSlider::groove:horizontal { height:3px; background:#444c55; border-radius:0px; }
QSlider::handle:horizontal { background:#dfc29b; width:10px; margin:-4px 0; border-radius:0px; }
QScrollArea { border:0; }
QScrollBar:vertical { background:#1b1f24; width:7px; }
QScrollBar::handle:vertical { background:#48515b; min-height:24px; border-radius:0px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar:horizontal { height:7px; background:#15181d; }
QScrollBar::handle:horizontal { background:#48515b; border-radius:0px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
QTabWidget::pane { border:0; }
QTabBar::tab { background:#1b1f24; color:#8e99a5; padding:10px 13px; border-bottom:2px solid transparent; }
QTabBar::tab:selected { color:#dec49f; border-bottom:2px solid #dec49f; }
QStatusBar { background:#14171b; color:#99a3ad; font-size:11px; }
QSplitter::handle { background:#101215; width:2px; }
QCheckBox { spacing:7px; padding:5px 0; }
QToolTip { background:#343c45; color:#f4e7d4; border:1px solid #66717d; padding:5px; }
'''


def configure_application(app):
    # Reapplying a global stylesheet repolishes every retained Qt widget and
    # registers duplicate font faces when embedded tests reuse QApplication.
    if app.property('lumaConfigured'):return
    # Explicit loading also gives headless QA the same Korean font as the desktop.
    fonts=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    for filename in ('malgun.ttf','malgunbd.ttf'):
        if (fonts/filename).is_file():
            QFontDatabase.addApplicationFont(str(fonts/filename))
    app.setFont(QFont(UI_FONT,10))
    # The macOS widget style ignores parts of the stylesheet (large disclosure arrows, its own check
    # boxes); Fusion draws the controls the way the design was made on Windows.
    if MAC:app.setStyle('Fusion')
    icon=Path(__file__).resolve().parents[1]/'assets'/'brand'/'grainy.ico'     # tools/make_brand_assets.py
    if icon.is_file():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyleSheet(STYLE.replace('"Malgun Gothic"',f'"{UI_FONT}"'))
    app.setProperty('lumaConfigured',True)


def import_reason(path,error):
    """One line for the "unreadable photos" list: the error's last line, or for a RAW file the decoder
    does not know, what the person can do about it."""
    last=error.strip().splitlines()[-1]
    if 'LibRawFileUnsupportedError' not in last:return last
    if Path(path).suffix.lower() in ('.nef','.nrw'):
        # LibRaw reads Nikon's lossless and lossy NEF; High Efficiency (TicoRAW, Z 8 / Z 9 and later) is licensed.
        return tr('이 압축 방식은 지원하지 않습니다. 니콘의 고효율(HE) RAW라면 카메라에서 "무손실 압축"으로 찍거나 DNG로 변환해 주세요.')
    return tr('지원하지 않는 RAW 형식입니다.')


class WorkerSignals(QObject):
    done = Signal(str, object, str)


class Worker(QRunnable):
    def __init__(self, token, function):
        super().__init__()
        self.token, self.function = token, function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.done.emit(self.token, self.function(), '')
        except Exception:
            self.signals.done.emit(self.token, None, traceback.format_exc())


class ExportDialog(QDialog):
    def __init__(self, count, parent):
        super().__init__(parent)
        self.setWindowTitle(tr('사진 내보내기'))
        self.setMinimumWidth(450)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr('선택한 사진 {0}장을 새 파일로 저장합니다.', f'{count}')))
        form = QFormLayout()
        self.folder = QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.folder)
        browse = QPushButton(tr('폴더 선택'))
        browse.clicked.connect(self.choose)
        row.addWidget(browse)
        form.addRow(tr('저장 위치'), row)
        self.format = QComboBox()
        from .quick_export import FORMATS,previous
        from . import engine
        for value in FORMATS:
            if value=='TIFF HDR 32-bit' and not engine.HDR_FEATURE:continue
            self.format.addItem(tr('원본 + 설정') if value=='Original' else value,value)
        form.addRow(tr('파일 형식'), self.format)
        self.quality = QSpinBox()
        self.quality.setRange(40,100)
        self.quality.setValue(95)
        form.addRow(tr('품질 (JPEG·AVIF·JPEG XL)'), self.quality)
        self.longest = QSpinBox()
        self.longest.setRange(0,30000)
        self.longest.setSpecialValueText(tr('원본 크기'))
        self.longest.setSingleStep(500)
        form.addRow(tr('긴 변 (px)'), self.longest)
        self.color_space=QComboBox();self.color_space.addItems(['sRGB','Adobe RGB','Display P3','ProPhoto RGB'])
        form.addRow(tr('출력 색공간'),self.color_space)
        self.keep_metadata=QCheckBox(tr('EXIF·IPTC·GPS 메타데이터 유지'));form.addRow(self.keep_metadata)
        self.grayscale=QCheckBox(tr('흑백 사진은 흑백 파일로 저장 (sRGB)'));self.grayscale.setChecked(True)
        self.grayscale.setToolTip(tr('모든 픽셀이 무채색이면 1채널 흑백 파일과 흑백 ICC로 저장합니다. 화면에 보이는 밝기는 같고 파일이 작아집니다.'))
        form.addRow(self.grayscale)
        self.dither=QCheckBox(tr('8비트 저장 시 계단 현상 방지 (디더링)'));self.dither.setChecked(True)
        self.dither.setToolTip(tr('8비트로 줄일 때 눈에 띄지 않는 고정 노이즈를 더해 하늘 같은 부드러운 영역의 띠를 막습니다. 순수한 검정·흰색은 그대로입니다.'))
        form.addRow(self.dither)
        self.naming=QLineEdit('{stem}_grainy');form.addRow(tr('파일 이름 규칙'),self.naming)
        layout.addLayout(form)
        note = QLabel(tr('이름 규칙: {stem} 원본 이름 / {n:03d} 순번\nICC 프로파일 포함 · 같은 이름이 있으면 번호를 붙여 저장합니다.'))
        note.setObjectName('muted')
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        def format_changed():
            value=self.format.currentData()
            self.quality.setEnabled(value in ('JPEG','AVIF','JPEG XL'))
            if value=='JPEG XL':self.color_space.setCurrentText('sRGB')
            self.color_space.setEnabled(value not in ('TIFF HDR 32-bit','Original','JPEG XL'))
            self.longest.setEnabled(value!='Original');self.keep_metadata.setEnabled(value!='Original')
            self.grayscale.setEnabled(value in ('JPEG','PNG','TIFF 8-bit','TIFF 16-bit'))
            self.dither.setEnabled(value in ('JPEG','AVIF','PNG','TIFF 8-bit'))
        self.format.currentIndexChanged.connect(format_changed)
        hdr_note=QLabel(tr('HDR TIFF: HDR 모드 사진만 저장 · 선형 작업 색공간 · 32비트\nJPEG·일반 PNG/TIFF는 SDR 미리보기 설정으로 저장합니다.'))
        hdr_note.setWordWrap(True);hdr_note.setObjectName('muted');layout.addWidget(hdr_note)
        hdr_note.setVisible(engine.HDR_FEATURE)
        saved=previous(parent.catalog)
        if saved:
            self.folder.setText(saved['folder']);self.format.setCurrentIndex(max(0,self.format.findData(saved['format'])))
            self.quality.setValue(saved['quality']);self.longest.setValue(saved['longest'])
            self.color_space.setCurrentText(saved['color_space']);self.keep_metadata.setChecked(saved['keep_metadata'])
            self.grayscale.setChecked(saved['grayscale']);self.dither.setChecked(saved['dither'])
            self.naming.setText(saved['naming'])
        format_changed()

    def choose(self):
        selected = QFileDialog.getExistingDirectory(self, tr('내보낼 폴더 선택'))
        if selected:
            self.folder.setText(selected)

    def validate(self):
        if not self.folder.text().strip() or not Path(self.folder.text()).is_dir():
            QMessageBox.information(self, tr('저장 위치'), tr('저장할 폴더를 선택해 주세요.'))
            return
        try:
            name=self.naming.text().format(stem='photo',n=1)
            if not name.strip() or Path(name).name!=name or any(c in name for c in '<>:"/\\|?*'):raise ValueError()
        except (KeyError,ValueError,IndexError):
            QMessageBox.information(self,tr('이름 규칙'),tr('{stem}, {n:03d}와 유효한 파일 이름만 사용하세요.'));return
        self.accept()


def section(text):
    label = QLabel(tr(text))
    label.setObjectName('section')
    return label


def button(text, action, check=False, primary=False):
    b = QPushButton(tr(text))
    b.setCheckable(check)
    b.clicked.connect(action)
    if primary:
        b.setObjectName('primary')
    return b


class MainWindow(QMainWindow):
    previewPresented = Signal(int, str, float)
    gpuPreferenceChanged = Signal(bool)
    languageChangeRequested = Signal()

    def __init__(self, data_dir):
        super().__init__()
        self.catalog = Catalog(data_dir)
        self.auth=CodexAuth(executable=self.catalog.preference('codex_executable'),parent=self)
        self.auth_dialog=None
        self.command_dialog=None
        self.codex_login_pending=False
        self.setWindowTitle('Grainy — Photo Studio')
        self.resize(1510,960)
        self.setMinimumSize(1120,740)
        self.setAcceptDrops(True)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.source_pool=QThreadPool(self);self.source_pool.setMaxThreadCount(2)
        from .source_cache import DecodedSourceCache
        self.source_cache=DecodedSourceCache()
        # One background thread decodes the neighbours of the current photo into source_cache.
        from concurrent.futures import ThreadPoolExecutor
        self.prefetch_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='GrainyPrefetch')
        self.prefetch_generation=0
        self.live_pool = QThreadPool(self)
        self.live_pool.setMaxThreadCount(1)
        self.quality_pool = QThreadPool(self)
        self.quality_pool.setMaxThreadCount(1)
        self.live_cache=DevelopmentCache(32*1024*1024)
        from . import native_gpu
        if native_gpu.enabled():
            # Create the device off the GUI thread so the first edit does not wait for it.
            Thread(target=native_gpu.status,daemon=True,name='GrainyGpuWarmup').start()
        from .render_cache import physical_memory
        memory=physical_memory() or 8*1024**3
        # 100% views develop the full source; keep every stage so a slider recomputes only its own stage.
        self.quality_cache=DevelopmentCache(160*1024*1024,per_source=8,ceiling=min(4*1024**3,memory//4))
        self.jobs = {};self.closing=False
        self.browser_pool=QThreadPool(self);self.browser_pool.setMaxThreadCount(1)
        self.thumbnail_pool=QThreadPool(self);self.thumbnail_pool.setMaxThreadCount(2)
        self.thumbnail_render_pool=QThreadPool(self);self.thumbnail_render_pool.setMaxThreadCount(1)
        self.display_pool=QThreadPool(self);self.display_pool.setMaxThreadCount(2)
        self.library_version=0;self.library_loading=False;self.library_cancel=Event();self.visible_ids=[]
        self.library_ensure_current=False;self.browser_preferences=self.catalog.preference('browser_view',{})
        self.photo_model=PhotoModel(self,self.thumbnail_pool)
        from .preview_queue import PreviewQueue
        self.preview_queue=PreviewQueue(self,self.thumbnail_render_pool)
        self.photo_selection=QItemSelectionModel(self.photo_model,self)
        self.photo_selection.currentChanged.connect(lambda current,previous:self.on_current(current))
        self.current_id = None
        self.source = None
        self.live_source = None
        self.full_source=None
        self.full_loading=False
        self.original_size=None
        self.settings = defaults()
        self.last_saved = defaults()
        self.undo_stack, self.redo_stack = [],[]
        self.load_version = self.render_version = 0
        self.live_running = False
        self.quality_running = False
        self.live_pending = False
        self.quality_pending = False
        self.presented_version = -1
        self.presented_quality = ''
        self.filter_mode = 'all'
        self.folder_preferences=self.catalog.preference('folder_panel',{})
        self.folder_filter=self.folder_preferences.get('selected')
        self.folder_items={}
        self.folder_tree_signature=None
        self.expanded_folders=set(self.folder_preferences.get('expanded',[]))
        self.folder_tree_updating=False
        self.copied = None
        self.export_running = False
        self.import_queue = deque()
        self.import_busy = False
        self.import_errors = []
        self.import_scans = 0
        self.import_pending_paths=set()
        self.import_extras={}      # path key -> {'keywords', 'preset'} for copy imports (luma/import_copy.py)
        self.import_cancel=Event()
        self.import_refresh_timer=QTimer(self);self.import_refresh_timer.setSingleShot(True)
        self.import_refresh_timer.timeout.connect(lambda:self.refresh_lists(ensure_current=self.current_id is None))
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(16)
        self.preview_timer.timeout.connect(self.render_live)
        self.refine_timer = QTimer(self)
        self.refine_timer.setSingleShot(True)
        self.refine_timer.setInterval(220)
        self.refine_timer.timeout.connect(self.render_quality)
        self.commit_timer = QTimer(self)
        self.commit_timer.setSingleShot(True)
        self.commit_timer.setInterval(450)
        self.commit_timer.timeout.connect(self.commit)
        self.build_ui()
        self.import_cancel_button=button(tr('가져오기 중단'),self.cancel_import)
        self.statusBar().addPermanentWidget(self.import_cancel_button);self.import_cancel_button.hide()
        self.preview_cancel_button=button(tr('일괄 미리보기 중단'),self.preview_queue.stop)
        self.statusBar().addPermanentWidget(self.preview_cancel_button);self.preview_cancel_button.hide()
        from .studio import StudioTools
        self.studio=StudioTools(self)
        self.tabs.finish()
        from .display_color import DisplayColor
        self.display_color=DisplayColor(self)
        from .hdr_display import HdrDisplay
        self.hdr_display=HdrDisplay(self)
        from .cloud_backup import CloudBackup
        self.cloud_backup=CloudBackup(self)
        from .manager import LibraryManager
        self.manager=LibraryManager(self)
        # Edit menu; the keys themselves are the window shortcuts, shown here as hints.
        self.edit_menu=QMenu(tr('편집'),self);self.menuBar().insertMenu(self.menuBar().actions()[1],self.edit_menu)
        for entry in [('실행 취소','Ctrl+Z',self.undo),('다시 실행','Ctrl+Shift+Z',self.redo),None,
                ('보정 복사','Ctrl+Shift+C',self.copy_edits),('보정 붙여넣기','Ctrl+Shift+V',self.paste_edits),None,
                ('보정 초기화','',self.reset_edits)]:
            if entry is None:self.edit_menu.addSeparator();continue
            title,key,function=entry
            self.edit_menu.addAction(tr(title)+('\t'+key if key else ''),lambda checked=False,fn=function:fn())
        from .extras import ExtraTools
        self.extras=ExtraTools(self)
        from .folder_panel import FolderPanel
        self.folder_panel=FolderPanel(self)
        from .workflow_ui import WorkflowTools
        self.workflow=WorkflowTools(self)
        settings_menu=self.menuBar().addMenu(tr('설정'))
        settings_menu.setObjectName('settingsMenu')
        self.language_action=settings_menu.addAction(tr('언어 / Language…'))
        self.language_action.triggered.connect(lambda checked=False:self.languageChangeRequested.emit())
        from . import native_gpu
        self.gpu_action=settings_menu.addAction(tr('GPU 가속 사용'))
        self.gpu_action.setCheckable(True);self.gpu_action.setChecked(native_gpu.enabled())
        self.gpu_action.setEnabled(native_gpu.available())
        self.gpu_action.toggled.connect(self.set_gpu)
        from .folder_sync import FolderSync
        self.folder_sync=FolderSync(self)
        self.folder_sync_action=settings_menu.addAction(tr('등록한 폴더의 새 사진 자동으로 가져오기'))
        self.folder_sync_action.setCheckable(True);self.folder_sync_action.setChecked(self.folder_sync.enabled())
        self.folder_sync_action.toggled.connect(self.folder_sync.set_enabled)
        settings_menu.aboutToShow.connect(self.update_gpu_action)
        self.auth.changed.connect(self.update_codex_button)
        self.update_codex_button()
        self.shortcuts()
        self.refresh_lists()
        self.statusBar().showMessage(tr('준비됨  ·  사진을 가져오면 시작할 수 있습니다.'))
        if self.folder_preferences:
            QTimer.singleShot(0,self.restore_folder_selection)

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(24,13,20,13)
        brand = QLabel('Grainy')
        brand.setObjectName('brand')
        header.addWidget(brand)
        subtitle = QLabel(f'PHOTO STUDIO  /  {__version__}')
        subtitle.setObjectName('muted')
        header.addSpacing(15)
        header.addWidget(subtitle)
        header.addStretch()
        from . import features,feedback
        # Shown by the update controller when a newer release exists.
        self.update_button=button('',lambda:None,primary=True);header.addWidget(self.update_button);self.update_button.hide()
        self.feedback_button=button(tr('피드백'),lambda:feedback.show(self))
        self.feedback_button.setToolTip(tr('문제점이나 개선 아이디어 보내기'))
        header.addWidget(self.feedback_button);self.feedback_button.setVisible(feedback.configured())
        self.command_button=button('Codex',self.show_commands)
        header.addWidget(self.command_button);self.command_button.setVisible(features.codex())
        self.library_button = button(tr('라이브러리'), lambda: self.set_mode(1), True)
        self.edit_button = button(tr('현상'), lambda: self.set_mode(0), True)
        self.edit_button.setChecked(True)
        header.addWidget(self.library_button)
        header.addWidget(self.edit_button)
        header.addSpacing(20)
        header.addWidget(button(tr('사진 가져오기'), self.import_files))
        self.export_button = button(tr('내보내기'), self.export_dialog, primary=True)
        header.addWidget(self.export_button)
        layout.addLayout(header)
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split,1)
        left_scroll=QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(230)
        left_scroll.setMaximumWidth(360)
        left = QWidget()
        left_scroll.setWidget(left)
        sidebar = QVBoxLayout(left)
        sidebar.setContentsMargins(12,8,12,12)
        sidebar.setSpacing(5)
        sidebar.addWidget(section('LIBRARY'))
        self.library_count = QLabel(tr('0장의 사진'))
        self.library_count.setObjectName('muted')
        sidebar.addWidget(self.library_count)
        self.library_buttons={}
        for label, mode in [('모든 사진','all'), ('선택 표시한 사진','picked'), ('별점 4개 이상','rated'), ('제외 표시한 사진','rejected')]:
            b = button(label, lambda checked=False,m=mode:self.set_filter(m),check=True)
            b.setObjectName('nav')
            self.library_buttons[mode]=b
            sidebar.addWidget(b)
        folder_header=QHBoxLayout()
        folder_header.addWidget(section(tr('폴더')),1)
        add_folder=button('+',self.import_folder)
        add_folder.setFixedWidth(30)
        add_folder.setToolTip(tr('사진 폴더 가져오기'))
        folder_header.addWidget(add_folder)
        sidebar.addLayout(folder_header)
        self.folder_search=QLineEdit()
        self.folder_search.setPlaceholderText(tr('폴더 찾기'))
        self.folder_search.setClearButtonEnabled(True)
        self.folder_search.textChanged.connect(self.filter_folder_tree)
        sidebar.addWidget(self.folder_search)
        self.include_subfolders=QCheckBox(tr('하위 폴더 사진 포함'))
        self.include_subfolders.setChecked(self.folder_preferences.get('include_subfolders',True))
        self.include_subfolders.toggled.connect(self.subfolders_changed)
        sidebar.addWidget(self.include_subfolders)
        self.folder_tree=QTreeWidget()
        self.folder_tree.setColumnCount(2)
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setIndentation(16)
        self.folder_tree.setUniformRowHeights(True)
        self.folder_tree.setMinimumHeight(240)
        self.folder_tree.setMaximumHeight(500)
        self.folder_tree.header().setStretchLastSection(False)
        self.folder_tree.header().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.folder_tree.header().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
        self.folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_tree.customContextMenuRequested.connect(self.folder_context_menu)
        self.folder_tree.currentItemChanged.connect(self.folder_current_changed)
        self.folder_tree.itemExpanded.connect(lambda item:self.folder_expansion_changed(item,True))
        self.folder_tree.itemCollapsed.connect(lambda item:self.folder_expansion_changed(item,False))
        sidebar.addWidget(self.folder_tree,1)
        self.folder_empty=QLabel(tr('사진 폴더를 가져오면 여기에 표시됩니다.'))
        self.folder_empty.setObjectName('muted')
        self.folder_empty.setWordWrap(True)
        sidebar.addWidget(self.folder_empty)
        presets_content=QWidget()
        presets_layout=QVBoxLayout(presets_content)
        presets_layout.setContentsMargins(0,0,0,0)
        self.preset_list = QListWidget()
        self.preset_list.setFixedHeight(190)
        self.preset_list.itemClicked.connect(self.apply_preset)
        presets_layout.addWidget(self.preset_list)
        self.refresh_presets()
        presets_layout.addWidget(button(tr('현재 보정을 프리셋으로 저장'), self.save_preset))
        self.add_foldout(sidebar,tr('프리셋'),presets_content)
        self.history_list = QListWidget()
        self.history_list.setFixedHeight(135)
        self.history_list.setToolTip(tr('더블클릭하여 해당 보정 이전 상태로 복원'))
        self.history_list.itemDoubleClicked.connect(self.restore_history)
        self.add_foldout(sidebar,tr('보정 기록'),self.history_list)
        sidebar.addWidget(button(tr('사용법 / 구현 범위'), self.show_help))
        footer = QLabel(tr('LOCAL FIRST\n원본 보존 · 보정값 자동 저장'))
        footer.setObjectName('muted')
        sidebar.addWidget(footer)
        split.addWidget(left_scroll)
        center = QWidget()
        center_box = QVBoxLayout(center)
        center_box.setContentsMargins(0,0,0,0)
        center_box.setSpacing(0)
        self.library_toolbar=QWidget()
        library_toolbar_box=QVBoxLayout(self.library_toolbar)
        library_toolbar_box.setContentsMargins(0,0,0,0)
        library_toolbar_box.setSpacing(0)
        source_bar=QHBoxLayout()
        source_bar.setContentsMargins(15,9,15,2)
        self.source_label=QLabel(tr('모든 사진'))
        self.source_label.setObjectName('muted')
        self.source_label.setMinimumWidth(0)
        self.source_count=QLabel('')
        self.source_count.setObjectName('muted')
        source_bar.addWidget(self.source_label,1)
        source_bar.addWidget(self.source_count)
        library_toolbar_box.addLayout(source_bar)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(15,8,15,8)
        self.filename = QLabel('YOUR LIGHT. YOUR LOOK.')
        self.filename.setObjectName('muted')
        toolbar.addWidget(self.filename,1)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('파일명 · 키워드 검색'))
        self.search.setMaximumWidth(230)
        self.search.textChanged.connect(lambda:self.refresh_lists(ensure_current=True))
        toolbar.addWidget(self.search)
        self.sort_order=QComboBox();self.sort_order.setMaximumWidth(130)
        for title,key in [('가져온 순서','imported'),('파일 이름','name'),('촬영 시간','capture'),('별점','rating')]:self.sort_order.addItem(tr(title),key)
        self.sort_order.setCurrentIndex(max(0,self.sort_order.findData(self.browser_preferences.get('sort','imported'))))
        self.sort_order.currentIndexChanged.connect(self.change_sort);toolbar.addWidget(self.sort_order)
        self.sort_descending=button('↓',self.change_sort,check=True)
        self.sort_descending.setChecked(self.browser_preferences.get('descending',False))
        self.sort_descending.setToolTip(tr('역순으로 정렬'));self.sort_descending.setMaximumWidth(35);toolbar.addWidget(self.sort_descending)
        library_toolbar_box.addLayout(toolbar)
        center_box.addWidget(self.library_toolbar)
        self.library_toolbar.hide()
        self.stack = QStackedWidget()
        self.view = PhotoView()
        self.view.cropSelected.connect(self.apply_crop)
        self.view.cropChanged.connect(self.crop_changed)
        self.view.cropCancelled.connect(self.cancel_crop)
        self.view.straightenDragged.connect(self.drag_straighten)
        self.view.straightenFinished.connect(self.finish_interaction)
        self.view.sampled.connect(self.sample_wb)
        self.view.fullResolutionRequested.connect(self.load_full_resolution)
        self.stack.addWidget(self.view)
        self.grid = self.photo_list(grid=True)
        self.grid.photoClicked.connect(self.open_library_photo)
        self.stack.addWidget(self.grid)
        center_box.addWidget(self.stack,1)
        tools = QHBoxLayout()
        tools.setContentsMargins(12,9,12,9)
        tools.addWidget(button(tr('맞춤'), self.view.fit_photo))
        tools.addWidget(button('1:1', self.view.actual_size))
        self.clipping_button=button(tr('클리핑'),self.toggle_clipping,True)
        tools.addWidget(self.clipping_button)
        self.zoom_label = QLabel(tr('화면 맞춤'))
        self.zoom_label.setObjectName('muted')
        self.view.zoomChanged.connect(self.zoom_label.setText)
        tools.addWidget(self.zoom_label)
        tools.addStretch()
        self.before_button = button(tr('원본 비교'), self.toggle_before, True)
        self.before_button.setToolTip(tr('단축키: \\'))
        tools.addWidget(self.before_button)
        tools.addWidget(button(tr('실행 취소'), self.undo))
        tools.addWidget(button(tr('다시 실행'), self.redo))
        center_box.addLayout(tools)
        self.filmstrip = self.photo_list(grid=False)
        self.filmstrip.setFixedHeight(132)
        center_box.addWidget(self.filmstrip)
        split.addWidget(center)
        right = QWidget()
        right.setMinimumWidth(290)
        right.setMaximumWidth(360)
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(16,8,16,12)
        right_box.addWidget(section('HISTOGRAM'))
        self.histogram = Histogram()
        self.histogram.clippingChanged.connect(self.clipping_changed)
        right_box.addWidget(self.histogram)
        self.info_label = QLabel(tr('사진 정보'))
        self.info_label.setObjectName('muted')
        self.info_label.setWordWrap(True)
        right_box.addWidget(self.info_label)
        from .develop_panels import DevelopPanels
        self.tabs = DevelopPanels(self)
        self.adjustments = {}
        scroll, edits = self.tab_page(tr('보정'))
        self.profile_box=QVBoxLayout();edits.addLayout(self.profile_box)
        auto_row = QHBoxLayout()
        self.auto_button=button(tr('자동 톤'),self.auto_tone)
        self.auto_button.setToolTip(tr('현재 크롭 영역의 밝기 하위·상위 0.1%를 기준으로 분석합니다. 검정·흰색 양끝에 3% 여유를 두고 약 8–247/255로 보정합니다.'))
        auto_row.addWidget(self.auto_button)
        self.auto_color_button=button(tr('자동 색'),lambda:self.auto_color())
        self.auto_color_button.setToolTip(tr('어두운 곳·중간·밝은 곳의 무채색 영역을 각각 찾아 색 틀어짐(녹색 끼 등)을 채널 커브로 바로잡습니다. 밝기는 그대로 둡니다. 강한 색 조명만 있는 영역은 건드리지 않습니다.'))
        auto_row.addWidget(self.auto_color_button)
        self.hdr_button=button('HDR',self.toggle_hdr,True)
        auto_row.addWidget(self.hdr_button)
        from . import engine
        self.hdr_button.setVisible(engine.HDR_FEATURE)
        edits.addLayout(auto_row)
        # Shown after auto colour on the current photo: the same analysis at another strength.
        from PySide6.QtWidgets import QSlider
        self.auto_color_row=QWidget();amount_row=QHBoxLayout(self.auto_color_row);amount_row.setContentsMargins(0,0,0,0)
        self.auto_color_slider=QSlider(Qt.Orientation.Horizontal);self.auto_color_slider.setRange(0,100)
        self.auto_color_slider.setValue(int(self.catalog.preference('auto_color_amount',100)))
        self.auto_color_value=QLabel(f'{self.auto_color_slider.value()}%')
        self.auto_color_slider.valueChanged.connect(lambda value:self.auto_color_value.setText(f'{value}%'))
        self.auto_color_slider.sliderReleased.connect(lambda:self.auto_color(remember=True))
        for item in (QLabel(tr('자동 색 세기')),self.auto_color_slider,self.auto_color_value):amount_row.addWidget(item)
        self.auto_color_row.hide();edits.addWidget(self.auto_color_row)
        self.auto_tone_note=QLabel(tr('자동톤: 검정·흰색 양끝 3% 여유\n목표 밝기 약 8–247 / 255'))
        self.auto_tone_note.setObjectName('muted')
        self.auto_tone_note.setWordWrap(True)
        self.auto_tone_note.hide()
        self.wb_box=QVBoxLayout();edits.addLayout(self.wb_box)
        self.wb_box.addWidget(section(tr('화이트 밸런스')))
        self.wb_button = button(tr('화이트 밸런스 스포이드'), self.toggle_wb, True)
        self.view.sampleCancelled.connect(lambda:self.wb_button.setChecked(False))
        self.wb_box.addWidget(self.wb_button)
        for key,label in [('temperature','색온도 (상대값)'),('tint','색조')]:self.add_adjustment(self.wb_box,key,label)
        edits.addWidget(section(tr('톤')))
        for key,label,low,high,scale in [('exposure','노출',-5,5,100),('contrast','대비',-100,100,1),('highlights','하이라이트',-100,100,1),('shadows','그림자',-100,100,1),('whites','흰색 계열',-200,200,1),('blacks','검정 계열',-200,200,1)]:
            self.add_adjustment(edits,key,label,low,high,scale)
        self.hdr_panel=QWidget();hdr_layout=QVBoxLayout(self.hdr_panel);hdr_layout.setContentsMargins(0,0,0,0)
        hdr_layout.addWidget(section('HDR'))
        self.add_adjustment(hdr_layout,'hdr_limit',tr('HDR 한계 (스톱)'),0,4,10)
        self.add_adjustment(hdr_layout,'hdr_brightness',tr('HDR 영역 밝기'),-100,100,1)
        self.hdr_visualize=QCheckBox(tr('HDR 영역 표시'))
        self.hdr_visualize.toggled.connect(self.toggle_hdr_visualize)
        hdr_layout.addWidget(self.hdr_visualize)
        self.hdr_status=QLabel();self.hdr_status.setWordWrap(True);self.hdr_status.setObjectName('muted');hdr_layout.addWidget(self.hdr_status)
        self.hdr_sdr_preview=QCheckBox(tr('SDR 디스플레이용 미리보기'));hdr_layout.addWidget(self.hdr_sdr_preview)
        self.hdr_sdr_controls=QWidget();sdr_layout=QVBoxLayout(self.hdr_sdr_controls);sdr_layout.setContentsMargins(0,0,0,0)
        self.add_adjustment(sdr_layout,'sdr_exposure',tr('SDR 미리보기 노출'),-5,5,100)
        self.add_adjustment(sdr_layout,'sdr_compression',tr('SDR 하이라이트 압축'),0,100,1)
        hdr_layout.addWidget(self.hdr_sdr_controls)
        edits.addWidget(self.hdr_panel);self.hdr_panel.hide()
        edits.addWidget(section(tr('존재감')))
        self.presence_box=QVBoxLayout();edits.addLayout(self.presence_box)
        self.add_adjustment(self.presence_box,'clarity',tr('부분 대비'))
        for key,label in [('vibrance','생동감'),('saturation','채도')]:
            self.add_adjustment(edits,key,label)
        self.bw = QCheckBox(tr('흑백').replace('&','&&'))
        self.bw.toggled.connect(lambda value:self.set_setting('monochrome',value))
        auto_row.insertWidget(0,self.bw)
        edits=self.panel_box('curve')
        from .targeted import TargetedAdjust
        self.targeted=TargetedAdjust(self)
        self.curve = ToneCurve()
        self.curve.changed.connect(lambda pts:self.set_setting('curve',pts))
        self.curve.committed.connect(self.finish_interaction)
        edits.addWidget(self.curve)
        target=button('사진에서 드래그로 조정',lambda checked:self.targeted.toggle('curve',checked),True)
        target.setToolTip(tr('사진의 한 곳을 누른 채 위아래로 끌면 그 밝기의 커브가 올라가거나 내려갑니다.'))
        self.targeted.buttons['curve']=target;edits.addWidget(target)
        edits=self.panel_box('mixer')
        self.hsl_color = QComboBox()
        self.hsl_color.addItems([tr('빨강'),tr('주황'),tr('노랑'),tr('초록'),tr('청록'),tr('파랑'),tr('보라'),tr('자홍')])
        self.hsl_color.currentIndexChanged.connect(self.update_hsl)
        edits.addWidget(self.hsl_color)
        target_row=QHBoxLayout()
        target=button('사진에서 드래그로 조정',lambda checked:self.targeted.toggle('hsl',checked),True)
        target.setToolTip(tr('사진의 한 곳을 누른 채 위아래로 끌면 그 색에 해당하는 색상 혼합 값이 바뀝니다.'))
        self.targeted.buttons['hsl']=target;target_row.addWidget(target,1)
        self.hsl_target=QComboBox();self.hsl_target.addItems([tr('색상'),tr('채도'),tr('밝기 (HSV)')]);self.hsl_target.setCurrentIndex(1)
        self.hsl_target.setAccessibleName(tr('사진에서 조정할 항목'));target_row.addWidget(self.hsl_target)
        edits.addLayout(target_row)
        self.hsl_widgets = []
        for i,label in enumerate(['색상','채도','밝기 (HSV)']):
            control = Adjustment(str(i),label)
            control.changed.connect(self.hsl_changed)
            control.committed.connect(self.finish_interaction)
            self.hsl_widgets.append(control)
            edits.addWidget(control)
        # 0–150: at 100 the unsharp gain (amount/65) is moderate, so the range goes further.
        self.add_adjustment(self.panel_box('detail'),'sharpen',tr('선명하게'),0,150)
        effects=self.panel_box('effects')
        # Effects panel: post-crop vignette shape and grain character. Double-click resets
        # each slider to the value that reproduces the plain vignette/grain.
        for key,label,low,high,default in [('vignette','비네팅',0,100,0),('vignette_midpoint','중간점',0,100,50),
                ('vignette_roundness','둥글기',-100,100,0),('vignette_feather','페더',0,100,50),
                ('vignette_highlights','하이라이트 보호',0,100,0),('grain','그레인',0,100,0),
                ('grain_size','크기',0,100,25),('grain_roughness','거칠기',0,100,50)]:
            self.add_adjustment(effects,key,label,low,high);self.adjustments[key].default_value=default
        _, crop = self.tab_page(tr('크롭'))
        crop.addWidget(section('FRAME'))
        hint = QLabel(tr('모서리·변을 드래그해 크기를 조절하고,\n안쪽을 드래그해 위치를 옮기세요.\n틀 바깥을 드래그하면 사진이 회전합니다.\nEnter: 적용 / Esc: 취소'))
        hint.setWordWrap(True)
        hint.setObjectName('muted')
        crop.addWidget(hint)
        self.crop_button = button(tr('크롭 편집'), self.toggle_crop, True)
        crop.addWidget(self.crop_button)
        crop_actions=QHBoxLayout()
        self.crop_apply=button(tr('적용'),self.view.accept_crop,primary=True)
        self.crop_cancel=button(tr('취소'),self.cancel_crop)
        self.crop_apply.setEnabled(False)
        self.crop_cancel.setEnabled(False)
        crop_actions.addWidget(self.crop_apply)
        crop_actions.addWidget(self.crop_cancel)
        crop.addLayout(crop_actions)
        self.crop_size=QLabel('')
        self.crop_size.setObjectName('muted')
        crop.addWidget(self.crop_size)
        self.aspect = QComboBox()
        for label,ratio in [('자유 비율',0),('1 : 1',1),('3 : 2',1.5),('4 : 3',4/3),('16 : 9',16/9),('2 : 3',2/3),('4 : 5',.8),('9 : 16',9/16),('65 : 24 · 파노라마',65/24)]:
            self.aspect.addItem(tr(label),ratio)
        self.aspect.currentIndexChanged.connect(lambda:self.set_aspect())
        crop.addWidget(self.aspect)
        self.crop_reset_button=button(tr('크롭 초기화'),self.reset_crop)
        crop.addWidget(self.crop_reset_button)
        self.border_crop_button=button(tr('스캔 테두리 자동 자르기'),self.auto_border_crop)
        self.border_crop_button.setToolTip(tr('필름 스캔 가장자리의 검은 필름 테두리를 찾아 크롭합니다. 크롭 초기화나 실행 취소로 되돌릴 수 있습니다.'))
        crop.addWidget(self.border_crop_button)
        crop.addWidget(button(tr('시계 방향으로 90° 회전'), self.rotate_photo))
        crop.addWidget(button(tr('좌우 반전'), lambda:self.set_setting('flip',not self.settings['flip'])))
        self.auto_level_button=button(tr('자동 수평 맞추기'),self.auto_straighten)
        self.auto_level_button.setToolTip(tr('사진의 수평·수직 직선을 분석해 기울기를 보정합니다.'))
        crop.addWidget(self.auto_level_button)
        self.auto_level_note=QLabel(tr('직선이 보이는 사진에서 자동 보정한 뒤 아래 슬라이더로 미세 조절하세요.'))
        self.auto_level_note.setObjectName('muted');self.auto_level_note.setWordWrap(True);crop.addWidget(self.auto_level_note)
        self.add_adjustment(crop,'straighten',tr('수평 맞추기'),-45,45,10)
        crop_note = QLabel(tr('각도를 바꾸면 크롭 틀이 사진 안쪽에 맞게 자동으로 조정됩니다. 틀 바깥은 적용할 때 잘립니다.'))
        crop_note.setObjectName('muted')
        crop_note.setWordWrap(True)
        crop.addWidget(crop_note)
        crop.addStretch()
        _, info = self.tab_page(tr('관리'))
        info.addWidget(section('ORGANIZE'))
        self.rating = QComboBox()
        self.rating.addItems([tr('별점 없음'),'★','★★','★★★','★★★★','★★★★★'])
        self.rating.currentIndexChanged.connect(lambda rating:self.set_rating(rating))
        info.addWidget(self.rating)
        self.flag = QComboBox()
        self.flag.addItem(tr('표시 없음'),0)
        self.flag.addItem(tr('선택 표시'),1)
        self.flag.addItem(tr('제외 표시'),-1)
        self.flag.currentIndexChanged.connect(lambda:self.set_flag(self.flag.currentData()))
        info.addWidget(self.flag)
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText(tr('키워드: 여행, 인물, 필름…'))
        self.keywords.editingFinished.connect(self.save_keywords)
        info.addWidget(self.keywords)
        info.addWidget(section('BATCH EDIT'))
        info.addWidget(QLabel(keys(tr('Ctrl / Shift로 여러 사진을 선택하세요.'))))
        info.addWidget(button(tr('현재 보정 복사'), self.copy_edits))
        self.copy_crop = QCheckBox(tr('붙여넣기에 크롭·회전도 포함'))
        info.addWidget(self.copy_crop)
        self.copy_crop.toggled.connect(self.copy_crop_changed)
        info.addWidget(button(tr('선택한 사진에 보정 붙여넣기'), self.paste_edits))
        info.addWidget(section('FILE'))
        self.file_info = QLabel('')
        self.file_info.setWordWrap(True)
        self.file_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info.addWidget(self.file_info)
        info.addWidget(button(tr('파일 위치 열기'), self.show_file))
        info.addStretch()
        right_box.addWidget(self.tabs,1)
        footer=QHBoxLayout();footer.setSpacing(4)
        footer.addWidget(button(tr('동기화'),lambda:self.manager.sync_selected()))
        footer.addWidget(button(tr('초기화'),self.reset_edits))
        right_box.addLayout(footer)
        split.addWidget(right)
        split.setSizes([270,910,330])
        self.tabs.setEnabled(False)

    def tab_page(self,label):
        return self.tabs.register_tab(tr(label))

    def panel_box(self,key):
        return self.tabs.box(key)

    def advanced_box(self,parent,title):
        content=QWidget();box=QVBoxLayout(content);box.setContentsMargins(0,0,0,0)
        self.add_foldout(parent,tr(title),content)
        return box

    def add_foldout(self,layout,title,content):
        toggle=QToolButton()
        toggle.setObjectName('foldout')
        toggle.setText(tr(title))
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.setArrowType(Qt.ArrowType.RightArrow)
        toggle.setCheckable(True)
        content.foldout_toggle=toggle
        content.setVisible(False)
        def changed(expanded):
            content.setVisible(expanded)
            toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        toggle.toggled.connect(changed)
        layout.addWidget(toggle)
        layout.addWidget(content)

    def add_adjustment(self,box,key,label,low=-100,high=100,scale=1):
        control = Adjustment(key,label,low,high,scale)
        control.changed.connect(self.set_straighten if key=='straighten' else self.set_setting)
        control.committed.connect(self.finish_interaction)
        if key in ('exposure','whites','blacks'):
            control.autoRequested.connect(self.auto_slider);control.supports_auto=True
            control.slider.setToolTip(tr('더블클릭하면 기본값으로 돌아갑니다. Shift+더블클릭하면 이 값만 자동으로 맞춥니다.'))
        self.adjustments[key] = control
        box.addWidget(control)

    def photo_list(self,grid):
        view = PhotoListView(self)
        view.setModel(self.photo_model);view.setSelectionModel(self.photo_selection)
        view.setViewMode(QListWidget.ViewMode.IconMode)
        view.setResizeMode(QListWidget.ResizeMode.Adjust)
        view.setMovement(QListWidget.Movement.Static)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setIconSize(QSize(200,146) if grid else QSize(112,77))
        view.setGridSize(QSize(228,190) if grid else QSize(140,111))
        view.setSpacing(8 if grid else 3)
        view.setWordWrap(False)
        view.setWrapping(grid)
        view.setFlow(QListWidget.Flow.LeftToRight)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda position,target=view:self.photo_context_menu(target,position))
        return view

    def open_library_photo(self,index):
        if self.library_loading or not index.isValid():return
        self.activate(index.data(Qt.ItemDataRole.UserRole))
        self.view.fit_photo()
        self.set_mode(0)

    def photo_context_menu(self,view,position):
        if self.library_loading:return
        index=view.indexAt(position)
        if not index.isValid():return
        if not self.photo_selection.isSelected(index):
            self.photo_selection.select(index,QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.photo_selection.setCurrentIndex(index,QItemSelectionModel.SelectionFlag.NoUpdate)
        from .photo_actions import build_menu
        menu=build_menu(self)
        menu.exec(view.viewport().mapToGlobal(position))
        menu.deleteLater()

    def shortcuts(self):
        quick=QShortcut(QKeySequence('Ctrl+E'),self)
        quick.activated.connect(lambda:self.safe_shortcut(lambda:self.quick_export('previous')))
        for key,action in [('Ctrl+O',self.import_files),('Ctrl+Shift+E',self.export_dialog),('Ctrl+Z',self.undo),('Ctrl+Shift+Z',self.redo),('Ctrl+Y',self.redo),('Ctrl+Shift+C',self.copy_edits),('Ctrl+Shift+V',self.paste_edits),('\\',lambda:self.before_button.click()),('G',lambda:self.set_mode(1)),('D',lambda:self.set_mode(0)),('R',lambda:self.crop_button.click()),('P',lambda:self.set_flag(1)),('X',lambda:self.set_flag(-1)),('U',lambda:self.set_flag(0)),
                ('B',lambda:self.manager.run(self.workflow.quick_toggle)),('Ctrl+Alt+V',lambda:self.manager.run(self.workflow.previous_edits))]:
            shortcut = QShortcut(QKeySequence(key),self)
            shortcut.activated.connect(lambda fn=action:self.safe_shortcut(fn))
        for i in range(6):
            shortcut = QShortcut(QKeySequence(str(i)),self)
            shortcut.activated.connect(lambda value=i:self.safe_shortcut(lambda:self.set_rating(value)))

    def safe_shortcut(self, action):
        if isinstance(QApplication.focusWidget(), (QLineEdit,QSpinBox)):
            return
        action()

    def spawn(self,function,callback,error=None,pool=None):
        token = uuid4().hex
        worker = Worker(token,function)
        self.jobs[token] = (worker,callback,error)
        worker.signals.done.connect(self.job_finished)
        (pool or self.pool).start(worker)

    def job_finished(self,token,result,error):
        entry = self.jobs.pop(token,None)
        if entry is None or self.closing:
            return
        _,callback,on_error = entry
        if error:
            if on_error:
                on_error(error)
            else:
                self.show_error(error)
        else:
            try:
                callback(result)
            except Exception:
                self.show_error(traceback.format_exc())

    def show_error(self,message):
        self.statusBar().showMessage(tr('처리하지 못했습니다. 자세한 내용을 확인해 주세요.'))
        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr('처리 오류'))
        dialog.setText(message.strip().splitlines()[-1])
        dialog.setDetailedText(message)
        dialog.exec()

    def set_mode(self,index):
        self.stack.setCurrentIndex(index)
        self.library_toolbar.setVisible(index==1)
        self.library_button.setChecked(index==1)
        self.edit_button.setChecked(index==0)
        self.filmstrip.setVisible(index==0)

    def set_filter(self,mode):
        self.filter_mode=mode
        if mode=='all':
            self.folder_filter=None
            self.folder_tree.blockSignals(True)
            self.folder_tree.setCurrentItem(None)
            self.folder_tree.clearSelection()
            self.folder_tree.blockSignals(False)
        self.refresh_lists(ensure_current=True)
        self.set_mode(1)
        self.save_folder_state()

    def refresh_lists(self,*args,ensure_current=False):
        if self.closing:return
        self.library_cancel.set();self.library_cancel=Event();cancel=self.library_cancel
        self.library_version+=1;version=self.library_version;self.library_loading=True
        self.library_ensure_current=self.library_ensure_current or ensure_current
        self.grid.setEnabled(False);self.filmstrip.setEnabled(False)
        manager=getattr(self,'manager',None)
        options=dict(search=self.search.text(),folder=self.folder_filter,
            recursive=self.include_subfolders.isChecked() or bool(self.folder_filter and Path(self.folder_filter)==Path(self.folder_filter).parent),
            mode=self.filter_mode,rules=deepcopy(manager.rules) if manager else {},collection=manager.collection if manager else None,
            collapsed=manager.collapsed if manager else False,stack_orders=self.catalog.preference('stack_orders',{}),sort=self.sort_order.currentData(),descending=self.sort_descending.isChecked())
        name=Path(self.folder_filter).name or self.folder_filter if self.folder_filter else tr('모든 사진')
        self.source_label.setText(name)
        self.source_label.setToolTip(self.folder_filter or tr('라이브러리에 가져온 모든 사진'))
        self.source_count.setText(tr('불러오는 중…'))
        for mode,b in self.library_buttons.items():
            b.setChecked(mode==self.filter_mode and (mode!='all' or not self.folder_filter))
        path=self.catalog.directory/'catalog.sqlite'
        def work():
            try:return library_query(path,options,cancel)
            except QueryCancelled:return None
        def ready(result):
            if version!=self.library_version or result is None:return
            selected=self.active_list().selected_ids();self.visible_ids=result['ids']
            self.photo_selection.blockSignals(True)
            self.photo_model.replace(self.visible_ids)
            self.active_list().restore_selection(selected,self.current_id)
            self.photo_selection.blockSignals(False)
            self.library_count.setText(tr('{0}장의 사진', f"{result['total']:,}"));self.source_count.setText(tr('{0}장', f'{len(self.visible_ids):,}'))
            self.refresh_folder_tree(result['folders'],result['roots'])
            self.library_loading=False
            self.grid.setEnabled(True);self.filmstrip.setEnabled(True)
            ensure_current=self.library_ensure_current;self.library_ensure_current=False
            desired=getattr(self,'restore_photo_id',None);self.restore_photo_id=None
            if desired in self.photo_model.positions:
                self.activate(desired);self.photo_selection.blockSignals(True)
                self.active_list().restore_selection([desired],desired);self.photo_selection.blockSignals(False)
            elif ensure_current and self.current_id not in self.photo_model.positions:
                if self.visible_ids:
                    self.activate(self.visible_ids[0]);self.photo_selection.blockSignals(True)
                    self.active_list().restore_selection([self.current_id],self.current_id);self.photo_selection.blockSignals(False)
                else:self.clear_active_photo()
        def failed(error):
            if version!=self.library_version:return
            self.library_loading=False;self.visible_ids=[];self.photo_model.replace([])
            self.grid.setEnabled(True);self.filmstrip.setEnabled(True);self.clear_active_photo()
            self.source_count.setText(tr('목록을 읽지 못했습니다'));self.show_error(error)
        self.spawn(work,ready,failed,self.browser_pool)

    def change_sort(self,*_):
        self.catalog.save_preference('browser_view',{'sort':self.sort_order.currentData(),'descending':self.sort_descending.isChecked()})
        self.refresh_lists()

    def clear_active_photo(self):
        self.commit()
        self.save_keywords()
        self.current_id=None
        self.source=self.live_source=None
        self.full_source=None;self.full_loading=False;self.original_size=None
        self.settings=self.last_saved=defaults()
        self.load_version+=1
        self.render_version+=1
        self.preview_timer.stop()
        self.refine_timer.stop()
        self.view.clear_photo()
        self.end_crop()
        self.tabs.setEnabled(False)
        self.histogram.clear()
        self.info_label.setText(tr('이 범위에 표시할 사진이 없습니다.'))
        self.filename.setText(tr('표시할 사진 없음'))
        self.file_info.clear()
        self.keywords.clear()
        self.history_list.clear()

    def refresh_folder_tree(self,photos=None,roots=None):
        if photos is None:self.refresh_lists();return
        roots=roots if roots is not None else self.catalog.folder_roots()
        nodes=self.folder_panel.nodes(photos,roots)
        signature=tuple((n['key'],n['parent'],n['direct'],n['total'],n['missing'],n['unscanned']) for n in nodes)
        if signature!=self.folder_tree_signature:
            previous=set(self.folder_items)
            self.folder_tree_signature=signature
            self.folder_tree_updating=True
            self.folder_tree.blockSignals(True)
            self.folder_tree.clear()
            self.folder_items={}
            for node in nodes:
                item=QTreeWidgetItem([('? ' if node['missing'] else '')+node['name'],''])
                item.setData(0,Qt.ItemDataRole.UserRole,node)
                item.setToolTip(0,node['path']+(tr('\n폴더를 찾을 수 없습니다. 우클릭하여 새 위치를 지정하세요.') if node['missing'] else tr('\n등록된 사진 {0}장 · 새 사진은 폴더 동기화로 가져오세요.',node['total'])))
                item.setTextAlignment(1,Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
                item.setIcon(0,self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon if node['drive'] else QStyle.StandardPixmap.SP_DirIcon))
                if node['parent']:
                    self.folder_items[node['parent']].addChild(item)
                else:
                    self.folder_tree.addTopLevelItem(item)
                self.folder_items[node['key']]=item
                if node['unscanned']:
                    item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                if node['total'] and node['key'] not in previous and 'expanded' not in self.folder_preferences:
                    self.expanded_folders.add(node['key'])
                item.setExpanded(node['key'] in self.expanded_folders)
                if self.folder_filter and node['key']==path_key(self.folder_filter):
                    self.folder_tree.setCurrentItem(item)
            self.folder_tree.blockSignals(False)
            self.folder_tree_updating=False
        for item in self.folder_items.values():
            node=item.data(0,Qt.ItemDataRole.UserRole)
            item.setText(1,str(node['total'] if self.include_subfolders.isChecked() or node['drive'] else node['direct']))
            item.setToolTip(1,f'이 폴더 {node["direct"]}장 · 하위 폴더 포함 {node["total"]}장')
        self.folder_empty.setVisible(not nodes)
        self.filter_folder_tree()

    def filter_folder_tree(self,*args):
        query=self.folder_search.text().strip().casefold()
        self.folder_tree_updating=True
        def visit(item,ancestor_matches=False):
            node=item.data(0,Qt.ItemDataRole.UserRole)
            matches=not query or query in node['name'].casefold() or ancestor_matches
            child_matches=[visit(item.child(i),matches) for i in range(item.childCount())]
            visible=matches or any(child_matches)
            item.setHidden(not visible)
            if item.childCount():
                item.setExpanded(bool(query and visible) or node['key'] in self.expanded_folders)
            return visible
        for i in range(self.folder_tree.topLevelItemCount()):
            visit(self.folder_tree.topLevelItem(i))
        self.folder_tree_updating=False

    def folder_expansion_changed(self,item,expanded):
        if self.folder_tree_updating or self.folder_search.text():
            return
        key=item.data(0,Qt.ItemDataRole.UserRole)['key']
        if expanded:
            self.expanded_folders.add(key)
        else:
            self.expanded_folders.discard(key)
        self.save_folder_state()

        if expanded:self.folder_panel.refresh()

    def folder_current_changed(self,item,previous):
        if item:
            self.select_folder(item.data(0,Qt.ItemDataRole.UserRole)['path'])

    def select_folder(self,path):
        self.commit()
        self.save_keywords()
        self.folder_filter=str(Path(path))
        self.refresh_lists(ensure_current=True)
        self.set_mode(1)
        item=self.folder_items.get(path_key(path))
        if item:
            self.folder_tree.blockSignals(True)
            self.folder_tree.setCurrentItem(item)
            self.folder_tree.blockSignals(False)
        self.save_folder_state()

    def subfolders_changed(self,*args):
        self.refresh_lists(ensure_current=True)
        self.save_folder_state()

    def save_folder_state(self):
        self.catalog.save_preference('folder_panel',{'selected':self.folder_filter,
            'include_subfolders':self.include_subfolders.isChecked(),
            'expanded':sorted(self.expanded_folders),'current_photo':self.current_id})

    def restore_folder_selection(self):
        self.restore_photo_id=self.folder_preferences.get('current_photo')
        self.refresh_lists(ensure_current=True)
        if self.folder_filter:
            self.set_mode(1)

    def show_parent_folder(self,path):
        self.catalog.register_folder(Path(path).parent)
        self.expanded_folders.add(path_key(Path(path).parent))
        self.refresh_folder_tree()
        item=self.folder_items.get(path_key(Path(path).parent))
        if item:
            item.setExpanded(True)
        self.save_folder_state()

    def sync_folder(self,path):
        if not Path(path).is_dir():
            self.statusBar().showMessage(tr('폴더를 찾을 수 없습니다. 드라이브 연결이나 원본 위치를 확인해 주세요.'))
            return
        self.import_paths([path])

    def folder_context_menu(self,position):
        item=self.folder_tree.itemAt(position)
        menu=QMenu(self)
        menu.addAction(tr('폴더 가져오기…'),self.import_folder)
        menu.addAction(tr('폴더 목록 새로 고침'),self.folder_panel.refresh)
        menu.addAction(tr('폴더 자동 찾기 중단') if self.folder_panel.recovery_running else tr('이름이 바뀐 폴더 자동 찾기'),
            lambda:QTimer.singleShot(0,lambda:self.folder_panel.recover(explicit=True)))
        if item:
            path=item.data(0,Qt.ItemDataRole.UserRole)['path']
            menu.addSeparator()
            menu.addAction(tr('폴더 동기화 · 새 사진 가져오기'),lambda:self.sync_folder(path))
            menu.addAction(tr('Finder에서 열기') if MAC else tr('탐색기에서 열기'),lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
            if Path(path).parent!=Path(path):
                menu.addAction(tr('상위 폴더 표시'),lambda:self.show_parent_folder(path))
                menu.addAction(tr('폴더 위치 다시 지정…'),lambda:self.manager.run(lambda:self.folder_panel.locate(path)))
        menu.exec(self.folder_tree.viewport().mapToGlobal(position))

    def active_list(self):
        return self.grid if self.stack.currentIndex()==1 else self.filmstrip

    def selected_ids(self):
        if self.library_loading:return []
        result=self.active_list().selected_ids()
        return result or ([self.current_id] if self.current_id in self.photo_model.positions else [])

    def on_current(self,item):
        if item.isValid() and not self.library_loading:
            self.activate(item.data(Qt.ItemDataRole.UserRole))

    def activate(self,photo_id,force=False):
        if photo_id==self.current_id and not force:
            return
        self.commit()
        if hasattr(self,'auto_color_row'):self.auto_color_row.hide()
        self.save_keywords()
        workflow=getattr(self,'workflow',None)
        if workflow is not None and self.current_id is not None and self.current_id!=photo_id:
            workflow.previous_id=self.current_id
        self.current_id = photo_id
        if force:self.source_cache.clear()
        photo = self.catalog.photo(photo_id)
        self.settings = deepcopy(photo['settings'])
        self.last_saved = deepcopy(self.settings)
        state=self.catalog.preference(f'undo:{photo_id}',{})
        self.undo_stack=[normalized(s) for s in state.get('undo',[])]
        self.redo_stack=[normalized(s) for s in state.get('redo',[])]
        self.source=None
        self.live_source=None
        self.full_source=None;self.full_loading=False;self.original_size=None
        self.view.set_tool('')
        self.preview_timer.stop()
        self.refine_timer.stop()
        self.load_version+=1
        version=self.load_version
        self.render_version+=1
        self.before_button.setChecked(False)
        self.wb_button.setChecked(False)
        self.view.sample_mode=False
        self.crop_button.setChecked(False)
        self.crop_apply.setEnabled(False)
        self.crop_cancel.setEnabled(False)
        self.crop_size.clear()
        self.view.set_crop_mode(False)
        self.view.clear_photo(loading=True)
        self.filename.setText(photo['name'])
        self.auto_tone_note.setText(tr('자동톤: 검정·흰색 양끝 3% 여유\n목표 밝기 약 8–247 / 255'))
        self.tabs.setEnabled(False)
        self.load_controls()
        self.statusBar().showMessage(tr('{0} 불러오는 중…', f"{photo['name']}"))
        def ready(result):
            if version!=self.load_version:
                return
            self.source,self.live_source,info=result
            from .preview_store import signature
            self.loaded_file_signature=info.get('file_fingerprint') if not info.get('offline_preview') else None
            self.loaded_working_space=info.get('working_space','sRGB')
            self.loaded_video_settings=deepcopy((photo['settings'].get('video_color'),photo['settings'].get('video_time',0)))
            self.loaded_offline_signature=info.get('offline_fingerprint') if info.get('offline_preview') else None
            if photo['info'].get('raw_default_applied'):info['raw_default_applied']=photo['info']['raw_default_applied']
            self.original_size=(info['width'],info['height'])
            self.catalog.update(photo_id,metadata=json.dumps(info))
            self.tabs.setEnabled(True)
            self.load_controls()
            self.info_label.setText(f'{info["width"]:,} × {info["height"]:,}  ·  {info["format"]}  ·  {info["size_mb"]} MB')
            self.file_info.setText(photo['path']+'\n\n'+'\n'.join(f'{k}: {v}' for k,v in info.items() if k not in ('file_fingerprint','offline_fingerprint')))
            self.render()
            if not self.view.fit_mode:self.load_full_resolution()
            self.prefetch_neighbors(photo_id)
        def failed(error):
            if version==self.load_version:
                self.info_label.setText(tr('사진을 읽을 수 없습니다.'))
                if not Path(photo['path']).is_file():
                    self.statusBar().showMessage(tr('원본 위치를 찾고 있습니다. 폴더 우클릭에서 자동 찾기나 위치 지정을 사용할 수 있습니다.'))
                    self.folder_panel.recover()
                else:self.show_error(error)
        def decode():
            if version!=self.load_version or self.closing:return None
            offline=self.catalog.directory/'previews'/f'{photo_id}.npz'
            def read():
                if not Path(photo['path']).is_file() and offline.is_file():
                    from .preview_store import load_offline
                    return load_offline(self.catalog.directory,photo_id,photo['settings'])
                return load_image(photo['path'],1800,photo['settings']['working_space'],raw_options=photo['settings'])
            source,info=self.source_cache.load(photo['path'],photo['settings'],1800,read,offline=offline)
            return source,resize_float(source,720),info
        self.spawn(decode,ready,failed,pool=self.source_pool)

    def prefetch_neighbors(self,photo_id):
        """Decode the next and previous photos while this one is viewed."""
        if self.closing or photo_id not in self.photo_model.positions:return
        from .engine import VIDEO_EXTENSIONS
        self.prefetch_generation+=1;generation=self.prefetch_generation
        position=self.photo_model.positions[photo_id];ids=self.photo_model.ids
        for index in (position+1,position-1):
            if not 0<=index<len(ids):continue
            photo=self.catalog.photo(ids[index])
            if photo is None or Path(photo['path']).suffix.lower() in VIDEO_EXTENSIONS:continue
            self.prefetch_pool.submit(self._prefetch,generation,photo['path'],deepcopy(photo['settings']))

    def _prefetch(self,generation,path,settings):
        # Stale requests (the user already moved on) are skipped before any decoding.
        if self.closing or generation!=self.prefetch_generation or not Path(path).is_file():return
        try:
            self.source_cache.load(path,settings,1800,lambda:load_image(path,1800,settings['working_space'],raw_options=settings))
        except Exception:
            pass   # the real load reports errors when the photo is opened

    def load_controls(self):
        if hasattr(self,'studio'):self.studio.load_settings()
        for key,control in self.adjustments.items():
            control.set_value(self.settings[key])
        self.sync_crop_limit()
        if hasattr(self,'studio'):self.studio.display_raw_white()
        self.bw.blockSignals(True)
        self.bw.setChecked(self.settings['monochrome'])
        self.bw.blockSignals(False)
        self.sync_hdr_controls()
        self.curve.set_points(self.settings['curve'])
        self.update_hsl()
        if self.current_id:
            p=self.catalog.photo(self.current_id)
            self.rating.blockSignals(True)
            self.rating.setCurrentIndex(p['rating'])
            self.rating.blockSignals(False)
            self.flag.blockSignals(True)
            self.flag.setCurrentIndex(self.flag.findData(p['flag']))
            self.flag.blockSignals(False)
            self.keywords.setText(p['keywords'])
            self.refresh_history()

    def sync_hdr_controls(self):
        enabled=bool(self.settings['hdr'])
        self.hdr_button.blockSignals(True);self.hdr_button.setChecked(enabled);self.hdr_button.blockSignals(False)
        self.hdr_panel.setVisible(enabled)
        self.curve.set_hdr(enabled)

    def toggle_hdr(self,checked):
        if self.source is None:
            self.sync_hdr_controls();return
        if checked and self.hdr_display.failure:
            self.hdr_display.failure='';self.hdr_display.update_ui()
        self.set_setting('hdr',checked);self.sync_hdr_controls();self.finish_interaction()

    def toggle_hdr_visualize(self,checked):
        self.render_version+=1;self.render()

    def set_setting(self,key,value):
        if self.source is None:
            return
        if hasattr(self,'studio'):self.studio.cancel_stroke()
        self.settings[key]=deepcopy(value)
        # Moving Highlights or Shadows takes the photo to the current process for them (roll-off photos only).
        if key in ('highlights','shadows') and self.settings.get('tone_version')==2:self.settings['tone_version']=3
        self.auto_tone_note.setText(tr('자동톤: 검정·흰색 양끝 3% 여유\n목표 밝기 약 8–247 / 255'))
        self.before_button.setChecked(False)
        self.render_version+=1
        # Throttle, don't debounce: a continuous drag must keep producing frames.
        if not self.preview_timer.isActive():
            self.preview_timer.start()
        self.refine_timer.start(220)
        self.commit_timer.start()

    def finish_interaction(self):
        # Released: start the final (quality) render together with the live one instead of 25 ms
        # later, and save after both have started (the catalog write overlaps the renders).
        # On screen (12 library JPEGs): exposure change 152 -> 93 ms, CHANGELOG 0.5.66.
        self.preview_timer.stop()
        self.render_live()
        self.refine_timer.stop()
        self.render_quality()
        self.commit()

    def commit(self,label='보정 변경'):
        self.commit_timer.stop()
        if self.current_id and self.settings!=self.last_saved:
            previous=deepcopy(self.last_saved)
            self.undo_stack.append(deepcopy(self.last_saved))
            self.redo_stack.clear()
            self.catalog.edit(self.current_id,self.settings,label)
            self.photo_model.invalidate_thumbnails([self.current_id])
            self.last_saved=deepcopy(self.settings)
            self.refresh_history()
            self.save_undo()
            if hasattr(self,'manager'):self.manager.after_commit(previous)

    def save_undo(self):
        if self.current_id:
            self.catalog.save_preference(f'undo:{self.current_id}',{'undo':self.undo_stack[-80:],'redo':self.redo_stack[-80:]})

    def refresh_history(self):
        self.history_list.clear()
        for row in self.catalog.histories(self.current_id):
            from .adobe_history import HISTORY_PREFIX
            item=QListWidgetItem(row['label'] if row['label'].startswith(HISTORY_PREFIX) else f'{row["label"]} 이전')
            item.setData(Qt.ItemDataRole.UserRole,row['settings'])
            item.setToolTip(row['created'])
            self.history_list.addItem(item)

    def restore_history(self,item):
        if self.source is None:
            return
        restored=normalized(item.data(Qt.ItemDataRole.UserRole))
        self.commit()
        self.settings=restored
        self.commit()
        self.load_controls()
        self.render_version+=1
        self.render()

    def undo(self):
        self.commit()
        if not self.undo_stack:
            return
        self.redo_stack.append(deepcopy(self.settings))
        self.settings=self.undo_stack.pop()
        self.apply_restored()

    def redo(self):
        self.commit()
        if not self.redo_stack:
            return
        self.undo_stack.append(deepcopy(self.settings))
        self.settings=self.redo_stack.pop()
        self.apply_restored()

    def apply_restored(self):
        self.settings=normalized(self.settings)
        self.save_undo()
        self.auto_tone_note.setText(tr('자동톤: 검정·흰색 양끝 3% 여유\n목표 밝기 약 8–247 / 255'))
        self.last_saved=deepcopy(self.settings)
        self.catalog.set_settings(self.current_id,self.settings)
        self.photo_model.invalidate_thumbnails([self.current_id])
        self.load_controls()
        if self.view.crop_mode:self.view.show_crop(self.settings['crop'])
        self.before_button.setChecked(False)
        self.render_version+=1
        self.render()

    @property
    def render_running(self):
        return (self.live_running or self.quality_running or self.preview_timer.isActive()
                or self.refine_timer.isActive())

    def interacting(self):
        return (bool(self.studio.pending_stroke and not self.studio.pending_stroke.get('staged')) or any(a.slider.isSliderDown() for a in self.findChildren(Adjustment))
                or any(c.active is not None for c in self.findChildren(ToneCurve)))

    def preview_context(self):
        s=defaults() if self.before_button.isChecked() else self.settings
        return (self.load_version,self.before_button.isChecked(),self.crop_button.isChecked(),
                s['rotation'],s['straighten'],s['flip'],tuple(s['crop'] or []),self.display_color.main.revision,self.studio.stroke_generation)

    def preview_size(self,settings,use_crop):
        w,h=self.original_size or (self.source.shape[1],self.source.shape[0])
        if int(settings['rotation'])%2:
            w,h=h,w
        if use_crop=='all':
            from .straighten import padding
            pad_x,pad_y=padding(settings['straighten'],w,h)
            return w+2*pad_x,h+2*pad_y
        if use_crop and settings['crop']:
            x0,y0,x1,y1=settings['crop']
            w,h=max(1,round(x1*w)-round(x0*w)),max(1,round(y1*h)-round(y0*h))
        return w,h

    def render(self):
        self.preview_timer.stop()
        if self.source is None:
            return
        if self.current_id and self.catalog.photo(self.current_id)['info'].get('working_space','sRGB')!=self.settings['working_space']:
            self.commit();self.activate(self.current_id,force=True);return
        self.render_live()
        self.refine_timer.start(120)

    def render_live(self):
        self.preview_timer.stop()
        if self.source is None or self.closing:
            return
        from .rawcolor import CameraSource,enabled
        from .engine import RAW_EXTENSIONS,VIDEO_EXTENSIONS
        if self.current_id:
            photo=self.catalog.photo(self.current_id)
            if (Path(photo['path']).suffix.lower() in VIDEO_EXTENSIONS and Path(photo['path']).is_file()
                    and getattr(self,'loaded_video_settings',None)!=(self.settings.get('video_color'),self.settings.get('video_time',0))):
                self.commit();self.activate(self.current_id,force=True);return
            if Path(photo['path']).suffix.lower() in RAW_EXTENSIONS and enabled(self.settings)!=isinstance(self.source,CameraSource) and (Path(photo['path']).is_file() or photo['info'].get('raw_dual_preview')):
                self.commit();self.activate(self.current_id,force=True);return
        if self.live_running:
            self.live_pending=True
            return
        if self.presented_version==self.render_version and self.presented_quality=='quality':
            return
        self.live_running=True
        self.live_pending=False
        self.start_preview('live')

    def render_quality(self):
        if self.source is None or self.closing:
            return
        if self.interacting():
            self.refine_timer.start(100)
            return
        if self.quality_running:
            self.quality_pending=True
            return
        if self.presented_version==self.render_version and self.presented_quality=='quality':
            return
        self.quality_running=True
        self.quality_pending=False
        self.start_preview('quality')

    def start_preview(self,quality):
        source=self.live_source if quality=='live' else (self.full_source if self.full_source is not None and not self.view.fit_mode else self.source)
        original_size=(source.shape[1],source.shape[0]) if source is self.full_source else self.original_size
        settings=defaults() if self.before_button.isChecked() else deepcopy(self.settings)
        if not self.before_button.isChecked():settings=self.studio.preview_settings(settings)
        settings['working_space']=self.settings['working_space']
        # Crop tool: uncropped, and for a straightened photo on a canvas large enough to show its corners.
        editing=self.crop_button.isChecked()
        use_crop='all' if editing and settings['straighten'] and not settings.get('upright') else not editing
        logical_size=self.preview_size(settings,use_crop)
        canvas=None
        if use_crop=='all':
            inner=self.preview_size(settings,False)
            canvas=((logical_size[0]-inner[0])/2,(logical_size[1]-inner[1])/2,*inner)
        version,context=self.render_version,self.preview_context()
        started=time.perf_counter()
        overlay_index=self.studio.overlay_layer()
        point_overlay=self.studio.point_overlay_target()
        visualize_hdr=settings['hdr'] and self.hdr_visualize.isChecked()
        sdr_preview=self.hdr_display.preview
        hdr_display=self.hdr_display.state
        native_hdr=settings['hdr'] and self.hdr_display.active and not sdr_preview and not visualize_hdr
        monitor_profile=self.display_color.main.profile
        cache=self.live_cache if quality=='live' else self.quality_cache
        save_thumb=(quality=='quality' and not self.studio.pending_stroke and not self.before_button.isChecked() and not self.crop_button.isChecked())
        photo_id=self.current_id;directory=self.catalog.directory
        file_signature=getattr(self,'loaded_file_signature',None);offline_signature=getattr(self,'loaded_offline_signature',None)
        if source is self.full_source:
            file_signature=self.full_file_signature;offline_signature=None
        def work():
            overlay=None
            def capture_mask(index,mask):
                nonlocal overlay
                if index!=overlay_index:return
                small=resize_float(mask[...,None],720)[...,0]
                rgba=np.zeros((*small.shape,4),np.uint8)
                rgba[...,0]=250;rgba[...,1]=65;rgba[...,2]=55;rgba[...,3]=np.uint8(small*105)
                overlay=QImage(rgba.data,rgba.shape[1],rgba.shape[0],rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
            from .engine import output_rgb
            working=develop(source,settings,use_crop,output_space=None,cache=cache,on_mask=capture_mask if overlay_index is not None else None,
                            original_size=original_size)
            if point_overlay is not None:
                from .point_color import preview as color_preview,preview_key
                def color_coverage():
                    try:return resize_float(color_preview(source,settings,point_overlay,use_crop=use_crop,original_size=original_size)[...,None],720)[...,0]
                    except ValueError:return np.zeros((1,1),np.float32)
                small=cache.evaluate('point-range',(preview_key(settings,point_overlay),point_overlay,use_crop,original_size),color_coverage)
                rgba=np.zeros((*small.shape,4),np.uint8);rgba[...,0]=65;rgba[...,1]=170;rgba[...,2]=255;rgba[...,3]=np.uint8(small*150)
                overlay=QImage(rgba.data,rgba.shape[1],rgba.shape[0],rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
            hdr_pixels=None;hdr_rgba=None;thumb_working=working
            if settings['hdr']:
                from .hdr import to_srgb_extended,sdr_rendition,visualize
                hdr_pixels=to_srgb_extended(working,settings['working_space'])
                if native_hdr:
                    from .hdr import display_linear
                    hdr_rgba=display_linear(working,settings['working_space'])
                thumb_working=sdr_rendition(working,settings)
                if sdr_preview:working=thumb_working
            # develop() already clipped SDR sRGB working pixels; output_rgb would only copy them.
            image=working if not settings['hdr'] and settings['working_space']=='sRGB' else output_rgb(working,settings['working_space'])
            display=output_rgb(working,settings['working_space'],monitor_profile) if monitor_profile is not None else image
            if visualize_hdr:
                from .colorio import display_rgb
                display=display_rgb(visualize(hdr_pixels),monitor_profile)
            prepared=qimage(display,display=False)
            # The library thumbnail is made after the frame is shown (it cost ~10 ms of every final preview).
            thumb=thumb_working if save_thumb else None
            analysis=hdr_pixels if hdr_pixels is not None else image
            if hdr_pixels is not None:
                from .hdr import histogram
                bins=histogram(analysis)
            else:bins=histogram_bins(analysis)
            ceiling=float(to_srgb(np.float32(2**settings['hdr_limit']))) if hdr_pixels is not None else 1.
            return image,prepared,overlay,thumb,bins,clipping_channels(analysis,ceiling),hdr_pixels,ceiling,hdr_rgba
        def done(result):
            image,prepared,overlay,thumb,bins,channels,hdr_pixels,ceiling,hdr_rgba=result
            if quality=='live':
                self.live_running=False
            else:
                self.quality_running=False
            same_context=not self.closing and self.source is not None and context==self.preview_context()
            fresh=(version>self.presented_version or version==self.presented_version and
                   (quality=='quality' or self.presented_quality!='quality'))
            # Display completed interactive frames even if the pointer has advanced
            # one step. Exact-version rejection used to starve updates during drags.
            allowed=quality=='live' or version==self.render_version and not self.interacting()
            if same_context and fresh and allowed:
                self.presented_version,self.presented_quality=version,quality
                self.view.canvas=canvas
                self.view.set_image(image,logical_size,prepared)
                self.studio.update_healing_overlay()
                self.view.hdr_pixels=hdr_pixels;self.view.clipping_ceiling=ceiling
                from .engine import to_srgb
                self.view.hdr_display_ceiling=float(to_srgb(np.float32(2**(hdr_display.stops if self.hdr_display.active else 0.))))
                current_hdr=self.hdr_display.active and self.hdr_display.state==hdr_display and not self.hdr_display.preview and not self.hdr_visualize.isChecked()
                self.view.hdr_surface.set_frame(hdr_rgba if current_hdr else None,hdr_display)
                self.histogram.hdr=hdr_pixels is not None;self.histogram.hdr_limit=settings['hdr_limit']
                if hdr_pixels is not None:self.histogram.set_hdr_ranges(hdr_pixels)
                if overlay_index==self.studio.overlay_layer() and point_overlay==self.studio.point_overlay_target():self.view.tool_overlay=overlay
                if self.view.crop_mode:self.update_histogram(image)
                else:self.histogram.set_bins(bins,channels)
                if save_thumb:
                    from .preview_store import store_render
                    from .engine import output_rgb
                    thumb_space='ProPhoto RGB' if settings['working_space']=='ProPhoto' else 'sRGB'
                    def make_thumb(working=thumb):
                        return output_rgb(resize_float(working,320),settings['working_space'],thumb_space)
                    # Resize on the render workers (not the two thumbnail threads, which the library's
                    # thumbnail loading also uses), then store on the thumbnail pool as before.
                    def store(small):
                        self.spawn(lambda:store_render(directory,photo_id,settings,small,file_signature,offline_signature,
                            color_space=thumb_space),
                            lambda stored:self.photo_model.invalidate_thumbnails([photo_id]) if stored else None,
                            lambda _:None,self.thumbnail_pool)
                    self.spawn(make_thumb,store,lambda _:None)
                self.previewPresented.emit(version,quality,(time.perf_counter()-started)*1000)
                self.statusBar().showMessage(tr('원본 비교 중') if self.before_button.isChecked() else
                    (tr('보정 미리보기') if quality=='live' else tr('보정값 자동 저장  ·  내보내기는 원본 해상도')))
            if quality=='live' and (self.live_pending or version!=self.render_version):
                self.render_live()
            elif quality=='quality' and self.quality_pending and not self.refine_timer.isActive():
                self.render_quality()
        def failed(error):
            if quality=='live': self.live_running=False
            else: self.quality_running=False
            self.show_error(error)
        self.spawn(work,done,failed,
                   self.live_pool if quality=='live' else self.quality_pool)

    def update_histogram(self,image):
        if image is self.view.on_screen and self.view.hdr_pixels is not None:image=self.view.hdr_pixels
        if self.view.crop_mode:
            x0,y0,x1,y1=self.view.crop_values()
            h,w=image.shape[:2]
            frame,canvas=self.view.image_rect,self.view.canvas_rect()
            if frame.width()>0 and frame.height()>0:
                # Crop fractions are of the photo's canvas, which is part of the shown frame while straightening.
                sx,sy=w/frame.width(),h/frame.height()
                x0,x1=((canvas.left()+v*canvas.width())*sx/w for v in (x0,x1))
                y0,y1=((canvas.top()+v*canvas.height())*sy/h for v in (y0,y1))
            l,t=min(w-1,int(x0*w)),min(h-1,int(y0*h))
            image=image[t:max(t+1,int(y1*h)),l:max(l+1,int(x1*w))]
        self.histogram.set_image(image)

    def toggle_clipping(self,checked):
        self.histogram.set_overlay(checked,checked)

    def clipping_changed(self,shadows,highlights):
        self.view.set_clipping(shadows,highlights)
        self.clipping_button.blockSignals(True)
        self.clipping_button.setChecked(self.histogram.shadow_button.isChecked() or self.histogram.highlight_button.isChecked())
        self.clipping_button.blockSignals(False)

    def load_full_resolution(self):
        if self.source is None or self.full_loading or self.full_source is not None:return
        version=self.load_version;photo=self.catalog.photo(self.current_id)
        if not Path(photo['path']).is_file():
            self.statusBar().showMessage(tr('원본을 다시 연결하면 원본 100%로 확인할 수 있습니다.'));return
        self.full_loading=True
        self.statusBar().showMessage(tr('원본 해상도 불러오는 중…'))
        def ready(result):
            if self.load_version!=version:return
            self.full_loading=False;self.full_source=result[0]
            self.full_file_signature=result[1].get('file_fingerprint')
            self.render_version+=1;self.render_quality()
        def failed(error):
            if self.load_version==version:self.full_loading=False;self.show_error(error)
        def decode():
            if version!=self.load_version or self.closing:return None
            return self.source_cache.load(photo['path'],photo['settings'],None,
                lambda:load_image(photo['path'],working_space=photo['settings']['working_space'],raw_options=photo['settings']))
        self.spawn(decode,ready,failed,pool=self.source_pool)

    def toggle_before(self,checked):
        self.render_version+=1
        if checked and self.crop_button.isChecked():
            self.end_crop()
        self.render()

    def toggle_crop(self,checked):
        if self.source is None:
            self.crop_button.setChecked(False)
            return
        if not checked:
            self.apply_crop(self.view.crop_values())
            return
        self.commit()
        self.set_mode(0)
        self.tabs.setCurrentIndex(1)
        self.before_button.setChecked(False)
        self.wb_button.setChecked(False)
        self.view.sample_mode=False
        self.view.set_crop_mode(True,self.settings['crop'])
        # Cancel returns the framing to what it was when the tool opened.
        self.crop_entry={key:deepcopy(self.settings[key]) for key in ('crop','straighten','upright')}
        self.sync_crop_limit()
        self.crop_apply.setEnabled(True)
        self.crop_cancel.setEnabled(True)
        self.view.fit_photo()
        self.crop_changed(self.view.crop_values())
        self.render_version+=1
        self.render()

    def keyPressEvent(self,event):
        # Crop tool: Enter applies and Esc cancels from a slider, a number box or a button too, not only
        # with the pointer on the photo. A number box has already taken its typed value by now.
        if self.view.crop_mode and event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter,Qt.Key.Key_Escape):
            from PySide6.QtWidgets import QLineEdit,QTextEdit,QPlainTextEdit
            if not isinstance(self.focusWidget(),(QLineEdit,QTextEdit,QPlainTextEdit)):
                if event.key()==Qt.Key.Key_Escape:self.cancel_crop()
                else:self.view.accept_crop()
                event.accept();return
        super().keyPressEvent(event)

    def canvas_size(self):
        """Oriented size of the uncropped photo in pixels."""
        info=self.catalog.photo(self.current_id)['info']
        w,h=info.get('width'),info.get('height')
        if not w or not h:h,w=self.source.shape[:2]
        return (h,w) if int(self.settings['rotation'])%2 else (w,h)

    def sync_crop_limit(self):
        """Tell the crop frame how far the photo is turned. Perspective alignment has its own framing."""
        if self.source is None or not self.current_id:return
        angle=0. if self.settings.get('upright') else float(self.settings['straighten'])
        self.view.set_crop_limit(angle,*self.canvas_size(),current=float(self.settings['straighten']))

    def start_crop(self):
        if self.source is not None and not self.view.crop_mode:
            self.crop_button.setChecked(True);self.toggle_crop(True)

    def fit_crop(self,previous_angle):
        """After the angle changed: the crop stays on the turned photo (straighten.refit)."""
        if self.source is None or self.settings.get('upright'):return
        from . import straighten
        current=self.view.crop_values() if self.view.crop_mode else self.settings['crop']
        crop=straighten.refit(current,previous_angle,float(self.settings['straighten']),*self.canvas_size(),self.aspect.currentData() or None)
        self.settings['crop']=crop
        self.sync_crop_limit()
        if self.view.crop_mode:self.view.show_crop(crop)

    def set_straighten(self,key,value):
        """The angle slider: the crop tool opens, the whole photo stays visible and the crop frame follows."""
        if self.source is None:return
        focus=self.focusWidget()
        self.start_crop()
        # Opening the tool moves the keyboard to the photo; a slider being nudged with the arrow keys keeps it.
        if focus is not None and focus is not self.view and focus is not self.focusWidget():focus.setFocus()
        previous=float(self.settings['straighten'])
        self.set_setting('straighten',value)
        self.fit_crop(previous)

    def drag_straighten(self,angle):
        """Dragging outside the crop frame turns the photo; the slider follows."""
        self.set_straighten('straighten',angle)
        self.adjustments['straighten'].set_value(self.settings['straighten'])

    def auto_straighten(self):
        """Level the photo with the angle slider, the crop frame following."""
        if self.source is None:return
        from .upright import estimate
        from .optics import lens_shading
        from .engine import geometry
        studio=self.studio;studio.upright_generation+=1;generation=studio.upright_generation
        ident,version,saved=self.current_id,self.load_version,deepcopy(self.settings)
        source=self.source;settings=deepcopy(saved);settings.update(upright=None,straighten=0,crop=None)
        buttons=studio.upright_buttons+[self.auto_level_button]
        for b in buttons:b.setEnabled(False)
        self.auto_level_note.setText(tr('사진의 기준선을 분석하고 있습니다…'))
        def work():
            return estimate(geometry(lens_shading(resize_float(source,1400),settings),settings,False),'level')
        def finished(result):
            for b in buttons:b.setEnabled(True)
            if generation!=studio.upright_generation or self.current_id!=ident or self.load_version!=version or self.settings!=saved:
                self.load_controls();self.statusBar().showMessage(tr('분석 중 사진이나 설정이 바뀌어 결과를 적용하지 않았습니다.'));return
            # The estimate turns the photo counter-clockwise by `rotation`; the slider's positive side is clockwise.
            angle=round(float(np.clip(-result['rotation'],-45,45)),1)
            self.commit();self.start_crop()
            previous=float(self.settings['straighten'])
            self.set_setting('upright',None);self.set_setting('straighten',angle)
            self.fit_crop(previous)
            self.commit('자동 수평 맞추기');self.load_controls();self.render()
            self.auto_level_note.setText(tr('자동 보정 {0}° · 아래 슬라이더로 미세 조절할 수 있습니다.',f'{angle:+.1f}'))
        def failed(error):
            for b in buttons:b.setEnabled(True)
            if generation==studio.upright_generation and self.current_id==ident and self.load_version==version and self.settings==saved:
                message=str(error).strip().splitlines()[-1].split(': ',1)[-1]
                self.load_controls();self.auto_level_note.setText(message)
        self.spawn(work,finished,failed)

    def set_aspect(self):
        self.view.set_aspect(self.aspect.currentData())

    def crop_changed(self,crop):
        if self.source is None:
            return
        info=self.catalog.photo(self.current_id)['info']
        w,h=info['width'],info['height']
        if int(self.settings['rotation'])%2:
            w,h=h,w
        self.crop_size.setText(f'{round((crop[2]-crop[0])*w):,} × {round((crop[3]-crop[1])*h):,} px')
        if self.view.on_screen is not None:
            self.update_histogram(self.view.on_screen)

    def end_crop(self):
        self.crop_button.setChecked(False)
        self.crop_apply.setEnabled(False)
        self.crop_cancel.setEnabled(False)
        self.view.set_crop_mode(False)

    def cancel_crop(self):
        if not self.view.crop_mode:
            return
        entry=getattr(self,'crop_entry',None)
        if entry and any(self.settings[key]!=value for key,value in entry.items()):
            for key,value in entry.items():self.settings[key]=deepcopy(value)
            self.end_crop();self.commit('크롭 취소');self.load_controls()
        else:self.end_crop()
        self.render_version+=1
        self.render()

    def reset_crop(self):
        if self.source is None:return
        self.studio.upright_generation+=1
        self.commit()
        self.end_crop()
        self.aspect.setCurrentIndex(0)
        self.crop_size.clear()
        self.set_setting('crop',None)
        self.set_setting('straighten',0)
        # Level is part of framing. Keep deliberate lens-panel perspective
        # correction, orientation and all non-framing edits intact.
        if (self.settings.get('upright') or {}).get('mode')=='level':
            self.set_setting('upright',None)
        self.commit('크롭·수평 초기화')
        self.load_controls()
        self.view.fit_photo()
        self.render()

    def auto_border_crop(self):
        """Crop away the unexposed film edge of a scan (scan_border.detect on the uncropped frame)."""
        if self.source is None:return
        self.commit();self.end_crop()
        ident,version,saved=self.current_id,self.load_version,deepcopy(self.settings)
        source,settings=self.source,deepcopy(saved)
        self.border_crop_button.setEnabled(False)
        def work():
            from .scan_border import crop_for
            return crop_for(source,settings)
        def finished(crop):
            self.border_crop_button.setEnabled(True)
            if self.current_id!=ident or self.load_version!=version or self.settings!=saved:
                self.statusBar().showMessage(tr('분석 중 사진이나 설정이 바뀌어 결과를 적용하지 않았습니다.'));return
            if crop is None:
                self.statusBar().showMessage(tr('자를 스캔 테두리를 찾지 못했습니다.'),5000);return
            self.set_setting('crop',list(crop));self.commit('스캔 테두리 자동 자르기');self.load_controls();self.render()
            self.statusBar().showMessage(tr('스캔 테두리를 잘랐습니다. 크롭 편집으로 조정할 수 있습니다.'),5000)
        def failed(error):
            self.border_crop_button.setEnabled(True)
            self.statusBar().showMessage(tr('스캔 테두리를 분석하지 못했습니다.'),5000)
        self.spawn(work,finished,failed)

    def apply_crop(self,crop):
        self.set_setting('crop',crop)
        self.end_crop()
        self.commit()
        self.render()

    def rotate_photo(self):
        self.set_setting('crop',None)
        self.set_setting('rotation',(self.settings['rotation']+1)%4)
        if self.settings['straighten']:self.fit_crop(float(self.settings['straighten']))
        self.view.fit_photo()
        self.commit()

    def toggle_wb(self,checked):
        if self.source is None:
            self.wb_button.setChecked(False);self.view.sample_mode=False;return
        if checked:
            self.set_mode(0)
            if self.view.crop_mode:
                self.cancel_crop()
            self.view.set_tool('')
            self.statusBar().showMessage(tr('사진에서 중성 회색이나 흰색 부분을 클릭하세요.'))
        self.view.sample_mode=checked
        if checked:self.view.setFocus()

    def sample_wb(self,x,y):
        from .rawcolor import CameraSource,neutral_white,default_profile
        if isinstance(self.source,CameraSource):
            point=self.studio.source_point([x,y])
            if point is None:return
            px=min(self.source.shape[1]-1,int(point[0]*self.source.shape[1]))
            py=min(self.source.shape[0]-1,int(point[1]*self.source.shape[0]))
            neutral=np.asarray(self.source[max(0,py-3):py+4,max(0,px-3):px+4]).mean(axis=(0,1))
            if neutral.min()<1e-5:
                self.statusBar().showMessage(tr('너무 어두운 영역입니다. 밝은 중성 회색을 선택하세요.'));return
            from .dcp import compiled
            p=compiled(self.settings['dcp_profile']) if self.settings['dcp_profile'] else default_profile(self.source.raw_info)
            _,kelvin,tint=neutral_white(p,neutral,self.source.raw_info)
            self.commit();self.set_setting('raw_neutral',(neutral/neutral[1]).tolist())
            self.set_setting('raw_mode','sample');self.set_setting('raw_kelvin',float(np.clip(kelvin,2000,50000)))
            self.set_setting('raw_tint',float(np.clip(tint,-150,150)))
            self.set_setting('temperature',0.);self.set_setting('tint',0.);self.set_setting('kelvin_enabled',False)
            self.wb_button.setChecked(False);self.view.sample_mode=False;self.load_controls();self.commit('RAW 화이트밸런스 스포이드')
            return
        if self.view.on_screen is None:
            return
        a=self.view.on_screen
        px,py=min(a.shape[1]-1,int(x*a.shape[1])),min(a.shape[0]-1,int(y*a.shape[0]))
        rgb=a[max(0,py-3):py+4,max(0,px-3):px+4].mean(axis=(0,1))+.001
        self.set_setting('temperature',float(np.clip(self.settings['temperature']+90*np.log2(rgb[2]/rgb[0]),-100,100)))
        self.set_setting('tint',float(np.clip(self.settings['tint']+100*np.log2(rgb[1]/np.sqrt(rgb[0]*rgb[2])),-100,100)))
        self.wb_button.setChecked(False)
        self.view.sample_mode=False
        self.load_controls()
        self.commit()

    def auto_slider(self,key):
        """Shift+double-click on exposure/whites/blacks: only that slider's automatic value (undoable)."""
        if self.source is None:return
        from .engine import auto_slider_value
        self.commit()
        settings=deepcopy(self.settings)
        if self.view.crop_mode:settings['crop']=self.view.crop_values()
        value=auto_slider_value(self.live_source,settings,key)
        if value is None:
            self.statusBar().showMessage(tr('이 사진에서는 자동 값을 찾지 못했습니다.'),5000);return
        self.settings[key]=value
        if key=='exposure':self.settings['tone_version']=max(2,self.settings.get('tone_version',2))
        self.adjustments[key].set_value(value)
        self.commit({'exposure':'자동 노출','whites':'자동 흰색 계열','blacks':'자동 검정 계열'}[key])
        self.render()

    def auto_tone(self):
        if self.source is None:
            return
        self.commit()
        settings=deepcopy(self.settings)
        if self.view.crop_mode:
            settings['crop']=self.view.crop_values()
        adjusted,report=auto_tone_settings(self.live_source,settings)
        if report['status']=='flat':
            self.auto_tone_note.setText(report['message'])
            return
        # Crop remains a draft until the user applies it.
        adjusted['crop']=self.settings['crop']
        self.settings=adjusted
        self.commit('자동톤')
        self.load_controls()
        self.before_button.setChecked(False)
        self.auto_tone_note.setText(
            tr('자동톤 적용 · 밝기 {0}–{1}\n→ 약 {2}–{3} / 255 (양끝 {4}% 여유)', f"{report['input_black']:g}", f"{report['input_white']:g}", f"{report['output_black']:.0f}", f"{report['output_white']:.0f}", f"{report['margin_percent']:g}"))
        self.last_auto_tone=report
        self.render_version+=1
        self.render()

    def auto_color(self,remember=False):
        """Correct a colour cast with automatic channel curves at the slider's strength."""
        if self.source is None:return
        from .auto_color import auto_color_settings
        amount=self.auto_color_slider.value()
        if remember:self.catalog.save_preference('auto_color_amount',amount)
        self.commit()
        settings=deepcopy(self.settings)
        if self.view.crop_mode:settings['crop']=self.view.crop_values()
        adjusted,report=auto_color_settings(self.live_source,settings,amount/100)
        if report['status']!='applied':
            self.auto_color_row.hide()
            self.statusBar().showMessage(tr({'monochrome':'흑백 사진에는 자동 색을 쓰지 않습니다.','neutral':'색 틀어짐을 찾지 못했습니다. 바꾼 것이 없습니다.'}
                .get(report['status'],'색을 판단할 영역이 부족해 자동 색을 적용하지 않았습니다.')),6000)
            return
        self.settings['rgb_curves']=adjusted['rgb_curves']
        self.commit('자동 색')
        self.load_controls()
        self.before_button.setChecked(False)
        self.auto_color_row.show()
        self.statusBar().showMessage(tr('자동 색 적용 · 세기 {0}% · 채널 커브에서 다듬을 수 있습니다.',f'{amount}'),6000)
        self.render_version+=1
        self.render()

    def reset_edits(self):
        if self.source is None:
            return
        self.commit()
        self.settings=defaults()
        self.commit()
        self.load_controls()
        self.render_version+=1
        self.render()

    def update_hsl(self,*args):
        for widget,value in zip(self.hsl_widgets,self.settings['hsl'][self.hsl_color.currentIndex()]):
            widget.set_value(value)

    def hsl_changed(self,key,value):
        values=deepcopy(self.settings['hsl'])
        values[self.hsl_color.currentIndex()][int(key)]=value
        self.set_setting('hsl',values)

    def refresh_presets(self):
        self.all_presets={**{name:normalized({'tone_version':defaults()['tone_version'],**values}) for name,values in PRESETS.items()},**self.catalog.presets()}
        self.preset_list.clear()
        self.preset_list.addItems(list(self.all_presets))
        from .adobe_preset import KEY
        for index in range(self.preset_list.count()):
            item=self.preset_list.item(index);preset=self.all_presets[item.text()]
            if KEY in preset:item.setToolTip(preset[KEY].get('report','Adobe 프리셋'))

    def apply_preset(self,item):
        if self.source is None:
            return
        self.commit()
        value=deepcopy(self.all_presets[item.text()])
        from .adobe_preset import resolve
        try:value=resolve(value,self.settings,self.catalog.photo(self.current_id)['path'])
        except (ValueError,TypeError) as error:
            self.show_error(str(error));return
        from .optics import retarget
        value=retarget(value,self.catalog.photo(self.current_id)['info'])
        from .rawcolor import retarget as retarget_raw
        value=retarget_raw(value,self.catalog.photo(self.current_id)['info'])
        for key in ('crop','rotation','straighten','flip'):
            value[key]=self.settings[key]
        self.settings=value
        self.commit()
        self.before_button.setChecked(False)
        self.load_controls()
        self.render_version+=1
        self.render()

    def save_preset(self):
        if self.source is None:
            return
        name,ok=QInputDialog.getText(self,tr('프리셋 저장'),tr('프리셋 이름'))
        if ok and name.strip():
            self.catalog.save_preset(name.strip(),self.settings)
            self.refresh_presets()

    def set_rating(self,rating):
        self.catalog.update_many(self.selected_ids(),'rating',rating)
        self.refresh_lists()
        if self.current_id:
            self.load_controls()

    def set_flag(self,flag):
        self.catalog.update_many(self.selected_ids(),'flag',flag)
        self.refresh_lists()
        if self.current_id:
            self.load_controls()

    def save_keywords(self):
        if self.current_id:
            self.catalog.update(self.current_id,keywords=self.keywords.text())

    def copy_edits(self):
        if self.current_id:
            self.copied=deepcopy(self.settings)
            self.statusBar().showMessage(tr('현재 보정값을 복사했습니다.'))

    def copy_crop_changed(self,checked):
        if hasattr(self,'manager'):
            from .manager import EDIT_GROUPS
            if checked:self.manager.copy_keys.update(EDIT_GROUPS['크롭·회전'])
            else:self.manager.copy_keys.difference_update(EDIT_GROUPS['크롭·회전'])

    def paste_edits(self):
        if not self.copied:
            self.statusBar().showMessage(tr('먼저 현재 보정을 복사하세요.'))
            return
        self.commit()
        ids=self.selected_ids()
        for photo_id in ids:
            original=self.catalog.photo(photo_id)['settings']
            value=deepcopy(original)
            keys=self.manager.copy_keys if hasattr(self,'manager') else self.copied.keys()
            value.update({k:deepcopy(self.copied[k]) for k in keys})
            if 'lensfun' in keys:
                from .optics import retarget
                value=retarget(value,self.catalog.photo(photo_id)['info'])
            if not self.copy_crop.isChecked():
                for key in ('crop','rotation','straighten','flip'):
                    value[key]=original[key]
            from .rawcolor import retarget as retarget_raw
            value=retarget_raw(value,self.catalog.photo(photo_id)['info'])
            self.manager.edit_photo(photo_id,value,'보정 붙여넣기')
            if photo_id==self.current_id:
                self.undo_stack.append(deepcopy(self.settings))
                self.redo_stack.clear()
                self.settings=value
                self.last_saved=deepcopy(value)
                self.save_undo()
        self.load_controls()
        self.render_version+=1
        self.render()
        self.statusBar().showMessage(tr('{0}장에 보정값을 적용했습니다.', f'{len(ids)}'))
        self.manager.refresh_thumbnails(ids)

    def show_file(self):
        if self.current_id:
            path=Path(self.catalog.photo(self.current_id)['path']).parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def import_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,tr('사진 가져오기'),'','사진 ('+' '.join('*'+ext for ext in sorted(IMAGE_EXTENSIONS))+')')
        self.import_paths(paths)

    def import_folder(self):
        folder=QFileDialog.getExistingDirectory(self,tr('사진 폴더 가져오기 (하위 폴더 포함)'))
        if folder:
            self.import_paths([folder])

    def import_paths(self,paths):
        if self.import_cancel.is_set():
            if self.import_busy or self.import_scans:
                self.statusBar().showMessage(tr('현재 파일 처리가 멈추면 다시 가져올 수 있습니다.'));return
            self.import_cancel=Event()
        cancelled=self.import_cancel
        def scan():
            result=[]
            roots=[]
            for path in map(Path,paths):
                if cancelled.is_set():return [],[]
                if path.is_dir():
                    roots.append(str(path.resolve()))
                    for p in path.rglob('*'):
                        if cancelled.is_set():return [],[]
                        if p.suffix.lower() in IMAGE_EXTENSIONS and p.is_file():result.append(str(p))
                elif path.suffix.lower() in IMAGE_EXTENSIONS:
                    roots.append(str(path.resolve().parent))
                    result.append(str(path))
            return sorted(set(result)),roots
        def scanned(result):
            self.import_scans-=1
            if cancelled.is_set():
                if not self.import_busy:self.import_next()
                return
            files,roots=result
            for root in roots:
                self.catalog.register_folder(root)
            known={path_key(p) for p in self.catalog.paths()} | self.import_pending_paths
            for file in files:
                key=path_key(str(Path(file).resolve()))
                if key not in known:
                    self.import_queue.append(file)
                    self.import_pending_paths.add(key)
                    known.add(key)
            self.refresh_folder_tree()
            if not self.import_busy:
                self.import_errors=[]
                self.import_next()
        def scan_failed(error):
            self.import_scans-=1
            if not cancelled.is_set():self.show_error(error)
            if not self.import_busy:self.import_next()
        if paths:
            self.import_scans+=1
            self.import_cancel_button.show()
            self.spawn(scan,scanned,scan_failed)

    def cancel_import(self):
        self.import_cancel.set();self.import_queue.clear();self.import_pending_paths.clear()
        self.import_cancel_button.setEnabled(False)
        self.statusBar().showMessage(tr('가져오기를 중단하고 있습니다. 이미 가져온 사진은 유지합니다.'))
        if not self.import_busy and not self.import_scans:self.import_next()

    def import_next(self):
        if not self.import_queue:
            self.import_busy=False
            self.import_refresh_timer.stop();self.refresh_lists(ensure_current=self.current_id is None)
            self.import_cancel_button.setVisible(bool(self.import_scans));self.import_cancel_button.setEnabled(True)
            self.statusBar().showMessage((tr('가져오기 중단 중…') if self.import_scans else tr('가져오기를 중단했습니다. 이미 가져온 사진은 유지합니다.'))
                if self.import_cancel.is_set() else tr('폴더를 찾는 중…') if self.import_scans else tr('사진 가져오기 완료'))
            if self.import_errors and not self.import_cancel.is_set():
                QMessageBox.information(self,tr('읽을 수 없는 사진'), '\n'.join(self.import_errors[:10]))
            return
        self.import_busy=True
        path=self.import_queue.popleft()
        self.statusBar().showMessage(tr('가져오는 중: {0}  ·  남은 사진 {1}장', f'{Path(path).name}', f'{len(self.import_queue)}'))
        def ready(result):
            if self.import_cancel.is_set():self.import_busy=False;self.import_next();return
            image,info=result[:2]
            settings=result[2] if len(result)>2 else None
            photo_id=self.catalog.add(path,settings=settings,metadata=info)
            self.import_pending_paths.discard(path_key(str(Path(path).resolve())))
            self.folder_sync.imported(path)
            extras=self.import_extras.pop(path_key(str(Path(path).resolve())),None)
            if extras:settings=self.apply_import_extras(photo_id,extras) or settings
            from .preview_store import store_render
            saved=settings or defaults()
            self.spawn(lambda:store_render(self.catalog.directory,photo_id,saved,image,info.get('file_fingerprint'),
                color_space=info.get('preview_color_space','sRGB')),
                lambda _:self.photo_model.invalidate_thumbnails([photo_id]),lambda _:None,self.thumbnail_pool)
            self.photo_model.invalidate_thumbnails([photo_id])
            if not self.import_refresh_timer.isActive():self.import_refresh_timer.start(150)
            if self.current_id is None and photo_id in self.visible_ids:
                self.activate(photo_id)
            QTimer.singleShot(0,self.import_next)
        def failed(error):
            self.import_pending_paths.discard(path_key(str(Path(path).resolve())))
            if not self.folder_sync.import_failed(path) and not self.import_cancel.is_set():
                self.import_errors.append(Path(path).name+': '+import_reason(path,error))
            QTimer.singleShot(0,self.import_next)
        from .raw_defaults import PREFERENCE,import_preview,is_raw
        rules=self.catalog.preference(PREFERENCE,[])
        # RAW always goes through the import rules: the built-in RAW development applies even without rules.
        self.spawn(lambda:import_preview(path,rules) if is_raw({},path) else thumbnail(path,with_info=True),ready,failed)

    def apply_import_extras(self,photo_id,extras):
        """Keywords and a develop preset chosen in the copy import; returns the new settings, if any."""
        record=self.catalog.photo(photo_id)
        if extras.get('keywords'):
            existing=[k.strip() for k in (record.get('keywords') or '').split(',') if k.strip()]
            added=[k.strip() for k in extras['keywords'].split(',') if k.strip() and k.strip() not in existing]
            self.catalog.update(photo_id,keywords=', '.join(existing+added))
        name=extras.get('preset')
        if not name or name not in self.all_presets:return None
        from .adobe_preset import resolve
        from .optics import retarget
        from .rawcolor import retarget as retarget_raw
        base=record['settings']
        try:value=resolve(deepcopy(self.all_presets[name]),base,record['path'])
        except (ValueError,TypeError) as error:
            self.import_errors.append(Path(record['path']).name+': '+str(error));return None
        value=retarget_raw(retarget(value,record['info']),record['info'])
        for key in ('crop','rotation','straighten','flip'):value[key]=base[key]
        self.catalog.set_settings(photo_id,value)
        return value

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self,event):
        self.import_paths([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
        event.acceptProposedAction()

    def export_dialog(self):
        self.commit()
        ids=self.selected_ids()
        if not ids or self.export_running:
            return
        dialog=ExportDialog(len(ids),self)
        if dialog.exec()!=QDialog.DialogCode.Accepted:
            return
        self.start_export(ids,Path(dialog.folder.text()),dialog.format.currentData(),dialog.quality.value(),dialog.longest.value(),
            dialog.color_space.currentText(),dialog.keep_metadata.isChecked(),dialog.naming.text(),
            dialog.grayscale.isChecked(),dialog.dither.isChecked())

    def quick_export(self,kind):
        if self.export_running:return
        self.commit();ids=self.selected_ids()
        if not ids:return
        from .quick_export import preset,previous
        saved=previous(self.catalog)
        if kind=='previous':
            if saved is None:
                self.export_dialog();return
            options=dict(saved);folder=options.pop('folder')
            if not Path(folder).is_dir():
                folder=QFileDialog.getExistingDirectory(self,tr('이전 저장 위치가 없습니다. 내보낼 폴더 선택'),folder)
        else:
            options=preset(kind)
            folder=QFileDialog.getExistingDirectory(self,tr('내보낼 폴더 선택'),saved['folder'] if saved else '')
        if folder:self.start_export(ids,Path(folder),**options)

    def start_export(self,ids,folder,format='JPEG',quality=95,longest=0,color_space='sRGB',keep_metadata=False,naming='{stem}_grainy',
                     grayscale=True,dither=True):
        if self.export_running or not ids:return
        self.commit()
        records=[self.catalog.photo(i) for i in ids]
        folder=Path(folder)
        if not folder.is_dir():raise ValueError(tr('저장할 폴더를 선택해 주세요.'))
        options=dict(folder=str(folder),format=format,quality=quality,longest=longest,
            color_space=color_space,keep_metadata=keep_metadata,naming=naming,grayscale=grayscale,dither=dither)
        self.export_running=True
        self.export_button.setEnabled(False)
        from .workflow_data import edit_signature,record_exports
        def work():
            from concurrent.futures import ThreadPoolExecutor
            from .quick_export import export_concurrency
            # Choose every file name first, in order, so parallel jobs never claim the same path.
            plans,failures,reserved=[],[],set()
            taken=lambda path:path.exists() or path in reserved
            for n,record in enumerate(records,1):
                try:
                    from .quick_export import SUFFIXES
                    suffix=Path(record['path']).suffix if format=='Original' else SUFFIXES[format]
                    # A macOS file name may hold characters Windows forbids (Finder shows ':' as '/', as in a
                    # date); the exported name gets '_' there instead of the export failing.
                    source_stem=Path(record['path']).stem.translate(str.maketrans({c:'_' for c in '<>:"/\\|?*'}))
                    stem=naming.format(stem=source_stem,n=n)
                    if not stem.strip() or Path(stem).name!=stem or any(c in stem for c in '<>:"/\\|?*'):
                        raise ValueError(tr('유효하지 않은 출력 이름'))
                    destination=folder/(stem+suffix);index=2
                    while taken(destination) or (format=='Original' and taken(destination.with_suffix('.xmp'))):
                        destination=folder/f'{stem}_{index}{suffix}';index+=1
                    reserved.add(destination)
                    if format=='Original':reserved.add(destination.with_suffix('.xmp'))
                    plans.append((record,destination))
                except Exception as exc:
                    failures.append(record['name']+': '+str(exc))
            def one(plan):
                record,destination=plan
                try:
                    if format=='Original':
                        from .quick_export import copy_original
                        size=copy_original(record,destination)
                    else:
                        size=export_image(record['path'],destination,record['settings'],format,quality,longest or None,
                            color_space,keep_metadata,record['user_metadata'],record['keywords'],grayscale=grayscale,dither=dither)
                    return {'path':str(destination),'size':size,'photo_id':record['id'],
                        'edit_signature':edit_signature(record['settings'])},None
                except Exception as exc:
                    return None,record['name']+': '+str(exc)
            with ThreadPoolExecutor(export_concurrency([record for record,_ in plans]),thread_name_prefix='GrainyExport') as pool:
                results=list(pool.map(one,plans))
            outputs=[output for output,_ in results if output]
            failures+=[failure for _,failure in results if failure]
            return {'outputs':outputs,'failures':failures}
        def finished(result):
            self.export_running=False
            self.export_button.setEnabled(True)
            self.last_export=result
            if result['outputs']:
                from .quick_export import PREFERENCE
                self.catalog.save_preference(PREFERENCE,options)
                record_exports(self.catalog,[{k:o[k] for k in ('path','photo_id','edit_signature')} for o in result['outputs']])
            self.statusBar().showMessage(tr('내보내기 완료: {0}장  ·  실패: {1}장', f"{len(result['outputs'])}", f"{len(result['failures'])}"))
            if result['failures']:
                QMessageBox.warning(self,tr('일부 파일 내보내기 실패'),'\n'.join(result['failures']))
        def failed(error):
            self.export_running=False
            self.export_button.setEnabled(True)
            self.show_error(error)
        self.statusBar().showMessage(tr('사진 {0}장 내보내는 중…', f'{len(records)}'))
        self.spawn(work,finished,failed)

    def update_gpu_action(self):
        from . import native_gpu
        state=native_gpu.status() if native_gpu.enabled() else tr('꺼짐')
        self.gpu_action.setToolTip(tr('GPU: {0}', state))
        settings_menu=self.gpu_action.parent()
        if hasattr(settings_menu,'setToolTipsVisible'):settings_menu.setToolTipsVisible(True)

    def set_gpu(self,enabled):
        from . import native_gpu
        native_gpu.set_enabled(enabled)
        self.gpuPreferenceChanged.emit(bool(enabled))
        state=native_gpu.status() if enabled else tr('꺼짐')
        self.statusBar().showMessage(tr('GPU 가속: {0}', state))
        self.render_version+=1;self.render()

    def show_help(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]/'README.html')))

    def show_about(self):
        box=QMessageBox(self);box.setWindowTitle(tr('Grainy 정보'));box.setIcon(QMessageBox.Icon.Information)
        box.setText(f'Grainy {__version__}')
        box.setInformativeText(tr('원본을 건드리지 않는 사진 보정·관리 프로그램입니다.\n\nGrainy의 코드는 MIT 라이선스입니다. 함께 들어 있는 외부 부품의 라이선스 고지는 배포본의 THIRD_PARTY_LICENSES 폴더에 있습니다.\n\nAdobe, Lightroom, Photoshop은 Adobe Inc.의 상표입니다. Grainy는 Adobe가 만들거나 후원·보증한 프로그램이 아니며 Adobe와 관련이 없습니다. 그 밖의 상표는 각 소유자의 것입니다.'))
        box.exec()

    def show_auth(self):
        if self.auth_dialog is None:
            self.auth_dialog=AuthDialog(self.auth,self,
                on_executable=lambda path:self.catalog.save_preference('codex_executable',path))
            self.auth_dialog.rejected.connect(self.cancel_codex_login)
        self.auth_dialog.show()
        self.auth_dialog.raise_()
        self.auth_dialog.activateWindow()
        self.auth.refresh()

    def command_context(self):
        if not self.current_id and not self.folder_filter:
            return None
        self.commit()
        from .command_batch import capture
        return capture(self.catalog,self.current_id,self.selected_ids(),self.folder_filter)

    def apply_command(self,snapshot,proposal):
        from .commands import checked_proposal,changed_settings
        safe=checked_proposal(proposal)
        if safe['action']=='chat':return 0
        scope=safe['scope']
        if scope!='current':
            from .command_batch import targets,verify,apply
            self.commit()
            records=targets(snapshot,scope);verify(self.catalog,records)
            values=self.command_auto_values(records) if safe['action']=='auto_tone' else [changed_settings(r['settings'],safe) for r in records]
            ids=apply(self.catalog,records,values)
            self.refresh_command_edits(ids)
            return len(ids)
        if not snapshot or self.source is None or self.current_id!=snapshot['photo_id']:
            raise ValueError('선택한 사진이 바뀌었습니다. 현재 사진으로 다시 요청해 주세요.')
        if self.settings!=snapshot['settings']:
            raise ValueError('보정값이 바뀌었습니다. 현재 상태로 다시 요청해 주세요.')
        previous=deepcopy(self.settings)
        if safe['action']=='auto_tone':
            syncing=self.manager.syncing;self.manager.syncing=True
            try:self.auto_tone()
            finally:self.manager.syncing=syncing
        elif safe['action']=='adjust':
            self.commit()
            values=changed_settings(self.settings,safe)
            # Explicit command scope must not expand through Auto Sync.
            syncing=self.manager.syncing;self.manager.syncing=True
            for key in safe['changes']:
                value=values[key]
                self.set_setting(key,value)
            try:self.commit('Codex 명령')
            finally:self.manager.syncing=syncing
            self.load_controls()
            self.render()
        return int(previous!=self.settings)

    def command_auto_values(self,records,compute=None,name='자동톤'):
        """Compute every photo first; cancellation or failure writes no edits.
        compute(source, settings) returns the new settings (auto tone by default)."""
        compute=compute or (lambda source,settings:auto_tone_settings(source,settings)[0])
        cancel=Event();state={'done':0};directory=self.catalog.directory
        progress=QProgressDialog(f'사진별 {name}을 계산하는 중…','취소',0,len(records),self)
        progress.setWindowTitle(tr('Codex 일괄 자동톤') if name=='자동톤' else tr(name));progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0);progress.setAutoClose(False);progress.setAutoReset(False)
        progress.canceled.connect(cancel.set)
        self.command_running=True
        def work():
            from .preview_store import signature,load_offline
            values=[]
            for index,record in enumerate(records):
                if cancel.is_set():return None
                settings=record['settings'];path=Path(record['path'])
                if path.is_file():
                    before=signature(path)
                    source,_=load_image(path,720,settings['working_space'],raw_options=settings)
                    if signature(path)!=before:raise ValueError('원본이 변경되었습니다.')
                else:source,_=load_offline(directory,record['id'],settings)
                values.append(compute(resize_float(source,720),settings))
                state['done']=index+1
            return values
        def finished(values):state['values']=values;progress.accept()
        def failed(error):state['failed']=True;progress.accept()
        timer=QTimer(progress);timer.setInterval(100)
        timer.timeout.connect(lambda:progress.setValue(state['done']));timer.start()
        self.spawn(work,finished,failed)
        try:progress.exec()
        finally:timer.stop();self.command_running=False
        if cancel.is_set() or 'values' not in state and not state.get('failed'):
            cancel.set();raise ValueError(f'일괄 {name}을 취소했습니다. 변경한 사진은 없습니다.')
        if state.get('failed'):raise ValueError('일부 사진을 읽거나 계산할 수 없습니다. 변경한 사진은 없습니다.')
        return state['values']

    def refresh_command_edits(self,ids):
        self.photo_model.invalidate_thumbnails(ids)
        if self.current_id in ids:
            self.studio.cancel_stroke()
            self.settings=deepcopy(self.catalog.photo(self.current_id)['settings']);self.last_saved=deepcopy(self.settings)
            state=self.catalog.preference(f'undo:{self.current_id}',{})
            self.undo_stack=state.get('undo',[]);self.redo_stack=state.get('redo',[])
            self.before_button.setChecked(False);self.render_version+=1
            self.load_controls();self.render()
        self.statusBar().showMessage(tr('Codex 명령 · {0}장 변경', f'{len(ids):,}'))

    def restore_command_batch(self,redo=False):
        from .command_batch import restore
        self.commit();ids=restore(self.catalog,redo)
        self.refresh_command_edits(ids)
        return len(ids)

    def show_commands(self):
        from . import features
        if self.closing or not features.codex():return
        if not self.auth.connected:
            self.codex_login_pending=True
            self.show_auth()
            return
        self.codex_login_pending=False
        if self.auth_dialog is not None:self.auth_dialog.hide()
        if self.command_dialog is None:
            self.command_dialog=CommandDialog(self.auth,self,self.command_context,self.apply_command,self.restore_command_batch)
        self.command_dialog.show();self.command_dialog.raise_();self.command_dialog.activateWindow()
        self.command_dialog.input.setFocus()
        self.auth.refresh()

    def cancel_codex_login(self):
        self.codex_login_pending=False

    def update_codex_button(self):
        self.command_button.setToolTip(tr('Codex 명령창 열기') if self.auth.connected else tr('Codex에 로그인하여 명령하기'))
        if self.auth.connected and self.codex_login_pending and not self.closing:
            self.codex_login_pending=False
            QTimer.singleShot(0,self.open_commands_after_login)

    def open_commands_after_login(self):
        if not self.closing and self.auth.connected and self.auth_dialog is not None and self.auth_dialog.isVisible():
            self.show_commands()

    def closeEvent(self,event):
        if getattr(self,'command_running',False):
            self.statusBar().showMessage(tr('일괄 명령을 취소하거나 완료한 뒤 닫아 주세요.'));event.ignore();return
        if getattr(self,'maintenance_running',False):
            self.statusBar().showMessage(tr('카탈로그 작업을 취소하거나 완료한 뒤 닫아 주세요.'))
            event.ignore();return
        if hasattr(self,'folder_sync'):self.folder_sync.release()
        if self.export_running or self.import_busy or self.import_scans:
            self.statusBar().showMessage(tr('사진 가져오기 또는 내보내기가 끝난 뒤 닫아 주세요.'))
            event.ignore()
            return
        self.commit()
        self.closing=True;self.library_cancel.set();self.photo_model.closed=True
        self.studio.cancel_stroke()
        self.preview_queue.shutdown();self.photo_model.check_timer.stop()
        self.display_color.shutdown()
        self.hdr_display.shutdown()
        self.import_refresh_timer.stop()
        if hasattr(self,'extras'):self.extras.shutdown()
        if hasattr(self,'folder_sync'):self.folder_sync.shutdown()
        if hasattr(self,'folder_panel'):self.folder_panel.shutdown()
        if hasattr(self,'workflow'):self.workflow.shutdown()
        if hasattr(self,'manager'):self.manager.smart_timer.stop()
        self.save_keywords()
        self.save_folder_state()
        self.preview_timer.stop()
        self.refine_timer.stop()
        self.pool.waitForDone()
        self.prefetch_generation+=1;self.prefetch_pool.shutdown(wait=True,cancel_futures=True)
        self.source_pool.waitForDone();self.source_cache.clear()
        self.live_pool.waitForDone()
        self.quality_pool.waitForDone()
        self.browser_pool.waitForDone();self.thumbnail_pool.waitForDone()
        self.thumbnail_render_pool.waitForDone()
        self.display_pool.waitForDone()
        if hasattr(self,'cloud_backup'):self.cloud_backup.shutdown()
        self.auth.shutdown()
        self.catalog.close()
        event.accept()
