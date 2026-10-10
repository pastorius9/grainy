"""Native controls for deterministic colour, detail, geometry and local edits."""
from .i18n import tr, keys
from copy import deepcopy
import json
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QLabel,QPushButton,QComboBox,QCheckBox,QHBoxLayout,
    QListWidget,QListWidgetItem,QInputDialog,QFileDialog,QMessageBox,QColorDialog,QGroupBox,QVBoxLayout)
from .widgets import Adjustment,ToneCurve
from .engine import geometry,resize_float,defaults,GEOMETRY_KEYS


def label(box,text):
    w=QLabel(tr(text));w.setWordWrap(True);w.setObjectName('muted');box.addWidget(w);return w


def action(box,text,callback):
    b=QPushButton(tr(text));b.clicked.connect(callback);box.addWidget(b);return b


# Film balance -> (wb_reference: what the stock is balanced for, kelvin: the light it was shot in).
FILM_BALANCE={'tungsten':(3200.,5500.),'daylight':(5500.,3200.)}


class StudioTools:
    def __init__(self,window):
        self.w=window;self.loading=False;self.photo_id=None;self.layer_index=0
        self.donor=None;self.guide_points=[];self.coordinate_key=None;self.coordinates=None
        self.point_index=0;self.local_point_index=0;self.component_index=-1;self.group_controls=[]
        self.color_pick_generation=0;self.color_pick_running=False
        self.upright_generation=0
        self.range_loading=False
        self.pending_stroke=None;self.stroke_generation=0
        self.fresh_layer=None
        self.healing_strokes=[];self.healing_context=None
        self.healing_mask=None;self.healing_overlay_key=None;self.healing_generation=0
        self.build_color();self.build_detail();self.build_masks();self.build_retouch()
        self.w.view.strokeCompleted.connect(self.stroke)
        self.w.view.strokeProgress.connect(self.preview_stroke)
        self.w.view.strokeCancelled.connect(self.cancel_stroke)
        self.w.view.healingApplyRequested.connect(self.apply_healing)
        self.w.view.healingClearRequested.connect(self.clear_healing)
        self.w.before_button.toggled.connect(lambda _:self.update_healing_overlay())
        self.w.view.brushSizeChanged.connect(self.set_brush_size)
        self.w.view.componentEdited.connect(self.move_component_handles)
        self.w.view.toolModeChanged.connect(self.mask_tool_changed)
        from PySide6.QtGui import QShortcut,QKeySequence
        self.overlay_shortcut=QShortcut(QKeySequence('O'),self.w)
        self.overlay_shortcut.activated.connect(lambda:self.w.safe_shortcut(self.toggle_overlay))
        self.w.tabs.currentChanged.connect(self.tab_changed)
        self.load_settings()

    def scalar(self,box,key,text,low=-100,high=100,scale=1):
        self.w.add_adjustment(box,key,text,low,high,scale)
        self.w.adjustments[key].default_value=defaults()[key]

    def group(self,box,key,index,labels,ranges):
        controls=[]
        for column,(name,(low,high,scale)) in enumerate(zip(labels,ranges)):
            c=Adjustment(str(column),name,low,high,scale)
            c.changed.connect(lambda _,value,k=key,i=index,j=column:self.group_changed(k,i,j,value))
            c.committed.connect(self.w.finish_interaction);box.addWidget(c);controls.append(c)
        self.group_controls.append((key,index,controls));return controls

    def group_changed(self,key,index,column,value):
        if self.loading:return
        values=deepcopy(self.w.settings[key]);values[index][column]=value
        self.w.set_setting(key,values)

    def build_color(self):
        self.w.tab_page(tr('색상'));box=self.w.panel_box('photo_filter')
        from .photo_filter import FILTERS
        self.photo_filter_enabled=QCheckBox(tr('컬러 필터'));box.addWidget(self.photo_filter_enabled)
        self.photo_filter_choice=QComboBox();self.photo_filter_choice.setAccessibleName(tr('필터 프리셋'))
        for key,title,_ in FILTERS:self.photo_filter_choice.addItem(tr(title),key)
        box.addWidget(self.photo_filter_choice)
        self.scalar(box,'photo_filter_density',tr('필터 강도'),0,100)
        self.photo_filter_luminosity=QCheckBox(tr('밝기 유지'));box.addWidget(self.photo_filter_luminosity)
        self.photo_filter_enabled.toggled.connect(lambda value:self.change_photo_filter('photo_filter_enabled',value))
        self.photo_filter_choice.currentIndexChanged.connect(lambda _:self.change_photo_filter('photo_filter',self.photo_filter_choice.currentData()))
        self.photo_filter_luminosity.toggled.connect(lambda value:self.change_photo_filter('photo_filter_luminosity',value))
        label(box,tr('필터 선택 후 강도를 조절하세요. 흑백 사진에서는 색에 따른 밝기 차이를 조절합니다.'))
        box=self.w.panel_box('calibration')
        self.working=QComboBox();self.working.addItem(tr('작업 색공간 · sRGB'),'sRGB');self.working.addItem(tr('작업 색공간 · ProPhoto 광색역'),'ProPhoto')
        self.working.currentIndexChanged.connect(self.change_working);box.addWidget(self.working)
        box=self.w.wb_box
        self.raw_mode=QComboBox()
        for text,value in [('RAW · 기존 현상 유지','legacy'),('RAW · 촬영 시 화이트밸런스','as_shot'),
                           ('RAW · 주광','daylight'),('RAW · 색온도 직접 지정','custom'),('RAW · 스포이드','sample')]:
            self.raw_mode.addItem(tr(text),value)
        self.raw_mode.currentIndexChanged.connect(self.change_raw_mode);box.addWidget(self.raw_mode)
        self.raw_note=QLabel(tr('RAW 사진에서 카메라 화이트밸런스와 DCP를 사용할 수 있습니다.'))
        self.raw_note.setWordWrap(True);self.raw_note.setObjectName('muted')
        self.scalar(box,'raw_kelvin',tr('RAW 색온도 K'),2000,50000)
        self.scalar(box,'raw_tint',tr('RAW 색조'),-150,150,10)
        for key in ('raw_kelvin','raw_tint'):
            self.w.adjustments[key].changed.connect(lambda _,v:self.raw_temperature_changed())
        self.dcp_browse=action(self.w.profile_box,tr('카메라 프로파일 찾아보기'),self.browse_dcp)
        self.dcp_note=label(self.w.profile_box,tr('DCP 프로파일 없음'))
        self.dcp_note.setTextFormat(Qt.TextFormat.PlainText)
        box=self.w.advanced_box(self.w.panel_box('calibration'),'RAW 프로파일 상세')
        box.addWidget(self.raw_note)
        self.dcp_button=action(box,tr('DCP 파일 직접 불러오기'),self.load_dcp)
        self.dcp_clear=action(box,tr('DCP 프로파일 해제'),self.clear_dcp)
        action(box,tr('RAW 가져오기 기본 보정'),self.raw_defaults)
        self.dcp_switches={}
        for key,text in [('dcp_huesat','프로파일 색상 보정'),('dcp_look','프로파일 룩'),
                         ('dcp_tone','프로파일 톤 곡선'),('dcp_exposure','프로파일 기준 노출')]:
            control=QCheckBox(tr(text));box.addWidget(control);self.dcp_switches[key]=control
            control.toggled.connect(lambda value,k=key:self.w.set_setting(k,value) if not self.loading else None)
        # Film balance conversion (an 85 or 80A filter after the fact): Kelvin white balance from the
        # stock's balance to the light it was shot in.
        row=QHBoxLayout();self.film_buttons={}
        for key,text,tip in (
            ('tungsten','텅스텐 → 데이라이트','텅스텐 필름(500T 등, 3200K)을 낮 빛에서 찍어 푸르게 나온 색을 바로잡습니다. 85 필터와 같은 방향입니다. 다시 누르면 해제합니다.'),
            ('daylight','데이라이트 → 텅스텐','데이라이트 필름(250D 등, 5500K)을 전구 빛에서 찍어 누렇게 나온 색을 바로잡습니다. 80A 필터와 같은 방향입니다. 다시 누르면 해제합니다.')):
            b=QPushButton(tr(text));b.setCheckable(True);b.setToolTip(tr(tip))
            b.clicked.connect(lambda checked,k=key:self.film_balance(k,checked));row.addWidget(b);self.film_buttons[key]=b
        self.w.wb_box.addLayout(row)
        box=self.w.advanced_box(self.w.wb_box,'화이트 밸런스 상세')
        self.kelvin=QCheckBox(tr('켈빈 화이트 밸런스 사용'))
        self.kelvin.toggled.connect(lambda b:self.w.set_setting('kelvin_enabled',b) if not self.loading else None)
        box.addWidget(self.kelvin)
        label(box,tr('카메라 WB로 현상한 사진의 기준 백색점에 대한 상대 보정입니다.'))
        self.scalar(box,'kelvin',tr('보정 색온도 K'),2000,50000)
        self.scalar(box,'wb_reference',tr('기준 백색점 K'),2000,50000)
        box=self.w.panel_box('mixer')
        self.mixer=QComboBox();self.mixer.addItem(tr('HSL · 명도 기반'),'hsl');self.mixer.addItem(tr('기존 HSV · 밝기 기반'),'hsv')
        self.mixer.currentIndexChanged.connect(lambda _:self.w.set_setting('mixer_mode',self.mixer.currentData()) if not self.loading else None)
        box.insertWidget(0,self.mixer)
        box=self.w.advanced_box(self.w.panel_box('curve'),'채널 및 파라메트릭 곡선')
        label(box,keys(tr('색상 채널 커브 · Ctrl+클릭: 점 추가 / 우클릭: 점 삭제 / 더블클릭: 초기화')))
        self.channel=QComboBox();self.channel.addItems([tr('빨강 R'),tr('초록 G'),tr('파랑 B')]);box.addWidget(self.channel)
        self.channel_curve=ToneCurve();box.addWidget(self.channel_curve)
        self.channel.currentIndexChanged.connect(lambda _:self.channel_curve.set_points(self.w.settings['rgb_curves'][self.channel.currentIndex()]))
        self.channel_curve.changed.connect(self.rgb_curve_changed);self.channel_curve.committed.connect(self.w.finish_interaction)
        self.parametric=[]
        for i,name in enumerate(['암부 곡선','어두운 영역 곡선','밝은 영역 곡선','명부 곡선']):
            c=Adjustment(str(i),name);c.changed.connect(self.parametric_changed);c.committed.connect(self.w.finish_interaction)
            box.addWidget(c);self.parametric.append(c)
        box=self.w.panel_box('grading')
        for i,title in enumerate(['그림자 컬러 그레이딩','중간톤 컬러 그레이딩','하이라이트 컬러 그레이딩']):
            label(box,title);self.group(box,'grading',i,['색상','채도','명도'],[(0,360,1),(0,100,1),(-100,100,1)])
        self.scalar(box,'grading_balance',tr('그레이딩 균형'))
        box=self.w.panel_box('calibration')
        for i,title in enumerate(['빨강 원색 보정','초록 원색 보정','파랑 원색 보정']):
            label(box,title);self.group(box,'calibration',i,['색상','채도'],[(-100,100,1),(-100,100,1)])
        box=self.w.advanced_box(self.w.panel_box('mixer'),'포인트 색상')
        action(box,tr('선택 색상 · 사진에서 찍기'),lambda:self.mode('point_color'))
        self.point_note=label(box,tr('사진에서 색을 고른 뒤 범위를 조절하세요.'))
        self.points=QComboBox();self.points.currentIndexChanged.connect(self.load_point);box.addWidget(self.points)
        self.point_controls={}
        for key,title,low,high,scale in [('range','색상 범위',.01,.5,100),('saturation_range','채도 범위',.01,1,100),('lightness_range','명도 범위',.01,1,100),('hue','색상',-100,100,1),('saturation','채도',-100,100,1),('lightness','명도',-100,100,1)]:
            c=Adjustment(key,title,low,high,scale);box.addWidget(c);c.changed.connect(self.point_changed)
            c.committed.connect(self.w.finish_interaction);self.point_controls[key]=c
            c.default_value={'range':.12,'saturation_range':.6,'lightness_range':.5}.get(key,0)
        action(box,tr('선택 색상 제거'),self.remove_point)
        self.point_preview=QCheckBox(tr('선택 색상 범위 표시'));box.addWidget(self.point_preview)
        self.point_preview.toggled.connect(lambda _:self.preview_ready(self.w.view.on_screen))
        label(box,tr('파란 표시가 진할수록 보정이 강하게 적용됩니다. 범위 표시는 사진에 저장되지 않습니다.'))
        box=self.w.advanced_box(self.w.panel_box('calibration'),'색 보정 행렬')
        action(box,tr('색 보정 행렬 프로파일 불러오기'),self.camera_profile)
        action(box,tr('색 보정 행렬 해제'),lambda:self.w.set_setting('camera_matrix',None))
        label(box,tr('행렬 프로파일: 현상된 RGB에 적용하는 3×3 JSON 행렬. RAW 카메라 DCP는 위에서 선택합니다.'))

    def build_detail(self):
        _,box=self.w.tab_page(tr('디테일·렌즈'))
        for key,text in [('texture','텍스처'),('dehaze','안개 제거')]:self.scalar(self.w.presence_box,key,text)
        self.w.presence_box.insertWidget(0,self.w.adjustments['texture'])
        for key,text,lo,hi,scale in [('sharpen_radius','선명도 반경',.2,5,100),('sharpen_detail','선명도 디테일',0,100,1),('sharpen_mask','선명도 마스킹',0,100,1),
                ('noise_luma','휘도 노이즈 제거',0,100,1),('noise_color','색상 노이즈 제거',0,100,1)]:
            self.scalar(box,key,text,lo,hi,scale)
        box=self.w.panel_box('lens')
        row=QHBoxLayout();box.addLayout(row)
        self.auto_lens_button=action(row,tr('렌즈 자동 선택'),self.auto_lens)
        self.auto_lens_button.setToolTip(tr('촬영 정보의 카메라·렌즈·초점 거리로 프로파일을 선택합니다.'))
        action(row,tr('카메라·렌즈 선택'),self.choose_lens)
        self.lens_switches={}
        for key,text in [('lensfun_enabled','렌즈 프로파일 사용'),('lensfun_distortion','프로파일 왜곡 보정'),
                ('lensfun_tca','프로파일 색수차 보정'),('lensfun_vignette','프로파일 주변광량 보정'),
                ('lensfun_scale','렌즈 보정의 빈 가장자리 맞춤')]:
            control=QCheckBox(tr(text));box.addWidget(control);self.lens_switches[key]=control
            control.toggled.connect(lambda value,k=key:self.w.set_setting(k,value) if not self.loading else None)
        self.auto_lens_label=label(box,tr('자동 렌즈 프로파일 없음'))
        manual=self.w.advanced_box(box,'수동 렌즈 교정')
        for key,text in [('distortion','왜곡 1차'),('distortion_k2','왜곡 2차'),('distortion_k3','왜곡 3차'),
                         ('lens_vignette','렌즈 주변광량'),('ca_red','빨강 색수차'),('ca_blue','파랑 색수차'),('defringe','보라색 프린지 제거')]:
            self.scalar(manual,key,text,0 if key=='defringe' else -100,100)
        action(manual,tr('현재 렌즈 프로파일 저장'),self.save_lens)
        action(manual,tr('렌즈 프로파일 불러오기'),self.load_lens)
        self.lens_label=label(manual,tr('렌즈 프로파일 없음'))
        box=self.w.panel_box('transform')
        label(box,tr('직선 기준 자동 정렬'))
        row=QHBoxLayout();box.addLayout(row);self.upright_buttons=[]
        for text,mode in [('자동','auto'),('수평','level'),('수직','vertical'),('전체 원근','full')]:
            self.upright_buttons.append(action(row,text,lambda _,m=mode:self.auto_upright(m)))
        self.upright_crop=QCheckBox(tr('자동 원근의 빈 가장자리 맞춤'));box.addWidget(self.upright_crop)
        self.upright_crop.toggled.connect(lambda value:self.w.set_setting('upright_crop',value) if not self.loading else None)
        self.upright_label=label(box,tr('자동 정렬 미적용'))
        action(box,tr('자동 정렬 초기화'),self.clear_upright)
        for key,text,lo,hi,scale in [('perspective_v','수직 원근',-100,100,1),('perspective_h','수평 원근',-100,100,1),
                ('aspect_scale','가로 비율',-100,100,1),('shift_x','가로 이동',-100,100,1),('shift_y','세로 이동',-100,100,1),
                ('transform_scale','확대',50,200,1)]:self.scalar(box,key,text,lo,hi,scale)
        action(box,tr('가이드 원근 · 네 모서리 선택'),self.start_guides)
        label(box,tr('사각형의 좌상 → 우상 → 우하 → 좌하 모서리를 클릭합니다.'))
        action(box,tr('가이드 원근 초기화'),lambda:self.w.set_setting('guides',None))

    # Mask kinds in menu order (None = separator). AI masks and depth range are not offered.
    MASK_KINDS=(('brush','브러시'),('linear','선형 그레이디언트'),('radial','방사형 그레이디언트'),(None,''),
                ('color_range','색상 범위'),('luma_range','광도 범위'))

    def kind_button(self,box,text,callback):
        """A button that opens the list of mask kinds and calls callback(kind)."""
        from PySide6.QtWidgets import QMenu
        button=QPushButton(tr(text));menu=QMenu(button)
        for kind,title in self.MASK_KINDS:
            if kind is None:menu.addSeparator()
            else:menu.addAction(tr(title),lambda k=kind:callback(k))
        button.setMenu(menu);box.addWidget(button);return button

    def build_masks(self):
        from PySide6.QtWidgets import QTreeWidget,QWidget
        from .app import section
        _,box=self.w.tab_page(tr('마스크'))
        # Order: create a mask, the mask list with add / subtract, the active tool's options, the mask's adjustments.
        self.create_button=self.kind_button(box,'새 마스크 만들기',self.create_mask)
        self.mask_tree=QTreeWidget();self.mask_tree.setHeaderHidden(True);self.mask_tree.setIndentation(14)
        box.addWidget(self.mask_tree)
        self.mask_tree.itemExpanded.connect(lambda _:self.update_mask_ui());self.mask_tree.itemCollapsed.connect(lambda _:self.update_mask_ui())
        self.mask_tree.currentItemChanged.connect(self.tree_selected);self.mask_tree.itemChanged.connect(self.tree_checked)
        self.mask_tree.itemDoubleClicked.connect(self.tree_double_clicked)
        self.mask_tree.itemClicked.connect(lambda item,_:self.tree_selected(item))   # also a click on the current row
        self.mask_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mask_tree.customContextMenuRequested.connect(lambda point:self.mask_menu().exec(self.mask_tree.viewport().mapToGlobal(point)))
        row=QHBoxLayout();box.addLayout(row)
        self.add_button=self.kind_button(row,'추가',lambda kind:self.extend_mask(kind,'add'))
        self.subtract_button=self.kind_button(row,'빼기',lambda kind:self.extend_mask(kind,'subtract'))
        self.more_button=action(row,'…',lambda:self.mask_menu().exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft())))
        self.more_button.setToolTip(tr('이름 바꾸기, 복제, 교차, 순서, 삭제'));self.more_button.setMaximumWidth(44)
        row=QHBoxLayout();box.addLayout(row)
        self.invert=QCheckBox(tr('반전'));self.invert.toggled.connect(lambda b:self.layer_property('invert',b));row.addWidget(self.invert)
        self.overlay=QCheckBox(tr('오버레이 표시 (O)'));self.overlay.setChecked(True)
        self.overlay.toggled.connect(lambda _:self.preview_ready(self.w.view.on_screen));row.addWidget(self.overlay)
        # State behind the buttons above; these two are not shown.
        self.mask_enabled=QCheckBox();self.mask_enabled.toggled.connect(lambda b:self.layer_property('enabled',b))
        self.operation=QComboBox();self.operation.addItem(tr('더하기'),'add');self.operation.addItem(tr('빼기'),'subtract');self.operation.addItem(tr('교차'),'intersect')
        self.tool_hint=label(box,'')
        # Options of the tool in use only (brush: all four; radial: feather; range: refine).
        self.tool_options=QWidget();options=QVBoxLayout(self.tool_options);options.setContentsMargins(0,0,0,0);box.addWidget(self.tool_options)
        options.addWidget(section(tr('도구 옵션')))
        self.radius=Adjustment('radius',tr('크기'),.002,.25,1000);self.radius.set_value(.025)
        self.radius.changed.connect(lambda _,v:self.brush_cursor());options.addWidget(self.radius)
        self.feather=Adjustment('feather',tr('페더'),0,100);self.feather.set_value(70);options.addWidget(self.feather)
        self.feather.changed.connect(lambda _,v:self.brush_cursor())
        self.flow=Adjustment('flow',tr('플로우'),1,100);self.flow.set_value(100);options.addWidget(self.flow)
        self.density=Adjustment('density',tr('밀도'),1,100);self.density.set_value(100);options.addWidget(self.density)
        for control,value in ((self.radius,.025),(self.feather,70),(self.flow,100),(self.density,100)):control.default_value=value
        self.auto_mask=QCheckBox(tr('자동 마스크'));options.addWidget(self.auto_mask)
        self.auto_mask.setToolTip(tr('처음 누른 곳과 비슷한 색 안에서만 칠합니다.'))
        self.auto_tolerance=Adjustment('auto_tolerance',tr('자동 마스크 허용량'),.02,1,100);self.auto_tolerance.set_value(.18);self.auto_tolerance.default_value=.18;options.addWidget(self.auto_tolerance)
        self.brush_note=label(options,tr('Alt: 지우기 · [ / ]: 크기 · Esc: 끝내기'))
        self.tolerance=Adjustment('tolerance',tr('다듬기'),.02,1.7,100);self.tolerance.set_value(.3);options.addWidget(self.tolerance)
        self.range_pick_note=label(options,tr('점이나 작은 사각형을 선택하세요. Shift+클릭: 같은 범위에 색 추가(최대 5개) · Esc: 종료'))
        # Numeric luminance range (used by add_luma).
        self.low=Adjustment('low',tr('최소 밝기'),0,100);self.low.set_value(25)
        self.high=Adjustment('high',tr('최대 밝기'),0,100);self.high.set_value(75)
        self.range_editor=QWidget();range_box=QVBoxLayout(self.range_editor);range_box.setContentsMargins(0,0,0,0);box.addWidget(self.range_editor)
        range_box.addWidget(section(tr('범위 다듬기')))
        self.range_controls={}
        for key,title,lo,hi,scale,default in [('low','광도 최소',0,100,1,25),('high','광도 최대',0,100,1,75),('softness','광도 경계 페더',.1,100,10,10),('tolerance','다듬기',.02,1.7,100,.3)]:
            c=Adjustment(key,title,lo,hi,scale);c.default_value=default;c.changed.connect(self.range_changed)
            c.committed.connect(self.w.finish_interaction);range_box.addWidget(c);self.range_controls[key]=c
        self.range_samples=QComboBox();range_box.addWidget(self.range_samples)
        self.range_add=action(range_box,tr('선택한 범위에 색 추가'),lambda:self.mode('color_range_add'))
        self.range_remove=action(range_box,tr('고른 색 제외'),self.remove_range_sample)
        self.range_note=label(range_box,tr('목록에서 밝기 또는 색상 범위를 선택하세요.'))
        self.range_editor.hide()
        self.done_button=action(box,tr('완료'),lambda:self.mode(''))
        # Adjustments of the selected mask, named as in the main panels.
        self.adjust_host=QWidget();adjust=QVBoxLayout(self.adjust_host);adjust.setContentsMargins(0,0,0,0);box.addWidget(self.adjust_host)
        self.opacity=Adjustment('opacity',tr('양'),0,100);adjust.addWidget(self.opacity)
        self.opacity.changed.connect(lambda _,v:self.layer_property('opacity',v/100))
        self.local_controls={}
        for title,controls in [('톤',[('exposure','노출',-5,5,100),('contrast','대비',-100,100,1),('highlights','하이라이트',-100,100,1),
                    ('shadows','그림자',-100,100,1),('whites','흰색 계열',-100,100,1),('blacks','검정 계열',-100,100,1)]),
                ('색상',[('temperature','색온도',-100,100,1),('tint','색조',-100,100,1),('hue','색상',-180,180,1),('saturation','채도',-100,100,1)]),
                ('효과',[('texture','텍스처',-100,100,1),('clarity','부분 대비',-100,100,1),('dehaze','안개 제거',-100,100,1)]),
                ('세부',[('sharpen','선명하게',0,100,1),('noise_luma','노이즈',0,100,1),('noise_color','색상 노이즈',0,100,1)])]:
            adjust.addWidget(section(tr(title)))
            for key,name,lo,hi,scale in controls:
                c=Adjustment(key,name,lo,hi,scale);adjust.addWidget(c);c.changed.connect(self.local_changed)
                c.committed.connect(self.w.finish_interaction);self.local_controls[key]=c
        box=self.w.advanced_box(adjust,'커브 · 색상 혼합 · 선택 색상')
        self.local_curve_channel=QComboBox();self.local_curve_channel.addItems([tr('부분 커브 · RGB'),tr('부분 커브 · 빨강'),tr('부분 커브 · 초록'),tr('부분 커브 · 파랑')]);box.addWidget(self.local_curve_channel)
        self.local_curve=ToneCurve();box.addWidget(self.local_curve)
        self.local_curve_channel.currentIndexChanged.connect(self.load_local_curve)
        self.local_curve.changed.connect(self.local_curve_changed);self.local_curve.committed.connect(self.w.finish_interaction)
        label(box,tr('부분 색상표 · 선택한 마스크 안에서 색별로 조절합니다.'))
        self.local_hsl_channel=QComboBox();self.local_hsl_channel.addItems([tr('빨강'),tr('주황'),tr('노랑'),tr('초록'),tr('청록'),tr('파랑'),tr('보라'),tr('자홍')]);box.addWidget(self.local_hsl_channel)
        self.local_hsl_channel.currentIndexChanged.connect(self.load_local_hsl)
        self.local_hsl_controls=[]
        for column,title in enumerate(['색상표 색상','색상표 채도','색상표 명도']):
            c=Adjustment(str(column),title,-100,100);box.addWidget(c)
            c.changed.connect(self.local_hsl_changed);c.committed.connect(self.w.finish_interaction);self.local_hsl_controls.append(c)
        self.local_point_add=action(box,tr('부분 선택 색상 · 사진에서 찍기'),lambda:self.mode('local_point_color'))
        self.local_point_note=label(box,tr('선택한 마스크 안에서 색을 고르세요.'))
        self.local_points=QComboBox();box.addWidget(self.local_points);self.local_points.currentIndexChanged.connect(self.load_local_point)
        self.local_point_controls={}
        for key,title,lo,hi,scale in [('range','선택 색상 범위',.01,.5,100),('saturation_range','선택 채도 범위',.01,1,100),('lightness_range','선택 명도 범위',.01,1,100),('hue','선택 색상 이동',-100,100,1),('saturation','선택 채도 보정',-100,100,1),('lightness','선택 명도 보정',-100,100,1)]:
            c=Adjustment(key,title,lo,hi,scale);box.addWidget(c);self.local_point_controls[key]=c
            c.changed.connect(self.local_point_changed);c.committed.connect(self.w.finish_interaction)
            c.default_value={'range':.12,'saturation_range':.6,'lightness_range':.5}.get(key,0)
        self.local_point_remove=action(box,tr('부분 선택 색상 제거'),self.remove_local_point)
        self.local_point_preview=QCheckBox(tr('부분 선택 색상 범위 표시'));box.addWidget(self.local_point_preview)
        self.local_point_preview.toggled.connect(lambda _:self.preview_ready(self.w.view.on_screen))
        label(box,tr('마스크 영역 안의 색을 클릭하세요. 파란 표시는 색상 범위와 마스크가 겹치는 부분입니다.'))
        adjust.addStretch()

    def build_retouch(self):
        _,box=self.w.tab_page(tr('복구'))
        label(box,tr('원본을 보존하며 복구 동작을 보정 기록에 저장합니다.'))
        action(box,tr('먼지 자동 감지 · 후보 확인…'),self.detect_dust)
        action(box,tr('여러 사진의 먼지 감지…'),self.detect_dust_batch)
        label(box,tr('검은 점과 흰 먼지를 찾아, 확인한 후보만 한 번에 제거합니다.'))
        for title,kind in [('복제 도장','clone'),('힐링 브러시','heal'),('먼지·흠집 제거','inpaint'),('적목 보정','red_eye')]:
            action(box,title,lambda _,k=kind:self.mode(k))
        label(box,tr('힐링: 지울 곳을 여러 번 칠한 뒤 실행하세요. 붉은 표시는 아직 적용되지 않은 영역입니다. Alt+클릭으로 원본을 직접 지정할 수 있습니다.'))
        self.retouch_source_note=label(box,tr('힐링 원본: 자동 선택'))
        action(box,tr('힐링 원본 자동 선택으로 복귀'),self.automatic_healing_source)
        self.retouch_radius=Adjustment('radius',tr('복구 브러시 크기'),.002,.12,1000);self.retouch_radius.set_value(.015);box.addWidget(self.retouch_radius)
        self.retouch_radius.changed.connect(lambda _,v:self.retouch_cursor())
        self.retouch_feather=Adjustment('feather',tr('복구 경계 흐림'),0,100);self.retouch_feather.set_value(60);box.addWidget(self.retouch_feather)
        self.retouch_feather.changed.connect(lambda _,v:self.retouch_cursor())
        self.healing_count=label(box,tr('실행 대기: {0}개', '0'))
        self.healing_apply=action(box,tr('실행 · 표시한 곳 제거'),self.apply_healing)
        self.healing_apply.setObjectName('primary')
        row=QHBoxLayout();box.addLayout(row)
        self.healing_remove=action(row,tr('마지막 표시 취소'),self.remove_healing)
        self.healing_clear=action(row,tr('표시 모두 취소'),self.clear_healing)
        label(box,tr('Enter: 실행 · Esc: 현재 획 / 대기 표시 취소. 다른 사진으로 이동하면 실행 전 표시는 취소됩니다.'))
        self.update_healing_controls()
        self.retouch_count=label(box,tr('복구 동작 0개'))
        action(box,tr('마지막 복구 제거'),self.remove_retouch)
        action(box,tr('도구 끝내기'),lambda:self.mode(''));box.addStretch()

    def update_healing_controls(self):
        count=len(self.healing_strokes)
        self.healing_count.setText(tr('실행 대기: {0}개', str(count)))
        self.healing_apply.setEnabled(bool(count));self.healing_remove.setEnabled(bool(count))
        self.healing_clear.setEnabled(bool(count) or self.pending_stroke is not None and self.pending_stroke.get('staged',False))

    def clear_healing(self):
        self.w.view.cancel_stroke();self.cancel_stroke()
        self.healing_strokes=[];self.healing_context=None;self.healing_mask=None
        self.healing_overlay_key=None;self.w.view.healing_overlay=None
        self.update_healing_controls();self.w.view.viewport().update()

    def remove_healing(self):
        self.w.view.cancel_stroke();self.cancel_stroke()
        if self.healing_strokes:self.healing_strokes.pop()
        self.healing_mask=None;self.healing_overlay_key=None
        self.update_healing_controls();self.update_healing_overlay()

    def apply_healing(self):
        if self.w.source is None:return
        if self.healing_context!=(self.w.current_id,self.w.load_version):
            self.clear_healing();return
        self.w.view.cancel_stroke();self.cancel_stroke()
        if not self.healing_strokes:return
        self.w.commit()
        count=len(self.healing_strokes)
        values=deepcopy(self.w.settings['retouch'])+deepcopy(self.healing_strokes)
        self.clear_healing()
        # Spatial repairs belong to this photo, even when auto sync is enabled.
        syncing=self.w.manager.syncing;self.w.manager.syncing=True
        try:
            self.w.set_setting('retouch',values)
            self.w.commit(tr('힐링 {0}개 실행',str(count)))
        finally:self.w.manager.syncing=syncing
        self.w.load_controls();self.w.render()
        self.w.statusBar().showMessage(tr('표시한 {0}곳에 힐링을 적용했습니다. 한 번의 실행 취소로 되돌릴 수 있습니다.',str(count)))

    def update_healing_overlay(self):
        """Show coverage only; never run healing or save a preview while marking."""
        view=self.w.view
        draft=self.pending_stroke if self.stroke_valid() and self.pending_stroke.get('staged') else None
        if (self.w.source is None or view.tool_mode!='heal' or self.w.before_button.isChecked()
                or not self.healing_strokes and draft is None):
            view.healing_overlay=None;self.healing_overlay_key=None;view.viewport().update();return
        self.source_point([.5,.5])
        key=(self.coordinate_key,self.healing_generation,len(self.healing_strokes))
        if key==self.healing_overlay_key and view.healing_overlay is not None:return
        from .processing import brush_mask
        from PySide6.QtGui import QImage
        import cv2
        shape=self.w.live_source.shape
        if self.healing_mask is None:
            self.healing_mask=np.zeros(shape[:2],np.float32)
            for op in self.healing_strokes:
                np.maximum(self.healing_mask,brush_mask(shape,op['points'],op['radius'],op['feather']),out=self.healing_mask)
        mask=self.healing_mask
        if draft:
            op=draft['component'];mask=np.maximum(mask,brush_mask(shape,op['points'],op['radius'],op['feather']))
        h,w=shape[:2];coords=self.coordinates
        coverage=cv2.remap(mask,np.float32(coords[...,0]*(w-1)),np.float32(coords[...,1]*(h-1)),cv2.INTER_LINEAR)
        coverage=np.where(coords[...,2]>.9,coverage,0)
        rgba=np.empty((*coverage.shape,4),np.uint8);rgba[...,:3]=[255,80,65];rgba[...,3]=np.uint8(np.clip(coverage,0,1)*140)
        view.healing_overlay=QImage(rgba.data,rgba.shape[1],rgba.shape[0],rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
        self.healing_overlay_key=key;view.viewport().update()

    def retouch_cursor(self):
        if self.w.view.tool_mode not in ('clone','heal','inpaint','red_eye'):return
        self.w.view.brush_radius=self.retouch_radius.slider.value()/1000
        self.w.view.brush_feather=self.retouch_feather.slider.value()
        self.w.view.viewport().update()

    def set_brush_size(self,value):
        control=self.retouch_radius if self.w.view.tool_mode in ('clone','heal','inpaint','red_eye') else self.radius
        control.slider.setValue(round(value*1000))

    def automatic_healing_source(self):
        self.w.view.cancel_stroke();self.donor=None
        self.retouch_source_note.setText(tr('힐링 원본: 자동 선택'))

    def detect_dust(self):
        if self.w.source is None:return
        from .dust_dialog import DustDialog
        from .engine import VIDEO_EXTENSIONS
        from PySide6.QtWidgets import QDialog
        if Path(self.w.catalog.photo(self.w.current_id)['path']).suffix.lower() in VIDEO_EXTENSIONS:
            self.w.statusBar().showMessage(tr('먼지 자동 감지는 사진에서 사용할 수 있습니다.'));return
        self.w.commit();self.mode('');dialog=DustDialog(self.w)
        result=dialog.exec();dialog.deleteLater()
        if result!=QDialog.DialogCode.Accepted or not dialog.operation:return
        if not dialog.valid_source():
            self.w.statusBar().showMessage(tr('사진이나 원본이 바뀌어 먼지 제거를 적용하지 않았습니다.'));return
        values=deepcopy(self.w.settings['retouch']);values.append(dialog.operation)
        syncing=self.w.manager.syncing;self.w.manager.syncing=True
        try:
            self.w.set_setting('retouch',values);self.w.commit(f'먼지 {len(dialog.operation["spots"])}개 제거')
        finally:self.w.manager.syncing=syncing
        self.w.load_controls();self.w.render()
        self.w.statusBar().showMessage(tr('먼지 {0}개를 제거했습니다. 실행 취소로 되돌릴 수 있습니다.', f"{len(dialog.operation['spots'])}"))

    def detect_dust_batch(self):
        from .dust_batch_dialog import DustBatchDialog
        self.w.commit();self.mode('')
        dialog=DustBatchDialog(self.w)
        dialog.exec();dialog.deleteLater()

    def mode(self,kind):
        if self.w.source is None:return
        self.color_pick_generation+=1
        if kind=='local_point_color' and self.current_layer() is None:
            self.w.statusBar().showMessage(tr('먼저 마스크와 선택 영역을 추가하세요.'));return
        if kind=='color_range_add' and (self.current_component() or {}).get('type')!='color':
            self.range_pick_note.setText(tr('목록에서 색상 범위를 먼저 선택하세요.'));return
        if kind in ('point_color','local_point_color','color_range','color_range_add','luma_range','clone','heal','inpaint','red_eye') and self.w.before_button.isChecked():self.w.before_button.setChecked(False)
        if self.w.crop_button.isChecked():self.w.cancel_crop()
        self.w.wb_button.setChecked(False);self.w.view.sample_mode=False
        self.w.set_mode(0);self.w.view.set_tool(kind)
        self.w.view.brush_radius=(self.retouch_radius if kind in ('clone','heal','inpaint','red_eye') else self.radius).slider.value()/1000
        self.w.view.brush_feather=(self.retouch_feather if kind in ('clone','heal','inpaint','red_eye') else self.feather).slider.value()
        self.preview_ready(self.w.view.on_screen)
        self.w.statusBar().showMessage((tr('사진에서 드래그하세요. Alt: 지우기 · [ / ]: 크기') if kind=='brush' else tr('사진에서 드래그하세요. Alt+클릭: 복구 원본 지정')) if kind else tr('도구를 종료했습니다.'))
        if kind=='heal':self.w.statusBar().showMessage(tr('여러 곳을 칠한 뒤 실행 또는 Enter를 누르세요. Alt+클릭: 원본 지정 · [ / ]: 크기'))
        if kind in ('point_color','local_point_color'):self.w.statusBar().showMessage(tr('보정할 색을 사진에서 클릭하세요.') if kind=='point_color' else tr('선택한 마스크 안에서 보정할 색을 클릭하세요.'))
        if kind in ('color_range','color_range_add','luma_range'):
            self.range_pick_note.setText(tr('사진의 점이나 작은 사각형을 선택하세요. Shift+클릭으로 색을 더하고 Esc로 마칩니다.'))
            self.w.statusBar().showMessage(tr('사진에서 클릭하거나 사각형을 드래그하세요.'))

    def tab_changed(self,index):
        self.color_pick_generation+=1
        self.w.view.set_tool('')
        self.preview_ready(self.w.view.on_screen)

    def source_point(self,point):
        s=self.w.settings
        keys=GEOMETRY_KEYS
        key=(self.w.load_version,json.dumps({k:s[k] for k in keys},sort_keys=True))
        if key!=self.coordinate_key:
            h,w=self.w.live_source.shape[:2];y,x=np.mgrid[0:h,0:w].astype(np.float32)
            self.coordinates=geometry(np.stack([x/max(1,w-1),y/max(1,h-1),np.ones_like(x),np.zeros_like(x)],axis=-1),s)
            self.coordinate_key=key
        h,w=self.coordinates.shape[:2]
        value=self.coordinates[min(h-1,round(point[1]*(h-1))),min(w-1,round(point[0]*(w-1)))]
        return np.clip(value[:2],0,1).tolist() if value[2]>.9 else None

    def stroke(self,points,alt):
        if self.w.source is None:return
        kind=self.w.view.tool_mode
        pending=self.pending_stroke
        if pending is not None and not self.stroke_valid():
            self.cancel_stroke();return
        frozen=deepcopy(pending['component']) if pending else None
        self.cancel_stroke()
        if kind in ('color_range','color_range_add','luma_range'):
            # Range sampling checks transformed borders in its background job.
            self.pick_range(points,kind,add=kind=='color_range_add' or getattr(self.w.view,'tool_shift',False));return
        source_points=self.source_points(points)
        if not source_points:
            self.w.statusBar().showMessage(tr('변형 후 생긴 빈 가장자리 대신 사진 안쪽을 선택하세요.'));return
        if kind in ('point_color','local_point_color'):
            self.pick_point_color(points[0],local=kind=='local_point_color');return
        if kind=='guide':
            self.guide_points.append(source_points[0])
            self.w.statusBar().showMessage(tr('가이드 모서리 {0}/4', f'{len(self.guide_points)}'))
            if len(self.guide_points)==4:
                pts=np.asarray(self.guide_points,np.float32)
                import cv2
                if cv2.isContourConvex(pts.reshape(-1,1,2)) and cv2.contourArea(pts)>.005:
                    self.w.set_setting('guides',self.guide_points)
                else:QMessageBox.information(self.w,tr('가이드'),tr('네 점이 겹치지 않는 사각형이 되도록 다시 선택해 주세요.'))
                self.guide_points=[];self.mode('')
        elif kind in ('clone','heal','inpaint','red_eye'):
            if alt:
                self.donor=source_points[0];self.w.statusBar().showMessage(tr('복구 원본 위치를 지정했습니다.'))
                self.retouch_source_note.setText(tr('힐링 원본: 직접 지정 (Alt+클릭)'));return
            if kind=='clone' and self.donor is None:
                self.w.statusBar().showMessage(tr('Alt+클릭으로 복구 원본 위치부터 지정하세요.'));return
            values=deepcopy(self.w.settings['retouch'])
            operation=frozen or self.make_retouch(kind,source_points)
            operation['points']=source_points
            if kind=='heal':
                self.healing_context=(self.w.current_id,self.w.load_version)
                self.healing_strokes.append(operation);self.healing_overlay_key=None
                if self.healing_mask is not None:
                    from .processing import brush_mask
                    np.maximum(self.healing_mask,brush_mask(self.w.live_source.shape,source_points,operation['radius'],operation['feather']),out=self.healing_mask)
                self.update_healing_controls();self.update_healing_overlay();return
            values.append(operation)
            self.w.set_setting('retouch',values);self.retouch_count.setText(tr('복구 동작 {0}개', f'{len(values)}'))
        elif kind in ('brush','linear','radial'):
            component=frozen or self.make_component(kind,source_points,alt)
            component['points']=source_points
            self.add_component(component)
        self.w.commit('부분 보정');self.w.render()

    def make_retouch(self,kind,points):
        return dict(type=kind,version=2,points=points,source=deepcopy(self.donor),
                    radius=self.retouch_radius.slider.value()/1000,feather=self.retouch_feather.slider.value())

    def make_component(self,kind,points,alt):
        component=dict(type=kind,points=points,radius=self.radius.slider.value()/1000,
            feather=self.feather.slider.value(),operation=self.operation.currentData())
        if kind=='brush':
            component.update(paint_version=2,flow=self.flow.slider.value(),density=self.density.slider.value())
            if alt:component['operation']='subtract'
            if self.auto_mask.isChecked():component.update(auto_mask=True,auto_tolerance=self.auto_tolerance.slider.value()/100,seed=points[0])
        return component

    def source_points(self,points):
        if not points:return []
        self.source_point(points[0])
        h,w=self.coordinates.shape[:2]
        positions=np.rint(np.clip(np.asarray(points),0,1)*[w-1,h-1]).astype(np.intp)
        values=self.coordinates[positions[:,1],positions[:,0]]
        return np.clip(values[values[:,2]>.9,:2],0,1).tolist()

    def stroke_valid(self):
        p=self.pending_stroke
        return p is not None and p['photo']==self.w.current_id and p['load']==self.w.load_version and p['base']==self.w.settings and p['layer']==self.layer_index

    def preview_stroke(self,points,alt):
        if self.w.source is None or not points:return
        kind=self.w.view.tool_mode;retouch=kind in ('clone','heal','inpaint','red_eye')
        if retouch and (alt or (kind=='clone' and self.donor is None)):return
        if self.pending_stroke is not None and not self.stroke_valid():
            self.w.view.cancel_stroke();return
        source_points=self.source_points(points)
        if not source_points:return
        if self.pending_stroke is None:
            self.w.commit()
            if kind!='heal':self.stroke_generation+=1
            self.pending_stroke=dict(photo=self.w.current_id,load=self.w.load_version,base=deepcopy(self.w.settings),
                layer=self.layer_index,retouch=retouch,staged=kind=='heal',component=self.make_retouch(kind,source_points) if retouch else self.make_component(kind,source_points,alt))
            if not retouch:self.brush_cursor()
        else:self.pending_stroke['component']['points']=source_points
        if kind=='heal':
            self.healing_generation+=1;self.update_healing_controls();self.update_healing_overlay();return
        self.w.render_version+=1
        if not self.w.preview_timer.isActive():self.w.preview_timer.start()
        self.w.refine_timer.stop()

    def cancel_stroke(self):
        if self.pending_stroke is None:return
        staged=self.pending_stroke.get('staged',False)
        if staged:
            self.pending_stroke=None;self.healing_generation+=1;self.w.view.tool_points=[]
            self.update_healing_controls();self.update_healing_overlay();return
        self.pending_stroke=None;self.stroke_generation+=1;self.w.render_version+=1
        self.w.view.tool_points=[]
        self.brush_cursor()
        if self.w.source is not None and not self.w.closing:
            if not self.w.preview_timer.isActive():self.w.preview_timer.start()
            self.w.refine_timer.start(25)

    def preview_settings(self,settings):
        if not self.stroke_valid():return settings
        p=self.pending_stroke
        if p.get('staged'):return settings
        if p.get('retouch'):
            settings['retouch'].append(deepcopy(p['component']));return settings
        layers=settings['masks']
        if 0<=p['layer']<len(layers):layer=layers[p['layer']]
        else:
            layer=dict(name='마스크',enabled=True,opacity=1,components=[],adjustments={});layers.append(layer)
        layer['components'].append(deepcopy(p['component']))
        return settings

    def current_layer(self):
        layers=self.w.settings['masks']
        return layers[self.layer_index] if 0<=self.layer_index<len(layers) else None

    def new_layer(self):
        self.w.commit()
        layers=deepcopy(self.w.settings['masks']);layers.append({'name':tr('마스크 {0}',len(layers)+1),'enabled':True,'opacity':1,'components':[],'adjustments':{}})
        self.w.set_setting('masks',layers);self.layer_index=len(layers)-1;self.component_index=-1;self.load_layers()

    def add_component(self,component):
        if self.current_layer() is None:self.new_layer()
        layers=deepcopy(self.w.settings['masks']);component.setdefault('operation',self.operation.currentData())
        layers[self.layer_index]['components'].append(component);self.w.set_setting('masks',layers)
        self.component_index=len(layers[self.layer_index]['components'])-1
        self.load_layers()

    def add_luma(self):
        lo,hi=sorted([self.low.slider.value()/100,self.high.slider.value()/100])
        self.add_component({'type':'luma','range':[lo,hi],'softness':.1});self.w.finish_interaction()

    def load_range_editor(self):
        self.range_loading=True;component=self.current_component() or {};kind=component.get('type')
        active=kind in ('color','luma');self.range_editor.setVisible(active)
        low,high=component.get('range',[.25,.75])
        values=dict(low=low*100,high=high*100,softness=component.get('softness',.1)*100,tolerance=component.get('tolerance',.3))
        for key,c in self.range_controls.items():c.set_value(values[key]);c.setVisible(active and (key=='tolerance')==(kind=='color'))
        samples=component.get('samples',[component.get('rgb',[.5,.5,.5])]) if kind=='color' else []
        self.range_samples.clear()
        for i in range(len(samples)):self.range_samples.addItem(tr('색 {0} / {1}', f'{i + 1}', f'{len(samples)}'))
        for control in (self.range_samples,self.range_add,self.range_remove):control.setVisible(kind=='color')
        self.range_add.setEnabled(len(samples)<5);self.range_remove.setEnabled(len(samples)>1)
        self.range_note.setText(tr('같은 범위 안의 색들을 함께 선택합니다. 허용량을 낮추면 선택이 좁아집니다.') if kind=='color' else tr('범위와 경계 흐림을 조절하며 사진과 선택 영역을 확인하세요.'))
        self.range_loading=False

    def range_changed(self,key,value):
        if self.loading or self.range_loading or self.current_component() is None:return
        component=deepcopy(self.current_component());kind=component.get('type')
        if kind=='color' and key=='tolerance':component['tolerance']=value
        elif kind=='luma' and key in ('low','high','softness'):
            if key=='softness':component['softness']=value/100
            else:
                low,high=component.get('range',[.25,.75])
                if key=='low':low=value/100;high=max(low,high)
                else:high=value/100;low=min(low,high)
                component['range']=[low,high]
                self.range_controls['low'].set_value(low*100);self.range_controls['high'].set_value(high*100)
        else:return
        layers=deepcopy(self.w.settings['masks']);layers[self.layer_index]['components'][self.component_index]=component
        self.w.set_setting('masks',layers)

    def remove_range_sample(self):
        component=deepcopy(self.current_component() or {});samples=component.get('samples',[]);index=self.range_samples.currentIndex()
        if component.get('type')=='color' and len(samples)>1 and 0<=index<len(samples):
            samples.pop(index);component['rgb']=samples[0];self.replace_component(component)

    def pick_range(self,points,kind,add=False):
        from .range_mask import sample_selection
        color=kind!='luma_range';component=self.current_component() or {}
        append=color and add and component.get('type')=='color'
        samples=component.get('samples',[component.get('rgb',[.5,.5,.5])]) if append else []
        if len(samples)>=5:self.range_pick_note.setText(tr('색은 최대 5개입니다. 고른 색을 제외한 뒤 추가하세요.'));return
        self.w.commit();self.color_pick_generation+=1;token=self.color_pick_generation
        settings=deepcopy(self.w.settings);ident=self.w.current_id;version=self.w.load_version
        layer=self.layer_index if self.current_layer() is not None else None;selection=self.component_index
        operation=self.operation.currentData();tolerance=self.tolerance.slider.value()/100
        source=self.w.full_source if self.w.full_source is not None and not self.w.view.fit_mode else self.w.source
        original_size=(source.shape[1],source.shape[0]) if source is self.w.full_source else self.w.original_size
        self.color_pick_running=True;self.color_pick_token=token;self.range_pick_note.setText(tr('마스크를 적용하기 전 색과 밝기를 읽는 중…'))
        def valid():
            return not self.w.closing and token==self.color_pick_generation and self.w.current_id==ident and self.w.load_version==version and self.w.settings==settings and self.component_index==selection and (self.layer_index if self.current_layer() is not None else None)==layer and self.w.view.tool_mode==kind
        def ready(result):
            if self.color_pick_token==token:self.color_pick_running=False
            if not valid():
                if not self.w.closing and self.color_pick_token==token:self.range_pick_note.setText(tr('대상이 바뀌어 선택을 취소했습니다. 다시 선택하세요.'))
                return
            layers=deepcopy(settings['masks']);target=layer
            if target is None:target=len(layers);layers.append(dict(name=tr('마스크 {0}',target+1),enabled=True,opacity=1,components=[],adjustments={}))
            if color:
                colors=deepcopy(samples)
                for rgb in result['colors']:
                    if len(colors)<5 and not any(np.allclose(rgb,old,atol=1e-6,rtol=0) for old in colors):colors.append(rgb)
                value={**component,'samples':colors,'rgb':colors[0]} if append else dict(type='color',samples=colors,rgb=colors[0],tolerance=tolerance,operation=operation)
            else:value=dict(type='luma',range=result['range'],softness=.1,operation=operation)
            if append:layers[target]['components'][selection]=value;index=selection
            else:layers[target]['components'].append(value);index=len(layers[target]['components'])-1
            self.w.set_setting('masks',layers);self.layer_index=target;self.component_index=index;self.load_layers()
            self.w.commit('색상 범위 선택' if color else '밝기 범위 선택');self.w.render()
            self.range_pick_note.setText(tr('색을 선택했습니다. Shift+클릭으로 추가하거나 목록에서 범위를 다듬으세요.') if color else tr('밝기 범위를 선택했습니다. 목록에서 범위와 경계 흐림을 다듬으세요.'))
        def failed(error):
            if self.color_pick_token==token:self.color_pick_running=False
            if valid():self.range_pick_note.setText(str(error))
        self.w.spawn(lambda:sample_selection(source,settings,points,layer,original_size=original_size),ready,failed)

    def remove_component(self):
        if self.current_layer() is None:return
        layers=deepcopy(self.w.settings['masks']);components=layers[self.layer_index]['components']
        if 0<=self.component_index<len(components):
            self.w.commit();components.pop(self.component_index);self.component_index=min(self.component_index,len(components)-1)
            self.w.set_setting('masks',layers);self.w.finish_interaction();self.load_layers()

    def layer_property(self,key,value):
        if self.loading or self.current_layer() is None:return
        layers=deepcopy(self.w.settings['masks']);layers[self.layer_index][key]=value;self.w.set_setting('masks',layers)

    def local_changed(self,key,value):
        if self.loading or self.current_layer() is None:return
        values=deepcopy(self.current_layer().get('adjustments',{}));values[key]=value;self.layer_property('adjustments',values)

    def rename_layer(self):
        if self.current_layer() is None:return
        text,ok=QInputDialog.getText(self.w,tr('마스크 이름'),tr('이름'),text=self.current_layer().get('name',''))
        if ok and text.strip():self.layer_property('name',text.strip());self.load_layers()

    def remove_layer(self):
        if self.current_layer() is None:return
        values=deepcopy(self.w.settings['masks']);values.pop(self.layer_index);self.layer_index=max(0,self.layer_index-1)
        self.w.set_setting('masks',values);self.w.finish_interaction();self.load_layers()

    def select_layer(self,index):
        if self.loading:return
        self.color_pick_generation+=1
        self.w.commit();self.layer_index=index;self.component_index=-1;self.w.view.set_tool('');self.load_layer();self.sync_tree();self.preview_ready(self.w.view.on_screen)

    def tree_item(self,layer,component=-1):
        if not 0<=layer<self.mask_tree.topLevelItemCount():return None
        item=self.mask_tree.topLevelItem(layer)
        return item.child(component) if 0<=component<item.childCount() else item

    def sync_tree(self):
        """Show the current mask and component as the tree's selection without acting on it."""
        item=self.tree_item(self.layer_index,self.component_index)
        if item is None or item is self.mask_tree.currentItem():return
        was=self.loading;self.loading=True
        if item.parent() is not None:item.parent().setExpanded(True)
        else:item.setExpanded(True)
        self.mask_tree.setCurrentItem(item);self.loading=was

    def tree_selected(self,item,_=None):
        if self.loading or item is None:return
        role=Qt.ItemDataRole.UserRole;layer=item.data(0,role);component=item.data(0,role+1)
        if layer!=self.layer_index:self.select_layer(layer)
        if component!=self.component_index:self.select_component(component)
        chosen=self.current_component()
        # Selecting a gradient lets it be dragged straight away.
        if chosen is not None and chosen.get('type') in ('linear','radial') and self.w.view.tool_mode in ('','edit_component'):self.edit_component_handles()
        self.update_mask_ui()

    def tree_checked(self,item,_=0):
        role=Qt.ItemDataRole.UserRole
        if self.loading or item.data(0,role+1)!=-1:return
        # Deferred: rebuilding the list inside its own signal would delete the item being handled.
        index,enabled=item.data(0,role),item.checkState(0)==Qt.CheckState.Checked
        QTimer.singleShot(0,lambda:self.set_layer_enabled(index,enabled))

    def tree_double_clicked(self,item,_=0):
        # Deferred for the same reason as tree_checked.
        QTimer.singleShot(0,self.rename_layer if item.data(0,Qt.ItemDataRole.UserRole+1)==-1 else self.edit_component)

    def set_layer_enabled(self,index,enabled):
        layers=deepcopy(self.w.settings['masks'])
        if not 0<=index<len(layers) or layers[index].get('enabled',True)==enabled:return
        self.w.commit();layers[index]['enabled']=enabled;self.w.set_setting('masks',layers);self.w.finish_interaction();self.load_layers()

    def set_operation(self,operation):
        self.operation.setCurrentIndex(self.operation.findData(operation))

    def create_mask(self,kind):
        """Create New Mask: a new mask whose first component is drawn with the chosen tool."""
        if self.w.source is None:return
        self.new_layer();self.fresh_layer=self.layer_index;self.set_operation('add');self.mode(kind)

    def extend_mask(self,kind,operation):
        """Add to, subtract from or intersect the selected mask with a new component of this kind."""
        if self.current_layer() is None:self.create_mask(kind);return
        self.set_operation(operation);self.mode(kind)

    def drop_fresh_layer(self):
        """A mask made by Create New Mask that never received a component is removed when its tool ends."""
        index,self.fresh_layer=self.fresh_layer,None;layers=self.w.settings['masks']
        if index is None or self.w.source is None or not 0<=index<len(layers) or layers[index].get('components'):return
        values=deepcopy(layers);values.pop(index);self.layer_index=max(0,min(self.layer_index,len(values)-1));self.component_index=-1
        self.w.set_setting('masks',values);self.w.finish_interaction();self.load_layers()

    def mask_tool_changed(self,mode):
        if not mode:self.drop_fresh_layer()
        self.update_mask_ui()

    def toggle_overlay(self):
        if self.w.tabs.is_open('masks'):self.overlay.toggle()

    def mask_menu(self):
        """The "..." menu for the selected mask (and component)."""
        from PySide6.QtWidgets import QMenu
        menu=QMenu(self.w);layer=self.current_layer()
        if layer is None:
            menu.addAction(tr('먼저 마스크를 만드세요.')).setEnabled(False);return menu
        index=self.layer_index;shown=layer.get('enabled',True)
        menu.addAction(tr('이름 바꾸기…'),self.rename_layer)
        menu.addAction(tr('마스크 복제'),self.duplicate_layer)
        menu.addAction(tr('마스크 숨기기') if shown else tr('마스크 표시'),lambda:self.set_layer_enabled(index,not shown))
        menu.addAction(tr('마스크 반전'),self.invert.toggle)
        intersect=menu.addMenu(tr('다음과 마스크 교차'))
        for kind,title in self.MASK_KINDS:
            if kind is None:intersect.addSeparator()
            else:intersect.addAction(tr(title),lambda k=kind:self.extend_mask(k,'intersect'))
        menu.addAction(tr('위로 이동'),lambda:self.move_layer(-1));menu.addAction(tr('아래로 이동'),lambda:self.move_layer(1))
        menu.addAction(tr('마스크 삭제'),self.remove_layer)
        if self.current_component() is not None:
            menu.addSeparator();menu.addAction(tr('선택한 구성 요소')).setEnabled(False)
            menu.addAction(tr('세부 편집…'),self.edit_component)
            menu.addAction(tr('구성 요소 복제'),self.duplicate_component)
            menu.addAction(tr('구성 요소 위로'),lambda:self.move_component(-1));menu.addAction(tr('구성 요소 아래로'),lambda:self.move_component(1))
            menu.addAction(tr('구성 요소 삭제'),self.remove_component)
        return menu

    def update_mask_ui(self):
        """Show only what applies now: the active tool's options, and adjustments once a mask exists."""
        tool=self.w.view.tool_mode;layer=self.current_layer();has=layer is not None
        for control in (self.add_button,self.subtract_button,self.more_button,self.invert):control.setEnabled(has)
        self.adjust_host.setVisible(has)
        rows=sum(1+(item.childCount() if item.isExpanded() else 0) for item in (self.mask_tree.topLevelItem(i) for i in range(self.mask_tree.topLevelItemCount())))
        self.mask_tree.setVisible(rows>0);self.mask_tree.setFixedHeight(min(230,10+34*max(1,rows)))
        brush=tool=='brush';radial=tool=='radial';picking=tool in ('color_range','color_range_add','luma_range')
        self.tool_options.setVisible(brush or radial or picking)
        for control in (self.radius,self.flow,self.density,self.auto_mask,self.auto_tolerance,self.brush_note):control.setVisible(brush)
        self.feather.setVisible(brush or radial);self.tolerance.setVisible(tool=='color_range');self.range_pick_note.setVisible(picking)
        self.done_button.setVisible(tool in ('brush','linear','radial','color_range','color_range_add','luma_range','edit_component'))
        names={kind:title for kind,title in self.MASK_KINDS if kind};names['color_range_add']='색상 범위'
        if tool in names:
            verb={'add':'추가','subtract':'빼기','intersect':'교차'}[self.operation.currentData() or 'add']
            how={'brush':'사진 위를 칠하세요.','linear':'사진 위에서 드래그하세요.','radial':'사진 위에서 드래그해 타원을 그리세요.',
                 'luma_range':'사진에서 밝기를 고를 곳을 클릭하거나 드래그하세요.'}.get(tool,'사진에서 색을 고를 곳을 클릭하거나 드래그하세요.')
            text=tr('{0} · {1}: {2}',layer.get('name','마스크') if has else tr('새 마스크'),tr(verb),tr(names[tool]))+'\n'+tr(how)
        elif tool=='edit_component':text=tr('핸들을 드래그해 위치와 범위를 바꾸세요.')
        elif not self.w.settings['masks']:text=tr('새 마스크 만들기에서 종류를 고른 뒤 사진 위에 그리세요.')
        else:text=''
        self.tool_hint.setText(text);self.tool_hint.setVisible(bool(text))

    def load_layers(self):
        from PySide6.QtWidgets import QTreeWidgetItem
        from .mask_dialog import NAMES
        self.loading=True;self.mask_tree.clear();role=Qt.ItemDataRole.UserRole
        for i,layer in enumerate(self.w.settings['masks']):
            item=QTreeWidgetItem([layer.get('name','마스크')]);item.setData(0,role,i);item.setData(0,role+1,-1)
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0,Qt.CheckState.Checked if layer.get('enabled',True) else Qt.CheckState.Unchecked)
            self.mask_tree.addTopLevelItem(item);counts={}
            for j,component in enumerate(layer.get('components',[])):
                kind=component.get('type');counts[kind]=counts.get(kind,0)+1
                sign={'add':'+','subtract':'-','intersect':'∩'}.get(component.get('operation','add'),'+')
                name=f'{sign}  {tr(NAMES.get(kind,"선택 영역"))} {counts[kind]}'+(tr(' · 꺼짐') if not component.get('enabled',True) else '')
                child=QTreeWidgetItem([name]);child.setData(0,role,i);child.setData(0,role+1,j);item.addChild(child)
            item.setExpanded(i==self.layer_index)
        self.loading=False;self.load_layer();self.sync_tree()
        if self.w.view.tool_mode=='edit_component':self.edit_component_handles()

    def load_layer(self):
        if self.layer_index>=len(self.w.settings['masks']):self.layer_index=len(self.w.settings['masks'])-1
        self.loading=True;layer=self.current_layer() or {}
        self.mask_enabled.setChecked(layer.get('enabled',True));self.invert.setChecked(layer.get('invert',False))
        self.opacity.set_value(layer.get('opacity',1)*100)
        for key,c in self.local_controls.items():c.set_value(layer.get('adjustments',{}).get(key,0))
        self.load_local_curve()
        self.loading=False;self.load_local_hsl();self.load_local_points();self.load_range_editor();self.update_mask_ui()

    def load_local_hsl(self,*_):
        edit=(self.current_layer() or {}).get('adjustments',{})
        values=edit.get('hsl',[[0,0,0] for _ in range(8)])[self.local_hsl_channel.currentIndex()]
        for c,value in zip(self.local_hsl_controls,values):c.set_value(value);c.setEnabled(self.current_layer() is not None)

    def local_hsl_changed(self,key,value):
        if self.loading or self.current_layer() is None:return
        edit=deepcopy(self.current_layer().get('adjustments',{}));groups=edit.setdefault('hsl',[[0,0,0] for _ in range(8)])
        groups[self.local_hsl_channel.currentIndex()][int(key)]=value;self.layer_property('adjustments',edit)

    def local_point_values(self):return (self.current_layer() or {}).get('adjustments',{}).get('point_colors',[])

    def load_local_points(self):
        self.local_points.blockSignals(True);self.local_points.clear()
        for i in range(len(self.local_point_values())):self.local_points.addItem(tr('부분 선택 색상 {0}', f'{i + 1}'))
        self.local_points.setCurrentIndex(min(max(0,self.local_point_index),self.local_points.count()-1));self.local_points.blockSignals(False)
        self.local_point_add.setEnabled(self.current_layer() is not None);self.load_local_point()

    def load_local_point(self,*_):
        self.local_point_index=self.local_points.currentIndex();values=self.local_point_values()
        point=values[self.local_point_index] if 0<=self.local_point_index<len(values) else {}
        for key,c in self.local_point_controls.items():
            c.set_value(point.get(key,{'range':.12,'saturation_range':.6,'lightness_range':.5}.get(key,0)));c.setEnabled(bool(point))
        self.local_point_remove.setEnabled(bool(point));self.local_point_preview.setEnabled(bool(point))
        if self.local_point_preview.isChecked() and not self.loading:self.preview_ready(self.w.view.on_screen)

    def local_point_changed(self,key,value):
        if self.loading or self.current_layer() is None:return
        edit=deepcopy(self.current_layer().get('adjustments',{}));points=edit.get('point_colors',[])
        if 0<=self.local_point_index<len(points):points[self.local_point_index][key]=value;self.layer_property('adjustments',edit)

    def remove_local_point(self):
        if self.current_layer() is None:return
        edit=deepcopy(self.current_layer().get('adjustments',{}));points=edit.get('point_colors',[])
        if 0<=self.local_point_index<len(points):
            points.pop(self.local_point_index);self.layer_property('adjustments',edit);self.w.finish_interaction();self.load_local_points()

    def pick_point_color(self,point,local=False):
        from .point_color import sample
        self.w.commit();self.mode('');token=self.color_pick_generation
        settings=deepcopy(self.w.settings);ident=self.w.current_id;version=self.w.load_version
        layer=self.layer_index if local else None
        source=self.w.full_source if self.w.full_source is not None and not self.w.view.fit_mode else self.w.source
        original_size=(source.shape[1],source.shape[0]) if source is self.w.full_source else self.w.original_size
        note=self.local_point_note if local else self.point_note;note.setText(tr('보정 단계의 색을 읽는 중…'))
        self.color_pick_running=True;self.color_pick_token=token;self.w.statusBar().showMessage(tr('보정 단계의 색을 읽는 중…'))
        def valid():
            return not self.w.closing and token==self.color_pick_generation and self.w.current_id==ident and self.w.load_version==version and self.w.settings==settings and (not local or self.layer_index==layer)
        def ready(rgb):
            if self.color_pick_token==token:self.color_pick_running=False
            if not valid():
                if not self.w.closing and self.color_pick_token==token:note.setText(tr('대상이 바뀌어 색 선택을 취소했습니다. 다시 선택하세요.'))
                return
            value=dict(version=2,rgb=rgb,range=.12,saturation_range=.6,lightness_range=.5,hue=0,saturation=0,lightness=0)
            if local:
                edit=deepcopy(self.current_layer().get('adjustments',{}));points=edit.setdefault('point_colors',[]);points.append(value)
                self.local_point_index=len(points)-1;self.layer_property('adjustments',edit);self.load_local_points()
            else:
                points=deepcopy(settings['point_colors']);points.append(value);self.point_index=len(points)-1
                self.w.set_setting('point_colors',points);self.load_points()
            self.w.commit('부분 선택 색상 추가' if local else '선택 색상 추가');self.w.render()
            note.setText(tr('색을 추가했습니다. 범위 표시로 적용될 영역을 확인하세요.'))
            self.w.statusBar().showMessage(tr('색을 추가했습니다. 색상·채도·명도 범위를 조절하고 범위 표시로 확인하세요.'))
        def failed(error):
            if self.color_pick_token==token:self.color_pick_running=False
            if valid():note.setText(str(error));self.w.statusBar().showMessage(str(error))
        self.w.spawn(lambda:sample(source,settings,point,layer,original_size=original_size),ready,failed)

    def load_local_curve(self):
        from .processing import IDENTITY
        curves=(self.current_layer() or {}).get('adjustments',{}).get('local_curves',[IDENTITY]*4)
        self.local_curve.set_points(curves[self.local_curve_channel.currentIndex()])

    def local_curve_changed(self,points):
        if self.loading or self.current_layer() is None:return
        from .processing import IDENTITY
        edit=deepcopy(self.current_layer().get('adjustments',{}));curves=deepcopy(edit.get('local_curves',[IDENTITY]*4))
        curves[self.local_curve_channel.currentIndex()]=points;edit['local_curves']=curves;self.layer_property('adjustments',edit)

    def brush_cursor(self):
        if self.w.view.tool_mode in ('clone','heal','inpaint','red_eye'):
            self.retouch_cursor();return
        component=self.pending_stroke['component'] if self.pending_stroke else {}
        self.w.view.brush_radius=component.get('radius',self.radius.slider.value()/1000)
        self.w.view.brush_feather=component.get('feather',self.feather.slider.value());self.w.view.viewport().update()

    def select_component(self,index):
        if self.loading:return
        self.w.commit();self.color_pick_generation+=1;self.component_index=index;self.load_range_editor();self.sync_tree()
        if self.w.view.tool_mode=='edit_component':self.edit_component_handles()

    def current_component(self):
        components=(self.current_layer() or {}).get('components',[])
        return components[self.component_index] if 0<=self.component_index<len(components) else None

    def replace_component(self,component):
        if self.current_component() is None:return
        self.w.commit();layers=deepcopy(self.w.settings['masks'])
        layers[self.layer_index]['components'][self.component_index]=component
        self.w.set_setting('masks',layers);self.w.finish_interaction();self.load_layers()

    def edit_component(self):
        if self.current_component() is None:return
        from .mask_dialog import MaskComponentDialog
        dialog=MaskComponentDialog(self.w,self.current_component())
        if dialog.exec():
            self.replace_component(dialog.result_component());self.w.view.set_tool('')

    def duplicate_component(self):
        if self.current_component() is None:return
        self.w.commit();self.add_component(deepcopy(self.current_component()));self.w.finish_interaction()

    def move_component(self,delta):
        if self.current_component() is None:return
        layers=deepcopy(self.w.settings['masks']);components=layers[self.layer_index]['components'];index=self.component_index
        if not 0<=index+delta<len(components):return
        self.w.commit();components[index],components[index+delta]=components[index+delta],components[index];self.component_index+=delta
        self.w.set_setting('masks',layers);self.w.finish_interaction();self.load_layers();self.w.view.set_tool('')

    def duplicate_layer(self):
        if self.current_layer() is None:return
        self.w.commit();layers=deepcopy(self.w.settings['masks']);copy=deepcopy(self.current_layer());copy['name']=copy.get('name','마스크')+' 사본'
        layers.insert(self.layer_index+1,copy);self.layer_index+=1;self.component_index=-1
        self.w.set_setting('masks',layers);self.w.finish_interaction();self.load_layers()

    def move_layer(self,delta):
        layers=deepcopy(self.w.settings['masks']);index=self.layer_index
        if not 0<=index<len(layers) or not 0<=index+delta<len(layers):return
        self.w.commit();layers[index],layers[index+delta]=layers[index+delta],layers[index];self.layer_index+=delta
        self.w.set_setting('masks',layers);self.w.finish_interaction();self.load_layers();self.w.view.set_tool('')

    def edit_component_handles(self):
        component=self.current_component();self.w.view.set_tool('')
        if component is None or component.get('type') not in ('linear','radial'):return
        self.source_point([.5,.5])  # Refresh the inverse geometry map.
        coords=self.coordinates;h,w=coords.shape[:2];handles=[]
        for point in (component['points'][0],component['points'][-1]):
            distance=np.sum((coords[...,:2]-point)**2,axis=-1);distance[coords[...,2]<.9]=np.inf
            y,x=np.unravel_index(distance.argmin(),distance.shape)
            if not np.isfinite(distance[y,x]) or distance[y,x]>.001:
                self.w.statusBar().showMessage(tr('크롭 밖에 있는 끝점은 영역 편집에서 수치로 조정하세요.'));return
            handles.append([x/max(1,w-1),y/max(1,h-1)])
        self.mode('edit_component');self.w.view.tool_handles=handles;self.w.view.handle_kind=component['type'];self.w.view.viewport().update()
        self.w.statusBar().showMessage(tr('양 끝점으로 크기를 조정하거나 가운데 점으로 이동하세요.'))

    def move_component_handles(self,points):
        component=self.current_component()
        if component is None:return
        source=[self.source_point(p) for p in points]
        if any(p is None for p in source):return
        updated=deepcopy(component);updated['points']=source;self.replace_component(updated)
        self.edit_component_handles()

    def preview_ready(self,image):
        """Invalidate overlay presentation; all pixel work runs on preview workers."""
        self.w.view.tool_overlay=None
        self.update_healing_overlay()
        self.w.view.viewport().update()
        if self.w.source is not None:
            self.w.render_version+=1
            self.w.render()

    def overlay_layer(self):
        if self.point_overlay_target() is not None:return None
        if (not self.overlay.isChecked() or self.w.before_button.isChecked()
                or not self.w.tabs.is_open('masks')
                or self.current_layer() is None and not self.stroke_valid()):return None
        if self.current_layer() is None:return len(self.w.settings['masks'])
        return self.layer_index

    def point_overlay_target(self):
        if self.w.before_button.isChecked():return None
        if self.w.tabs.is_open('mixer') and self.point_preview.isChecked() and 0<=self.point_index<len(self.w.settings['point_colors']):return (None,self.point_index)
        if self.w.tabs.is_open('masks') and self.local_point_preview.isChecked() and self.current_layer() is not None and self.current_layer().get('enabled',True) and 0<=self.local_point_index<len(self.local_point_values()):return (self.layer_index,self.local_point_index)
        return None

    def film_balance(self,key,checked):
        """Convert between tungsten and daylight film balance, or turn the conversion off again."""
        w=self.w
        if self.loading or w.source is None:
            self.load_film_buttons();return
        w.commit()
        if checked:
            reference,light=FILM_BALANCE[key]
            w.settings.update(kelvin_enabled=True,wb_reference=reference,kelvin=light)
        else:w.settings['kelvin_enabled']=False
        w.commit({'tungsten':'텅스텐 → 데이라이트','daylight':'데이라이트 → 텅스텐'}[key] if checked else '필름 색온도 변환 해제')
        w.load_controls();w.before_button.setChecked(False);w.render_version+=1;w.render()

    def load_film_buttons(self):
        s=self.w.settings
        for key,(reference,light) in FILM_BALANCE.items():
            self.film_buttons[key].setChecked(bool(s['kelvin_enabled']) and s['wb_reference']==reference and s['kelvin']==light)

    def rgb_curve_changed(self,points):
        if self.loading:return
        values=deepcopy(self.w.settings['rgb_curves']);values[self.channel.currentIndex()]=points;self.w.set_setting('rgb_curves',values)

    def parametric_changed(self,key,value):
        if self.loading:return
        values=deepcopy(self.w.settings['parametric']);values[int(key)]=value;self.w.set_setting('parametric',values)

    def load_points(self):
        self.loading=True;self.points.clear()
        for i,p in enumerate(self.w.settings['point_colors']):self.points.addItem(tr('선택 색상 {0}', f'{i + 1}'))
        self.points.setCurrentIndex(min(max(0,self.point_index),self.points.count()-1));self.loading=False;self.load_point()

    def load_point(self,*_):
        if self.loading:return
        self.point_index=self.points.currentIndex();values=self.w.settings['point_colors']
        point=values[self.point_index] if 0<=self.point_index<len(values) else {}
        for key,c in self.point_controls.items():
            c.set_value(point.get(key,{'range':.12,'saturation_range':.6,'lightness_range':1}.get(key,0)));c.setEnabled(bool(point))
        self.point_preview.setEnabled(bool(point))
        if self.point_preview.isChecked():self.preview_ready(self.w.view.on_screen)

    def point_changed(self,key,value):
        if self.loading:return
        values=deepcopy(self.w.settings['point_colors'])
        if 0<=self.point_index<len(values):
            point=values[self.point_index]
            if key in ('saturation_range','lightness_range') and point.get('version',1)<2:point.update(version=2,saturation_range=.6,lightness_range=1)
            point[key]=value;self.w.set_setting('point_colors',values)

    def remove_point(self):
        values=deepcopy(self.w.settings['point_colors'])
        if 0<=self.point_index<len(values):values.pop(self.point_index);self.w.set_setting('point_colors',values);self.load_points()

    def remove_retouch(self):
        values=deepcopy(self.w.settings['retouch'])
        if values:values.pop();self.w.set_setting('retouch',values);self.w.finish_interaction();self.load_settings()

    def start_guides(self):
        self.guide_points=[];self.mode('guide');self.w.statusBar().showMessage(tr('좌상 → 우상 → 우하 → 좌하 순으로 클릭하세요.'))

    def camera_profile(self):
        path,_=QFileDialog.getOpenFileName(self.w,tr('RGB 행렬 프로파일'),'','JSON (*.json)')
        if not path:return
        try:
            matrix=np.asarray(json.loads(Path(path).read_text(encoding='utf-8'))['matrix'],np.float32)
            if matrix.shape!=(3,3) or not np.isfinite(matrix).all() or np.max(np.abs(matrix))>10:raise ValueError('3×3 유한 행렬이 필요합니다.')
            self.w.set_setting('camera_matrix',matrix.tolist())
        except (ValueError,KeyError,OSError) as e:QMessageBox.warning(self.w,tr('프로파일'),str(e))

    lens_keys=('distortion','distortion_k2','distortion_k3','lens_vignette','ca_red','ca_blue','defringe')

    def apply_lens(self,data):
        self.w.commit();self.w.set_setting('lensfun',data);self.w.set_setting('lensfun_enabled',True)
        self.w.commit('렌즈 자동 보정');self.w.load_controls();self.w.render()
        self.w.statusBar().showMessage(tr('렌즈 보정을 적용했습니다. 실행 취소로 되돌릴 수 있습니다.'))

    def auto_lens(self):
        if self.w.source is None:return
        from .optics import automatic_profile
        data,reason=automatic_profile(self.w.catalog.photo(self.w.current_id)['info'])
        if data:self.apply_lens(data)
        else:
            self.w.statusBar().showMessage(reason);self.choose_lens()

    def choose_lens(self):
        if self.w.source is None:return
        from .lens_dialog import LensDialog
        dialog=LensDialog(self.w,self.w.catalog.photo(self.w.current_id)['info'],self.w.settings.get('lensfun'))
        if dialog.exec():self.apply_lens(dialog.result_profile)

    def clear_upright(self):
        self.w.set_setting('upright',None);self.w.finish_interaction();self.w.load_controls()

    def auto_upright(self,mode):
        if self.w.source is None:return
        self.upright_generation+=1;generation=self.upright_generation
        from .upright import estimate
        from .optics import lens_shading
        ident=self.w.current_id;version=self.w.load_version;saved=deepcopy(self.w.settings)
        source=self.w.source;settings=deepcopy(saved)
        settings.update(upright=None,straighten=0,crop=None)
        for b in self.upright_buttons+[self.w.auto_level_button]:b.setEnabled(False)
        self.upright_label.setText(tr('사진의 직선과 소실점을 분석하고 있습니다…'))
        self.w.auto_level_note.setText(tr('사진의 기준선을 분석하고 있습니다…'))
        def work():
            image=geometry(lens_shading(resize_float(source,1400),settings),settings,False)
            return estimate(image,mode)
        def finished(result):
            for b in self.upright_buttons+[self.w.auto_level_button]:b.setEnabled(True)
            if generation!=self.upright_generation or self.w.current_id!=ident or self.w.load_version!=version or self.w.settings!=saved:
                self.w.load_controls();self.w.statusBar().showMessage(tr('분석 중 사진이나 설정이 바뀌어 결과를 적용하지 않았습니다.'));return
            self.w.commit();self.w.set_setting('straighten',0);self.w.set_setting('upright',result)
            self.w.commit('자동 수평 맞추기' if mode=='level' else '자동 수평·원근');self.w.load_controls();self.w.render()
        def failed(error):
            for b in self.upright_buttons+[self.w.auto_level_button]:b.setEnabled(True)
            if generation==self.upright_generation and self.w.current_id==ident and self.w.load_version==version and self.w.settings==saved:
                message=str(error).strip().splitlines()[-1].split(': ',1)[-1]
                self.w.load_controls();self.upright_label.setText(message);self.w.auto_level_note.setText(message)
        self.w.spawn(work,finished,failed)

    def save_lens(self):
        path,_=QFileDialog.getSaveFileName(self.w,tr('렌즈 보정 프로파일 저장'),'lens.luma-lens.json','Grainy lens (*.json)')
        if path:
            Path(path).write_text(json.dumps({'type':'luma-lens','version':1,'name':Path(path).stem,'settings':{k:self.w.settings[k] for k in self.lens_keys}},ensure_ascii=False,indent=2),encoding='utf-8')

    def load_lens(self):
        path,_=QFileDialog.getOpenFileName(self.w,tr('렌즈 보정 프로파일'),'','Grainy lens (*.json)')
        if not path:return
        try:
            data=json.loads(Path(path).read_text(encoding='utf-8'))
            if data.get('type')!='luma-lens':raise ValueError('Luma 렌즈 프로파일이 아닙니다.')
            values={k:float(data['settings'].get(k,0)) for k in self.lens_keys}
            if any(not np.isfinite(v) or abs(v)>100 for v in values.values()):raise ValueError('프로파일 보정값이 범위를 벗어났습니다.')
            self.w.commit()
            for k,v in values.items():self.w.set_setting(k,v)
            self.w.set_setting('lens_profile',str(data.get('name') or Path(path).stem))
            self.w.commit('렌즈 프로파일');self.w.load_controls()
        except (ValueError,KeyError,OSError,TypeError) as e:QMessageBox.warning(self.w,tr('프로파일'),str(e))

    def load_settings(self):
        self.loading=True;s=self.w.settings
        if self.healing_context is not None and self.healing_context!=(self.w.current_id,self.w.load_version):self.clear_healing()
        self.photo_filter_enabled.setChecked(s['photo_filter_enabled'])
        self.photo_filter_choice.setCurrentIndex(self.photo_filter_choice.findData(s['photo_filter']))
        self.photo_filter_luminosity.setChecked(s['photo_filter_luminosity'])
        self.w.adjustments['photo_filter_density'].setEnabled(s['photo_filter_enabled'])
        self.photo_filter_luminosity.setEnabled(s['photo_filter_enabled'])
        if self.photo_id!=self.w.current_id:
            self.photo_id=self.w.current_id;self.donor=None;self.guide_points=[];self.layer_index=0;self.component_index=-1
            self.fresh_layer=None
            self.retouch_source_note.setText(tr('힐링 원본: 자동 선택'))
        self.kelvin.setChecked(s['kelvin_enabled']);self.mixer.setCurrentIndex(self.mixer.findData(s['mixer_mode']))
        self.load_film_buttons()
        self.working.setCurrentIndex(self.working.findData(s['working_space']))
        self.load_raw_controls()
        self.channel_curve.set_points(s['rgb_curves'][self.channel.currentIndex()])
        for c,v in zip(self.parametric,s['parametric']):c.set_value(v)
        for key,index,controls in self.group_controls:
            for c,v in zip(controls,s[key][index]):c.set_value(v)
        self.lens_label.setText(s['lens_profile'] or tr('렌즈 프로파일 없음'))
        from .optics import description
        self.auto_lens_label.setText(description(s.get('lensfun')) if s.get('lensfun') else tr('자동 렌즈 프로파일 없음'))
        for key,control in self.lens_switches.items():
            caps=(s.get('lensfun') or {}).get('capabilities',[])
            capability={'lensfun_distortion':'distortion','lensfun_tca':'tca','lensfun_vignette':'vignette'}.get(key)
            control.setChecked(s[key]);control.setEnabled(bool(s.get('lensfun')) and (capability is None or capability in caps))
        self.upright_crop.setChecked(s['upright_crop'])
        upright=s.get('upright')
        modes={'level':'수평','vertical':'수직','full':'전체 원근'}
        self.upright_label.setText(tr('{0} 적용 · 회전 {1}° · 가장자리 확대 {2}배', f"{modes.get(upright['mode'], '자동')}", f"{upright['rotation']:+.2f}", f"{upright['crop_scale']:.2f}") if upright else tr('자동 정렬 미적용'))
        self.w.auto_level_note.setText(tr('자동 보정 {0}° · 아래 슬라이더로 미세 조절할 수 있습니다.', f"{upright['rotation']:+.2f}") if upright else tr('직선이 보이는 사진에서 자동 보정한 뒤 아래 슬라이더로 미세 조절하세요.'))
        self.retouch_count.setText(tr('복구 동작 {0}개', f"{len(s['retouch'])}"))
        self.loading=False;self.load_layers();self.load_points()

    def change_photo_filter(self,key,value):
        if self.loading or self.w.source is None:return
        self.w.commit();self.w.set_setting(key,value)
        if key=='photo_filter':self.w.set_setting('photo_filter_enabled',True)
        self.w.commit('컬러 필터');self.w.load_controls();self.w.render()

    def change_working(self):
        if self.loading or self.w.source is None:return
        self.w.set_setting('working_space',self.working.currentData());self.w.commit('작업 색공간');self.w.render()

    def load_raw_controls(self):
        from .engine import RAW_EXTENSIONS
        s=self.w.settings;photo=self.w.catalog.photo(self.w.current_id) if self.w.current_id else None
        info=photo['info'] if photo else {}
        usable=bool(photo and Path(photo['path']).suffix.lower() in RAW_EXTENSIONS)
        offline=usable and not Path(photo['path']).is_file()
        if offline and not (info.get('raw_native') or info.get('raw_dual_preview')):usable=False
        self.raw_mode.setEnabled(usable);self.dcp_button.setEnabled(usable);self.dcp_browse.setEnabled(usable)
        self.raw_mode.setVisible(usable);self.dcp_browse.setVisible(usable);self.dcp_note.setVisible(usable)
        for key in ('raw_kelvin','raw_tint'):self.w.adjustments[key].setVisible(usable)
        self.raw_mode.setCurrentIndex(max(0,self.raw_mode.findData(s['raw_mode'])))
        self.raw_mode.model().item(self.raw_mode.findData('sample')).setEnabled(bool(s.get('raw_neutral')))
        for key in ('raw_kelvin','raw_tint'):self.w.adjustments[key].setEnabled(usable and s['raw_mode']!='legacy')
        message=tr('RAW 센서 단계에서 적용합니다. 양의 색조는 자홍색 방향 보정입니다.') if usable else tr('RAW 원본을 선택하면 카메라 색온도와 DCP를 사용할 수 있습니다.')
        if info.get('raw_as_shot_kelvin'):
            message+=tr('\n촬영 시 약 {0}K · 색조 {1}',f'{info["raw_as_shot_kelvin"]:,}',f'{info["raw_as_shot_tint"]:+g}')
        if offline:message+=tr('\n저장된 RAW 미리보기로 편집 중입니다.')
        self.raw_note.setText(message)
        p=s.get('dcp_profile');self.dcp_note.setText((p['name']+' · '+p.get('camera','')+'\n'+p.get('copyright','')) if p else tr('DCP 프로파일 없음'))
        self.dcp_clear.setEnabled(bool(p))
        for key,control in self.dcp_switches.items():control.setChecked(s[key]);control.setEnabled(usable and bool(p))

    def change_raw_mode(self):
        if self.loading or self.w.source is None:return
        mode=self.raw_mode.currentData()
        if mode=='legacy' and self.w.settings.get('dcp_profile'):
            self.w.set_setting('dcp_profile',None)
        if mode=='custom' and self.w.settings['raw_mode'] in ('legacy','as_shot','daylight'):
            info=getattr(self.w.source,'raw_info',None)
            if info:
                from .rawcolor import resolve_white,default_profile
                from .dcp import compiled
                p=compiled(self.w.settings['dcp_profile']) if self.w.settings['dcp_profile'] else default_profile(info)
                _,temperature,tint=resolve_white(p,info,self.w.settings)
                self.w.set_setting('raw_kelvin',float(np.clip(temperature,2000,50000)));self.w.set_setting('raw_tint',float(np.clip(tint,-150,150)))
        self.w.set_setting('raw_mode',mode);self.w.commit('RAW 화이트밸런스');self.w.load_controls();self.w.render()

    def raw_temperature_changed(self):
        if self.loading or self.w.source is None:return
        for key in ('raw_kelvin','raw_tint'):
            control=self.w.adjustments[key]
            self.w.set_setting(key,control.slider.value()/control.scale)
        self.w.set_setting('raw_mode','custom');self.w.set_setting('raw_neutral',None)
        self.raw_mode.blockSignals(True);self.raw_mode.setCurrentIndex(self.raw_mode.findData('custom'));self.raw_mode.blockSignals(False)

    def display_raw_white(self):
        from .rawcolor import CameraSource,default_profile,resolve_white
        from .dcp import compiled
        s=self.w.settings
        if not isinstance(self.w.source,CameraSource) or s['raw_mode'] not in ('as_shot','daylight','sample'):return
        info=self.w.source.raw_info;p=compiled(s['dcp_profile']) if s['dcp_profile'] else default_profile(info)
        _,temperature,tint=resolve_white(p,info,s)
        self.w.adjustments['raw_kelvin'].set_value(float(np.clip(temperature,2000,50000)))
        self.w.adjustments['raw_tint'].set_value(float(np.clip(tint,-150,150)))

    def load_dcp(self):
        if self.w.source is None:return
        path,_=QFileDialog.getOpenFileName(self.w,tr('DCP 카메라 프로파일 선택'),'','DCP (*.dcp)')
        if not path:return
        try:self.apply_dcp(path)
        except (ValueError,OSError,TypeError) as error:QMessageBox.warning(self.w,tr('DCP 프로파일'),str(error))

    def apply_dcp(self,path):
        from .dcp import load_profile
        self.apply_dcp_record(load_profile(path))

    def apply_dcp_record(self,record):
        from .dcp import compiled,camera_matches
        from .engine import RAW_EXTENSIONS
        photo=self.w.catalog.photo(self.w.current_id)
        if Path(photo['path']).suffix.lower() not in RAW_EXTENSIONS:raise ValueError('DCP는 RAW 사진에 적용합니다.')
        if not camera_matches(compiled(record),photo['info']):raise ValueError('프로파일의 카메라 모델이 선택한 RAW와 다릅니다.')
        self.w.commit();self.w.set_setting('dcp_profile',record)
        if self.w.settings['raw_mode']=='legacy':self.w.set_setting('raw_mode','as_shot')
        self.w.commit('DCP 프로파일');self.w.load_controls();self.w.render()

    def browse_dcp(self):
        if self.w.source is None:return
        from .profile_dialog import ProfileDialog
        from PySide6.QtWidgets import QDialog
        ident=self.w.current_id;dialog=ProfileDialog(self.w,self.w.catalog.photo(ident)['info'])
        if dialog.exec()==QDialog.DialogCode.Accepted and self.w.current_id==ident:
            self.apply_dcp_record(dialog.record)

    def raw_defaults(self):
        from .profile_dialog import RawDefaultsDialog
        RawDefaultsDialog(self.w).exec()

    def clear_dcp(self):
        self.w.set_setting('dcp_profile',None);self.w.commit('DCP 해제');self.w.load_controls();self.w.render()
