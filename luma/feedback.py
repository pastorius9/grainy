"""Feedback: the text a person types, plus the app version and a one-line system description, is posted
to a Google Form that fills a spreadsheet. Nothing is sent unless they press Send; photos, file paths
and names are never included. The button is hidden while no form is configured.

Each message carries a short random reference at the end of the system line. The references sent are
kept on this computer only (preference 'feedback_sent'), so that an update can say when one shipped
(whats_new.shipped and assets/feedback-done.json)."""
import platform
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from . import __version__
from .i18n import tr

# Google Form receiving the answers (the part of its public URL between /d/e/ and /viewform) and the
# entry number of each question.
FORM_ID = '1FAIpQLSd586uKnf3TmtV25wV0kthw_YwOiNHAIpXE8AbefLpSNaQXEw'
FIELDS = {'version': '1045781291', 'message': '1065046570', 'system': '839337160'}
LIMIT = 4000
TIMEOUT = 15
ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'      # no look-alikes: 0/O, 1/I/L
SENT, KEEP = 'feedback_sent', 50                  # preference: references sent from this computer, newest last
SENDING = set()      # dialogs with a send in flight, kept alive so that one closed meanwhile still records its reference


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


def reference():
    """Five random characters naming one message; nothing about the person or the computer."""
    return ''.join(secrets.choice(ALPHABET) for _ in range(5))


def tagged(system, ref):
    """The reference rides at the end of the existing 'system' answer: no new column, no change to the form."""
    return f'{system} · #{ref}'


def records():
    """Where sent references are kept: the per-user settings (tests replace this)."""
    from .preferences import Preferences
    return Preferences()


def record(preferences, ref, version=__version__, today=None):
    """Keep a sent reference, newest last, at most KEEP. Raises OSError when the settings cannot be written."""
    sent = preferences.values.get(SENT); sent = [entry for entry in sent if isinstance(entry, dict)] if isinstance(sent, list) else []
    preferences.save(SENT, (sent+[{'ref': ref, 'version': version, 'sent': (today or date.today()).isoformat()}])[-KEEP:])


def payload(message, system=None):
    values = {'version': __version__, 'message': message.strip()[:LIMIT],
              'system': system if system is not None else system_info()}
    return {f'entry.{FIELDS[key]}': value for key, value in values.items()}


def send(message, system=None, opener=None):
    """Post one answer. Returns '' on success, otherwise a short reason. The default opener is looked up here,
    so that tests blocking urllib.request.urlopen block it."""
    opener = opener or urllib.request.urlopen
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
    """Modal dialog for MainWindow w; `sender` defaults to send (looked up now, so tests can replace it), `preferences` to records()."""

    def __init__(self, w, sender=None, preferences=None):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton
        from PySide6.QtCore import QTimer, Qt
        self.w, self.sender, self.preferences, self.thread, self.result = w, sender or send, preferences, None, None
        self.reference = self.attempt = None          # kept while the same text is sent again after a failure
        d = self.dialog = QDialog(w); d.setWindowTitle(tr('피드백 보내기')); d.resize(520, 380); box = QVBoxLayout(d)
        intro = QLabel(tr('문제점이나 개선 아이디어를 자유롭게 적어 주세요. 사진, 파일 이름과 경로는 보내지 않습니다.'))
        intro.setWordWrap(True); box.addWidget(intro)
        self.message = QPlainTextEdit(); self.message.setPlaceholderText(tr('어떤 상황에서 무슨 일이 있었는지, 무엇이 있으면 좋을지 적어 주세요.'))
        box.addWidget(self.message, 1)
        self.system = system_info()
        note = self.note = QLabel(tr('함께 보내는 정보: Grainy {0} · {1} · 보낼 때 정하는 무작위 접수 번호', __version__, self.system))
        note.setWordWrap(True); note.setObjectName('muted'); box.addWidget(note)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.status)
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
        if self.reference is None or text != self.attempt:self.reference, self.attempt = reference(), text
        self.send_button.setEnabled(False); self.status.setText(tr('보내는 중…'))
        box = {}; arguments = (text, tagged(self.system, self.reference))
        def work():
            try:box['error'] = self.sender(*arguments)
            except Exception as error:box['error'] = str(error) or type(error).__name__
        self.result = box; self.thread = threading.Thread(target=work, daemon=True); self.thread.start(); self.poll.start(); SENDING.add(self)

    def check(self):
        if self.thread is None or self.thread.is_alive():return
        from PySide6.QtCore import QTimer
        self.poll.stop(); self.thread = None; error = self.result.get('error', '')
        QTimer.singleShot(0, lambda: SENDING.discard(self))       # a closed dialog is freed after this call, not inside its timer's signal
        if error:
            self.send_button.setEnabled(True)
            self.status.setText(tr('보내지 못했습니다 ({0}). 인터넷 연결을 확인하고 다시 시도해 주세요.', error))
        else:
            ref, self.reference, self.attempt = self.reference, None, None
            self.message.setPlainText(''); self.send_button.setEnabled(True)
            self.status.setText(tr('보냈습니다. 고맙습니다. 접수 번호는 #{0}입니다. 반영되면 업데이트한 뒤 Grainy가 알려 드립니다.', ref))
            try:record(records() if self.preferences is None else self.preferences, ref)
            except OSError:pass

    def exec(self):
        return self.dialog.exec()


def show(w):
    FeedbackDialog(w).exec()
