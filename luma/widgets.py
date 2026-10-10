from __future__ import annotations
from .i18n import tr
import sys
import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QSize, QEvent, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QImage, QCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QApplication,
                              QGraphicsView, QGraphicsScene, QGraphicsRectItem, QPushButton, QToolButton,
                              QDoubleSpinBox, QAbstractSpinBox)

MAC = sys.platform == 'darwin'


def qimage(rgb,display=True):
    if display:
        from .colorio import display_rgb
        rgb=display_rgb(rgb)
    rgb = np.asarray(rgb)
    if rgb.ndim == 3:
        # Row tiles on the pixel workers: the same per-pixel math, ~6x faster for a 12 MP view.
        from .pixel_jobs import rows
        def tile(first, stop):
            part = np.clip(rgb[first:stop], 0, 1);part *= 255;part += .5   # np.clip(rgb,0,1)*255+.5, in place
            return part.astype(np.uint8)
        a = rows(rgb.shape[0], rgb.shape[1], tile, channels=rgb.shape[2], dtype=np.uint8)
    else:
        a = np.ascontiguousarray(np.uint8(np.clip(rgb, 0, 1)*255+.5))
    h, w = a.shape[:2]
    return QImage(a.data, w, h, a.strides[0], QImage.Format.Format_RGB888).copy()


class AdjustmentNumber(QDoubleSpinBox):
    def textFromValue(self,value):
        return f'{value:+.{self.decimals()}f}'

    def mousePressEvent(self,event):
        super().mousePressEvent(event)
        if event.button()==Qt.MouseButton.LeftButton:self.selectAll()

    def focusInEvent(self,event):
        super().focusInEvent(event)
        QTimer.singleShot(0,self.selectAll)

    def wheelEvent(self,event):
        if self.hasFocus():super().wheelEvent(event)
        else:event.ignore()


class Adjustment(QWidget):
    changed = Signal(str, float)
    committed = Signal()
    autoRequested = Signal(str)     # Shift+double-click: per-slider auto

    def __init__(self, key, label, low=-100, high=100, scale=1):
        super().__init__()
        self.key, self.scale = key, scale
        self.default_value=0;self.supports_auto=False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 5)
        row = QHBoxLayout()
        self.label = QLabel(tr(label))
        self.value = AdjustmentNumber()
        self.value.setObjectName('adjustmentValue')
        self.value.setDecimals(max(0,int(np.ceil(np.log10(scale)))))
        self.value.setRange(low,high);self.value.setSingleStep(1/scale)
        self.value.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.value.setKeyboardTracking(False);self.value.setFixedWidth(76)
        self.value.setAccessibleName(tr('{0} 값',tr(label)))
        self.value.setToolTip(tr('숫자를 입력한 뒤 Enter 또는 다른 곳을 클릭해 적용합니다.'))
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self.label)
        row.addWidget(self.value)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(low*scale), int(high*scale))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, int(scale)))
        self.slider.setToolTip(tr('더블클릭하면 기본값으로 돌아갑니다.'))
        self.slider.mouseDoubleClickEvent = self.double_clicked
        self.slider.valueChanged.connect(self.on_change)
        self.slider.sliderReleased.connect(self.committed)
        self.value.valueChanged.connect(self.number_changed)
        self.value.editingFinished.connect(self.committed)
        layout.addLayout(row)
        layout.addWidget(self.slider)

    def reset(self):
        self.slider.setValue(round(self.default_value*self.scale))
        self.committed.emit()

    def double_clicked(self,event):
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self.supports_auto:
            self.autoRequested.emit(self.key)
        else:self.reset()

    def compact(self):
        if self.property('compact'):return
        self.setProperty('compact',True)
        layout=self.layout();layout.removeWidget(self.slider)
        row=layout.itemAt(0).layout();row.insertWidget(1,self.slider,1)
        layout.setContentsMargins(0,1,0,1);row.setSpacing(5)
        self.label.setFixedWidth(88);self.label.setWordWrap(True)
        self.label.setToolTip(self.label.text());self.value.setFixedWidth(55)
        self.slider.setMinimumWidth(45)

    def on_change(self, value):
        actual = value/self.scale
        self.value.blockSignals(True);self.value.setValue(actual);self.value.blockSignals(False)
        self.changed.emit(self.key, actual)

    def number_changed(self,value):
        self.slider.setValue(round(value*self.scale))
        self.value.blockSignals(True);self.value.setValue(self.slider.value()/self.scale);self.value.blockSignals(False)

    def set_value(self, value):
        self.slider.blockSignals(True)
        self.slider.setValue(round(value*self.scale))
        self.value.blockSignals(True);self.value.setValue(self.slider.value()/self.scale);self.value.blockSignals(False)
        self.slider.blockSignals(False)


