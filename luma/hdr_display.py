"""HDR presentation is a viewing choice, separate from stored photo edits."""
import numpy as np
from PySide6.QtCore import QObject,QTimer,QEvent,Qt
from PySide6.QtGui import QImage,QPainter
from PySide6.QtWidgets import QWidget,QApplication
from .i18n import tr
from . import native_hdr

class Viewport(QWidget):
    def __init__(self):
        super().__init__();self.gpu=False
    def paintEngine(self):
        return None if self.gpu else super().paintEngine()

class Surface:
    def __init__(self,view):
        self.view=view;self.renderer=None;self.pixels=None;self.uploaded=None
        self.display=native_hdr.Display();self.error='';self.frames=0
    def set_frame(self,pixels,display):
        self.pixels=pixels;self.display=display
        if pixels is None:self.close()
        self.view.viewport().update()
    def close(self):
        if self.renderer:self.renderer.close();self.renderer=None
        self.uploaded=None
        viewport=self.view.viewport()
        if isinstance(viewport,Viewport) and viewport.gpu:
            viewport.gpu=False
            viewport.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen,False)
            viewport.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground,False)
            viewport.update()
    def paint(self):
        if self.pixels is None or not self.display.active or QApplication.platformName()!='windows':return False
        view=self.view;viewport=view.viewport()
        try:
            if self.renderer is None:
                viewport.setAttribute(Qt.WidgetAttribute.WA_NativeWindow,True)
                self.renderer=native_hdr.Renderer(int(viewport.winId()))
                viewport.gpu=True;viewport.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen,True)
                viewport.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground,True)
            if self.pixels is not self.uploaded:
                self.renderer.upload(self.pixels);self.uploaded=self.pixels
            dpr=viewport.devicePixelRatioF();width=round(viewport.width()*dpr);height=round(viewport.height()*dpr)
            if width<1 or height<1:return True
            overlay=QImage(width,height,QImage.Format.Format_RGBA8888);overlay.setDevicePixelRatio(dpr);overlay.fill(Qt.GlobalColor.transparent)
            painter=QPainter(overlay);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setTransform(view.viewportTransform());view.drawForeground(painter,view.sceneRect());painter.end()
            data=np.frombuffer(overlay.constBits(),np.uint8).reshape(height,overlay.bytesPerLine())[:,:width*4].reshape(height,width,4)
            rect=view.viewportTransform().mapRect(view.image_rect)
            bounds=[rect.x()*dpr,rect.y()*dpr,rect.width()*dpr,rect.height()*dpr]
            self.renderer.draw(width,height,bounds,data,self.display.white,self.display.peak)
            self.frames+=1;return True
        except (OSError,ValueError) as error:
            self.error=str(error);self.pixels=None;self.close()
            view.hdrFailed.emit(self.error);return False

class HdrDisplay(QObject):
    def __init__(self,w):
        super().__init__(w);self.w=w;self.state=native_hdr.Display();self.failure='';self.closed=False
        self.preview=bool(w.catalog.preference('hdr_sdr_preview',False))
        w.hdr_sdr_preview.setChecked(self.preview)
        w.hdr_sdr_preview.toggled.connect(self.set_preview)
        w.view.hdrFailed.connect(self.failed)
        self.timer=QTimer(self);self.timer.setInterval(3000);self.timer.timeout.connect(self.refresh);self.timer.start()
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(150);self.debounce.timeout.connect(self.refresh)
        w.installEventFilter(self);QTimer.singleShot(0,self.refresh);self.update_ui()
    @property
    def active(self):return self.state.active and not self.failure
    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.Move,QEvent.Type.Show,QEvent.Type.WindowStateChange) and not self.closed:self.debounce.start()
        return False
    def refresh(self):
        if self.closed or not self.w.isVisible():return
        state=native_hdr.probe(int(self.w.winId())) if QApplication.platformName()=='windows' else native_hdr.Display()
        if state!=self.state:
            self.state=state;self.failure='';self.w.view.hdr_surface.set_frame(None,state)
            self.update_ui();self.w.render_version+=1;self.w.render()
    def set_preview(self,value):
        self.preview=bool(value);self.w.catalog.save_preference('hdr_sdr_preview',self.preview)
        self.update_ui();self.w.render_version+=1;self.w.render()
    def failed(self,error):
        if self.failure:return
        self.failure=error;self.update_ui()
        QTimer.singleShot(0,self.rerender)
    def rerender(self):
        if not self.closed:self.w.render_version+=1;self.w.render()
    def update_ui(self):
        w=self.w
        if self.preview:label=tr('SDR 미리보기 · 하이라이트 압축 적용')
        elif self.failure:label=tr('HDR 출력 오류 · SDR 화면으로 표시')
        elif self.active:label=tr('HDR 화면 · +{0}스톱 · 최대 {1}니트',f'{self.state.stops:.1f}',f'{self.state.peak:.0f}')
        else:label=tr('SDR 화면 · 흰색까지 표시 · HDR 계조는 유지')
        w.hdr_status.setText(label)
        w.hdr_status.setToolTip(self.failure or self.state.error or tr('HDR 미리보기에서는 SDR 압축을 적용하지 않습니다. SDR 화면의 흰색을 넘는 밝기는 화면에서 구분되지 않습니다.'))
        w.hdr_sdr_controls.setVisible(self.preview)
        w.histogram.display_stops=self.state.stops if self.active else 0.
        w.histogram.hdr_preview=self.preview;w.histogram.native_hdr=self.active and not self.preview
        w.histogram.update()
    def shutdown(self):
        self.closed=True;self.timer.stop();self.debounce.stop();self.w.view.hdr_surface.close()
