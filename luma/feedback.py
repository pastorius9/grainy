"""Feedback: the text a person types, plus the app version and a one-line system description, is posted
to a Google Form that fills a spreadsheet. Nothing is sent unless they press Send; photos, file paths
and names are never included. The button is hidden while no form is configured."""
import platform
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from . import __version__
from .i18n import tr

# Google Form receiving the answers (the part of its public URL between /d/e/ and /viewform) and the
# entry number of each question.
FORM_ID = '1FAIpQLSd586uKnf3TmtV25wV0kthw_YwOiNHAIpXE8AbefLpSNaQXEw'
FIELDS = {'version': '1045781291', 'message': '1065046570', 'system': '839337160'}
LIMIT = 4000
TIMEOUT = 15


def configured():
    return bool(FORM_ID) and all(FIELDS.values())


def system_info():
    """What accompanies a message: OS and graphics adapter only (no user, computer or file names)."""
    from . import native_gpu
    # macOS: the product version; platform.version() there is the kernel's long build line.
    system = f'macOS {platform.mac_ver()[0]}' if sys.platform == 'darwin' else f'Windows {platform.release()} ({platform.version()})'
    parts = [system, platform.machine()]
    if native_gpu.adapter:
        parts.append(f'GPU {native_gpu.adapter}' if native_gpu.enabled() else f'GPU {native_gpu.adapter} (off)')
    return ' · '.join(parts)


def payload(message, system=None):
    values = {'version': __version__, 'message': message.strip()[:LIMIT],
              'system': system if system is not None else system_info()}
    return {f'entry.{FIELDS[key]}': value for key, value in values.items()}


def send(message, system=None, opener=urllib.request.urlopen):
    """Post one answer. Returns '' on success, otherwise a short reason."""
    if not configured():
        return 'not configured'
    data = urllib.parse.urlencode(payload(message, system)).encode('utf-8')
    request = urllib.request.Request(f'https://docs.google.com/forms/d/e/{FORM_ID}/formResponse', data=data,
                                     headers={'User-Agent': f'Grainy/{__version__}', 'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with opener(request, timeout=TIMEOUT) as response:
            return '' if response.status == 200 else f'HTTP {response.status}'
    except urllib.error.HTTPError as error:
        return f'HTTP {error.code}'
    except (OSError, ValueError) as error:
        return str(getattr(error, 'reason', error)) or type(error).__name__


class FeedbackDialog:
    """Modal dialog for MainWindow w; `sender` is replaceable in tests."""

    def __init__(self, w, sender=send):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton
        from PySide6.QtCore import QTimer
        self.w, self.sender, self.thread, self.result = w, sender, None, None
        d = self.dialog = QDialog(w); d.setWindowTitle(tr('피드백 보내기')); d.resize(520, 380); box = QVBoxLayout(d)
        intro = QLabel(tr('문제점이나 개선 아이디어를 자유롭게 적어 주세요. 사진, 파일 이름과 경로는 보내지 않습니다.'))
        intro.setWordWrap(True); box.addWidget(intro)
        self.message = QPlainTextEdit(); self.message.setPlaceholderText(tr('어떤 상황에서 무슨 일이 있었는지, 무엇이 있으면 좋을지 적어 주세요.'))
        box.addWidget(self.message, 1)
        self.system = system_info()
        note = QLabel(tr('함께 보내는 정보: Grainy {0} · {1}', __version__, self.system)); note.setWordWrap(True); note.setObjectName('muted')
        box.addWidget(note)
        self.status = QLabel(); self.status.setWordWrap(True); box.addWidget(self.status)
        row = QHBoxLayout(); row.addStretch()
        self.send_button = QPushButton(tr('보내기')); self.close_button = QPushButton(tr('닫기'))
        row.addWidget(self.send_button); row.addWidget(self.close_button); box.addLayout(row)
        self.send_button.clicked.connect(self.submit); self.close_button.clicked.connect(d.reject)
        self.poll = QTimer(d); self.poll.setInterval(100); self.poll.timeout.connect(self.check)

    def submit(self):
        text = self.message.toPlainText().strip()
        if not text:
            self.status.setText(tr('내용을 입력해 주세요.')); return
        if self.thread is not None:return
        self.send_button.setEnabled(False); self.status.setText(tr('보내는 중…'))
        box = {}; arguments = (text, self.system)
        def work():
            try:box['error'] = self.sender(*arguments)
            except Exception as error:box['error'] = str(error) or type(error).__name__
        self.result = box; self.thread = threading.Thread(target=work, daemon=True); self.thread.start(); self.poll.start()

    def check(self):
        if self.thread is None or self.thread.is_alive():return
        self.poll.stop(); self.thread = None; error = self.result.get('error', '')
        if error:
            self.send_button.setEnabled(True)
            self.status.setText(tr('보내지 못했습니다 ({0}). 인터넷 연결을 확인하고 다시 시도해 주세요.', error))
        else:
            self.message.setPlainText(''); self.send_button.setEnabled(True)
            self.status.setText(tr('보냈습니다. 고맙습니다.'))

    def exec(self):
        return self.dialog.exec()


def show(w):
    FeedbackDialog(w).exec()