def histogram_bins(image):
    """128 bins per channel of the 8-bit values, from about 300 rows; row tiles on the pixel workers
    (one bincount per tile over channel-offset indices), the same counts as a single pass."""
    step = max(1, image.shape[0]//300)
    sub = image[::step, ::step]
    if sub.ndim != 3 or sub.shape[2] != 3:
        data = np.uint8(np.clip(sub,0,1)*255+.5) >> 1
        return [np.bincount(data[...,c].ravel(), minlength=128) for c in range(3)]
    from threading import Lock
    from .pixel_jobs import each
    parts, lock = [], Lock()
    offsets = np.array([0, 128, 256], np.uint16)
    def tile(first, stop):
        data = (np.uint8(np.clip(sub[first:stop],0,1)*255+.5) >> 1).astype(np.uint16)+offsets
        counts = np.bincount(data.ravel(), minlength=384)
        with lock:parts.append(counts)
    each(sub.shape[0], sub.shape[1], tile)
    total = np.sum(parts, axis=0)
    return [total[:128], total[128:256], total[256:]]


def clipping_channels(image,ceiling=1.):
    # Use every preview pixel, not the histogram's subsample or its two-value
    # bins. Thresholds match rounding to 0/255 in the displayed SDR RGB image.
    # Per-channel extremes on the pixel workers; fmin/fmax skip NaN like the any() comparisons do.
    from .pixel_jobs import each
    image=np.asarray(image);channels=image.shape[-1]
    lows=[];highs=[]
    def tile(first,stop):
        # Reduce whole rows first (long contiguous inner loops), then the pixels of the row vector.
        part=image[first:stop].reshape(stop-first,-1)
        if part.size:
            lows.append(np.fmin.reduce(np.fmin.reduce(part,axis=0).reshape(-1,channels),axis=0))
            highs.append(np.fmax.reduce(np.fmax.reduce(part,axis=0).reshape(-1,channels),axis=0))
    if image.ndim==3:each(image.shape[0],image.shape[1],tile)
    else:
        flat=image.reshape(-1,channels)
        if len(flat):lows.append(np.fmin.reduce(flat,axis=0));highs.append(np.fmax.reduce(flat,axis=0))
    if not lows:return (False,)*3,(False,)*3
    low=np.fmin.reduce(np.stack(lows),axis=0);high=np.fmax.reduce(np.stack(highs),axis=0)
    shadows=tuple(bool(low[c]<1/510) for c in range(3))
    highlights=tuple(bool(high[c]>=ceiling-1/510) for c in range(3))
    return shadows,highlights


class Histogram(QWidget):
    clippingChanged=Signal(bool,bool)

    def __init__(self):
        super().__init__()
        self.setFixedHeight(104)
        self.bins = None
        self.hdr=False;self.hdr_limit=4.
        self.display_stops=0.;self.hdr_preview=False;self.native_hdr=False
        self.hdr_inside=False;self.hdr_outside=False
        self.channels=((False,False,False),(False,False,False))
        self.hovered=[False,False]
        self.shadow_button=QToolButton(self);self.highlight_button=QToolButton(self)
        for index,button in enumerate((self.shadow_button,self.highlight_button)):
            button.setText('▲');button.setCheckable(True);button.setFixedSize(22,19)
            button.setAccessibleName(tr('검정 클리핑') if index==0 else tr('흰색 클리핑'))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.toggled.connect(self.emit_clipping);button.installEventFilter(self)
        self.refresh_indicators()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.Enter,QEvent.Type.Leave):
            index=0 if watched is self.shadow_button else 1
            self.hovered[index]=event.type()==QEvent.Type.Enter and watched.isEnabled()
            self.emit_clipping()
        return super().eventFilter(watched,event)

    def emit_clipping(self,*_):
        self.clippingChanged.emit(self.shadow_button.isChecked() or self.hovered[0],self.highlight_button.isChecked() or self.hovered[1])

    def set_overlay(self,shadows,highlights):
        for button,value in zip((self.shadow_button,self.highlight_button),(shadows,highlights)):
            button.blockSignals(True);button.setChecked(value);button.blockSignals(False)
        self.emit_clipping()

    def refresh_indicators(self):
        for index,button in enumerate((self.shadow_button,self.highlight_button)):
            channels=self.channels[index]
            color=QColor(*(255 if present else 0 for present in channels)).name() if any(channels) else '#59636f'
            if index==1 and self.hdr:color='#e9695a' if self.hdr_outside else '#f3d15d' if self.hdr_inside else '#59636f'
            style=f'QToolButton {{ background:#171b20; color:{color}; border:1px solid #414954; border-radius:0px; padding:0px; font-size:13px; }} QToolButton:checked {{ border-color:#f4e7d4; }}'
            if button.styleSheet()!=style:button.setStyleSheet(style)
            title=tr('검정 클리핑') if index==0 else tr('흰색 클리핑')
            if index==1 and self.hdr:title=tr('HDR 화면 범위 · 노랑: 표시 가능 · 빨강: 화면 한계 초과')
            state=' / '.join(c for c,present in zip('RGB',channels) if present) if any(channels) else tr('없음')
            button.setToolTip(tr('{0}: {1}\n마우스를 올리면 미리보기 · 클릭하면 표시 고정',title,state))
            button.setEnabled(self.bins is not None)

    def resizeEvent(self,event):
        self.shadow_button.move(3,3);self.highlight_button.move(self.width()-25,3)
        super().resizeEvent(event)

    def clear(self):
        self.hdr=False
        self.hdr_inside=False;self.hdr_outside=False
        self.bins=None;self.channels=((False,False,False),(False,False,False));self.hovered=[False,False]
        self.refresh_indicators();self.update()

    def set_image(self, image):
        # Bin the same rounded 8-bit values that are displayed/exported. A value
        # just below float 1.0 still displays as white and belongs in the last bin.
        if self.hdr:
            from .hdr import histogram
            from .engine import to_srgb
            self.set_hdr_ranges(image)
            self.set_bins(histogram(image),clipping_channels(image,float(to_srgb(np.float32(2**self.hdr_limit)))))
        else:self.set_bins(histogram_bins(image),clipping_channels(image))

    def set_hdr_ranges(self,image):
        from .engine import to_srgb
        peak=image.max(axis=-1);ceiling=float(to_srgb(np.float32(2**self.display_stops)))
        self.hdr_inside=bool(np.any((peak>1+1e-5)&(peak<=ceiling+1e-5)))
        self.hdr_outside=bool(np.any(peak>ceiling+1e-5))

    def set_bins(self,bins,channels=None):
        self.bins = bins
        self.channels=channels or ((False,False,False),(False,False,False))
        self.refresh_indicators()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#171b20'))
        if self.bins is None:
            return
        high = max(float(np.percentile(b, 98)) for b in self.bins) or 1
        for bins, color in zip(self.bins, ['#da866b','#88bca1','#7baad1']):
            path = QPainterPath(QPointF(0, self.height()))
            for i,v in enumerate(bins):
                path.lineTo(i/127*self.width(), self.height()-min(1,float(v)/high)*(self.height()-25))
            path.lineTo(self.width(), self.height())
            c = QColor(color)
            c.setAlpha(85)
            p.fillPath(path, c)
            p.setPen(QPen(QColor(color), .6))
            p.drawPath(path)
        if self.hdr:
            p.setPen(QColor('#c4c9d0'))
            p.drawText(30,17,'SDR')
            p.drawText(int(self.width()*.5)+4,17,'HDR' if not self.hdr_preview else 'HDR / SDR')
            for stop in range(5):
                x=int(self.width()*(.5+stop/8))
                p.setPen(QPen(QColor('#c4c9d0'),1,Qt.PenStyle.SolidLine if stop==0 else Qt.PenStyle.DashLine))
                p.drawLine(x,25,x,self.height()-13)
                p.drawText(x-13,self.height()-1,'W' if stop==0 else f'+{stop}')
            edge=int(self.width()*(.5+min(4,self.display_stops)/8))
            p.fillRect(int(self.width()*.5),self.height()-14,edge-int(self.width()*.5),3,QColor('#ddd4b6'))
            p.fillRect(edge,self.height()-14,self.width()-edge,3,QColor('#e9695a'))


