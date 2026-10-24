"""Non-modal Ask AI window. The editor and MCP service remain usable while it runs."""
from pathlib import Path
import json
import shutil
import sys
from uuid import uuid4

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from .ai_programs import PROFILES, AiRunner, Request
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
        for index, profile in enumerate(profiles):
            self.program.addItem(tr(profile.name), profile.key)
            if not self.executables[index]:
                self.program.model().item(index).setEnabled(False)
                self.program.setItemData(index, tr('설치: {0}', profile.install), Qt.ItemDataRole.ToolTipRole)
        selected = next((i for i, p in enumerate(profiles)
                         if p.key == self.preferences.values.get('ai_program') and self.executables[i]), None)
        if selected is None:
            selected = next((i for i, command in enumerate(self.executables) if command), -1)
        self.program.setCurrentIndex(selected)
        row.addWidget(self.program, 1)
        row.addWidget(QLabel(tr('모델 (선택)')))
        self.model = QLineEdit(); self.model.setPlaceholderText(tr('비워 두면 프로그램 기본 모델'))
        row.addWidget(self.model, 1); layout.addLayout(row)
        self.hints = QLabel('\n'.join(tr('{0} 설치: {1}', tr(p.name), p.install)
                                     for i, p in enumerate(profiles) if not self.executables[i]))
        self.hints.setWordWrap(True); self.hints.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.hints)
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

    def enable_connection(self):
        self.service.set_enabled(True); self.refresh()

    def refresh(self):
        running = self.in_flight
        self.program.setEnabled(not running); self.model.setEnabled(not running)
        self.send_button.setEnabled(not running and self.program.currentIndex() >= 0 and self.service.enabled
                                    and bool(self.preferences.values.get('ai_notice_seen')))
        self.stop_button.setEnabled(running); self.new_button.setEnabled(not running)
        self.enable.setVisible(not self.service.enabled)
        self.connection.setText(tr('AI 연결이 꺼져 있습니다. 켠 뒤 보내 주세요.') if not self.service.enabled
                                else self.service.error or tr('Grainy 도구로 사진을 읽고 작업합니다.'))
        McpDialog.refresh_undo(self)
        if running:
            self.undo_button.setEnabled(False)

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
        request = Request(self.executables[index], workspace, server, sent_prompt, language(), self.model.text().strip(), self.session_id)
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
        elif event.kind == 'error':
            self.transcript.appendPlainText('\n' + tr('오류: {0}', event.text))
        elif event.kind in ('launch_error', 'exit'):
            self.transcript.appendPlainText(tr('AI 프로그램이 종료되었습니다. 터미널에서 해당 프로그램의 설치·로그인·사용 한도를 확인해 주세요.'))

    def on_finished(self, result):
        self.in_flight = False
        self.history.append({'role': 'assistant', 'content': self.turn_text, 'status': result})
        self.history = self.history[-6:]
        text = {'done': tr('완료'), 'stopped': tr('중지됨 · 이미 적용한 작업은 유지됩니다.'),
                'timeout': tr('시간 초과 · 프로그램을 중지했습니다.'),
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
