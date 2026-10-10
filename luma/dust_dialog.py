"""Source-resolution dust review. Detection never writes catalog edits."""
from .i18n import tr
from copy import deepcopy
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from threading import Event
import numpy as np
import cv2
from PySide6.QtCore import Qt,QTimer,Signal,QRectF,QPointF
from PySide6.QtGui import QColor,QPen,QPixmap,QPainter,QPainterPath,QPainterPathStroker
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QPushButton,QComboBox,QSpinBox,QListWidget,QListWidgetItem,QCheckBox,
    QGraphicsView,QGraphicsScene,QProgressBar,QScrollArea,QWidget,QFrame,QLayout)
from .dust import Options,Cancelled,checkpoint,detect,repair,visualize
from .dust_shapes import ellipses as spot_ellipses,manual as manual_stroke
from .engine import load_image,to_srgb,output_rgb
from .processing import apply_retouch
from .preview_store import signature
from .widgets import qimage

PAGE_SIZE=500
MAX_CANDIDATES=5000


def prepare_source(path,settings,cancel):
    """One source at a time; shared by individual and folder detection."""
    checkpoint(cancel);source,_=load_image(path,working_space=settings['working_space'],raw_options=settings)
    checkpoint(cancel)
    from .rawcolor import develop_camera
    linear=apply_retouch(develop_camera(source,settings),settings['retouch']);del source
    rgb=output_rgb(to_srgb(linear),settings['working_space'])
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    checkpoint(cancel)
    return linear,gray


