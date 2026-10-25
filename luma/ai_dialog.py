"""Non-modal Ask AI window. The editor and MCP service remain usable while it runs."""
from pathlib import Path
import json
import shutil
import sys
from uuid import uuid4

from PySide6.QtCore import QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from .ai_programs import PROFILES, AiRunner, Request
from .ai_auth import AuthController
from .ai_install import InstallController, NODE_URL, NODE_WINDOWS, node_ready
from .i18n import language, tr
from .mcp_dialog import EXPLAIN, McpDialog, install_commands

NOTICE = ('이 창에서 보내기를 누르면 선택한 AI 프로그램이 실행됩니다. AI는 사진 목록·파일 이름과 경로·사진 정보'
          '(GPS 제외)·보정값·미리보기와 밀착 이미지를 읽고, 읽은 내용을 자신의 AI 서비스로 보냅니다. '
          '보정·분류 변경, 가져오기·내보내기, 스냅샷·가상 사본·밀착 이미지 저장도 할 수 있습니다. '
          '프로그램의 기존 계정과 구독·사용 한도가 적용됩니다.')


class PromptInput(QPlainTextEdit):
    sendRequested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.sendRequested.emit(); event.accept()
        else:
            super().keyPressEvent(event)


class AiDialog(QDialog):
    def __init__(self, service, window, *, profiles=PROFILES, runner=None):
        super().__init__(window)
        self.service, self.w = service, window
        self.preferences = service.preferences
        self.profiles = profiles
        self.executables = [profile.detect() for profile in profiles]
        self.runner = runner or AiRunner(self)
        self.auth = AuthController(self)
        self.installs = InstallController(self)
        self.auth_labels = {}
        self.login_buttons = {}
        self.install_buttons = {}
        self.recheck_buttons = {}
        self.auth_needed = set()
        self.session_id = ''
        self.session_choice = None
        self.history = []
        self.turn_text = ''
        self.in_flight = False
        self.run_key = uuid4().hex
        self.workspaces = set()
        self.setWindowTitle(tr('AI에게 시키기'))
        self.setModal(False)
        self.resize(760, 660)
        layout = QVBoxLayout(self)
        self.notice = QFrame()
        notice_box = QVBoxLayout(self.notice)
        label = QLabel(tr(NOTICE)); label.setWordWrap(True)
        notice_box.addWidget(label)
        row = QHBoxLayout()
        details = QPushButton(tr('자세히')); details.clicked.connect(self.privacy_details)
        row.addWidget(details)
        self.notice_ok = QPushButton(tr('확인')); self.notice_ok.clicked.connect(self.acknowledge)
        row.addWidget(self.notice_ok); row.addStretch(1)
        notice_box.addLayout(row); layout.addWidget(self.notice)
        self.notice.setVisible(not self.preferences.values.get('ai_notice_seen', False))
        row = QHBoxLayout(); row.addWidget(QLabel(tr('AI 프로그램')))
        self.program = QComboBox()
        for profile in profiles:
            self.program.addItem(tr(profile.name), profile.key)
        selected = next((i for i, p in enumerate(profiles)
                         if p.key == self.preferences.values.get('ai_program')), None)
        if selected is None:
            selected = next((i for i, command in enumerate(self.executables) if command), 0 if profiles else -1)
        self.program.setCurrentIndex(selected)
        row.addWidget(self.program, 1)
        row.addWidget(QLabel(tr('모델 (선택)')))
        self.model = QLineEdit(); self.model.setPlaceholderText(tr('비워 두면 프로그램 기본 모델'))
        row.addWidget(self.model, 1); layout.addLayout(row)
        self.hints = QLabel(tr('프로그램이 없으면 설치…를 누르세요. 실행 전에 공식 설치 명령을 보여 줍니다.'))
        self.hints.setWordWrap(True); self.hints.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.hints)
        for index, profile in enumerate(profiles):
            row = QHBoxLayout()
            label = QLabel(tr('{0}: 확인할 수 없음', tr(profile.name)))
            label.setWordWrap(True)
            self.auth_labels[profile.key] = label
            row.addWidget(label, 1)
            login = QPushButton(tr('로그인…'))
            login.clicked.connect(lambda _checked=False, i=index: self.sign_in(i))
            self.login_buttons[profile.key] = login
            row.addWidget(login)
            install = QPushButton(tr('설치…'))
            install.clicked.connect(lambda _checked=False, i=index: self.install_program(i))
            self.install_buttons[profile.key] = install
            row.addWidget(install)
            recheck = QPushButton(tr('다시 확인'))
            recheck.clicked.connect(lambda _checked=False, i=index: self.detect_program(i))
            self.recheck_buttons[profile.key] = recheck
            row.addWidget(recheck)
            if not self.executables[index]:
                label.setText(tr('{0}: 설치되지 않음', tr(profile.name)))
                login.hide()
            else:
                install.hide()
            layout.addLayout(row)
        self.auth_note = QLabel(tr('로그인은 프로그램의 공식 창에서 진행합니다. Grainy는 비밀번호나 키를 받지 않습니다. '
                                  '표시된 상태와 실제 인증 유효성은 다를 수 있습니다. '
                                  'Gemini는 인증 파일·환경변수의 존재만 확인합니다.'))
        self.auth_note.setWordWrap(True); layout.addWidget(self.auth_note)
        self.login_help = QLabel(); self.login_help.setWordWrap(True); self.login_help.hide()
        layout.addWidget(self.login_help)
        self.auth.status_ready.connect(self.auth_status_ready)
        self.auth.login_closed.connect(self.sign_in_closed)
        self.installs.closed.connect(self.install_closed)
        row = QHBoxLayout()
        self.connection = QLabel(); self.connection.setWordWrap(True); row.addWidget(self.connection, 1)
        self.enable = QPushButton(tr('AI 연결 켜기')); self.enable.clicked.connect(self.enable_connection)
        row.addWidget(self.enable); layout.addLayout(row)
        self.transcript = QPlainTextEdit(); self.transcript.setReadOnly(True)
        self.transcript.setMaximumBlockCount(3000); layout.addWidget(self.transcript, 1)
        self.input = PromptInput(); self.input.setPlaceholderText(tr('Grainy에 시킬 일을 입력하세요. Ctrl+Enter로 보냅니다.'))
        self.input.setMaximumHeight(130); layout.addWidget(self.input)
        self.input.sendRequested.connect(self.send)
        row = QHBoxLayout()
        self.send_button = QPushButton(tr('보내기')); self.send_button.clicked.connect(self.send); row.addWidget(self.send_button)
        self.stop_button = QPushButton(tr('중지')); self.stop_button.clicked.connect(self.stop); row.addWidget(self.stop_button)
        self.new_button = QPushButton(tr('새 대화')); self.new_button.clicked.connect(self.new_conversation); row.addWidget(self.new_button)
        self.status = QLabel(tr('명령을 입력해 주세요.')); self.status.setWordWrap(True); row.addWidget(self.status, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.undo_label = QLabel(); self.undo_label.setWordWrap(True); row.addWidget(self.undo_label, 1)
        self.undo_button = QPushButton(tr('마지막 AI 작업 되돌리기')); self.undo_button.clicked.connect(self.undo_last)
        row.addWidget(self.undo_button); layout.addLayout(row)
        self.undo_message = QLabel(); self.undo_message.setWordWrap(True); self.undo_message.hide(); layout.addWidget(self.undo_message)
        self.runner.event.connect(self.on_event)
        self.runner.finished.connect(self.on_finished)
        self.program.currentIndexChanged.connect(self.choice_changed)
        self.model.editingFinished.connect(self.choice_changed)
        self.timer = QTimer(self); self.timer.setInterval(500); self.timer.timeout.connect(self.refresh)
        self.refresh()

    def detect_program(self, index):
        if index < 0:
            return
        self.executables[index] = self.profiles[index].detect()
        profile = self.profiles[index]
        if self.executables[index]:
            self.check_auth(index)
        else:
            self.auth_labels[profile.key].setText(tr('{0}: 설치되지 않음', tr(profile.name)))
        self.refresh()

    def confirm_install(self, installer):
        box = QMessageBox(self)
        box.setWindowTitle(tr('설치 확인'))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(tr('설치할 프로그램: {0}\n제공자: {1}\n공식 안내: {2}\n\n실행할 명령:\n{3}',
                       tr(installer.name), installer.vendor, installer.source, installer.command))
        box.setInformativeText(tr('새 콘솔/Terminal 창에서 설치 과정을 볼 수 있습니다. 그 창에서 추가 확인을 '
                                  '요청할 수 있습니다. 결과를 읽고 아무 키나 누르면 설치 실행이 끝납니다.'))
        install = box.addButton(tr('설치'), QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(tr('취소'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is install

    def node_prerequisite(self):
        box = QMessageBox(self)
        box.setWindowTitle(tr('Node.js가 필요합니다'))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(tr('npm으로 설치하려면 Node.js와 npm이 먼저 필요합니다. 공식 Node.js LTS를 설치한 뒤 '
                       '다시 확인하고 설치…를 눌러 주세요. AI 프로그램은 자동으로 이어서 설치하지 않습니다.'))
        box.setInformativeText(NODE_URL)
        website = box.addButton(tr('Node.js 공식 페이지 열기'), QMessageBox.ButtonRole.ActionRole)
        winget = (box.addButton(tr('Node.js 설치… (winget)'), QMessageBox.ButtonRole.ActionRole)
                  if sys.platform == 'win32' else None)
        cancel = box.addButton(tr('취소'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is website:
            if not QDesktopServices.openUrl(QUrl(NODE_URL)):
                self.login_help.setText(tr('브라우저를 열지 못했습니다. 공식 주소를 직접 열어 주세요: {0}', NODE_URL))
                self.login_help.show()
        elif winget is not None and box.clickedButton() is winget:
            return NODE_WINDOWS
        return None

    def install_program(self, index):
        if self.in_flight or self.installs.sessions or index < 0 or self.executables[index]:
            return
        profile = self.profiles[index]
        installer = profile.installer()
        if installer is None:
            return
        if installer.needs_node and not node_ready():
            installer = self.node_prerequisite()
            if installer is None:
                return
        if not self.confirm_install(installer):
            return
        try:
            self.installs.start(profile.key, installer, Path(self.preferences.path).parent)
        except (OSError, ValueError):
            self.login_help.setText(tr('설치 창을 열지 못했습니다. 공식 안내에서 설치 방법을 확인해 주세요.'))
        else:
            self.login_help.setText(tr('열린 설치 창에서 결과를 확인한 뒤 아무 키나 누르세요. 프로그램을 다시 찾습니다. '
                                      '필요하면 다시 확인을 누르세요.'))
        self.login_help.show(); self.refresh()

    def install_closed(self, key):
        index = next(i for i, p in enumerate(self.profiles) if p.key == key)
        self.detect_program(index)
        self.login_help.setText(tr('프로그램을 찾았습니다. 로그인 상태를 확인하고 로그인…을 누르세요.')
                                if self.executables[index] else
                                tr('프로그램을 아직 찾지 못했습니다. 설치 창의 결과를 확인하세요. Node.js만 설치했다면 '
                                   '설치…를 다시 눌러 AI 프로그램을 설치하세요.'))
        self.login_help.show()

    def check_auth(self, index):
        if index < 0 or not self.executables[index]:
            return
        profile = self.profiles[index]
        self.auth_labels[profile.key].setText(tr('{0}: 확인 중…', tr(profile.name)))
        self.auth.check(profile, self.executables[index])

    def auth_status_ready(self, key, status):
        if not self.executables[next(i for i, p in enumerate(self.profiles) if p.key == key)]:
            return
        profile = next(p for p in self.profiles if p.key == key)
        text = {'signed_in': tr('로그인됨'), 'signed_out': tr('로그인되지 않음'),
                'timeout': tr('응답 없음')}.get(status.state, tr('확인할 수 없음'))
        if status.kind in ('Claude', 'ChatGPT', 'Google', 'API key', 'Cloud'):
            text = tr('{0} ({1})', text, tr(status.kind))
        self.auth_labels[key].setText(tr('{0}: {1}', tr(profile.name), text))

    def sign_in(self, index):
        if self.in_flight or not self.executables[index]:
            return
        profile = self.profiles[index]
        try:
            self.auth.login(profile, self.executables[index], Path(self.preferences.path).parent)
        except (OSError, ValueError):
            self.login_help.setText(tr('로그인 창을 열지 못했습니다. 프로그램 설치를 확인해 주세요.'))
        else:
            self.login_help.setText(tr('Gemini 창에서 "Login with Google"을 선택하세요. 이미 대화 화면이면 /auth로 '
                                      '로그인을 바꿀 수 있습니다. 완료 후 콘솔 창을 닫고 다시 확인하세요.')
                                    if profile.key == 'gemini' else
                                    tr('열린 프로그램 창에서 로그인을 마친 뒤 창을 닫으세요. 상태는 자동으로 다시 확인합니다.'))
        self.login_help.show(); self.refresh()

    def sign_in_closed(self, key):
        self.check_auth(next(i for i, p in enumerate(self.profiles) if p.key == key))
        self.refresh()

    def privacy_details(self):
        QMessageBox.information(self, tr('AI 연결 (MCP)'), tr(EXPLAIN) + '\n\n' + tr(NOTICE))

    def acknowledge(self):
        try:
            self.preferences.save('ai_notice_seen', True)
        except OSError:
            self.status.setText(tr('AI 창 설정을 저장하지 못했습니다.'))
            return
        self.notice.hide(); self.refresh()

    def choice_changed(self):
        if self.runner.running:
            return
        if self.session_choice and self.session_choice != (self.program.currentData(), self.model.text().strip()):
            self.new_conversation()
        if self.program.currentIndex() >= 0:
            try:
                self.preferences.save('ai_program', self.program.currentData())
            except OSError:
                self.status.setText(tr('AI 창 설정을 저장하지 못했습니다.'))
        self.refresh()

        self.check_auth(self.program.currentIndex())

    def enable_connection(self):
        self.service.set_enabled(True); self.refresh()

    def refresh(self):
        running = self.in_flight
        self.program.setEnabled(not running); self.model.setEnabled(not running)
        self.send_button.setEnabled(not running and self.program.currentIndex() >= 0 and self.service.enabled
                                    and bool(self.executables[self.program.currentIndex()])
                                    and self.program.currentData() not in self.installs.sessions
                                    and bool(self.preferences.values.get('ai_notice_seen')))
        self.stop_button.setEnabled(running); self.new_button.setEnabled(not running)
        self.enable.setVisible(not self.service.enabled)
        self.connection.setText(tr('AI 연결이 꺼져 있습니다. 켠 뒤 보내 주세요.') if not self.service.enabled
                                else self.service.error or tr('Grainy 도구로 사진을 읽고 작업합니다.'))
        McpDialog.refresh_undo(self)
        if running:
            self.undo_button.setEnabled(False)
        self.hints.setVisible(not all(self.executables))
        for index, profile in enumerate(self.profiles):
            key = profile.key
            installed = bool(self.executables[index])
            button = self.login_buttons[key]
            button.setVisible(installed)
            button.setEnabled(not running and key not in self.auth.logins and key not in self.installs.sessions)
            button.setStyleSheet('font-weight: bold; border: 2px solid #d58c25;' if key in self.auth_needed else '')
            self.install_buttons[key].setVisible(not installed)
            self.install_buttons[key].setEnabled(not running and not self.installs.sessions and profile.installer() is not None)
            self.recheck_buttons[key].setEnabled(not running)

    def grainy_context(self):
        """What the person sees in Grainy now, for 'this folder', 'these photos', 'this photo'. English, for the AI."""
        w = self.w; parts = []
        try:
            folder = getattr(w, 'folder_filter', None)
            if folder:
                recursive = bool(getattr(w, 'include_subfolders', None) and w.include_subfolders.isChecked())
                parts.append(f'The folder open in Grainy is {json.dumps(str(folder), ensure_ascii=False)} '
                             f'(list_photos with where folder={json.dumps(str(folder), ensure_ascii=False)}, '
                             f'recursive {str(recursive).lower()}); it shows {len(getattr(w, "visible_ids", []) or [])} photos.')
            selected = list(w.selected_ids()) if hasattr(w, 'selected_ids') else []
            if selected:
                shown = ', '.join(map(str, selected[:50])) + (' …' if len(selected) > 50 else '')
                parts.append(f"{len(selected)} photos are selected (scope 'selection'; ids {shown}).")
            current = getattr(w, 'current_id', None)
            if current:
                photo = w.catalog.photo(current)
                if photo:
                    parts.append(f"The photo open in the editor is id {current}, {json.dumps(photo['name'], ensure_ascii=False)} (scope 'current').")
        except Exception:                                                     # context is a help, never a reason to fail
            return ''
        if not parts:
            return ''
        return ('What the person sees in Grainy right now (read "this folder", "these photos", "this photo" '
                'with it): ' + ' '.join(parts))

    def send(self):
        if not self.send_button.isEnabled():
            return
        prompt = self.input.toPlainText().strip()
        if not prompt:
            return
        if len(prompt.encode('utf-16-le')) > 16000:
            self.status.setText(tr('요청이 너무 깁니다. 8,000자 이내로 줄여 주세요.'))
            return
        if not self.service.running and not self.service.start():
            self.refresh(); return
        # A running HTTP server can have failed to write its bridge on startup.
        if not self.service.write_bridges() or not self.service.write_connection():
            self.status.setText(tr('AI 중계 파일을 준비하지 못했습니다. AI 연결 설정을 확인해 주세요.'))
            return
        index = self.program.currentIndex()
        profile = self.profiles[index]
        sent_prompt = prompt
        if self.history and not profile.persistent:
            recent = self.history[:]
            context = json.dumps(recent, ensure_ascii=False)
            while len(context.encode('utf-16-le')) > 12000 and recent:
                recent = recent[2:]
                context = json.dumps(recent, ensure_ascii=False)
            sent_prompt = ('Previous conversation, for context only; do not repeat its completed actions:\n'
                           + context + '\n\nNew request:\n' + prompt)
            self.transcript.appendPlainText(tr('기록 저장을 끈 새 실행에 이전 대화 내용을 전달합니다.'))
        server = install_commands(sys.platform == 'win32', self.service.folder)['claude_desktop_json']['mcpServers']['grainy']
        workspace = Path(self.preferences.path).parent / 'ai-workspace' / profile.key / self.run_key
        self.workspaces.add(workspace)
        request = Request(self.executables[index], workspace, server, sent_prompt, language(), self.model.text().strip(),
                          self.session_id, self.grainy_context())
        try:
            self.preferences.save('ai_program', profile.key)
            self.runner.start(profile, request, secrets=(self.service.token,))
        except (OSError, ValueError, RuntimeError):
            self.status.setText(tr('AI 프로그램을 시작하지 못했습니다. 설치와 계정 로그인을 확인해 주세요.'))
            return
        self.session_choice = (profile.key, request.model)
        self.in_flight = True
        self.history.append({'role': 'user', 'content': prompt[:8000]})
        self.turn_text = ''
        self.transcript.appendPlainText(tr('나: {0}', prompt) + '\n')
        self.input.clear(); self.status.setText(tr('AI가 작업 중입니다…')); self.refresh()

    def on_event(self, event):
        if event.kind == 'session':
            self.session_id = event.text
        elif event.kind == 'text':
            self.turn_text = (self.turn_text + event.text)[-8000:]
            cursor = self.transcript.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(event.text); self.transcript.setTextCursor(cursor); self.transcript.ensureCursorVisible()
        elif event.kind == 'tool':
            text = event.text if event.count is None else tr('{0} · 대상 {1}장', event.text, event.count)
            self.transcript.appendPlainText('\n' + text + '\n')
        elif event.kind == 'auth_error':
            profile = self.profiles[self.program.currentIndex()]
            self.auth_needed.add(profile.key)
            self.transcript.appendPlainText(tr("로그인이 필요합니다. '로그인…'을 눌러 {0}에 다시 로그인하세요.", tr(profile.name)))
            self.refresh()
        elif event.kind == 'error':
            self.transcript.appendPlainText(tr('AI 프로그램 오류가 발생했습니다. 다시 시도해 주세요.'))
        elif event.kind in ('launch_error', 'exit'):
            self.transcript.appendPlainText(tr('AI 프로그램이 종료되었습니다. 터미널에서 해당 프로그램의 설치·로그인·사용 한도를 확인해 주세요.'))

    def on_finished(self, result):
        self.in_flight = False
        self.history.append({'role': 'assistant', 'content': self.turn_text, 'status': result})
        self.history = self.history[-6:]
        if result == 'done':
            self.auth_needed.discard(self.program.currentData())
        text = {'done': tr('완료'), 'stopped': tr('중지됨 · 이미 적용한 작업은 유지됩니다.'),
                'timeout': tr('시간 초과 · 프로그램을 중지했습니다.'),
                'auth_required': tr('로그인이 필요합니다.'),
                'failed': tr('실패 · 프로그램의 로그인과 사용 한도를 확인해 주세요.')}.get(result, tr('실패'))
        self.status.setText(text); self.transcript.appendPlainText('\n' + text + '\n'); self.refresh()

    def stop(self):
        self.runner.stop(); self.status.setText(tr('중지하는 중…'))

    def new_conversation(self):
        if self.runner.running:
            return
        self.session_id = ''; self.runner.session_id = ''; self.session_choice = None
        self.history.clear(); self.turn_text = ''
        self.clean_workspaces(); self.run_key = uuid4().hex
        self.transcript.clear(); self.status.setText(tr('새 대화를 시작합니다.'))

    def undo_last(self):
        McpDialog.undo_last(self)

    def showEvent(self, event):
        super().showEvent(event); self.timer.start(); self.refresh(); self.input.setFocus()
        for index in range(len(self.profiles)):
            self.detect_program(index)

    def hideEvent(self, event):
        self.timer.stop(); super().hideEvent(event)

    def closeEvent(self, event):
        self.runner.shutdown(); super().closeEvent(event)

    def shutdown(self):
        self.timer.stop(); self.runner.shutdown(); self.clean_workspaces(); self.hide()

    def clean_workspaces(self):
        """Only this window's generated run directories, after their processes stop."""
        if self.runner.running:
            return
        root = (Path(self.preferences.path).parent / 'ai-workspace').resolve()
        for path in tuple(self.workspaces):
            resolved = path.resolve()
            if resolved.is_relative_to(root) and resolved.name == self.run_key:
                try:
                    shutil.rmtree(resolved)
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                self.workspaces.discard(path)
