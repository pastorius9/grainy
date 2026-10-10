"""Reopen the workspace through its normal save/shutdown path on language changes."""
import sys
from PySide6.QtCore import QLibraryInfo, QTranslator, QTimer
from PySide6.QtWidgets import QDialog, QMessageBox
from .i18n import language, set_language, tr
from .language_dialog import LanguageDialog


class DesktopSession:
    def __init__(self, app, data_dir, preferences, window_factory=None):
        if window_factory is None:
            from .app import MainWindow
            window_factory = MainWindow
        self.app, self.data_dir, self.preferences = app, data_dir, preferences
        self.window_factory = window_factory
        self.window = None
        self.switching = False
        self.translator = None
        app.setQuitOnLastWindowClosed(False)
        app.lastWindowClosed.connect(self.last_window_closed)

    def apply_language(self, value):
        if self.translator is not None:
            self.app.removeTranslator(self.translator)
            self.translator = None
        set_language(value)
        if value == 'ko':
            translator = QTranslator(self.app)
            if translator.load('qtbase_ko', QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
                self.app.installTranslator(translator)
                self.translator = translator

    def start(self):
        selected = self.preferences.language
        if selected is None:
            dialog = LanguageDialog(first_run=True)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            selected = dialog.selected_language
            try:
                self.preferences.save_language(selected)
            except OSError:
                QMessageBox.warning(None, 'Grainy', 'Could not save the language setting.\n언어 설정을 저장하지 못했습니다.')
                return False
        self.apply_language(selected)
        from . import native_gpu
        native_gpu.set_enabled(self.preferences.use_gpu)
        self.open_window()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self.updates.startup())     # after the window is up
        return True

    def open_window(self, geometry=None, photo=None, mode=None):
        self.window = self.window_factory(self.data_dir)
        self.window.languageChangeRequested.connect(self.choose_language)
        self.window.gpuPreferenceChanged.connect(self.save_gpu)
        from .updater import UpdateController
        self.updates = UpdateController(self.window, self.preferences)
        from PySide6.QtWidgets import QMenu
        menu = self.window.findChild(QMenu, 'settingsMenu'); menu.addSeparator()
        menu.addAction(tr('Grainy 정보…'), self.window.show_about)
        if sys.platform == 'darwin':
            # macOS moves a command whose name starts with "Settings", "About", "Quit" (or their
            # translations) into the application menu; every command here stays in its own menu.
            from PySide6.QtGui import QAction
            for action in self.window.menuBar().findChildren(QAction):
                action.setMenuRole(QAction.MenuRole.NoRole)
        if geometry is not None:
            self.window.restoreGeometry(geometry)
        self.window.show()
        if photo is not None and self.window.catalog.photo(photo):
            self.window.activate(photo)
        if mode is not None:
            self.window.set_mode(mode)

    def save_gpu(self, enabled):
        try:
            self.preferences.save_use_gpu(enabled)
        except OSError:
            QMessageBox.warning(self.window, tr('설정'), tr('GPU 설정을 저장하지 못했습니다.'))

    def choose_language(self):
        dialog = LanguageDialog(language(), self.window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.change_language(dialog.selected_language)

    def change_language(self, selected):
        if selected == language():
            return True
        old = self.window
        # Do not interrupt an import, export, maintenance job or active command.
        if getattr(old, 'command_running', False) or getattr(old, 'maintenance_running', False) or old.export_running or old.import_busy or old.import_scans:
            old.statusBar().showMessage(tr('작업이 끝난 뒤 언어를 변경해 주세요.'))
            return False
        try:
            self.preferences.save_language(selected)
        except OSError:
            QMessageBox.warning(old, tr('설정'), tr('언어 설정을 저장하지 못했습니다.'))
            return False
        geometry, photo, mode = old.saveGeometry(), old.current_id, old.stack.currentIndex()
        panels = old.tabs.navigation_state()
        previous = language()
        self.switching = True
        try:
            if not old.close():
                self.preferences.save_language(previous)
                return False
            self.apply_language(selected)
            self.open_window(geometry, photo, mode)
            self.window.tabs.restore_navigation(panels)
            old.deleteLater()
        finally:
            self.switching = False
        return True

    def last_window_closed(self):
        if self.window is not None and not self.switching:
            QTimer.singleShot(0, self.app.quit)
