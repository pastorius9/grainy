"""Tell an original the system will not let Grainy read apart from one that is gone.

macOS keeps Files and Folders permission per app. An ad hoc signed update looks like a new app to it,
so the permission is reset and every read of the photos, even a stat, fails with EPERM. os.path.isfile()
then says False exactly as for a deleted file, and Path.is_file() raises PermissionError (Python 3.13),
so originals are checked here instead. Such photos need access, not relinking: they keep their saved
previews and the person is told once per session how to allow the folder again.
"""
import os
import stat
from threading import Thread
from .i18n import tr
from .user_paths import MAC

PRESENT,MISSING,DENIED='present','missing','denied'
SETTINGS_URL='x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders'
shown=False         # the guidance appears once per run of Grainy
pending=False


def access(path,folder=False):
    """PRESENT, MISSING or DENIED for a photo file (a folder with folder=True). Other errors, such as a
    network volume that does not answer, count as missing as before."""
    try:mode=os.stat(path).st_mode
    except PermissionError:return DENIED
    except (OSError,ValueError):return MISSING
    return PRESENT if (stat.S_ISDIR(mode) if folder else stat.S_ISREG(mode)) else MISSING


def present(path):
    """Path.is_file() for an original that never raises: Python 3.13 lets PermissionError through it."""
    return access(path)==PRESENT


def denied(path):return access(path)==DENIED


def unreadable_folders(roots):
    """Registered folders the system will not let Grainy list. Only each folder itself is read; on macOS
    this is also what makes the system ask the person for access. Missing or disconnected folders are
    left to the folder panel."""
    result=[]
    for root in roots:
        try:
            with os.scandir(root) as entries:next(entries,None)
        except PermissionError:result.append(root)
        except (OSError,ValueError):pass
    return result


def guide(parent,place=''):
    """Explain how to let Grainy read the folder again, once per session. True when shown now."""
    global shown,pending
    if shown:return False
    from PySide6.QtCore import QTimer,QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication,QMessageBox
    if QApplication.activeModalWidget() is not None:
        # Not on top of another question; asked again when that one is closed.
        if not pending:
            pending=True
            def later():
                global pending
                pending=False
                if not getattr(parent,'closing',False):guide(parent,place)
            QTimer.singleShot(1000,parent,later)
        return False
    shown=True
    box=QMessageBox(parent);box.setObjectName('fileAccess');box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(tr('사진 폴더 접근 권한'))
    where='\n\n'+tr('위치: {0}',place) if place else ''
    settings=None
    if MAC:
        box.setText(tr('macOS가 Grainy의 사진 폴더 접근을 막고 있습니다.'))
        box.setInformativeText(tr('Grainy를 업데이트한 뒤 macOS가 폴더·디스크 접근 허용을 다시 묻거나 막는 일이 있습니다. 사진 파일과 보정 기록은 그대로 있고, 허용하면 다시 가져오지 않아도 그대로 돌아옵니다.\n\n허용하는 방법: 시스템 설정 › 개인정보 보호 및 보안 › 파일 및 폴더에서 Grainy 아래의 해당 폴더를 켭니다. 외장 디스크는 "이동식 볼륨", 네트워크 디스크는 "네트워크 볼륨"으로 나옵니다. 목록에 없으면 같은 화면의 전체 디스크 접근 권한에 Grainy를 추가합니다. 그다음 Grainy를 종료했다가 다시 여세요.')+where)
        settings=box.addButton(tr('시스템 설정 열기'),QMessageBox.ButtonRole.AcceptRole)
    else:
        box.setText(tr('Grainy가 사진 폴더를 읽을 권한이 없습니다.'))
        box.setInformativeText(tr('사진 파일과 보정 기록은 그대로 있습니다. 탐색기에서 폴더를 오른쪽 클릭 › 속성 › 보안에서 내 계정에 읽기 권한이 있는지 확인한 뒤 Grainy를 다시 여세요.')+where)
    box.addButton(tr('닫기'),QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if settings is not None and box.clickedButton() is settings:QDesktopServices.openUrl(QUrl(SETTINGS_URL))
    return True


def check_folders(w,interval=200):
    """Once after start-up and off the GUI thread: list each registered folder root (never the photos in
    it). A folder the system refuses shows the guidance. A volume that does not answer only keeps a
    daemon thread waiting; the window never waits for it."""
    if shown or getattr(w,'closing',False):return None
    try:roots=w.catalog.folder_roots()
    except Exception:return None
    if not roots:return None
    from PySide6.QtCore import QTimer
    found=[]
    thread=Thread(target=lambda:found.append(unreadable_folders(roots)),daemon=True,name='GrainyFolderAccess')
    timer=QTimer(w);timer.setInterval(interval)
    def poll():
        if thread.is_alive():return
        timer.stop();timer.deleteLater()
        if found and found[0] and not getattr(w,'closing',False):guide(w,found[0][0])
    timer.timeout.connect(poll);thread.start();timer.start()
    return thread