class ToneCurve(QWidget):
    changed = Signal(list)
    committed = Signal()

    def __init__(self):
        super().__init__()
        self.points = [[0.,0.],[.25,.25],[.5,.5],[.75,.75],[1.,1.]]
        self.active = None
        self.hdr=False
        self.setFixedHeight(165)
        self.setToolTip(tr('점을 위아래로 드래그해 톤을 조정합니다. 더블클릭: 초기화'))

    def set_points(self, points):
        self.points = [list(p) for p in points]
        self.update()

    def point_xy(self, pt):
        return QPointF(12+self.axis(pt[0])*(self.width()-24), 12+(1-self.axis(pt[1]))*(self.height()-24))

    def set_hdr(self,enabled):
        self.hdr=bool(enabled);self.update()

    def axis(self,value):
        if not self.hdr:return value
        from math import log2
        return value*.5 if value<=1 else .5+log2(((value+.055)/1.055)**2.4)/8

    def axis_value(self,value):
        if not self.hdr:return value
        return value*2 if value<=.5 else 1.055*(2**((value-.5)*8))**(1/2.4)-.055

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#171b20'))
        p.setPen(QPen(QColor('#31373e'), 1))
        maximum=self.axis_value(1.)
        for t in [0,.25,.5,.75,1]:
            value=self.axis_value(t)
            p.drawLine(self.point_xy([value,0]), self.point_xy([value,maximum]))
            p.drawLine(self.point_xy([0,value]), self.point_xy([maximum,value]))
        p.setPen(QPen(QColor('#4e5760'), 1, Qt.PenStyle.DashLine))
        p.drawLine(self.point_xy([0,0]), self.point_xy([maximum,maximum]))
        if self.hdr:
            p.setPen(QPen(QColor('#81858c'),1))
            p.drawLine(self.point_xy([1,0]),self.point_xy([1,maximum]));p.drawLine(self.point_xy([0,1]),self.point_xy([maximum,1]))
            p.drawText(15,23,'SDR / HDR +4 EV')
        p.setPen(QPen(QColor('#dfc29b'), 2))
        from .hdr import curve_extended
        xs=np.array([self.axis_value(v) for v in np.linspace(0,1,100)],np.float32)
        ys=curve_extended(xs,self.points)
        p.save();p.setClipRect(QRectF(12,12,self.width()-24,self.height()-24))
        for i in range(len(xs)-1):p.drawLine(self.point_xy([xs[i],ys[i]]),self.point_xy([xs[i+1],ys[i+1]]))
        p.restore()
        p.setBrush(QColor('#dfc29b'))
        for pt in self.points:
            if 0<=pt[0]<=maximum and 0<=pt[1]<=maximum:p.drawEllipse(self.point_xy(pt), 4, 4)

    def mousePressEvent(self, event):
        if event.button()==Qt.MouseButton.RightButton:
            distances=[(self.point_xy(pt)-event.position()).manhattanLength() for pt in self.points]
            index=int(np.argmin(distances))
            removable=0<index<len(self.points)-1 or self.hdr and index>0 and self.points[index][0]>1
            if removable and distances[index]<24:
                self.points.pop(index);self.changed.emit(self.points);self.committed.emit();self.update()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        distances = [(self.point_xy(pt)-event.position()).manhattanLength() for pt in self.points]
        index = int(np.argmin(distances))
        if distances[index] < 24:
            self.active = index
        elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            x=self.axis_value(float(np.clip((event.position().x()-12)/(self.width()-24),.001,.999)))
            y=self.axis_value(float(np.clip(1-(event.position().y()-12)/(self.height()-24),0,1)))
            self.points.append([x,y]);self.points.sort();self.active=self.points.index([x,y])
            self.changed.emit(self.points);self.update()

    def mouseMoveEvent(self, event):
        if self.active is not None:
            if 0<self.active<len(self.points)-1 or self.hdr and self.active>0 and self.points[self.active][0]>1:
                upper=self.points[self.active+1][0]-.001 if self.active<len(self.points)-1 else self.axis_value(1.)
                self.points[self.active][0]=float(np.clip(self.axis_value((event.position().x()-12)/(self.width()-24)),
                    self.points[self.active-1][0]+.001,upper))
            self.points[self.active][1] = self.axis_value(float(np.clip(1-(event.position().y()-12)/(self.height()-24), 0, 1)))
            self.changed.emit([list(p) for p in self.points])
            self.update()

    def mouseReleaseEvent(self, event):
        if self.active is not None:
            self.active = None
            self.committed.emit()

    def mouseDoubleClickEvent(self, event):
        self.set_points([[0.,0.],[.25,.25],[.5,.5],[.75,.75],[1.,1.]])
        self.changed.emit(self.points)
        self.committed.emit()


