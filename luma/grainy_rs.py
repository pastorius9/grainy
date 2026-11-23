"""The last Python version points to Grainy RS, where Grainy continues.

A notice once after the start of the packaged app (preference 'rs_notice_seen'; any answer, or closing it, ends it) and a
Settings menu command that opens the same page. Nothing is fetched."""
import os
import sys
from .i18n import tr

PAGE = 'https://pastorius9.github.io/grainy/'
KEY = 'rs_notice_seen'


def open_page():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl(PAGE))


def notice(window, preferences, packaged=None):
    """Once per computer; never from source, in tests or headless runs (offscreen)."""
    if not (getattr(sys, 'frozen', False) if packaged is None else packaged) or os.environ.get('QT_QPA_PLATFORM') == 'offscreen':return False
    if preferences.values.get(KEY) is True:return False
    try:preferences.save(KEY, True)           # first: whatever happens next (a crash, closing the box), it is not shown again
    except OSError:pass
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox(window); box.setObjectName('grainyRsNotice'); box.setWindowTitle(tr('Grainy RS로 옮겨 주세요')); box.setIcon(QMessageBox.Icon.Information)
    box.setText(tr('Grainy는 이제 Grainy RS로 이어집니다. 새 기능과 고침은 Grainy RS에만 나옵니다.'))
    box.setInformativeText(tr('Grainy RS를 처음 켜면 이 라이브러리(사진, 보정, 컬렉션)를 가져올지 묻습니다. 이 라이브러리는 그대로 남습니다. AI 연결을 쓰셨다면 Grainy RS의 AI 연결 창에서 다시 설정해 주세요.'))
    get = box.addButton(tr('Grainy RS 받기'), QMessageBox.ButtonRole.AcceptRole); box.addButton(tr('나중에'), QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is get:open_page()
    return True
