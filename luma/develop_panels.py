"""One scrollable Develop column with collapsible panels and a tool strip."""
from PySide6.QtCore import Qt,Signal,QTimer
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QToolButton,QScrollArea,QMenu,QSizePolicy
from .i18n import tr

PANELS=(('crop','크롭'),('healing','복구'),('masks','마스크'),
        ('basic','기본'),('curve','톤 곡선'),('mixer','색상 혼합'),('grading','컬러 그레이딩'),
        ('detail','디테일'),('lens','렌즈 교정'),('transform','변형'),('effects','효과'),
        ('photo_filter','컬러 필터'),('calibration','카메라 보정'),('metadata','사진 정보·관리'))
ROUTES=('basic','crop','metadata','mixer','detail','masks','healing')
TOOLS=('crop','healing','masks')


class Panel(QWidget):
    def __init__(self,host,key,title):
        super().__init__();self.host=host;self.key=key;self.setObjectName('developPanel')
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        self.header=QToolButton();self.header.setObjectName('developPanelHeader')
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.header.setText(tr(title));self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header.customContextMenuRequested.connect(lambda p:host.context_menu(self.header,p))
        self.header.toggled.connect(lambda expanded:host.toggle_panel(key,expanded))
        layout.addWidget(self.header)
        self.body=QWidget();self.body.setObjectName('developPanelBody')
        box=QVBoxLayout(self.body);box.setContentsMargins(8,9,8,12);box.setSpacing(4)
        layout.addWidget(self.body);self.set_expanded(False)

    def set_expanded(self,value):
        self.header.blockSignals(True);self.header.setChecked(value);self.header.blockSignals(False)
        self.header.setArrowType(Qt.ArrowType.DownArrow if value else Qt.ArrowType.RightArrow)
        self.body.setVisible(value)


class PanelScroll(QScrollArea):
    def __init__(self,host):
        super().__init__();self.host=host;self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def ensureWidgetVisible(self,widget,xmargin=50,ymargin=50):
        parent=widget
        while parent is not None:
            toggle=getattr(parent,'foldout_toggle',None)
            if toggle is not None:toggle.setChecked(True)
            if isinstance(parent,Panel):
                if parent.key in TOOLS and self.host.currentIndex()!=ROUTES.index(parent.key):
                    self.host.setCurrentIndex(ROUTES.index(parent.key))
                self.host.open_panel(parent.key,scroll=False);break
            parent=parent.parentWidget()
        self.host.scroll_revision+=1
        self.widget().layout().activate()
        super().ensureWidgetVisible(widget,xmargin,ymargin)


