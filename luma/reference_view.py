"""Secondary photo windows: a fixed reference photo or a view that follows the current photo."""
from copy import deepcopy
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton
from .i18n import tr
from .widgets import PhotoView

PREVIEW_SIZE=1800


def render_photo(record):
    """Develop a preview in a worker thread; returns display RGB and the wide-gamut source."""
    from .engine import load_image,develop,output_rgb
    s=record['settings']
    source,info=load_image(record['path'],PREVIEW_SIZE,s['working_space'],raw_options=s)
    working=develop(source,s,output_space=None,original_size=(info['width'],info['height']))
    return output_rgb(working,s['working_space']),(working,s['working_space']) if s['working_space']=='ProPhoto' else None


class ReferenceWindow(QDialog):
    """Non-modal window. With `reference` it keeps that snapshot; otherwise it follows the main window."""

    def __init__(self,w,reference=None):
        super().__init__(w)
        self.w=w;self.reference=reference;self.follow=reference is None
        self.closed=False;self.version=0;self.shown_key=None
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1000,720)
        box=QVBoxLayout(self)
        self.label=QLabel();box.addWidget(self.label)
        self.view=PhotoView();box.addWidget(self.view,1)
        row=QHBoxLayout();box.addLayout(row)
        fit=QPushButton(tr('맞춤'));fit.clicked.connect(self.view.fit_photo);row.addWidget(fit);row.addStretch(1)
        self.target=w.display_color.watch(self)
        self.target.changed.connect(lambda:self.w.display_color.refresh_view(self.view,self.target))
        self.finished.connect(self.release)
        if self.follow:
            self.setWindowTitle(tr('두 번째 화면'))
            # Poll the committed catalog state so edits appear after the main window saves them.
            self.timer=QTimer(self);self.timer.setInterval(500);self.timer.timeout.connect(self.sync);self.timer.start()
            self.sync()
        else:
            self.timer=None
            self.setWindowTitle(tr('참조 사진')+' · '+reference['name'])
            self.show_record(reference)

    def current_record(self):
        ident=self.w.current_id
        return None if ident is None else self.w.catalog.photo(ident)

    def sync(self):
        if self.closed:return
        record=self.current_record()
        if record is None:
            self.label.setText(tr('표시할 사진이 없습니다.'));return
        from .workflow_data import edit_signature
        key=(record['id'],record['path'],edit_signature(record['settings']))
        if key!=self.shown_key:self.show_record(record,key)

    def show_record(self,record,key=None):
        self.shown_key=key;self.version+=1;version=self.version
        self.label.setText(record['name']+'  ·  '+tr('불러오는 중…'))
        record=deepcopy(record)
        def loaded(result):
            if self.closed or version!=self.version:return
            image,working=result;self.view.display_source=working
            self.view.set_image(image);self.w.display_color.refresh_view(self.view,self.target)
            self.label.setText(record['name'])
        def failed(error):
            if not self.closed and version==self.version:self.label.setText(record['name']+'  ·  '+tr('원본을 읽을 수 없습니다.'))
        self.w.spawn(lambda:render_photo(record),loaded,failed)

    def release(self,*_):
        if self.closed:return
        self.closed=True
        if self.timer:self.timer.stop()
        self.w.display_color.release(self.target)
        windows=getattr(getattr(self.w,'workflow',None),'windows',[])
        if self in windows:windows.remove(self)

    def closeEvent(self,event):
        self.release();super().closeEvent(event)
