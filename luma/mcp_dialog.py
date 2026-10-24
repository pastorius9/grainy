"""Settings › AI Connection (MCP): the switch (on unless the person turned it off), how to install Grainy's MCP in
each AI program, undoing the last AI task, the recent activity, and under Advanced the key and the port for programs
that connect over HTTP. The install commands hold neither the key nor the port: they start the bridge in Grainy's
per-user folder, which finds both there (docs/mcp-install.json has the same commands). Grainy never runs them and
never edits other programs' configuration files: the person copies the text."""
from datetime import datetime
import json
import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import QTimer,Qt
from PySide6.QtGui import QFontDatabase,QGuiApplication
from PySide6.QtWidgets import (QCheckBox,QDialog,QFileDialog,QHBoxLayout,QLabel,QLineEdit,QListWidget,QMessageBox,
    QPlainTextEdit,QTabWidget,QToolButton,QVBoxLayout,QWidget)

from .i18n import tr

# Names everything get_photo and list_photos send (tests/test_mcp_service.py checks it).
EXPLAIN=('Grainy가 실행 중이면 이 컴퓨터의 AI 프로그램(Claude Code, Codex, Claude 데스크톱)이 Grainy를 쓸 수 있습니다. 그 프로그램에 '
    '아래 방법으로 Grainy의 MCP를 설치하고 일을 시켰을 때만, 그 프로그램이 사진 목록과 사진 정보, 보정값, 미리보기 이미지(최대 1568픽셀)를 '
    '읽고, 보정값과 별점·선택 표시·색 라벨을 바꾸고, 가상 사본과 스냅샷을 만들고, 그 프로그램이 정한 폴더의 사진을 보관함으로 가져오고, '
    '그 프로그램이 정한 폴더로 사진을 내보냅니다. 사진 정보는 파일 이름과 폴더 경로, 파일 형식과 픽셀 크기, 키워드, 별점, 선택·제외 표시, '
    '색 라벨, 카메라와 렌즈, 노출 정보(ISO, 셔터 속도, 조리개, 초점 거리), 촬영 날짜이며 위치 정보는 보내지 않습니다. AI가 읽은 내용은 '
    '그 AI 서비스로 전송되고 그 프로그램에도 남을 수 있습니다. Grainy 자체는 사진을 인터넷으로 보내지 않으며, 연결은 이 컴퓨터 안에서만 '
    '열립니다. 원본 사진 파일은 바뀌지 않고, AI가 한 보정은 최근 작업부터 차례로 되돌릴 수 있습니다.')
REGEN=('새 연결 키를 만들까요? HTTP로 직접 연결한 프로그램은 새 키로 다시 설정해야 합니다. 위 설치 방법으로 설치한 AI 프로그램은 '
    '그대로 쓸 수 있습니다.')
# The bridges' place on every computer, as PowerShell or the shell expands it when the command runs.
WINDOWS_BRIDGE='$env:LOCALAPPDATA\\Grainy\\mcp\\grainy-mcp-bridge.ps1'
MACOS_BRIDGE='$HOME/Library/Application Support/Grainy/mcp/grainy-mcp-bridge.sh'
CLIENTS=(('claude_code','Claude Code'),('codex','Codex'),('claude_desktop_json','Claude 데스크톱'))