class PhotoView(QGraphicsView):
    fullResolutionRequested=Signal()
    strokeCompleted=Signal(list,bool)
    strokeProgress=Signal(list,bool)
    strokeCancelled=Signal()
    healingApplyRequested=Signal()
    healingClearRequested=Signal()
    brushSizeChanged=Signal(float)
    componentEdited=Signal(list)
    cropSelected = Signal(list)
    cropChanged = Signal(list)
    cropCancelled = Signal()
    straightenDragged = Signal(float)   # crop tool: dragging outside the frame turns the photo (degrees)
    straightenFinished = Signal()
    sampled = Signal(float, float)
    sampleCancelled = Signal()
    # Targeted adjustment ('adjust' tool): press position, then vertical drag in pixels (up = positive).
    adjustStarted = Signal(float, float)
    adjustDragged = Signal(float)
    adjustFinished = Signal()
    toolModeChanged = Signal(str)
    zoomChanged = Signal(str)
    hdrFailed = Signal(str)

    def __init__(self):
        super().__init__()
        from .hdr_display import Viewport,Surface
        self.setViewport(Viewport());self.hdr_surface=Surface(self)
        self.setScene(QGraphicsScene(self))
        self.photo_item = self.scene().addPixmap(QPixmap())
        self.photo_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setBackgroundBrush(QColor('#101215'))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.fit_mode, self.crop_mode, self._sample_mode = True, False, False
        self.crop_limit = (0., 1., 1.)    # straighten angle and canvas size: the crop stays on the turned photo
        self.canvas = None                # crop tool: the part of the shown frame that crops are measured in (x, y, w, h)
        self.straighten_angle = 0.        # the photo's current angle, the start of a turn by dragging
        self.image_rect = QRectF()
        self.crop_rect = QRectF(0, 0, 1, 1)
        self.drag_handle = None
        self.drag_start = None
        self.drag_rect = None
        self.crop_aspect_pending = False
        self.setMouseTracking(True)
        self.aspect = 0.
        self.has_photo = False
        self.loading_photo=False;self.zoom_press=None
        self.on_screen = None
        self.hdr_pixels=None;self.clipping_ceiling=1.
        self.hdr_display_ceiling=1.
        self.tool_mode=''
        self.tool_points=[]
        self.brush_radius=.02
        self.brush_feather=70
        self.brush_position=None
        self.tool_handles=[]
        self.handle_drag=None
        self.handle_kind='linear'
        self.tool_overlay=None
        self.healing_overlay=None
        self.clipping=False
        self.shadow_clipping=True;self.highlight_clipping=True
        self.clipping_image=None
        self.display_profile=None
        self.display_source=None

    @property
    def sample_mode(self):
        return self._sample_mode

    @sample_mode.setter
    def sample_mode(self,enabled):
        self._sample_mode=bool(enabled)
        self.sync_cursor()

    def sync_cursor(self):
        active=self.sample_mode or self.crop_mode or self.tool_mode
        self.setDragMode(QGraphicsView.DragMode.NoDrag if active else QGraphicsView.DragMode.ScrollHandDrag)
        if self.sample_mode:
            if not hasattr(self,'eyedropper_cursor'):
                pixmap=QPixmap(32,32);pixmap.fill(Qt.GlobalColor.transparent)
                painter=QPainter(pixmap);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                tube=QPainterPath(QPointF(4,28))
                for x,y in ((7,21),(18,10),(23,15),(12,26),(4,28)):tube.lineTo(x,y)
                painter.setPen(QPen(QColor('black'),3));painter.setBrush(QColor('white'));painter.drawPath(tube)
                painter.setPen(QPen(QColor('white'),1));painter.drawPath(tube)
                cap=QPainterPath(QPointF(15,9))
                for x,y in ((20,4),(24,4),(28,8),(28,12),(23,17),(15,9)):cap.lineTo(x,y)
                painter.setPen(QPen(QColor('white'),1.5));painter.setBrush(QColor('#20242a'));painter.drawPath(cap)
                painter.end();self.eyedropper_cursor=QCursor(pixmap,4,28)
            self.viewport().setCursor(self.eyedropper_cursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.SizeVerCursor if self.tool_mode=='adjust' else
                                      Qt.CursorShape.CrossCursor if self.tool_mode or self.crop_mode else Qt.CursorShape.OpenHandCursor)

    def set_image(self, rgb, logical_size=None, prepared=None):
        self.hdr_pixels=None;self.clipping_ceiling=1.
        self.on_screen = rgb
        self.clipping_image=None
        old_rect = QRectF(self.image_rect)
        logical_w, logical_h = logical_size or (rgb.shape[1], rgb.shape[0])
        self.image_rect = QRectF(0, 0, logical_w, logical_h)
        from .colorio import display_rgb
        self.photo_item.setPixmap(QPixmap.fromImage(prepared if prepared is not None else qimage(display_rgb(rgb,self.display_profile),display=False)))
        # Keep the scene and zoom stable when a quick preview becomes a refined one.
        from PySide6.QtGui import QTransform
        self.photo_item.setTransform(QTransform.fromScale(logical_w/rgb.shape[1], logical_h/rgb.shape[0]))
        self.setSceneRect(self.image_rect)
        self.has_photo = True
        self.loading_photo=False
        if self.fit_mode and (old_rect != self.image_rect or self.transform().isIdentity()):
            self.fit_photo()
        if self.crop_mode and self.crop_aspect_pending:
            self.crop_aspect_pending=False
            self.set_aspect(self.aspect)
        self.viewport().update()

    def paintEvent(self,event):
        if self.has_photo and self.hdr_surface.paint():return
        super().paintEvent(event)

    def set_tool(self,mode):
        self.cancel_stroke()
        self.tool_mode=mode;self.tool_points=[];self.tool_overlay=None;self.healing_overlay=None
        self.tool_handles=[];self.handle_drag=None;self.brush_position=None;self.adjust_origin=None
        self.sync_cursor()
        self.viewport().update()
        self.toolModeChanged.emit(mode)

    def cancel_stroke(self):
        if self.tool_points:
            self.tool_points=[];self.strokeCancelled.emit();self.viewport().update()

    def clear_photo(self,loading=False):
        self.hdr_surface.set_frame(None,self.hdr_surface.display)
        self.photo_item.setPixmap(QPixmap())
        self.has_photo = False
        self.loading_photo=loading;self.zoom_press=None
        self.on_screen = None
        self.display_source=None
        self.healing_overlay=None
        self.image_rect = QRectF()
        self.drag_handle = None
        self.viewport().update()

    def fit_photo(self):
        self.fit_mode = True
        if self.has_photo:
            self.fitInView(self.image_rect.adjusted(-18,-18,18,18), Qt.AspectRatioMode.KeepAspectRatio)
        self.zoomChanged.emit(tr('화면 맞춤'))

    def actual_size(self):
        self.fit_mode = False
        self.resetTransform()
        self.zoomChanged.emit(tr('원본 100%'))
        self.fullResolutionRequested.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_mode:
            self.fit_photo()

    def zoom_by(self, factor):
        self.fit_mode = False
        zoom = self.transform().m11()*factor
        if .025 < zoom < 16:
            self.scale(factor,factor)
            self.zoomChanged.emit(tr('원본 {0}%',round(zoom*100)))
            if zoom>=.8:self.fullResolutionRequested.emit()

    def wheelEvent(self, event):
        if not self.has_photo:
            return
        if MAC and (not event.pixelDelta().isNull() or event.phase()!=Qt.ScrollPhase.NoScrollPhase):
            # A trackpad or Magic Mouse scrolls in pixels, many events per swipe: it moves the photo, as
            # everywhere on macOS, and pinching zooms (viewportEvent). A wheel mouse zooms as on Windows.
            delta = event.pixelDelta() if not event.pixelDelta().isNull() else event.angleDelta()/8
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-delta.y())
            event.accept(); return
        self.zoom_by(1.18 if event.angleDelta().y()>0 else 1/1.18)
        event.accept()

    def viewportEvent(self, event):
        if MAC and event.type()==QEvent.Type.NativeGesture and self.has_photo:
            if event.gestureType()==Qt.NativeGestureType.ZoomNativeGesture:      # pinch: value is the change, e.g. 0.02
                self.zoom_by(max(.5,min(2.,1+event.value()))); return True
            if event.gestureType()==Qt.NativeGestureType.SmartZoomNativeGesture:  # two-finger double tap
                self.actual_size() if self.fit_mode else self.fit_photo(); return True
        return super().viewportEvent(event)

    def set_crop_mode(self, enabled, crop=None):
        if enabled:self.set_tool('')
        self.crop_mode = enabled
        self.drag_handle = None
        if enabled:
            x0,y0,x1,y1 = crop or [0.,0.,1.,1.]
            self.crop_rect = QRectF(x0,y0,x1-x0,y1-y0)
            self.crop_aspect_pending=True
            self.setFocus()
        self.sync_cursor()
        self.viewport().update()

    def crop_values(self):
        r = self.crop_rect
        return [r.left(),r.top(),r.right(),r.bottom()]

    def canvas_rect(self):
        """The photo's own canvas inside the shown frame; the frame is larger while a straightened photo is cropped."""
        return QRectF(*self.canvas) if self.canvas else self.image_rect

    def set_crop_limit(self,angle,width,height,current=None):
        self.crop_limit=(float(angle),float(width),float(height))
        self.straighten_angle=float(angle if current is None else current)

    @staticmethod
    def pointer_angle(center,position):
        """Direction of the pointer from the frame's centre in degrees; grows clockwise on screen."""
        return float(np.degrees(np.arctan2(position.y()-center.y(),position.x()-center.x())))

    def dragged_angle(self,position):
        """The photo's angle after dragging outside the frame to this position."""
        turn=(self.pointer_angle(self.rotate_center,position)-self.rotate_from+180)%360-180
        return round(float(np.clip(self.rotate_start+turn,-45,45)),1)

    def rotate_cursor(self):
        if getattr(self,'_rotate_cursor',None) is None:
            from PySide6.QtGui import QCursor
            pixmap=QPixmap(28,28);pixmap.fill(Qt.GlobalColor.transparent)
            painter=QPainter(pixmap);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for color,width in ((QColor(20,23,27),4.5),(QColor('#f7dfbc'),2)):
                painter.setPen(QPen(color,width,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
                painter.drawArc(QRectF(5,5,18,18),35*16,250*16)
                painter.drawLine(QPointF(21.5,8.5),QPointF(21.5,3));painter.drawLine(QPointF(21.5,8.5),QPointF(16,8.5))
            painter.end();self._rotate_cursor=QCursor(pixmap,14,14)
        return self._rotate_cursor

    def drag_crop(self,point):
        """The frame for the pointer at normalized point, kept on the straightened photo."""
        wanted=self.resize_crop(point)
        if self.drag_handle=='new':
            # A new frame grows from its starting corner.
            a=self.drag_start;tiny=lambda v,origin:origin+(v-origin)*1e-4
            start=sorted([tiny(wanted.left(),a.x()),tiny(wanted.right(),a.x())])+sorted([tiny(wanted.top(),a.y()),tiny(wanted.bottom(),a.y())])
            previous=[start[0],start[2],start[1],start[3]]
        else:previous=self.crop_values()
        return self.limited_crop(wanted,previous)

    def show_crop(self,crop):
        """Replace the crop frame (None = the whole photo), as when the angle changes."""
        x0,y0,x1,y1=crop or [0.,0.,1.,1.]
        self.crop_rect=QRectF(x0,y0,x1-x0,y1-y0)
        self.cropChanged.emit(self.crop_values())
        self.viewport().update()

    def limited_crop(self,rect,previous=None):
        """rect kept on the straightened photo: as far from `previous` as fits (a drag), else shrunk in place."""
        angle,width,height=self.crop_limit
        if not angle:return rect
        from . import straighten
        candidate=[rect.left(),rect.top(),rect.right(),rect.bottom()]
        if previous is None:x0,y0,x1,y1=straighten.shrink(candidate,angle,width,height)
        else:x0,y0,x1,y1=straighten.limit(candidate,previous,angle,width,height)
        return QRectF(x0,y0,x1-x0,y1-y0)

    def crop_screen_rect(self):
        r,b = self.crop_rect,self.canvas_rect()
        top = self.mapFromScene(QPointF(b.left()+r.left()*b.width(),b.top()+r.top()*b.height()))
        bottom = self.mapFromScene(QPointF(b.left()+r.right()*b.width(),b.top()+r.bottom()*b.height()))
        return QRectF(QPointF(top),QPointF(bottom)).normalized()

    @staticmethod
    def handle_points(rect):
        c=rect.center()
        return {'nw':rect.topLeft(), 'n':QPointF(c.x(),rect.top()), 'ne':rect.topRight(),
                'e':QPointF(rect.right(),c.y()), 'se':rect.bottomRight(), 's':QPointF(c.x(),rect.bottom()),
                'sw':rect.bottomLeft(), 'w':QPointF(rect.left(),c.y())}

    def hit_handle(self, position):
        rect=self.crop_screen_rect()
        for name,point in self.handle_points(rect).items():
            if (position-point).manhattanLength() <= 15:
                return name
        if rect.top()-6 <= position.y() <= rect.bottom()+6:
            if abs(position.x()-rect.left())<=6:
                return 'w'
            if abs(position.x()-rect.right())<=6:
                return 'e'
        if rect.left()-6 <= position.x() <= rect.right()+6:
            if abs(position.y()-rect.top())<=6:
                return 'n'
            if abs(position.y()-rect.bottom())<=6:
                return 's'
        return 'move' if rect.contains(position) else 'rotate'

    def crop_cursor(self,handle):
        if handle=='rotate':return self.rotate_cursor()
        return {'nw':Qt.CursorShape.SizeFDiagCursor,'se':Qt.CursorShape.SizeFDiagCursor,
                'ne':Qt.CursorShape.SizeBDiagCursor,'sw':Qt.CursorShape.SizeBDiagCursor,
                'n':Qt.CursorShape.SizeVerCursor,'s':Qt.CursorShape.SizeVerCursor,
                'e':Qt.CursorShape.SizeHorCursor,'w':Qt.CursorShape.SizeHorCursor,
                'move':Qt.CursorShape.SizeAllCursor,'new':Qt.CursorShape.CrossCursor}[handle]

    def normalized_point(self, point):
        b=self.canvas_rect()
        return QPointF(float(np.clip((point.x()-b.left())/b.width(),0,1)),
                       float(np.clip((point.y()-b.top())/b.height(),0,1)))

    def normalized_aspect(self):
        b=self.canvas_rect()
        return self.aspect*b.height()/b.width() if self.aspect and self.has_photo else 0

    def set_aspect(self,aspect):
        self.aspect=float(aspect)
        if not self.crop_mode or not self.has_photo or not aspect:
            return
        ratio=self.normalized_aspect()
        c=self.crop_rect.center()
        width=min(self.crop_rect.width(),self.crop_rect.height()*ratio)
        height=width/ratio
        self.crop_rect=self.limited_crop(QRectF(c.x()-width/2,c.y()-height/2,width,height))
        self.cropChanged.emit(self.crop_values())
        self.viewport().update()

    def reset_crop(self):
        self.crop_rect=QRectF(0,0,1,1)
        self.set_aspect(self.aspect)
        self.crop_rect=self.limited_crop(self.crop_rect)
        self.cropChanged.emit(self.crop_values())
        self.viewport().update()

    def accept_crop(self):
        if self.crop_mode:
            self.cropSelected.emit(self.crop_values())

    def resize_crop(self,point):
        r=self.drag_rect
        h=self.drag_handle
        ratio=self.normalized_aspect()
        # Minimum size is independent of the preview resolution.
        minimum=.015
        if h=='move':
            dx,dy=point.x()-self.drag_start.x(),point.y()-self.drag_start.y()
            x=float(np.clip(r.left()+dx,0,1-r.width()))
            y=float(np.clip(r.top()+dy,0,1-r.height()))
            return QRectF(x,y,r.width(),r.height())
        if h=='new':
            anchor=self.drag_start
            sx=1 if point.x()>=anchor.x() else -1
            sy=1 if point.y()>=anchor.y() else -1
            width,height=abs(point.x()-anchor.x()),abs(point.y()-anchor.y())
            if ratio:
                width=max(width,height*ratio)
                width=min(width,(1-anchor.x() if sx>0 else anchor.x()),
                          (1-anchor.y() if sy>0 else anchor.y())*ratio)
                height=width/ratio
            end=anchor+QPointF(sx*width,sy*height)
            return QRectF(anchor,end).normalized()
        if not ratio:
            left,right,top,bottom=r.left(),r.right(),r.top(),r.bottom()
            if 'w' in h: left=min(point.x(),right-minimum)
            if 'e' in h: right=max(point.x(),left+minimum)
            if 'n' in h: top=min(point.y(),bottom-minimum)
            if 's' in h: bottom=max(point.y(),top+minimum)
            return QRectF(QPointF(left,top),QPointF(right,bottom))
        if len(h)==2:
            anchor=QPointF(r.right() if 'w' in h else r.left(),r.bottom() if 'n' in h else r.top())
            sx,sy=(-1 if 'w' in h else 1),(-1 if 'n' in h else 1)
            width=max(minimum,sx*(point.x()-anchor.x()),sy*(point.y()-anchor.y())*ratio)
            width=min(width,(1-anchor.x() if sx>0 else anchor.x()),(1-anchor.y() if sy>0 else anchor.y())*ratio)
            return QRectF(anchor,anchor+QPointF(sx*width,sy*width/ratio)).normalized()
        if h in ('w','e'):
            anchor=r.right() if h=='w' else r.left()
            sign=-1 if h=='w' else 1
            width=max(minimum,sign*(point.x()-anchor))
            width=min(width,anchor if sign<0 else 1-anchor,2*min(r.center().y(),1-r.center().y())*ratio)
            return QRectF(min(anchor,anchor+sign*width),r.center().y()-width/ratio/2,width,width/ratio)
        anchor=r.bottom() if h=='n' else r.top()
        sign=-1 if h=='n' else 1
        height=max(minimum,sign*(point.y()-anchor))
        height=min(height,anchor if sign<0 else 1-anchor,2*min(r.center().x(),1-r.center().x())/ratio)
        return QRectF(r.center().x()-height*ratio/2,min(anchor,anchor+sign*height),height*ratio,height)

    def mousePressEvent(self, event):
        self.zoom_press=None
        if self.tool_mode=='adjust' and self.has_photo and event.button()==Qt.MouseButton.LeftButton:
            point=self.mapToScene(event.position().toPoint())
            if self.image_rect.contains(point):
                point=self.normalized_point(point);self.adjust_origin=event.position().y()
                self.setFocus();self.adjustStarted.emit(point.x(),point.y())
            event.accept();return
        if self.tool_mode=='edit_component' and self.tool_handles and event.button()==Qt.MouseButton.LeftButton:
            handles=self.tool_handles+[np.mean(self.tool_handles,axis=0).tolist()]
            for index,point in enumerate(handles):
                screen=self.mapFromScene(QPointF(point[0]*self.image_rect.width(),point[1]*self.image_rect.height()))
                if (screen-event.position().toPoint()).manhattanLength()<=14:
                    p=self.normalized_point(self.mapToScene(event.position().toPoint()))
                    self.handle_drag=(index,[p.x(),p.y()],[list(p) for p in self.tool_handles])
                    self.setFocus();event.accept();return
            event.accept();return
        if self.tool_mode and self.has_photo and event.button()==Qt.MouseButton.LeftButton:
            point=self.mapToScene(event.position().toPoint())
            if self.image_rect.contains(point):
                point=self.normalized_point(point);self.tool_points=[[point.x(),point.y()]]
                self.tool_alt=bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
                self.tool_shift=bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self.setFocus()
                if self.tool_mode in ('brush','linear','radial','clone','heal','inpaint','red_eye'):self.strokeProgress.emit(self.tool_points,self.tool_alt)
                event.accept();self.viewport().update();return
        if event.button() == Qt.MouseButton.LeftButton and self.has_photo:
            point = self.mapToScene(event.position().toPoint())
            bounds = self.image_rect
            if bounds.contains(point) and self.sample_mode:
                self.sampled.emit(point.x()/bounds.width(), point.y()/bounds.height())
                return
            if self.crop_mode:
                self.drag_handle=self.hit_handle(event.position())
                self.drag_start=self.normalized_point(point)
                self.drag_rect=QRectF(self.crop_rect)
                if self.drag_handle=='rotate':
                    # The centre stays where it was pressed: the view refits while the photo turns.
                    self.rotate_center=self.crop_screen_rect().center()
                    self.rotate_from=self.pointer_angle(self.rotate_center,event.position())
                    self.rotate_start=self.straighten_angle;self.rotate_moved=False
                self.viewport().setCursor(self.crop_cursor(self.drag_handle))
                event.accept()
                return
            if bounds.contains(point) and not self.tool_mode and not self.sample_mode and not self.crop_mode and not event.modifiers():
                self.zoom_press=event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.zoom_press is not None and (event.position().toPoint()-self.zoom_press).manhattanLength()>=QApplication.startDragDistance():
            self.zoom_press=None
        self.brush_position=self.mapToScene(event.position().toPoint())
        if self.tool_mode=='adjust':
            if getattr(self,'adjust_origin',None) is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.adjustDragged.emit(float(self.adjust_origin-event.position().y()))
            event.accept();return
        if self.tool_mode:self.viewport().update()
        if self.handle_drag and event.buttons() & Qt.MouseButton.LeftButton:
            index,start,original=self.handle_drag
            point=self.normalized_point(self.mapToScene(event.position().toPoint()));p=np.array([point.x(),point.y()])
            values=np.array(original)
            if index<2:values[index]=p
            else:
                delta=np.clip(p-start,-values.min(axis=0),1-values.max(axis=0));values+=delta
            self.tool_handles=values.tolist();self.viewport().update();event.accept();return
        if self.tool_mode and self.tool_points and event.buttons() & Qt.MouseButton.LeftButton:
            p=self.normalized_point(self.mapToScene(event.position().toPoint()))
            if self.tool_mode in ('linear','radial'):
                self.tool_points=self.tool_points[:1]+[[p.x(),p.y()]]
            elif self.tool_points[-1]!=[p.x(),p.y()]:self.tool_points.append([p.x(),p.y()])
            if self.tool_mode in ('brush','linear','radial','clone','heal','inpaint','red_eye'):self.strokeProgress.emit(self.tool_points,self.tool_alt)
            self.viewport().update();event.accept();return
        if self.crop_mode and self.has_photo:
            if self.drag_handle=='rotate':
                angle=self.dragged_angle(event.position())
                if angle!=self.straighten_angle:
                    self.rotate_moved=True;self.straighten_angle=angle;self.straightenDragged.emit(angle)
                self.viewport().update();event.accept();return
            if self.drag_handle:
                point=self.normalized_point(self.mapToScene(event.position().toPoint()))
                self.crop_rect=self.drag_crop(point)
                self.cropChanged.emit(self.crop_values())
                self.viewport().update()
            else:
                self.viewport().setCursor(self.crop_cursor(self.hit_handle(event.position())))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        zoom_press=self.zoom_press;self.zoom_press=None
        if self.tool_mode=='adjust':
            if getattr(self,'adjust_origin',None) is not None and event.button()==Qt.MouseButton.LeftButton:
                self.adjustDragged.emit(float(self.adjust_origin-event.position().y()))
                self.adjust_origin=None;self.adjustFinished.emit()
            event.accept();return
        if self.handle_drag and event.button()==Qt.MouseButton.LeftButton:
            # Include the release position even when Windows coalesces moves.
            index,start,original=self.handle_drag
            point=self.normalized_point(self.mapToScene(event.position().toPoint()));p=np.array([point.x(),point.y()]);values=np.array(original)
            if index<2:values[index]=p
            else:values+=np.clip(p-start,-values.min(axis=0),1-values.max(axis=0))
            self.handle_drag=None;self.tool_handles=values.tolist()
            if not np.allclose(values,original):self.componentEdited.emit(self.tool_handles)
            self.viewport().update();event.accept();return
        if self.tool_mode and self.tool_points:
            if event.button()!=Qt.MouseButton.LeftButton:return super().mouseReleaseEvent(event)
            end=self.normalized_point(self.mapToScene(event.position().toPoint()))
            if self.tool_mode in ('linear','radial'):self.tool_points=self.tool_points[:1]+[[end.x(),end.y()]]
            elif self.tool_points[-1]!=[end.x(),end.y()]:self.tool_points.append([end.x(),end.y()])
            points=self.tool_points;self.tool_points=[]
            self.strokeCompleted.emit(points,self.tool_alt)
            self.viewport().update();event.accept();return
        if self.drag_handle=='rotate':
            self.drag_handle=None
            if self.rotate_moved:self.straightenFinished.emit()
            self.viewport().update();event.accept();return
        if self.drag_handle:
            # Releasing the pointer keeps the crop editable until Enter/Apply.
            point=self.normalized_point(self.mapToScene(event.position().toPoint()))
            self.crop_rect=self.drag_crop(point)
            if self.crop_rect.width()<.015 or self.crop_rect.height()<.015:
                self.crop_rect=self.drag_rect
            self.drag_handle=None
            self.crop_rect=self.limited_crop(self.crop_rect)
            self.cropChanged.emit(self.crop_values())
            self.viewport().update()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if zoom_press is not None and event.button()==Qt.MouseButton.LeftButton and not event.modifiers() and self.has_photo and not (self.tool_mode or self.crop_mode or self.sample_mode):
            if (event.position().toPoint()-zoom_press).manhattanLength()<QApplication.startDragDistance():
                point=self.mapToScene(event.position().toPoint())
                if self.image_rect.contains(point):
                    if self.fit_mode:self.actual_size();self.centerOn(point)
                    else:self.fit_photo()

    def mouseDoubleClickEvent(self,event):
        self.zoom_press=None
        if self.crop_mode:
            self.drag_handle=None
            self.accept_crop()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def leaveEvent(self,event):
        self.brush_position=None;self.viewport().update();super().leaveEvent(event)

    def keyPressEvent(self,event):
        if self.sample_mode and event.key()==Qt.Key.Key_Escape:
            self.sample_mode=False;self.sampleCancelled.emit();event.accept()
        elif self.tool_mode and event.key()==Qt.Key.Key_Escape:
            if self.handle_drag:self.tool_handles=self.handle_drag[2];self.handle_drag=None
            elif self.tool_points:self.cancel_stroke()
            elif self.tool_mode=='heal' and self.healing_overlay is not None:self.healingClearRequested.emit()
            else:self.set_tool('')
            self.viewport().update();event.accept()
        elif self.tool_mode=='heal' and event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter):
            self.healingApplyRequested.emit();event.accept()
        elif self.tool_mode in ('brush','clone','heal','inpaint','red_eye') and event.key() in (Qt.Key.Key_BracketLeft,Qt.Key.Key_BracketRight):
            maximum=.25 if self.tool_mode=='brush' else .12
            self.brush_radius=float(np.clip(self.brush_radius*(1.2 if event.key()==Qt.Key.Key_BracketRight else 1/1.2),.002,maximum))
            self.brushSizeChanged.emit(self.brush_radius);self.viewport().update();event.accept()
        elif self.crop_mode and event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter):
            self.accept_crop()
            event.accept()
        elif self.crop_mode and event.key()==Qt.Key.Key_Escape:
            self.cropCancelled.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def set_clipping(self,shadows,highlights):
        self.shadow_clipping=shadows;self.highlight_clipping=highlights
        self.clipping=shadows or highlights;self.clipping_image=None;self.viewport().update()

    def drawForeground(self, painter, rect):
        if self.has_photo:
            if self.clipping and self.on_screen is not None:
                if self.clipping_image is None:
                    a=self.hdr_pixels if self.hdr_pixels is not None else self.on_screen;clipped=np.zeros((*a.shape[:2],4),np.uint8)
                    if self.highlight_clipping:
                        if self.hdr_pixels is not None:
                            peak=a.max(axis=-1);clipped[peak>1+1e-5]=[255,215,45,150]
                            clipped[peak>self.hdr_display_ceiling+1e-5]=[255,40,30,150]
                        else:clipped[np.any(a>=self.clipping_ceiling-1/510,axis=-1)]=[255,40,30,150]
                    if self.shadow_clipping:clipped[np.any(a<1/510,axis=-1)]=[40,90,255,160]
                    self.clipping_image=QImage(clipped.data,clipped.shape[1],clipped.shape[0],clipped.strides[0],QImage.Format.Format_RGBA8888).copy()
                painter.drawImage(self.image_rect,self.clipping_image)
            if self.tool_overlay is not None:
                painter.drawImage(self.image_rect,self.tool_overlay)
            if self.healing_overlay is not None and self.tool_mode=='heal':
                painter.drawImage(self.image_rect,self.healing_overlay)
            if self.tool_handles and self.tool_mode=='edit_component':
                painter.save();pen=QPen(QColor('#f7dfbc'),1.5/max(.01,self.transform().m11()));painter.setPen(pen)
                points=[QPointF(x*self.image_rect.width(),y*self.image_rect.height()) for x,y in self.tool_handles]
                if self.handle_kind=='radial':painter.drawEllipse(QRectF(points[0],points[1]).normalized())
                else:painter.drawLine(points[0],points[1])
                painter.setBrush(QColor('#202327'));radius=5/max(.01,self.transform().m11())
                for point in points+[(points[0]+points[1])/2]:painter.drawEllipse(point,radius,radius)
                painter.restore()
            if self.tool_mode in ('brush','clone','heal','inpaint','red_eye') and self.brush_position is not None and self.image_rect.contains(self.brush_position):
                painter.save();pen=QPen(QColor(255,255,255,210),1/max(.01,self.transform().m11()));painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush);radius=self.brush_radius*min(self.image_rect.width(),self.image_rect.height())
                painter.drawEllipse(self.brush_position,radius,radius);inner=radius*(1-self.brush_feather/100)
                pen.setStyle(Qt.PenStyle.DotLine);painter.setPen(pen);painter.drawEllipse(self.brush_position,inner,inner);painter.restore()
            # Brush coverage comes from the live, feathered mask. Painting the
            # raw path on top hid edits and ignored the colour boundary/overlay toggle.
            if self.tool_points and self.tool_mode not in ('brush','clone','heal','inpaint','red_eye'):
                painter.save()
                points=[QPointF(x*self.image_rect.width(),y*self.image_rect.height()) for x,y in self.tool_points]
                pen=QPen(QColor(245,100,70,150),max(2,self.brush_radius*min(self.image_rect.width(),self.image_rect.height())*2))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap);pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                if self.tool_mode in ('linear','radial','color_range','color_range_add','luma_range'):
                    pen.setWidthF(2/max(.01,self.transform().m11()))
                painter.setPen(pen)
                if self.tool_mode=='radial':painter.drawEllipse(QRectF(points[0],points[-1]).normalized())
                elif self.tool_mode in ('color_range','color_range_add','luma_range'):painter.drawRect(QRectF(points[0],points[-1]).normalized())
                elif len(points)>1:
                    for start,end in zip(points,points[1:]):painter.drawLine(start,end)
                else:painter.drawPoint(points[0])
                painter.restore()
            if self.crop_mode:
                self.draw_crop(painter)
            return
        if self.loading_photo:return
        painter.save()
        painter.resetTransform()
        w,h = self.viewport().width(),self.viewport().height()
        painter.setPen(QColor('#dfc29b'))
        font = painter.font()
        font.setPixelSize(13)
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 5)
        painter.setFont(font)
        painter.drawText(QRectF(20,h/2-78,w-40,30), Qt.AlignmentFlag.AlignCenter, 'ROOM FOR YOUR PHOTOGRAPHS')
        font.setPixelSize(27)
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing,0)
        painter.setFont(font)
        painter.setPen(QColor('#f0ebe4'))
        painter.drawText(QRectF(20,h/2-30,w-40,46), Qt.AlignmentFlag.AlignCenter, tr('사진 한 장에서 시작하세요'))
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QColor('#8c939b'))
        painter.drawText(QRectF(20,h/2+28,w-40,30), Qt.AlignmentFlag.AlignCenter, tr('사진이나 폴더를 끌어 놓거나, 사진 가져오기를 누르세요.'))
        painter.restore()

    def draw_crop(self,painter):
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=self.crop_screen_rect()
        outer=QPainterPath()
        outer.addRect(QRectF(self.viewport().rect()))
        hole=QPainterPath()
        hole.addRect(rect)
        painter.fillPath(outer.subtracted(hole),QColor(0,0,0,155))
        painter.setPen(QPen(QColor(255,255,255,135),1))
        # While the photo is being turned: a finer grid to judge horizontals and verticals by.
        parts=[i/9 for i in range(1,9)] if self.drag_handle=='rotate' else (1/3,2/3)
        for part in parts:
            x,y=rect.left()+rect.width()*part,rect.top()+rect.height()*part
            painter.drawLine(QPointF(x,rect.top()),QPointF(x,rect.bottom()))
            painter.drawLine(QPointF(rect.left(),y),QPointF(rect.right(),y))
        painter.setPen(QPen(QColor('#f7dfbc'),1.5))
        painter.drawRect(rect)
        painter.setBrush(QColor('#f7dfbc'))
        painter.setPen(QPen(QColor('#202327'),1))
        for point in self.handle_points(rect).values():
            painter.drawRect(QRectF(point.x()-4,point.y()-4,8,8))
        font=painter.font()
        font.setPixelSize(12)
        painter.setFont(font)
        label=QRectF(rect.center().x()-210,max(8,rect.top()-31),420,24)
        painter.fillRect(label,QColor(20,23,27,225))
        painter.setPen(QColor('#f7dfbc'))
        painter.drawText(label,Qt.AlignmentFlag.AlignCenter,tr('안쪽 이동 · 모서리 조절 · 바깥 끌어 회전   |   Enter 적용 · Esc 취소'))
        painter.restore()