class DevelopPanels(QWidget):
    currentChanged=Signal(int)

    def __init__(self,window):
        super().__init__();self.w=window;self.labels=[];self.index=0;self.ready=False
        self.scroll_revision=0
        self.solo=True;self.sections={};self.tool_buttons={}
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        strip=QWidget();strip.setObjectName('developTools');row=QHBoxLayout(strip)
        row.setContentsMargins(0,4,0,7);row.setSpacing(3)
        for index,title in ((0,'보정'),(1,'크롭'),(6,'복구'),(5,'마스크')):
            control=QToolButton();control.setObjectName('developTool');control.setText(tr(title))
            control.setAccessibleName(tr(title));control.setCheckable(True)
            control.clicked.connect(lambda checked=False,i=index:self.select_tool(i))
            row.addWidget(control,1);self.tool_buttons[index]=control
        self.tool_buttons[0].setChecked(True);layout.addWidget(strip)
        self.scroll=PanelScroll(self);content=QWidget();self.column=QVBoxLayout(content)
        self.column.setContentsMargins(0,0,5,0);self.column.setSpacing(0)
        for key,title in PANELS:
            section=Panel(self,key,title);self.sections[key]=section;self.column.addWidget(section)
            if key in TOOLS:section.hide()
        self.column.addStretch();self.scroll.setWidget(content);layout.addWidget(self.scroll,1)
        self.sections['basic'].set_expanded(True)

    def register_tab(self,title):
        index=len(self.labels);self.labels.append(title)
        return self.scroll,self.sections[ROUTES[index]].body.layout()

    def box(self,key):return self.sections[key].body.layout()
    def count(self):return len(self.labels)
    def tabText(self,index):return self.labels[index]
    def currentIndex(self):return self.index
    def currentWidget(self):return self.scroll
    def widget(self,index):return self.scroll

    def finish(self):
        from .widgets import Adjustment
        for adjustment in self.findChildren(Adjustment):adjustment.compact()
        saved=self.w.catalog.preference('develop_panels',{})
        if isinstance(saved,dict):
            self.solo=bool(saved.get('solo',True))
            expanded=[key for key in saved.get('expanded',['basic']) if key in self.sections and key not in TOOLS]
            if self.solo:expanded=expanded[:1]
            for key,panel in self.sections.items():
                if key not in TOOLS:panel.set_expanded(key in expanded)
        self.ready=True

    def save(self):
        if self.ready and not getattr(self.w,'closing',False):
            self.w.catalog.save_preference('develop_panels',dict(solo=self.solo,
                expanded=[key for key,p in self.sections.items() if key not in TOOLS and p.header.isChecked()]))

    def navigation_state(self):
        return dict(index=self.index,solo=self.solo,expanded=[key for key,p in self.sections.items() if p.header.isChecked()],
                    scroll=self.scroll.verticalScrollBar().value())

    def restore_navigation(self,state):
        self.setCurrentIndex(state['index']);self.solo=state['solo']
        for key,panel in self.sections.items():panel.set_expanded(key in state['expanded'])
        self.scroll_revision+=1
        self.save();QTimer.singleShot(0,lambda:self.scroll.verticalScrollBar().setValue(state['scroll']))

    def setCurrentIndex(self,index):
        if not 0<=index<len(ROUTES):return
        self.index=index;key=ROUTES[index]
        for tool in TOOLS:self.sections[tool].setVisible(tool==key)
        for i,button in self.tool_buttons.items():button.setChecked(i==(index if key in TOOLS else 0))
        self.currentChanged.emit(index)
        self.open_panel(key)

    def select_tool(self,index):
        if index==self.index and index in (1,5,6):index=0
        self.w.wb_button.setChecked(False);self.w.view.sample_mode=False
        if self.w.view.crop_mode and index!=1:self.w.view.accept_crop()
        self.setCurrentIndex(index)
        if index==1 and not self.w.crop_button.isChecked():self.w.crop_button.click()
        elif index==6:self.w.studio.mode('heal')

    def toggle_panel(self,key,expanded):
        studio=getattr(self.w,'studio',None)
        overlay=(studio.overlay_layer(),studio.point_overlay_target()) if studio else None
        if key in TOOLS and not expanded:
            self.sections[key].set_expanded(False)
            self.select_tool(0);return
        if expanded:
            if key not in TOOLS and self.index in (1,5,6):
                if self.w.view.crop_mode:self.w.view.accept_crop()
                self.index=0
                for tool in TOOLS:self.sections[tool].hide()
                for i,button in self.tool_buttons.items():button.setChecked(i==0)
                self.currentChanged.emit(0)
            if self.solo:
                for other,panel in self.sections.items():
                    if other!=key and other not in TOOLS:panel.set_expanded(False)
        self.sections[key].set_expanded(expanded);self.save()
        if studio and overlay!=(studio.overlay_layer(),studio.point_overlay_target()):
            studio.preview_ready(self.w.view.on_screen)

    def open_panel(self,key,scroll=True):
        self.toggle_panel(key,True)
        if scroll:
            self.column.activate()
            self.scroll_revision+=1;revision=self.scroll_revision
            QTimer.singleShot(0,lambda:self.scroll.ensureVisible(0,self.sections[key].y(),0,0) if self.scroll_revision==revision else None)

    def is_open(self,key):
        panel=self.sections[key]
        return panel.isVisible() and panel.header.isChecked()

    def set_solo(self,value):
        self.solo=bool(value)
        if self.solo:
            opened=[key for key,p in self.sections.items() if key not in TOOLS and p.header.isChecked()]
            for key in opened[1:]:self.sections[key].set_expanded(False)
        self.save()

    def context_menu(self,header,position):
        menu=QMenu(self)
        solo=menu.addAction(tr('단일 패널 모드'));solo.setCheckable(True);solo.setChecked(self.solo)
        solo.triggered.connect(self.set_solo)
        collapse=menu.addAction(tr('모든 패널 접기'))
        def close_all():
            for key,p in self.sections.items():
                if key not in TOOLS:p.set_expanded(False)
            self.save()
        collapse.triggered.connect(close_all)
        menu.exec(header.mapToGlobal(position));menu.deleteLater()
