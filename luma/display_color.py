"""Per-window monitor state, asynchronous refresh and a small settings dialog."""
from .i18n import tr
from pathlib import Path
from PySide6.QtCore import QObject,QTimer,QEvent,Signal,Qt
from PySide6.QtWidgets import (QApplication,QDialog,QVBoxLayout,QHBoxLayout,QLabel,
    QPushButton,QComboBox,QLineEdit,QFileDialog,QDialogButtonBox)
from . import display_profiles,colorio


class DisplayTarget(QObject):
    changed=Signal()

    def __init__(self,manager,window):
        super().__init__(window);self.manager=manager;self.window=window
        self.state=display_profiles.DisplayProfile();self.revision=0;self.generation=0
        self.running=False;self.pending=False;self.closed=False;self.handle=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.refresh)
        self.poll=QTimer(self);self.poll.setInterval(15000);self.poll.timeout.connect(self.refresh);self.poll.start()
        window.installEventFilter(self);self.schedule()

    @property
    def profile(self):return self.state.data

    def screen_name(self):
        if QApplication.platformName()=='offscreen':return ''
        handle=self.window.windowHandle()
        screen=handle.screen() if handle else self.window.screen()
        if screen and display_profiles.MAC:
            # Qt gives no display number on macOS; the profile is asked for by it (display_profiles.mac_profile_data).
            g=screen.geometry();ident=display_profiles.mac_display_id(g.x(),g.y(),g.width(),g.height())
            return f'{screen.name()} ({ident})' if ident is not None else screen.name()
        return screen.name() if screen else ''

    def bind(self):
        handle=self.window.windowHandle()
        if handle is not None and handle is not self.handle:
            self.handle=handle;handle.screenChanged.connect(self.schedule)

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.Show:self.bind();self.schedule()
        elif event.type()==QEvent.Type.Hide:
            self.generation+=1;self.timer.stop()
        return False

    def schedule(self,*args):
        if not self.closed:
            self.generation+=1;self.timer.start(50)

    def refresh(self):
        if self.closed or not self.window.isVisible():return
        self.bind()
        if self.running:self.pending=True;return
        self.running=True;generation=self.generation
        mode,path=self.manager.mode,self.manager.path;screen=self.screen_name()
        def ready(state):
            self.running=False
            if self.closed:return
            if generation==self.generation and screen==self.screen_name() and self.window.isVisible():
                changed=state.digest!=self.state.digest
                different=state!=self.state;self.state=state
                if changed:self.revision+=1
                if different:self.changed.emit()
            if self.pending or generation!=self.generation:
                self.pending=False;self.schedule()
        self.manager.w.spawn(lambda:display_profiles.resolve(mode,screen,path),ready,
            lambda error:ready(display_profiles.DisplayProfile(mode=mode,screen=screen,error=True,message=error)),
            self.manager.w.display_pool)

    def shutdown(self):
        self.closed=True;self.generation+=1;self.timer.stop();self.poll.stop()


class DisplayColor(QObject):
    configurationChanged=Signal()

    def __init__(self,w):
        super().__init__(w);self.w=w;self.targets=[];self.dialog=None
        saved=w.catalog.preference('display_color')
        if not saved:
            # Existing manual choices (including explicit off) survive upgrading.
            legacy=w.catalog.preference('monitor_icc','__unset__')
            saved=dict(mode='auto' if legacy=='__unset__' else 'manual' if legacy else 'off',path=legacy if legacy and legacy!='__unset__' else '')
        self.mode=saved.get('mode','auto');self.path=saved.get('path','')
        if self.mode not in ('auto','manual','off'):self.mode='auto'
        self.main=self.watch(w);self.main.changed.connect(self.main_changed)
        self.button=QPushButton(tr('색상 · 자동') if self.mode=='auto' else tr('색상 · 수동') if self.mode=='manual' else tr('색상 · 해제'))
        self.button.clicked.connect(self.show_dialog);w.statusBar().addPermanentWidget(self.button)
        self.last_revision=-1

    def watch(self,window):
        target=DisplayTarget(self,window);self.targets.append(target);return target

    def release(self,target):
        target.shutdown()
        if target in self.targets:self.targets.remove(target)

    def configure(self,mode,path=''):
        if mode not in ('auto','manual','off'):raise ValueError('지원하지 않는 화면 색상 모드입니다.')
        self.mode,self.path=mode,str(path)
        self.w.catalog.save_preference('display_color',dict(mode=mode,path=self.path))
        self.w.catalog.save_preference('monitor_icc',self.path if mode=='manual' else None)
        self.configurationChanged.emit()
        for target in self.targets:target.schedule()

    def main_changed(self):
        target=self.main;state=target.state
        label='확인 필요' if state.error else {'auto':'자동','manual':'수동','off':'해제'}[state.mode]
        self.button.setText(tr('색상 · ')+tr(label))
        self.button.setToolTip('\n'.join(x for x in (state.screen,state.description,state.path,state.message) if x))
        if target.revision!=self.last_revision:
            self.last_revision=target.revision
            self.w.photo_model.invalidate_thumbnails()
            self.w.render_version+=1;self.w.render()

    def refresh_view(self,view,target):
        image=view.on_screen
        if image is None or target.closed:return
        revision=target.revision;profile=target.profile
        working=view.display_source
        from .widgets import qimage
        def work():
            if target.closed or revision!=target.revision:return None
            if working is not None:
                from .engine import output_rgb
                pixels=output_rgb(working[0],working[1],profile or 'sRGB')
            else:pixels=colorio.display_rgb(image,profile)
            return qimage(pixels,display=False)
        def ready(prepared):
            if prepared is not None and not target.closed and target.window.isVisible() and revision==target.revision and view.on_screen is image:
                view.set_image(image,logical_size=(view.image_rect.width(),view.image_rect.height()),prepared=prepared)
        self.w.spawn(work,ready,lambda _:None,self.w.display_pool)

    def show_dialog(self):
        if self.dialog is None:self.dialog=DisplayDialog(self)
        self.dialog.show();self.dialog.raise_();self.main.schedule()

    def shutdown(self):
        for target in self.targets:target.shutdown()