def activity_text(name,outcome):
    """'name · outcome' of one McpService.note entry in the interface language. The outcomes are short English
    codes (the protocol layer and the tests use them); tool and method names stay as they are."""
    name={'server':tr('AI 연결'),'files':tr('중계 파일'),'key':tr('연결 키')}.get(name,name)
    fixed={'started':tr('시작함'),'stopped':tr('멈춤'),'could not start':tr('시작하지 못함'),
           'port not saved':tr('포트를 저장하지 못함'),'on in another Grainy':tr('다른 Grainy에서 켜져 있음'),
           'new key':tr('새 키를 만듦'),'could not write the bridge files':tr('쓰지 못함 (설치한 AI 프로그램이 연결할 수 없음)'),
           'refused 401':tr('거부함: 연결 키가 없거나 맞지 않음'),
           'refused 403':tr('거부함: 허용되지 않은 주소나 웹 페이지에서 온 요청'),
           'ok':tr('성공'),'error':tr('오류'),'undone in Grainy':tr('Grainy에서 되돌림')}
    text=fixed.get(outcome)
    if text is None and (match:=re.fullmatch(r'job (j\d+) (done|failed|cancelled|stopped in Grainy)',outcome)):
        job,state=match.groups()
        if state=='done':text=tr('작업 {0} 완료',job)
        elif state=='failed':text=tr('작업 {0} 실패',job)
        elif state=='cancelled':text=tr('작업 {0} 취소됨',job)
        else:text=tr('작업 {0}을(를) Grainy에서 멈춤',job)
    elif text is None and (match:=re.fullmatch(r'(refused|error) (-?\d+)',outcome)):
        text=tr('거부함 ({0})',match[2]) if match[1]=='refused' else tr('오류 {0}',match[2])
    elif text is None and (match:=re.fullmatch(r'(error|failed): (.*)',outcome,re.S)):
        text=tr('오류: {0}',match[2]) if match[1]=='error' else tr('실패: {0}',match[2])
    return f'{name} · {text if text is not None else outcome}'


def standard_folder(windows):
    """Where Grainy keeps the bridges for this user (McpService.folder with the normal settings): the folder the
    install commands name. None where there is no such place."""
    if windows:
        base=os.environ.get('LOCALAPPDATA')
        return Path(base)/'Grainy'/'mcp' if base else None
    return Path.home()/'Library'/'Application Support'/'Grainy'/'mcp' if sys.platform=='darwin' else None


def same_folder(a,b):
    return b is not None and os.path.normcase(os.path.abspath(a))==os.path.normcase(os.path.abspath(b))


def install_commands(windows,folder=None):
    """{'claude_code','codex','claude_desktop_json'}: what the person runs once (in PowerShell on Windows, in
    Terminal on macOS) or puts into claude_desktop_config.json. Without folder, or with the standard one, the same
    on every computer (docs/mcp-install.json); another folder (a temporary library from tools/try_mcp.py) is named
    as it is."""
    if folder is not None and same_folder(folder,standard_folder(windows)):folder=None
    if windows:
        if folder is None:
            path=f'"{WINDOWS_BRIDGE}"'
            # JSON cannot expand %LOCALAPPDATA%: PowerShell finds the folder when Claude Desktop starts the bridge.
            start=['-Command',"& (Join-Path $env:LOCALAPPDATA 'Grainy\\mcp\\grainy-mcp-bridge.ps1')"]
        else:
            script=str(Path(folder)/'grainy-mcp-bridge.ps1');path="'"+script.replace("'","''")+"'";start=['-File',script]
        # '--' quoted: PowerShell drops a bare -- before a .ps1 script, and npm installs claude and codex as scripts.
        run=f"'--' powershell -NoProfile -ExecutionPolicy Bypass -File {path}"
        server={'command':'powershell','args':['-NoProfile','-ExecutionPolicy','Bypass',*start]}
    else:
        if folder is None:
            path=f'"{MACOS_BRIDGE}"';server={'command':'/bin/sh','args':['-c',f'exec /bin/sh {path}']}
        else:
            script=str(Path(folder)/'grainy-mcp-bridge.sh');path="'"+script.replace("'","'\\''")+"'"
            server={'command':'/bin/sh','args':[script]}
        run=f'-- sh {path}'
    return {'claude_code':f'claude mcp add --scope user grainy {run}','codex':f'codex mcp add grainy {run}',
            'claude_desktop_json':{'mcpServers':{'grainy':server}}}


def install_text(value):
    return value if isinstance(value,str) else json.dumps(value,indent=2,ensure_ascii=False)


