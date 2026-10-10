from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QComboBox, QDialogButtonBox


class LanguageDialog(QDialog):
    def __init__(self, current=None, parent=None, first_run=False):
        super().__init__(parent)
        self.setWindowTitle('Grainy · Language / 언어')
        self.setMinimumWidth(440)
        box = QVBoxLayout(self)
        title = QLabel('Welcome to grainy / 그레이니에 오신 것을 환영합니다' if first_run else 'Interface language / 화면 언어')
        title.setWordWrap(True)
        box.addWidget(title)
        self.languages = QComboBox()
        self.languages.setObjectName('interfaceLanguage')
        self.languages.addItem('한국어', 'ko')
        self.languages.addItem('English', 'en')
        default = current or ('ko' if QLocale.system().language() == QLocale.Language.Korean else 'en')
        self.languages.setCurrentIndex(self.languages.findData(default))
        box.addWidget(self.languages)
        note = QLabel('You can change this in Settings → Language.\n설정 → 언어에서 언제든 변경할 수 있습니다.' if first_run else 'Your edits will be saved and the workspace will reopen.\n보정을 저장한 뒤 선택한 언어로 작업 화면을 다시 엽니다.')
        note.setWordWrap(True)
        box.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Continue / 시작' if first_run else 'Apply / 적용')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Cancel / 취소')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    @property
    def selected_language(self):
        return self.languages.currentData()
