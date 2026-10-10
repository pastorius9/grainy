"""Cancelable catalog maintenance; all Qt operations stay on the UI thread."""
from .i18n import tr
from queue import Empty, SimpleQueue
from threading import Event
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton


class MaintenanceDialog(QDialog):
    def __init__(self, parent, title='카탈로그 최적화', note=None):
        super().__init__(parent)
        self.cancel = Event()
        self.updates = SimpleQueue()
        self.running = True
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        self.label = QLabel(tr('안전 백업을 준비하고 있습니다…'))
        layout.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        layout.addWidget(self.bar)
        self.note = QLabel(note or '프로파일 중복과 빈 공간을 정리합니다.\n원본 사진은 그대로 유지합니다. 취소 후에도 다시 시작할 수 있습니다.')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.stop = QPushButton(tr('취소'))
        self.stop.clicked.connect(self.request_cancel)
        layout.addWidget(self.stop)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def refresh(self):
        latest = None
        while True:
            try:
                latest = self.updates.get_nowait()
            except Empty:
                break
        if latest and not self.cancel.is_set():
            self.label.setText(latest['stage'])
            if latest.get('total'):
                self.bar.setRange(0, max(1, latest['total']))
                self.bar.setValue(latest.get('done', latest.get('examined', 0)))
            else:
                self.bar.setRange(0, 0)

    def request_cancel(self):
        self.cancel.set()
        self.label.setText(tr('현재 작업을 안전하게 마무리하는 중…'))
        self.stop.setEnabled(False)

    def reject(self):
        if self.running:
            self.request_cancel()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.running:
            self.request_cancel()
            event.ignore()
        else:
            super().closeEvent(event)

    def finish(self):
        self.running = False
        self.timer.stop()
        self.accept()