class DustView(QGraphicsView):
    candidateClicked=Signal(int)
    spotAdded=Signal(float,float)
    strokeAdded=Signal(object)

    def __init__(self):
        super().__init__();self.setScene(QGraphicsScene(self));self.item=self.scene().addPixmap(QPixmap())
        self.setBackgroundBrush(QColor('#101215'));self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.spots=[];self.checked=set();self.current=-1;self.show_spots=True;self.fitted=True;self.add_mode=False
        self.stroke_mode=False;self.stroke=[];self.stroke_diameter=12;self.shape_cache=OrderedDict();self.shape_cache_cost=0

    def candidate_path(self,spot):
        r=self.sceneRect();key=(id(spot),r.width(),r.height(),spot.get('scale',100))
        if key in self.shape_cache:
            self.shape_cache.move_to_end(key);return self.shape_cache[key][1]
        path=QPainterPath();path.setFillRule(Qt.FillRule.WindingFill)
        for x,y,rx,ry in spot_ellipses(spot,(r.height(),r.width())):path.addEllipse(QPointF(x,y),rx,ry)
        path=path.simplified();cost=max(512,path.elementCount()*32)
        self.shape_cache[key]=(spot,path,cost);self.shape_cache_cost+=cost
        while self.shape_cache_cost>16*1024*1024:
            _,entry=self.shape_cache.popitem(last=False);self.shape_cache_cost-=entry[2]
        return path

    def image(self,image):
        first=self.item.pixmap().isNull()
        self.item.setPixmap(QPixmap.fromImage(image));self.setSceneRect(self.item.boundingRect())
        if self.fitted:self.fit()
        elif first and self.current>=0:self.actual(self.current)
        self.viewport().update()

    def fit(self):
        self.fitted=True;self.fitInView(self.sceneRect().adjusted(-12,-12,12,12),Qt.AspectRatioMode.KeepAspectRatio)

    def actual(self,index=None):
        self.fitted=False;self.resetTransform()
        if index is not None and 0<=index<len(self.spots):
            s=self.spots[index];r=self.sceneRect();self.centerOn(s['x']*(r.width()-1),s['y']*(r.height()-1))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.fitted:self.fit()

    def wheelEvent(self,event):
        self.fitted=False;factor=1.2 if event.angleDelta().y()>0 else 1/1.2
        if .015<self.transform().m11()*factor<16:self.scale(factor,factor)
        event.accept()

    def mousePressEvent(self,event):
        if self.stroke_mode and event.button()==Qt.MouseButton.LeftButton and not self.item.pixmap().isNull():
            point=self.mapToScene(event.position().toPoint())
            if self.sceneRect().contains(point):self.stroke=[point];event.accept();self.viewport().update();return
        if self.add_mode and event.button()==Qt.MouseButton.LeftButton and not self.item.pixmap().isNull():
            point=self.mapToScene(event.position().toPoint());r=self.sceneRect()
            if r.contains(point):
                self.spotAdded.emit(float(np.clip(point.x()/max(1,r.width()-1),0,1)),float(np.clip(point.y()/max(1,r.height()-1),0,1)))
                event.accept();return
        if self.show_spots and event.button()==Qt.MouseButton.LeftButton:
            point=self.mapToScene(event.position().toPoint());r=self.sceneRect();zoom=max(.01,self.transform().m11())
            hits=[]
            for i,s in enumerate(self.spots):
                if 'parts' in s:
                    path=self.candidate_path(s);margin=7/zoom
                    if not path.boundingRect().adjusted(-margin,-margin,margin,margin).contains(point):continue
                    stroker=QPainterPathStroker();stroker.setWidth(margin*2)
                    if path.contains(point) or stroker.createStroke(path).contains(point):hits.append((0,i))
                    continue
                dx=point.x()-s['x']*(r.width()-1);dy=point.y()-s['y']*(r.height()-1)
                scale=s.get('scale',100)/100
                if (dx/max(7/zoom,s['rx']*r.width()*scale))**2+(dy/max(7/zoom,s['ry']*r.height()*scale))**2<=1:
                    hits.append((dx*dx+dy*dy,i))
            if hits:self.candidateClicked.emit(min(hits)[1]);event.accept();return
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self.stroke:
            point=self.mapToScene(event.position().toPoint());r=self.sceneRect()
            point=QPointF(max(0,min(r.width()-1,point.x())),max(0,min(r.height()-1,point.y())))
            delta=point-self.stroke[-1]
            if delta.x()**2+delta.y()**2>=max(.5,self.stroke_diameter*.2)**2:
                self.stroke.append(point)
                # Preserve the full path at bounded event storage; interpolation
                # at release fills every segment with the selected brush width.
                if len(self.stroke)>8192:self.stroke=self.stroke[::2]+[self.stroke[-1]]
            self.viewport().update();event.accept();return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        if self.stroke and event.button()==Qt.MouseButton.LeftButton:
            point=self.mapToScene(event.position().toPoint());r=self.sceneRect()
            self.stroke.append(QPointF(max(0,min(r.width()-1,point.x())),max(0,min(r.height()-1,point.y()))))
            points=[[p.x()/max(1,r.width()-1),p.y()/max(1,r.height()-1)] for p in self.stroke]
            self.stroke=[];self.strokeAdded.emit(points);self.viewport().update();event.accept();return
        super().mouseReleaseEvent(event)

    def drawForeground(self,painter,rect):
        super().drawForeground(painter,rect)
        if self.stroke:
            path=QPainterPath(self.stroke[0])
            for point in self.stroke[1:]:path.lineTo(point)
            painter.save();painter.setPen(QPen(QColor('#ffe2a1'),self.stroke_diameter,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path);painter.restore()
        if not self.show_spots:return
        r=self.sceneRect();zoom=max(.01,self.transform().m11());painter.save();painter.setBrush(Qt.BrushStyle.NoBrush)
        for i,s in enumerate(self.spots):
            path=self.candidate_path(s) if 'parts' in s else None
            if path is not None and not rect.intersects(path.boundingRect().adjusted(-3/zoom,-3/zoom,3/zoom,3/zoom)):continue
            scale=s.get('scale',100)/100
            cx=s['x']*(r.width()-1);cy=s['y']*(r.height()-1)
            rx=max(5/zoom,s['rx']*r.width()*scale);ry=max(5/zoom,s['ry']*r.height()*scale)
            if path is None and not rect.intersects(QRectF(cx-rx-3/zoom,cy-ry-3/zoom,2*rx+6/zoom,2*ry+6/zoom)):continue
            color='#ffe2a1' if i==self.current else '#ff6d52' if i in self.checked else '#d9b163' if s.get('review') else '#8e959b'
            pen=QPen(QColor(color),(2.5 if i==self.current else 1.3)/zoom)
            if i not in self.checked:pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            if path is not None:painter.drawPath(path)
            else:painter.drawEllipse(QPointF(cx,cy),rx,ry)
        painter.restore()


class DustDialog(QDialog):
    scanProgress=Signal(int,int)

    def __init__(self,w,*,record=None,cached=None,review_state=None):
        super().__init__(w);self.w=w;self.ident=w.current_id;self.load_version=w.load_version
        self.batch_review=record is not None
        if record is not None:self.ident=record['id']
        self.settings=deepcopy(record['settings'] if record is not None else w.settings)
        self.path=record['path'] if record is not None else w.catalog.photo(self.ident)['path']
        self.fingerprint=record.get('fingerprint') if record is not None else signature(self.path)
        self.cached=deepcopy(cached);self.saved_review=deepcopy(review_state);self.review_state=None
        self.closed=False;self.busy=False;self.cancel=Event();self.data=None;self.result=None;self.operation=None
        self.pending_spots=[]
        self.preview_running=False;self.preview_pending=False;self.preview_generation=0;self.preview_cancel=Event()
        saved=cached['options'] if cached else w.catalog.preference('dust_detection_options',{})
        try:
            initial=Options(**{k:v for k,v in saved.items() if k in asdict(Options())});initial.validate()
        except (AttributeError,TypeError,ValueError):initial=Options()
        self.setWindowTitle(tr('먼지 자동 감지')+(' · '+Path(self.path).name if self.batch_review else ''));self.resize(1180,850);self.setMinimumSize(850,650)
        box=QVBoxLayout(self);intro=QLabel(tr('표시된 후보를 클릭해 제외·포함하고 목록에서 100%로 확인하세요. 추가 후보는 기본 제외되며 확인한 것만 체크하세요.\n자르기·톤 보정 전 전체 사진입니다. 무늬·별도 후보가 될 수 있습니다. 재검출하면 후보 선택과 수동 표시가 초기화됩니다.'))
        intro.setWordWrap(True);box.addWidget(intro)
        if self.batch_review:
            note=QLabel(tr('검토 완료를 누르면 목록에 보관합니다. 여러 사진 창의 적용 버튼을 눌러야 사진에 반영됩니다.'))
            note.setWordWrap(True);box.addWidget(note)
        body=QHBoxLayout();box.addLayout(body,1);self.view=DustView();body.addWidget(self.view,1)
        self.controls_scroll=QScrollArea();self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.controls_scroll.setMinimumWidth(326);self.controls_scroll.setMaximumWidth(366)
        controls=QWidget();self.controls_scroll.setWidget(controls);body.addWidget(self.controls_scroll)
        side=QVBoxLayout(controls);side.setContentsMargins(0,0,0,0);side.setSpacing(5)
        side.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        form=QFormLayout();form.setVerticalSpacing(4);side.addLayout(form)
        self.sensitivity=QSpinBox();self.sensitivity.setRange(1,100);self.sensitivity.setValue(initial.sensitivity);form.addRow(tr('검출 민감도'),self.sensitivity)
        self.minimum=QSpinBox();self.maximum=QSpinBox()
        for control,value in ((self.minimum,initial.minimum),(self.maximum,initial.maximum)):
            control.setRange(2,160);control.setValue(value);control.setSuffix(' px')
        form.addRow(tr('최소 점 크기'),self.minimum);form.addRow(tr('최대 점 크기'),self.maximum)
        self.polarity=QComboBox()
        for text,key in [('검은 점 + 흰 먼지','both'),('검은 점','dark'),('흰 먼지','bright')]:self.polarity.addItem(tr(text),key)
        self.polarity.setCurrentIndex(self.polarity.findData(initial.polarity))
        form.addRow(tr('찾을 종류'),self.polarity)
        self.detailed=QCheckBox(tr('경계·미세 먼지까지 정밀 검색'));form.addRow(self.detailed)
        self.detailed.setChecked(initial.detailed)
        self.detailed.setToolTip(tr('작은 크기를 추가 탐색합니다. 무늬 근처의 추가 후보는 자동으로 체크하지 않습니다. 처리 시간이 더 걸립니다.'))
        self.soft=QCheckBox(tr('입자 속 옅은 먼지 추가 검색'));form.addRow(self.soft)
        self.soft.setChecked(initial.soft);self.soft.setEnabled(initial.detailed)
        self.soft.setToolTip(tr('입자가 많은 사진의 옅은 점을 추가로 찾습니다. 시간이 더 걸리고 무늬도 후보에 포함될 수 있습니다. 추가 후보는 확인한 뒤 체크하세요.'))
        self.suppress_grain=QCheckBox(tr('입자 후보 억제'));form.addRow(self.suppress_grain)
        self.suppress_grain.setChecked(initial.suppress_grain);self.suppress_grain.setEnabled(initial.detailed and initial.soft)
        self.suppress_grain.setToolTip(tr('옅은 점의 검출 기준을 높여 검토 후보를 줄입니다. 옅은 먼지도 놓칠 수 있습니다. 후보가 너무 많을 때 켜고 필요하면 끄고 다시 검색하세요.'))
        self.reduce_patterns=QCheckBox(tr('무늬 후보 줄이기'));form.addRow(self.reduce_patterns)
        self.reduce_patterns.setChecked(initial.reduce_patterns);self.reduce_patterns.setEnabled(initial.detailed or initial.scratches)
        self.reduce_patterns.setToolTip(tr('정밀 후보는 반복 무늬와 비교하고, 긴 흠집은 넓은 물체와 붙은 부분을 추가로 제외합니다. 실제 먼지나 흠집도 놓칠 수 있으므로 필요에 따라 끄고 다시 검출하세요.'))
        self.scratches=QCheckBox(tr('긴 흠집·머리카락 후보 찾기'));form.addRow(self.scratches);self.scratches.setChecked(initial.scratches)
        self.scratches.setToolTip(tr('설정한 최대 굵기보다 충분히 긴 검은 선·흰 흠집을 추가로 찾습니다. 짧은 점은 위 점 검색을 사용하세요. 전선·가지·글자도 잡힐 수 있어 확인한 것만 체크하세요. 시간은 더 걸립니다.'))
        self.scratch_width=QSpinBox();self.scratch_width.setRange(2,40);self.scratch_width.setValue(initial.scratch_width);self.scratch_width.setSuffix(' px')
        self.scratch_width.setEnabled(initial.scratches);form.addRow(tr('흠집 최대 굵기'),self.scratch_width)
        scan_row=QHBoxLayout();side.addLayout(scan_row)
        self.scan_button=QPushButton(tr('먼지 검출'));self.scan_button.clicked.connect(self.scan);scan_row.addWidget(self.scan_button)
        self.stop_button=QPushButton(tr('검출 취소'));self.stop_button.clicked.connect(self.cancel_scan);self.stop_button.setEnabled(False);scan_row.addWidget(self.stop_button)
        self.list=QListWidget();self.list.setMinimumWidth(300);self.list.setMinimumHeight(150);side.addWidget(self.list,1)
        self.list.itemChanged.connect(self.selection_changed);self.list.currentRowChanged.connect(self.focus_candidate)
        self.more_button=QPushButton(tr('후보 더 보기'));self.more_button.setVisible(False);side.addWidget(self.more_button)
        self.more_button.clicked.connect(self.show_more)
        row=QHBoxLayout();side.addLayout(row)
        for title,checked in [('목록 모두 포함',True),('목록 모두 제외',False)]:
            b=QPushButton(tr(title));b.clicked.connect(lambda _,value=checked:self.select_all(value));row.addWidget(b)
        self.show_spots=QCheckBox(tr('먼지 후보 표시'));self.show_spots.setChecked(True);side.addWidget(self.show_spots)
        self.show_spots.toggled.connect(self.toggle_overlay)
        self.emphasis=QCheckBox(tr('먼지 강조 보기'));side.addWidget(self.emphasis);self.emphasis.toggled.connect(self.request_preview)
        self.strength=QSpinBox();self.strength.setRange(1,100);self.strength.setValue(50)
        detail_form=QFormLayout();side.addLayout(detail_form);detail_form.addRow(tr('강조 강도'),self.strength)
        self.strength.valueChanged.connect(self.request_preview)
        self.add_mode=QCheckBox(tr('클릭해서 놓친 먼지 추가'));self.add_mode.setEnabled(False);side.addWidget(self.add_mode)
        self.add_mode.toggled.connect(self.manual_mode)
        self.stroke_mode=QCheckBox(tr('드래그해서 흠집 추가'));self.stroke_mode.setEnabled(False);side.addWidget(self.stroke_mode)
        self.stroke_mode.toggled.connect(self.manual_stroke_mode)
        self.manual_size=QSpinBox();self.manual_size.setRange(2,160);self.manual_size.setValue(12);self.manual_size.setSuffix(' px')
        self.scale=QSpinBox();self.scale.setRange(25,250);self.scale.setValue(100);self.scale.setSuffix(' %');self.scale.setEnabled(False)
        manual_form=QFormLayout();side.addLayout(manual_form);manual_form.addRow(tr('수동 먼지 크기'),self.manual_size);manual_form.addRow(tr('선택 후보 제거 범위'),self.scale)
        manual_form.setVerticalSpacing(4)
        for control in (self.sensitivity,self.minimum,self.maximum,self.scratch_width,self.strength,self.manual_size,self.scale):
            control.setStyleSheet('QSpinBox { padding: 3px 8px; }')
        self.polarity.setStyleSheet('QComboBox { padding: 3px 8px; }')
        self.scale.valueChanged.connect(self.resize_candidate);self.view.spotAdded.connect(self.add_candidate)
        self.view.strokeAdded.connect(self.add_stroke);self.manual_size.valueChanged.connect(lambda value:setattr(self.view,'stroke_diameter',value))
        self.after=QCheckBox(tr('제거 결과 미리보기'));side.addWidget(self.after);self.after.toggled.connect(self.request_preview)
        self.repair_method=QComboBox()
        self.repair_method.addItem(tr('주변 질감 살리기'),'texture');self.repair_method.addItem(tr('부드럽게 연결'),'smooth')
        initial_method=review_state.get('repair_method','smooth') if review_state is not None else w.catalog.preference('dust_repair_method','texture')
        self.repair_method.setCurrentIndex(max(0,self.repair_method.findData(initial_method)))
        self.repair_method.setToolTip(tr('주변 질감 살리기는 가까운 깨끗한 영역의 입자를 가져옵니다. 무늬가 어색하면 부드럽게 연결로 바꾸고 제거 미리보기에서 비교하세요. 이전에 적용한 보정은 바뀌지 않습니다.'))
        repair_form=QFormLayout();repair_form.addRow(tr('복구 방식'),self.repair_method);side.addLayout(repair_form)
        self.repair_method.currentIndexChanged.connect(self.request_preview)
        self.preview_note=QLabel();self.preview_note.setWordWrap(True);side.addWidget(self.preview_note)
        self.view.candidateClicked.connect(self.toggle_candidate)
        row=QHBoxLayout();box.addLayout(row)
        for title,callback in [('화면 맞춤',self.view.fit),('100%',lambda:self.view.actual(self.list.currentRow()))]:
            b=QPushButton(tr(title));b.clicked.connect(callback);row.addWidget(b)
        self.count=QLabel(tr('체크된 후보 0개'));row.addWidget(self.count);row.addStretch()
        self.apply_button=QPushButton(tr('선택한 먼지 제거'));self.apply_button.setObjectName('primary');self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selection);row.addWidget(self.apply_button)
        close=QPushButton(tr('닫기'));close.clicked.connect(self.reject);row.addWidget(close)
        self.progress=QProgressBar();self.progress.hide();box.addWidget(self.progress)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);box.addWidget(self.status)
        self.scanProgress.connect(self.scan_progress)
        self.display=w.display_color.watch(self);self.display.changed.connect(self.request_preview)
        self.preview_timer=QTimer(self);self.preview_timer.setSingleShot(True);self.preview_timer.timeout.connect(self.start_preview)
        for control in (self.sensitivity,self.minimum,self.maximum):control.valueChanged.connect(self.options_changed)
        self.polarity.currentIndexChanged.connect(self.options_changed)
        self.detailed.toggled.connect(self.options_changed)
        self.soft.toggled.connect(self.options_changed)
        self.suppress_grain.toggled.connect(self.options_changed)
        self.reduce_patterns.toggled.connect(self.options_changed)
        self.scratches.toggled.connect(self.options_changed);self.scratch_width.valueChanged.connect(self.options_changed)
        QTimer.singleShot(0,self.scan)

    def options(self):return Options(self.sensitivity.value(),self.minimum.value(),self.maximum.value(),self.polarity.currentData(),self.detailed.isChecked(),self.reduce_patterns.isChecked(),self.soft.isChecked(),self.suppress_grain.isChecked(),self.scratches.isChecked(),self.scratch_width.value())

    def valid_source(self):
        if self.batch_review:
            row=self.w.catalog.photo(self.ident)
            return bool(row and row['path']==self.path and row['settings']==self.settings and signature(self.path)==self.fingerprint)
        return self.w.current_id==self.ident and self.w.load_version==self.load_version and self.w.settings==self.settings and signature(self.path)==self.fingerprint

    def set_busy(self,busy):
        self.busy=busy;self.scan_button.setEnabled(not busy);self.stop_button.setEnabled(busy)
        for control in (self.sensitivity,self.minimum,self.maximum,self.polarity,self.detailed,self.scratches):control.setEnabled(not busy)
        self.scratch_width.setEnabled(not busy and self.scratches.isChecked())
        self.reduce_patterns.setEnabled(not busy and (self.detailed.isChecked() or self.scratches.isChecked()))
        self.soft.setEnabled(not busy and self.detailed.isChecked())
        self.suppress_grain.setEnabled(not busy and self.detailed.isChecked() and self.soft.isChecked())
        self.selection_changed()

    def options_changed(self):
        self.cached=None;self.saved_review=None
        self.scratch_width.setEnabled(not self.busy and self.scratches.isChecked())
        self.reduce_patterns.setEnabled(not self.busy and (self.detailed.isChecked() or self.scratches.isChecked()))
        self.soft.setEnabled(not self.busy and self.detailed.isChecked())
        self.suppress_grain.setEnabled(not self.busy and self.detailed.isChecked() and self.soft.isChecked())
        self.result=None;self.pending_spots=[];self.list.clear();self.view.spots=[];self.selection_changed()
        self.status.setText(tr('검출 조건이 바뀌었습니다. 먼지 검출을 다시 누르세요.'))

    def scan(self):
        if self.closed or self.busy:return
        options=self.options()
        try:options.validate()
        except ValueError as error:self.status.setText(str(error));return
        if not self.fingerprint or not self.valid_source():
            self.status.setText(tr('원본을 연결하고 현재 사진으로 다시 열어 주세요.'));return
        self.cancel=Event();cancel=self.cancel;data=self.data;self.result=None;self.pending_spots=[]
        self.list.clear();self.view.spots=[];self.set_busy(True)
        self.progress.setRange(0,0);self.progress.show();self.status.setText(tr('원본 해상도로 먼지를 찾고 있습니다…'))
        path=self.path;settings=deepcopy(self.settings);fingerprint=self.fingerprint
        cached=self.cached;self.cached=None;saved_review=self.saved_review;self.saved_review=None
        def work():
            try:
                prepared=prepare_source(path,settings,cancel) if data is None else data
                checkpoint(cancel)
                # Missing newly added flags in old saved work mean their
                # default value, not a request to throw away cached detection.
                result=cached['result'] if cached and Options(**cached['options'])==options else detect(prepared[1],options,cancel=cancel,progress=self.scanProgress.emit,limit=MAX_CANDIDATES)
                if signature(path)!=fingerprint:raise ValueError('분석 중 원본 파일이 바뀌었습니다. 사진을 다시 열어 주세요.')
                return prepared,result,asdict(options)
            except Cancelled:return None
        def ready(result):
            if self.closed:return
            self.set_busy(False);self.progress.hide()
            if result is None or cancel.is_set():self.status.setText(tr('검출을 취소했습니다. 사진은 변경되지 않았습니다.'));return
            if not self.valid_source():self.status.setText(tr('사진이나 보정값이 바뀌었습니다. 현재 사진으로 다시 열어 주세요.'));return
            self.data,self.result,self.detect_options=result
            self.w.catalog.save_preference('dust_detection_options',self.detect_options)
            self.pending_spots=list(self.result['spots']);self.view.spots=[]
            if self.detect_options.get('scratches'):
                self.pending_spots.sort(key=lambda spot:spot.get('detail_kind')!='scratch')
            if saved_review:
                self.view.spots=deepcopy(saved_review['spots']);self.pending_spots=deepcopy(saved_review['pending'])
                self.list.blockSignals(True)
                for index,spot in enumerate(self.view.spots):self.append_item(spot,index,index in saved_review['checked'])
                self.list.blockSignals(False);self.list.setCurrentRow(saved_review['current'])
                self.selection_changed();self.result_status()
            else:self.show_more()
            self.request_preview()
        def failed(error):
            if not self.closed:self.set_busy(False);self.progress.hide();self.status.setText(str(error))
        self.w.spawn(work,ready,failed)

    def scan_progress(self,done,total):
        if not self.closed and self.busy:self.progress.setRange(0,total);self.progress.setValue(done)

    def cancel_scan(self):
        self.cancel.set();self.stop_button.setEnabled(False);self.status.setText(tr('검출을 취소하는 중…'))

    def selected(self):
        return [i for i in range(self.list.count()) if self.list.item(i).checkState()==Qt.CheckState.Checked]

    def show_more(self):
        if self.busy or self.result is None:return
        batch=self.pending_spots[:PAGE_SIZE];self.pending_spots=self.pending_spots[PAGE_SIZE:]
        self.list.blockSignals(True)
        for spot in batch:
            index=len(self.view.spots);self.view.spots.append(spot)
            self.append_item(spot,index,not spot.get('review',False))
        self.list.blockSignals(False);self.selection_changed();self.result_status()

    def result_status(self):
        if self.result is None:return
        tail=f' · 자동 후보는 최대 {MAX_CANDIDATES:,}개까지 확인할 수 있습니다.' if self.result['truncated'] else ''
        if self.detect_options.get('suppress_grain') and self.detect_options.get('soft') and self.detect_options.get('detailed'):
            tail+=' · 입자 억제 켜짐: 옅은 먼지가 빠질 수 있습니다.'
        if self.detect_options.get('scratches') and self.detect_options.get('reduce_patterns'):
            tail+=' · 무늬 억제 켜짐: 배경과 붙은 흠집이 빠질 수 있습니다.'
        extra=sum(bool(s.get('review')) for s in self.view.spots)
        manual=sum(bool(s.get('manual')) for s in self.view.spots);shown=len(self.view.spots)-manual
        self.status.setText(tr('{0} × {1} · 전체 {2}개 중 목록 {3}개 · 수동 {4}개 · 추가 후보 {5}개는 기본 제외 · 검출 {6}초', f"{self.result['width']:,}", f"{self.result['height']:,}", f"{self.result['total']:,}", f'{shown:,}', f'{manual}', f'{extra}', f"{self.result['seconds']:.2f}")+tail)

    def selection_changed(self,*_):
        chosen=self.selected();self.view.checked=set(chosen);self.view.viewport().update()
        self.count.setText(tr('체크된 후보 {0}개', f'{len(chosen)}'))
        self.apply_button.setText(tr('검토 완료 · {0}개 선택', f'{len(chosen)}') if self.batch_review else tr('선택한 먼지 {0}개 제거', f'{len(chosen)}'))
        self.apply_button.setEnabled((bool(chosen) or self.batch_review) and self.result is not None and not self.busy)
        self.add_mode.setEnabled(self.result is not None and self.data is not None and not self.busy)
        if not self.add_mode.isEnabled():self.add_mode.setChecked(False)
        self.stroke_mode.setEnabled(self.add_mode.isEnabled())
        if not self.stroke_mode.isEnabled():self.stroke_mode.setChecked(False)
        self.scale.setEnabled(0<=self.list.currentRow()<len(self.view.spots) and not self.busy)
        self.more_button.setVisible(bool(self.pending_spots));self.more_button.setEnabled(bool(self.pending_spots) and not self.busy)
        self.more_button.setText(tr('후보 {0}개 더 보기 · 남은 {1}개', f'{min(PAGE_SIZE, len(self.pending_spots))}', f'{len(self.pending_spots):,}'))
        if self.after.isChecked():self.request_preview()

    def select_all(self,checked):
        self.list.blockSignals(True)
        for i in range(self.list.count()):self.list.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.list.blockSignals(False);self.selection_changed()

    def toggle_candidate(self,index):
        item=self.list.item(index)
        if item is not None:
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState()==Qt.CheckState.Checked else Qt.CheckState.Checked)
            self.list.blockSignals(True);self.list.setCurrentRow(index);self.list.blockSignals(False)
            self.view.current=index;self.view.viewport().update()
            self.scale.blockSignals(True);self.scale.setValue(self.view.spots[index].get('scale',100));self.scale.blockSignals(False)
            self.scale.setEnabled(not self.busy)

    def focus_candidate(self,index):
        self.view.current=index
        if index>=0:self.view.actual(index)
        self.scale.blockSignals(True);self.scale.setValue(self.view.spots[index].get('scale',100) if 0<=index<len(self.view.spots) else 100);self.scale.blockSignals(False)
        self.scale.setEnabled(0<=index<len(self.view.spots) and not self.busy)
        self.view.viewport().update()

    def append_item(self,spot,index,checked=True):
        title='수동 표시' if spot.get('manual') else '검은 점' if spot['polarity']=='dark' else '흰 먼지'
        if spot.get('detail_kind')=='soft':title='옅은 점 · '+title
        if spot.get('detail_kind')=='scratch':title='수동 흠집' if spot.get('manual') else '긴 흠집 · '+('어두운 선' if spot['polarity']=='dark' else '밝은 선')
        if spot.get('review'):title='추가 확인 · '+title
        item=QListWidgetItem(f'{index+1}. {title} · {spot["diameter"]:g} px')
        item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked);self.list.addItem(item)

    def manual_mode(self,enabled):
        if enabled:self.stroke_mode.setChecked(False)
        self.view.add_mode=enabled
        self.update_manual_mode()

    def manual_stroke_mode(self,enabled):
        if enabled:self.add_mode.setChecked(False)
        self.view.stroke_mode=enabled;self.view.stroke=[];self.view.viewport().update();self.update_manual_mode()

    def update_manual_mode(self):
        enabled=self.view.add_mode or self.view.stroke_mode
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag if enabled else QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)

    def add_stroke(self,points):
        if self.busy or self.data is None or self.result is None:return
        if len(self.view.spots)+len(self.pending_spots)>=MAX_CANDIDATES+1000:self.status.setText(tr('후보가 많습니다. 일부를 먼저 적용한 뒤 다시 표시하세요.'));return
        try:spot=manual_stroke(points,self.data[1].shape,self.manual_size.value())
        except ValueError as error:self.status.setText(str(error));return
        self.insert_manual(spot)

    def add_candidate(self,x,y):
        if self.busy or self.data is None or self.result is None:return
        if len(self.view.spots)+len(self.pending_spots)>=MAX_CANDIDATES+1000:self.status.setText(tr('후보가 많습니다. 일부를 먼저 적용한 뒤 다시 검출하세요.'));return
        h,w=self.data[1].shape;diameter=min(self.manual_size.value(),min(h,w))
        spot=dict(x=x,y=y,rx=diameter/(2*w),ry=diameter/(2*h),manual=True,polarity='manual',diameter=diameter,score=0)
        self.insert_manual(spot)

    def insert_manual(self,spot):
        self.view.spots.append(spot);index=len(self.view.spots)-1
        self.list.blockSignals(True);self.append_item(spot,index);self.list.setCurrentRow(index);self.list.blockSignals(False)
        self.view.current=index;self.scale.blockSignals(True);self.scale.setValue(100);self.scale.blockSignals(False)
        self.selection_changed();self.result_status()

    def resize_candidate(self,value):
        index=self.list.currentRow()
        if self.busy or not 0<=index<len(self.view.spots):return
        self.view.spots[index]['scale']=value;self.view.viewport().update()
        if self.after.isChecked():self.request_preview()

    def toggle_overlay(self,visible):self.view.show_spots=visible;self.view.viewport().update()

    def request_preview(self,*_):
        if self.closed or self.data is None:return
        self.preview_note.setText(tr('제거 미리보기 계산 중…') if self.after.isChecked() else tr('원본 보기 준비 중…'))
        self.preview_generation+=1;self.preview_cancel.set();self.preview_timer.start(120)

    def start_preview(self):
        if self.closed or self.data is None:return
        if self.preview_running:self.preview_pending=True;return
        self.preview_running=True;self.preview_pending=False;generation=self.preview_generation
        self.preview_cancel=Event();cancel=self.preview_cancel;linear=self.data[0];profile=self.display.profile
        spots=[deepcopy(self.view.spots[i]) for i in self.selected()] if self.after.isChecked() else []
        space=self.settings['working_space']
        emphasis=self.emphasis.isChecked();strength=self.strength.value();method=self.repair_method.currentData()
        def work():
            try:
                checkpoint(cancel);image=repair(linear,spots,cancel=cancel,method=method) if spots else linear
                if emphasis:
                    from .colorio import display_rgb
                    rgb=output_rgb(to_srgb(image),space)
                    gray=visualize(cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY),strength)
                    rgb=display_rgb(np.repeat(gray[...,None],3,-1),profile)
                else:rgb=output_rgb(to_srgb(image),space,profile or 'sRGB')
                checkpoint(cancel)
                return qimage(rgb,False)
            except Cancelled:return None
        def ready(image):
            self.preview_running=False
            if self.closed:return
            if image is not None and generation==self.preview_generation:
                self.view.image(image);self.preview_note.setText((tr('먼지 강조 · ') if emphasis else '')+(tr('체크된 후보 제거 미리보기') if self.after.isChecked() else tr('제거 전 보기')))
            if self.preview_pending:self.start_preview()
        def failed(error):
            self.preview_running=False
            if not self.closed:
                self.status.setText(str(error))
                if self.preview_pending:self.start_preview()
        self.w.spawn(work,ready,failed)

    def apply_selection(self):
        if self.busy or self.result is None or not self.selected() and not self.batch_review:return
        if not self.valid_source():
            self.status.setText(tr('사진이나 보정값·원본이 바뀌었습니다. 현재 사진으로 다시 열어 주세요.'));return
        method=self.repair_method.currentData()
        self.operation=dict(type='dust',version=4,repair_method=method,spots=[deepcopy(self.view.spots[i]) for i in self.selected()],
            detection=deepcopy(self.detect_options),source_size=[self.result['width'],self.result['height']])
        self.review_state=dict(spots=deepcopy(self.view.spots),pending=deepcopy(self.pending_spots),checked=self.selected(),current=self.list.currentRow(),repair_method=method)
        self.w.catalog.save_preference('dust_repair_method',method)
        self.accept()

    def done(self,result):
        self.closed=True;self.cancel.set();self.preview_cancel.set();self.preview_timer.stop()
        self.view.item.setPixmap(QPixmap())
        self.view.shape_cache.clear();self.view.shape_cache_cost=0;self.view.stroke=[]
        self.data=None;self.pending_spots=[];self.w.display_color.release(self.display)
        super().done(result)
