"""User-facing Codex command window with streamed answers and edit review."""
from .i18n import tr, keys
from copy import deepcopy
from html import escape
from PySide6.QtCore import Qt
from PySide6.QtGui import QShortcut,QKeySequence
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QTextBrowser,QPlainTextEdit,QCheckBox,QComboBox)
from .commands import CommandSession,EDIT_FIELDS
from .auth_dialog import AuthDialog,plain_label


class CommandDialog(QDialog):
    def __init__(self,client,parent=None,context=None,apply=None,restore_batch=None):
        super().__init__(parent)
        self.client=client;self.context=context;self.apply_callback=apply
        self.restore_callback=restore_batch;self.request_scope='auto'
        self.session=CommandSession(client,self)
        self.auth_dialog=None;self.records=[];self.proposal=None;self.snapshot=None
        self.current_answer='';self.model_loaded=False
        self.setWindowTitle(tr('Codex 명령 — Grainy'))
        self.resize(760,820);self.setMinimumSize(540,630)
        layout=QVBoxLayout(self);layout.setContentsMargins(22,20,22,20);layout.setSpacing(12)
        top=QHBoxLayout();title=plain_label('Codex 명령')
        title.setStyleSheet('font-size:22px;color:#f4e7d4;font-weight:600')
        top.addWidget(title);top.addStretch()
        self.account_button=QPushButton(tr('계정 연결'));self.account_button.clicked.connect(self.show_account)
        top.addWidget(self.account_button)
        self.new_button=QPushButton(tr('새 대화'));self.new_button.clicked.connect(self.new_chat)
        top.addWidget(self.new_button);layout.addLayout(top)
        intro=plain_label('원하는 보정을 말하거나 사용법을 물어보세요.\n예: “이 폴더의 모든 사진을 흑백으로”, “선택한 사진의 노출을 0.5 올려줘”')
        intro.setObjectName('muted');layout.addWidget(intro)
        self.transcript=QTextBrowser();self.transcript.setOpenExternalLinks(False)
        self.transcript.setStyleSheet('background:#14181d;border:1px solid #363f49;border-radius:0px;padding:12px;')
        self.transcript.setAccessibleName(tr('Codex 대화'));layout.addWidget(self.transcript,1)
        self.proposal_label=plain_label();self.proposal_label.hide();layout.addWidget(self.proposal_label)
        self.apply_button=QPushButton(tr('이 보정을 사진에 적용'));self.apply_button.setObjectName('primary')
        self.apply_button.clicked.connect(self.apply_proposal);self.apply_button.hide();layout.addWidget(self.apply_button)
        scope_row=QHBoxLayout();scope_row.addWidget(plain_label('적용 범위'))
        self.scope=QComboBox();self.scope.setAccessibleName(tr('명령 적용 범위'))
        for title,key in [('명령에서 지정 · 기본 현재 사진','auto'),('현재 사진','current'),('선택한 사진','selection'),('현재 폴더 전체','folder')]:self.scope.addItem(tr(title),key)
        scope_row.addWidget(self.scope,1);layout.addLayout(scope_row)
        undo_row=QHBoxLayout()
        self.batch_undo=QPushButton(tr('최근 일괄 명령 실행 취소'));self.batch_redo=QPushButton(tr('일괄 명령 다시 실행'))
        self.batch_undo.clicked.connect(lambda:self.restore_batch(False));self.batch_redo.clicked.connect(lambda:self.restore_batch(True))
        for button in (self.batch_undo,self.batch_redo):
            button.setVisible(restore_batch is not None);undo_row.addWidget(button)
        layout.addLayout(undo_row)
        self.input=QPlainTextEdit();self.input.setPlaceholderText(keys(tr('명령 입력…  Ctrl+Enter로 보내기')))
        self.input.setStyleSheet('background:#14181d;border:1px solid #6c7b89;border-radius:0px;padding:10px;')
        self.input.setMinimumHeight(90)
        self.input.setAccessibleName(tr('명령 입력'));self.input.setMaximumHeight(115);layout.addWidget(self.input)
        options=QHBoxLayout()
        self.attach=QCheckBox(tr('현재 보정값 함께 보내기'));self.attach.setChecked(context is not None)
        self.attach.setEnabled(context is not None);options.addWidget(self.attach);options.addStretch()
        self.models=QComboBox();self.models.addItem(tr('계정 기본 모델'),None);self.models.setMaximumWidth(240)
        options.addWidget(self.models);layout.addLayout(options)
        self.notice=plain_label('텍스트와 선택한 보정값만 전송합니다. 사진은 전송하지 않습니다. Codex 구독 사용량이 사용됩니다.')
        self.notice.setObjectName('muted');layout.addWidget(self.notice)
        bottom=QHBoxLayout();self.status=plain_label();bottom.addWidget(self.status,1)
        self.stop_button=QPushButton(tr('중단'));self.stop_button.clicked.connect(self.session.cancel);bottom.addWidget(self.stop_button)
        self.send_button=QPushButton(tr('보내기'));self.send_button.setObjectName('primary');self.send_button.clicked.connect(self.send)
        bottom.addWidget(self.send_button);layout.addLayout(bottom)
        shortcut=QShortcut(QKeySequence('Ctrl+Return'),self);shortcut.activated.connect(self.send)
        self.session.changed.connect(self.update_state)
        self.session.messageChanged.connect(self.answer_changed)
        self.session.completed.connect(self.finished_answer)
        self.session.failed.connect(self.failed)
        self.session.modelsChanged.connect(self.load_models)
        client.changed.connect(self.account_changed)
        self.account_changed();self.paint_transcript()

    def show_account(self):
        if self.auth_dialog is None:self.auth_dialog=AuthDialog(self.client,self)
        self.auth_dialog.show();self.auth_dialog.raise_();self.client.refresh()

    def account_changed(self):
        if self.client.connected and not self.model_loaded:
            self.model_loaded=True;self.session.models()
        if not self.client.connected:self.model_loaded=False
        self.update_state()

    def load_models(self,models):
        previous=self.models.currentData();self.models.clear();self.models.addItem(tr('계정 기본 모델'),None)
        for m in models:
            if not m.get('hidden') and m.get('model'):
                self.models.addItem(m.get('displayName') or m['model'],m['model'])
        index=self.models.findData(previous);self.models.setCurrentIndex(max(0,index))

    def update_state(self):
        running=self.session.running
        self.send_button.setEnabled(self.client.connected and not running and not self.client.busy)
        self.stop_button.setEnabled(running and not self.session.cancel_pending)
        self.new_button.setEnabled(not running);self.models.setEnabled(not running)
        self.attach.setEnabled(not running and self.context is not None)
        self.scope.setEnabled(not running and self.context is not None)
        self.batch_undo.setEnabled(not running);self.batch_redo.setEnabled(not running)
        self.status.setText(self.session.status if self.client.connected else self.client.message)
        self.account_button.setText(tr('계정 · 연결됨') if self.client.connected else tr('계정 연결'))

    def paint_transcript(self):
        parts=[]
        for role,text in self.records:
            color='#dec49f' if role=='나' else '#a6c2d3'
            parts.append(f'<p style="color:{color}"><b>{escape(tr(role))}</b></p><p>{escape(text).replace(chr(10),"<br>")}</p>')
        if self.session.running or self.current_answer:
            parts.append('<p style="color:#a6c2d3"><b>Codex</b></p><p>'+escape(self.current_answer or tr('응답을 준비하고 있습니다…')).replace('\n','<br>')+'</p>')
        self.transcript.setHtml(''.join(parts) or tr('<p style="color:#8e99a5">아래에 명령을 입력해 대화를 시작하세요.</p>'))
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())

    def send(self):
        text=self.input.toPlainText().strip()
        if not text or self.session.running:return
        if len(text)>20000:
            self.status.setText(tr('명령은 20,000자 이내로 입력해 주세요.'));return
        self.snapshot=deepcopy(self.context()) if self.context and self.attach.isChecked() else None
        self.request_scope=self.scope.currentData()
        targets={'explicit_scope':self.request_scope,'counts':{key:len(ids) for key,ids in self.snapshot.get('scopes',{}).items()}} if self.snapshot else None
        if self.session.send(text,self.snapshot['settings'] if self.snapshot else None,self.models.currentData(),targets):
            self.records.append(('나',text));self.current_answer='';self.input.clear()
            self.proposal=None;self.apply_button.hide();self.proposal_label.hide();self.paint_transcript()

    def answer_changed(self,text):
        self.current_answer=text;self.paint_transcript()

    def finished_answer(self,proposal):
        if self.request_scope!='auto':proposal={**proposal,'scope':self.request_scope}
        self.current_answer='';self.records.append(('Codex',proposal['message']));self.paint_transcript()
        self.proposal=proposal
        if proposal['action']=='chat':return
        if not self.snapshot or not self.apply_callback:
            self.proposal_label.setText(tr('사진에 적용하려면 편집기에서 사진을 선택하고 “현재 보정값 함께 보내기”를 켜서 요청하세요.'))
            self.proposal_label.show();return
        scope=proposal.get('scope','current')
        if 'scopes' in self.snapshot:
            from .command_batch import describe
            target_text=describe(self.snapshot,scope)
            count=len(self.snapshot['scopes'][scope])
        else:target_text='현재 사진 · 1장';count=1
        lines=[]
        for key,value in proposal['changes'].items():
            if key=='monochrome':description='흑백으로 전환' if value else '컬러로 전환'
            elif proposal.get('operations',{}).get(key)=='add':description=f'각 사진의 현재 값에 {value:+g} (허용 범위 내)'
            else:description=f'{value:g}로 설정'
            lines.append(f'{EDIT_FIELDS[key][0]}: {description}')
        text=target_text+'\n\n'+('사진별 자동톤 · 검정과 흰색 양끝 3% 여유' if proposal['action']=='auto_tone' else '\n'.join(lines))
        self.proposal_label.setText(text or '변경할 보정값이 없습니다.');self.proposal_label.show()
        self.apply_button.setVisible(proposal['action']=='auto_tone' or bool(proposal['changes']))
        self.apply_button.setText(tr('{0}장에 보정 적용', f'{count:,}'));self.apply_button.setEnabled(count>0)

    def apply_proposal(self):
        if not self.proposal or not self.apply_callback:return
        try:count=self.apply_callback(self.snapshot,self.proposal)
        except (ValueError,RuntimeError) as error:
            self.status.setText(str(error));return
        except Exception:
            self.status.setText(tr('보정을 저장하지 못했습니다. 카탈로그 저장 위치를 확인해 주세요.'));return
        self.apply_button.setEnabled(False)
        batch=self.proposal.get('scope','current')!='current'
        self.status.setText(tr('{0}장을 변경했습니다. ', f'{(count if count is not None else 1):,}')+(tr('“최근 일괄 명령 실행 취소”로 함께 되돌릴 수 있습니다.') if batch else tr('편집기의 실행 취소로 되돌릴 수 있습니다.')))

    def restore_batch(self,redo):
        if not self.restore_callback:return
        try:count=self.restore_callback(redo)
        except (ValueError,RuntimeError) as error:self.status.setText(str(error));return
        except Exception:self.status.setText(tr('일괄 명령을 복원하지 못했습니다. 카탈로그 저장 위치를 확인해 주세요.'));return
        self.apply_button.setEnabled(False)
        self.status.setText(tr('{0}장 일괄 명령을 ', f'{count:,}')+(tr('다시 실행했습니다.') if redo else tr('실행 취소했습니다.')))

    def failed(self,text):
        if self.current_answer:self.records.append(('Codex · 미완료',self.current_answer))
        self.current_answer='';self.records.append(('알림',text));self.paint_transcript()

    def new_chat(self):
        if self.session.running:return
        self.session.new_chat();self.records=[];self.current_answer='';self.proposal=None;self.snapshot=None
        self.apply_button.hide();self.proposal_label.hide();self.paint_transcript()

    def reject(self):
        if self.session.running:self.session.cancel()
        super().reject()

    def closeEvent(self,event):
        if self.session.running:self.session.cancel()
        super().closeEvent(event)
