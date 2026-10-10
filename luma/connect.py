"""Open account settings while the editor is busy importing or exporting."""
import sys

from PySide6.QtWidgets import QApplication
from .app import configure_application
from .auth import CodexAuth
from .auth_dialog import AuthDialog
from .command_dialog import CommandDialog


def main():
    app=QApplication(sys.argv)
    configure_application(app)
    client=CodexAuth()
    dialog=CommandDialog(client) if '--commands' in sys.argv else AuthDialog(client)
    app.aboutToQuit.connect(client.shutdown)
    dialog.show()
    client.start()
    return app.exec()


if __name__=='__main__':
    sys.exit(main())