class McpDialog(QDialog):
    def __init__(self,service,window):
        super().__init__(window)
        from .app import button
        self.service=service;self.w=window;self.updating=False;self.key_visible=False;self.opened_for=None;self.grown=0
        self.setWindowTitle(tr('AI 연결 (MCP)'));self.setModal(False);self.resize(720,760)
        box=QVBoxLayout(self)
        explain=QLabel(tr(EXPLAIN));explain.setWordWrap(True);explain.setObjectName('muted');box.addWidget(explain)
        row=QHBoxLayout()
        self.enable=QCheckBox(tr('AI 연결 켜기'));self.enable.toggled.connect(self.toggled);row.addWidget(self.enable)
        self.status=QLabel();self.status.setWordWrap(True);row.addWidget(self.status,1);box.addLayout(row)
        box.addWidget(QLabel(tr('AI 프로그램에 설치하기')))
        self.tabs=QTabWidget();self.setups={}
        mono=QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        for key,title in CLIENTS:
            page=QWidget();layout=QVBoxLayout(page)
            instruction=QLabel();instruction.setWordWrap(True);layout.addWidget(instruction)
            text=QPlainTextEdit();text.setReadOnly(True);text.setFont(mono);text.setMinimumHeight(90);text.setMaximumHeight(130)
            layout.addWidget(text)
            copy=button(tr('복사'),lambda checked=False,key=key:self.copy(self.setup_text(key)));layout.addWidget(copy)
            self.tabs.addTab(page,tr(title));self.setups[key]=(instruction,text,copy)
        box.addWidget(self.tabs)
        note=QLabel(tr('한 번만 설치하면 됩니다. 설치에는 연결 키가 들어가지 않습니다. 설치한 AI 프로그램은 Grainy가 실행 중이고 AI 연결이 '
                       '켜져 있을 때 Grainy를 쓰며, Grainy보다 먼저 켜 두어도 됩니다.'))
        note.setWordWrap(True);note.setObjectName('muted');box.addWidget(note)
        row=QHBoxLayout();row.addWidget(QLabel(tr('AI 내보내기 폴더')))
        self.export_folder=QLineEdit();self.export_folder.setReadOnly(True);row.addWidget(self.export_folder,1)
        row.addWidget(button(tr('바꾸기…'),self.choose_export_folder));box.addLayout(row)
        row=QHBoxLayout();self.undo_label=QLabel();self.undo_label.setWordWrap(True);row.addWidget(self.undo_label,1)
        self.undo_button=button(tr('마지막 AI 작업 되돌리기'),self.undo_last);row.addWidget(self.undo_button);box.addLayout(row)
        self.undo_message=QLabel();self.undo_message.setWordWrap(True);self.undo_message.hide();box.addWidget(self.undo_message)
        box.addWidget(QLabel(tr('최근 AI 작업')))
        self.activity=QListWidget();self.activity.setMaximumHeight(150);box.addWidget(self.activity)
        # Advanced: for programs that connect over HTTP with the key instead of the bridge.
        self.advanced_toggle=QToolButton();self.advanced_toggle.setText(tr('고급'));self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setAutoRaise(True);self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.toggled.connect(self.show_advanced);box.addWidget(self.advanced_toggle)
        self.advanced=QWidget();layout=QVBoxLayout(self.advanced);layout.setContentsMargins(0,0,0,0)
        self.http=QLabel();self.http.setWordWrap(True);self.http.setObjectName('muted');self.http.setTextFormat(Qt.TextFormat.PlainText)
        self.http.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse);layout.addWidget(self.http)
        row=QHBoxLayout();row.addWidget(QLabel(tr('연결 키')))
        self.key=QLineEdit();self.key.setReadOnly(True);self.key.setEchoMode(QLineEdit.EchoMode.Password);row.addWidget(self.key,1)
        self.show_key=button(tr('보기'),self.toggle_key);row.addWidget(self.show_key)
        self.copy_key=button(tr('복사'),lambda:self.copy(self.service.token or ''));row.addWidget(self.copy_key)
        self.new_key=button(tr('새 키 만들기'),self.regenerate);row.addWidget(self.new_key);layout.addLayout(row)
        row=QHBoxLayout();self.port_button=button(tr('다른 포트로 바꾸기'),self.change_port);row.addWidget(self.port_button)
        row.addStretch(1);layout.addLayout(row)
        self.port_note=QLabel(tr('포트를 바꿨습니다. HTTP로 직접 연결한 프로그램은 새 주소로 다시 설정해야 합니다. 위 설치 방법으로 설치한 '
                                 'AI 프로그램은 그대로 쓸 수 있습니다.'))
        self.port_note.setWordWrap(True);self.port_note.hide();layout.addWidget(self.port_note)
        self.advanced.hide();box.addWidget(self.advanced)
        row=QHBoxLayout();row.addStretch(1);row.addWidget(button(tr('닫기'),self.close));box.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.refresh)
        self.refresh()

    # state
    def showEvent(self,event):
        super().showEvent(event);self.timer.start();self.refresh()

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)

    def show_advanced(self,on):
        self.advanced.setVisible(on)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        # The window grows by the area it opens (as far as the screen allows) and shrinks back when it closes,
        # so the install texts keep their height.
        if on:
            screen=self.screen().availableGeometry().height() if self.screen() is not None else self.height()
            extra=self.advanced.sizeHint().height()+self.layout().spacing()
            self.grown=max(0,min(extra,screen-self.height()-40));self.resize(self.width(),self.height()+self.grown)
        else:
            self.resize(self.width(),self.height()-self.grown);self.grown=0

    def refresh(self):
        service=self.service;running=service.running
        self.updating=True
        try:self.enable.setChecked(service.enabled)
        finally:self.updating=False
        if running:self.status.setText(tr('켜짐 · 포트 {0}',service.bound_port))
        elif service.error:self.status.setText(service.error)
        else:self.status.setText(tr('꺼짐'))
        # A connection that should be on but did not start (the port is taken): Advanced opens once, at Use Another Port.
        problem=service.error if service.enabled and not running and not service.elsewhere else None
        if problem and problem!=self.opened_for:self.advanced_toggle.setChecked(True)
        self.opened_for=problem
        self.port_button.setEnabled(service.enabled and not service.elsewhere)
        self.port_note.setVisible(service.port_changed)
        from .mcp_service import DEFAULT_PORT
        address=f'http://127.0.0.1:{service.bound_port or service.port or DEFAULT_PORT}/mcp'
        self.http.setText(tr('HTTP로 직접 연결하는 프로그램용입니다. 주소 {0}, 요청 헤더 Authorization: Bearer <연결 키>. 새 키를 만들거나 '
                             '포트를 바꾸면 그 프로그램은 다시 설정해야 합니다. 위 설치 방법으로 설치했다면 필요 없습니다.',address))
        token=service.token or ''
        self.key.setText(token);self.key.setEchoMode(QLineEdit.EchoMode.Normal if self.key_visible else QLineEdit.EchoMode.Password)
        self.show_key.setText(tr('숨기기') if self.key_visible else tr('보기'))
        for widget in (self.key,self.show_key,self.copy_key,self.new_key):widget.setEnabled(running)
        self.export_folder.setText(str(service.export_root()))
        shell='PowerShell' if sys.platform=='win32' else tr('터미널')
        texts={'claude_code':tr('{0}에서 아래 한 줄을 한 번 실행한 뒤 Claude Code를 다시 시작하세요. 확인은 claude mcp list. 이미 있다고 '
                                '나오면 먼저 claude mcp remove grainy --scope user를 실행하세요.',shell),
               'codex':tr('{0}에서 아래 한 줄을 한 번 실행한 뒤 Codex를 다시 시작하세요. 확인은 codex mcp list.',shell),
               'claude_desktop_json':tr('Claude 데스크톱에서 설정 › 개발자 › 구성 편집을 눌러 claude_desktop_config.json을 열고, '
                                        '"mcpServers" 안에 아래 "grainy" 항목을 넣은 뒤(파일이 비어 있으면 아래 내용을 그대로 붙여 넣으세요) '
                                        'Claude 데스크톱을 다시 시작하세요.')}
        for key,(instruction,text,_) in self.setups.items():
            instruction.setText(texts[key])
            shown=self.setup_text(key)
            if text.toPlainText()!=shown:text.setPlainText(shown)
        self.refresh_undo()
        lines=[f'{when} · {activity_text(name,outcome)}' for when,name,outcome in reversed(list(service.activity))]
        if [self.activity.item(i).text() for i in range(self.activity.count())]!=lines:
            self.activity.clear();self.activity.addItems(lines)

    def refresh_undo(self):
        from . import photo_service
        w=self.w;entry=None
        try:entry=photo_service.last_batch(w.catalog) if not w.closing else None
        except Exception:entry=None
        if entry is None:
            self.undo_label.setText(tr('되돌릴 AI 작업이 없습니다.'));self.undo_button.setEnabled(False);return
        try:when=datetime.fromisoformat(entry.get('created','')).astimezone().strftime('%H:%M')
        except (TypeError,ValueError):when=''
        self.undo_label.setText(tr('{0} · {1}장 · {2}',tr(str(entry.get('label',''))),f"{int(entry.get('count') or 0):,}",when))
        job=self.service.job
        self.undo_button.setEnabled(job is None or job.state!='running')

    def setup_text(self,key):
        """The install text for one AI program, as copied: never the key or the port."""
        return install_text(install_commands(sys.platform=='win32',self.service.folder)[key])

    # actions
    def toggled(self,on):
        if self.updating:return
        self.service.set_enabled(on);self.refresh()

    def toggle_key(self):
        self.key_visible=not self.key_visible;self.refresh()

    def copy(self,text):
        QGuiApplication.clipboard().setText(text)

    def save_failed(self):
        """A settings write for the person's action failed: said at once, also while the connection is on (the
        status line shows the connection, not this)."""
        QMessageBox.warning(self,tr('AI 연결 (MCP)'),tr('AI 연결 설정을 저장하지 못했습니다.'))

    def regenerate(self):
        answer=QMessageBox.question(self,tr('새 키 만들기'),tr(REGEN))
        if answer!=QMessageBox.StandardButton.Yes:return
        try:self.service.regenerate_token()
        except OSError:self.save_failed()
        self.refresh()

    def change_port(self):
        self.service.change_port();self.refresh()

    def choose_export_folder(self):
        chosen=QFileDialog.getExistingDirectory(self,tr('AI 내보내기 폴더'),str(self.service.export_root()))
        if chosen:
            try:self.service.preferences.save('mcp_export_folder',chosen)
            except OSError:self.save_failed()
        self.refresh()

    def undo_last(self):
        """The person's own undo of the last AI task; works whether or not the connection is on."""
        from . import photo_service
        from .mcp_service import ready
        w=self.w;self.undo_message.hide()
        try:
            ready(w,write=True,ids=[w.current_id] if w.current_id is not None else [])
            if w.current_id is not None:w.commit();w.save_keywords()
            _,_,ids=photo_service.undo_batch(w.catalog)
        except photo_service.Busy:
            self.undo_message.setText(tr('지금은 되돌릴 수 없습니다. 열린 창이나 진행 중인 작업을 마친 뒤 다시 눌러 주세요.'));self.undo_message.show()
        except photo_service.ServiceError as error:
            if 'can no longer be undone' in str(error):
                self.undo_message.setText(tr('이후에 다시 보정한 사진이 있어 이 AI 작업은 되돌릴 수 없습니다. 사진마다 실행 취소를 사용해 주세요.'))
                self.undo_message.show()
        else:
            if ids:w.refresh_command_edits(ids,'AI 실행 취소')
            self.service.note('undo_last_batch','undone in Grainy')
        self.refresh()
