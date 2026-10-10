import argparse
import sys
import os
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import QLockFile
from luma.app import MainWindow, configure_application


def main():
    if sys.argv[1:2]==['--finish-update']:
        # The new version, started from its staged folder: main.py --finish-update APP --wait-pid PID -- ARGS
        from luma.updater import run_finish
        rest=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
        return run_finish(sys.argv[2],int(sys.argv[4]),rest)
    if '--update-now' in sys.argv[1:]:
        from luma.updater import update_now
        reason=update_now()
        if reason:print(reason)
        return 1 if reason else 0
    parser=argparse.ArgumentParser()
    from luma.user_paths import local_data
    default_data=local_data('Luma')/'Library' if getattr(sys,'frozen',False) else Path(__file__).parent/'data'
    parser.add_argument('--data-dir',type=Path,default=default_data)
    parser.add_argument('--commands',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--self-test-hdr',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('photos',nargs='*')
    args=parser.parse_args()
    if args.self_test_hdr:
        from luma.hdr_selftest import main as hdr_test
        return hdr_test()
    if args.self_test:
        os.environ['QT_QPA_PLATFORM']='offscreen'
        from luma.selftest import run
        return run()
    args.data_dir.mkdir(parents=True,exist_ok=True)
    from luma.power import disable_background_throttling
    disable_background_throttling()
    if sys.platform=='win32':
        # Own taskbar identity, so Windows shows Grainy's icon rather than Python's when run from source.
        import ctypes
        try:ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Grainy.PhotoStudio')
        except (AttributeError,OSError):pass
    app=QApplication(sys.argv)
    app.setApplicationName('Grainy')
    configure_application(app)
    lock=QLockFile(str(args.data_dir/'luma.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(None,'Grainy','This photo library is already open.\n이 사진 라이브러리가 이미 열려 있습니다.')
        return 0
    from luma.preferences import Preferences
    from luma.desktop_session import DesktopSession
    session=DesktopSession(app,args.data_dir,Preferences())
    if not session.start():return 0
    window=session.window
    if args.commands:
        window.show_commands()
    if args.photos:
        window.import_paths(args.photos)
    return app.exec()


if __name__=='__main__':
    sys.exit(main())