class DisplayDialog(QDialog):
    def __init__(self,manager):
        super().__init__(manager.w);self.manager=manager;self.setWindowTitle(tr('화면 색상 관리 — Grainy'));self.resize(580,420)
        layout=QVBoxLayout(self);layout.setContentsMargins(24,24,24,24);layout.setSpacing(14)
        heading=QLabel(tr('화면 색상 관리'));heading.setStyleSheet('font-size:22px;font-weight:600;color:#e7c9a2');layout.addWidget(heading)
        self.mode=QComboBox()
        for text,key in [('자동 · macOS 모니터 설정 사용' if display_profiles.MAC else '자동 · Windows 모니터 설정 사용','auto'),('직접 선택 · ICC 프로파일','manual'),('앱의 화면 색상 보정 해제','off')]:self.mode.addItem(tr(text),key)
        self.mode.setCurrentIndex(self.mode.findData(manager.mode));layout.addWidget(self.mode)
        row=QHBoxLayout();self.path=QLineEdit(manager.path);self.path.setPlaceholderText(tr('ICC 프로파일 경로'));row.addWidget(self.path)
        self.choose=QPushButton(tr('파일 선택'));self.choose.clicked.connect(self.browse);row.addWidget(self.choose);layout.addLayout(row)
        self.status=QLabel();self.status.setTextFormat(Qt.TextFormat.PlainText);self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse);layout.addWidget(self.status,1)
        note=QLabel(tr('편집 화면·사진 목록·비교창의 표시 색에 적용합니다. 보정값과 내보내는 파일은 바뀌지 않습니다.\n자동 모드에서는 각 창이 있는 모니터를 따릅니다. Windows가 별도 프로파일을 제공하지 않으면 sRGB를 사용합니다.').replace('Windows',display_profiles.SYSTEM))
        note.setWordWrap(True);note.setObjectName('muted');layout.addWidget(note)
        row=QHBoxLayout();refresh=QPushButton(tr('다시 확인'));refresh.clicked.connect(self.refresh);row.addWidget(refresh);row.addStretch()
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Apply|QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText(tr('적용'));buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr('닫기'))
        buttons.rejected.connect(self.reject);row.addWidget(buttons);layout.addLayout(row)
        self.mode.currentIndexChanged.connect(self.fields);manager.main.changed.connect(self.update_status)
        manager.configurationChanged.connect(self.sync);self.fields();self.update_status()

    def fields(self):
        manual=self.mode.currentData()=='manual';self.path.setEnabled(manual);self.choose.setEnabled(manual)

    def sync(self):
        self.mode.setCurrentIndex(self.mode.findData(self.manager.mode));self.path.setText(self.manager.path)

    def browse(self):
        path,_=QFileDialog.getOpenFileName(self,tr('화면 ICC 프로파일 선택'),'','ICC (*.icc *.icm)')
        if path:self.path.setText(path)

    def apply(self):
        if self.mode.currentData()=='manual' and not self.path.text().strip():
            self.status.setText(tr('먼저 화면용 RGB ICC 파일을 선택하세요.'));return
        self.manager.configure(self.mode.currentData(),self.path.text().strip());self.status.setText(tr('화면 프로파일을 확인하고 있습니다…'))

    def refresh(self):
        display_profiles._read_profile.cache_clear()
        for target in self.manager.targets:target.schedule()

    def update_status(self):
        s=self.manager.main.state
        self.status.setText('\n'.join(x for x in (f'모니터: {s.screen or "기본 화면"}',f'적용 프로파일: {s.description}',s.path,s.message) if x))
