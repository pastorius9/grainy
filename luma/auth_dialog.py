"""Account management UI; the browser owns the actual sign-in ceremony."""
from .i18n import tr
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QFileDialog

from .auth import usage_lines, valid_login_url


def plain_label(text=''):
    label=QLabel(tr(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class AuthDialog(QDialog):
    def __init__(self,client,parent=None,on_executable=None):
        super().__init__(parent)
        self.client=client
        self.on_executable=on_executable
        self.setWindowTitle(tr('ChatGPT 연결 — Grainy'))
        self.setMinimumWidth(510)
        self.resize(550,390)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(26,24,26,22)
        layout.setSpacing(14)
        heading=plain_label('ChatGPT 연결')
        heading.setStyleSheet('font-size:22px; color:#f4e7d4; font-weight:600;')
        layout.addWidget(heading)
        intro=plain_label('ChatGPT 계정과 구독의 Codex 사용량을 연결합니다.')
        intro.setObjectName('muted')
        layout.addWidget(intro)
        self.status=plain_label()
        self.status.setStyleSheet('padding:12px; background:#15191e; border-radius:0px;')
        layout.addWidget(self.status)
        self.account_label=plain_label()
        self.account_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.account_label)
        self.usage=plain_label()
        layout.addWidget(self.usage)
        self.browser_note=plain_label()
        self.browser_note.setObjectName('muted')
        self.browser_note.hide()
        layout.addWidget(self.browser_note)
        row=QHBoxLayout()
        self.login_button=QPushButton(tr('ChatGPT로 로그인'))
        self.login_button.setObjectName('primary')
        self.login_button.clicked.connect(client.login)
        row.addWidget(self.login_button)
        self.reopen_button=QPushButton(tr('브라우저 다시 열기'))
        self.reopen_button.clicked.connect(lambda:self.open_browser(client.login_url))
        row.addWidget(self.reopen_button)
        self.cancel_button=QPushButton(tr('로그인 취소'))
        self.cancel_button.clicked.connect(client.cancel_login)
        row.addWidget(self.cancel_button)
        self.logout_button=QPushButton(tr('로그아웃'))
        self.logout_button.clicked.connect(client.logout)
        row.addWidget(self.logout_button)
        layout.addLayout(row)
        note=plain_label('사용량은 같은 계정의 Codex와 공유됩니다.\n로그인은 기본 브라우저에서 진행되며, 이 화면에서 사진을 전송하지 않습니다.')
        note.setObjectName('muted')
        layout.addWidget(note)
        self.commands_button=QPushButton(tr('Codex 명령창 열기'))
        self.commands_button.setObjectName('primary')
        self.commands_button.setMinimumHeight(40)
        self.commands_button.clicked.connect(self.open_commands)
        layout.addWidget(self.commands_button)
        layout.addStretch()
        bottom=QHBoxLayout()
        self.refresh_button=QPushButton(tr('새로고침'))
        self.refresh_button.clicked.connect(client.refresh)
        bottom.addWidget(self.refresh_button)
        self.choose_button=QPushButton(tr('Codex 실행 파일 선택'))
        self.choose_button.clicked.connect(self.choose_executable)
        bottom.addWidget(self.choose_button)
        bottom.addStretch()
        close=QPushButton(tr('닫기'))
        close.clicked.connect(self.reject)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        client.changed.connect(self.update_state)
        client.browserRequested.connect(self.open_browser)
        self.update_state()

    def update_state(self):
        c=self.client
        self.status.setText(c.message)
        self.account_label.setVisible(c.connected)
        if c.connected:
            account=c.account
            plan=str(account.get('planType') or '확인 불가').replace('_',' ').title()
            self.account_label.setText(tr('{0}\n구독: {1}', f"{account.get('email') or '연결된 ChatGPT 계정'}", f'{plan}'))
        else:
            self.account_label.clear()
        self.usage.setVisible(c.connected)
        self.usage.setText(c.limits_error or '\n'.join(usage_lines(c.limits)))
        waiting=c.phase in {'login_start','waiting','cancelling'}
        self.login_button.setVisible(not c.connected and not waiting)
        self.login_button.setEnabled(c._ready and not c.busy)
        self.logout_button.setVisible(c.connected)
        self.commands_button.setVisible(c.connected)
        self.commands_button.setEnabled(not c.busy)
        self.logout_button.setEnabled(not c.busy)
        self.reopen_button.setVisible(c.phase=='waiting')
        self.cancel_button.setVisible(waiting)
        self.cancel_button.setEnabled(c.phase!='cancelling' and not c._cancel_on_start)
        self.refresh_button.setEnabled(not c.busy)
        self.choose_button.setVisible(c.phase in {'missing','error'})
        if c.phase!='waiting':
            self.browser_note.hide()

    def open_browser(self,url):
        if self.isVisible() and self.client.phase=='waiting' and valid_login_url(url):
            if not QDesktopServices.openUrl(QUrl(url)):
                self.browser_note.setText(tr('브라우저를 열지 못했습니다. 기본 브라우저를 확인한 뒤 다시 열어 주세요.'))
                self.browser_note.show()

    def choose_executable(self):
        import os
        path,_=QFileDialog.getOpenFileName(self,tr('Codex 실행 파일 선택'),'','Codex (codex.exe)' if os.name=='nt' else 'Codex (codex)')
        if path:
            self.client.shutdown()
            self.client.executable=path
            if self.on_executable:
                self.on_executable(path)
            self.client.start()

    def open_commands(self):
        parent=self.parent()
        if parent is not None and hasattr(parent,'show_commands'):
            self.hide();parent.show_commands();return
        if parent is not None and hasattr(parent,'session'):
            self.hide();parent.show();parent.raise_();return
        from .command_dialog import CommandDialog
        if not hasattr(self,'commands_dialog'):
            self.commands_dialog=CommandDialog(self.client,self)
        self.commands_dialog.show();self.commands_dialog.raise_()

    def reject(self):
        self.client.cancel_login()
        super().reject()
