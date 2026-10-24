"""Two pictures side by side in the main window.

Either the current photo before and after its edits, or the current photo beside another one. The
main view stays what it is (the current photo, with every tool); a second view is put next to it.
The second view follows the zoom and position of the main one. In the two-photo mode the current
photo carries a white frame and a click on the other photo makes that one current. A double click on
either picture ends the comparison and shows that photo.
"""
from copy import deepcopy
from PySide6.QtCore import Qt,QObject,QEvent,QTimer,QItemSelectionModel
from PySide6.QtWidgets import QWidget,QFrame,QVBoxLayout,QHBoxLayout,QBoxLayout,QLabel,QPushButton,QComboBox,QGraphicsView
from .i18n import tr
from .widgets import PhotoView

SIZE=1800                      # long side of the second picture until the main view is zoomed in
MODES=(('보정 전·후','before'),('여러 이미지','photos'))
POINTER=(QEvent.Type.MouseButtonPress,QEvent.Type.MouseButtonRelease,QEvent.Type.MouseMove,QEvent.Type.Wheel)


def before_settings(settings):
    """The photo as it was before its edits, in the same frame: crop, rotation and lens geometry are kept so
    both sides show the same part of the picture."""
    from .engine import defaults
    from .manager import EDIT_GROUPS
    plain=defaults()
    for key in ('working_space',)+EDIT_GROUPS['크롭·회전']+EDIT_GROUPS['렌즈·원근']:plain[key]=deepcopy(settings[key])
    return plain


class Pane(QFrame):
    """One picture with its caption; the frame is white around the current photo."""

    def __init__(self,view):
        super().__init__();self.view=view;self.setObjectName('comparePane')
        box=QVBoxLayout(self);box.setContentsMargins(0,0,0,0);box.setSpacing(0)
        self.caption=QLabel();self.caption.setObjectName('muted');self.caption.setContentsMargins(8,5,8,5)
        box.addWidget(self.caption);box.addWidget(view,1);self.caption.hide();self.marked=False

    def dress(self,compared,marked=False,caption=''):
        if (compared,marked,caption)==getattr(self,'dressed',None):return
        self.dressed=(compared,marked,caption)
        self.marked=marked;self.caption.setVisible(compared);self.caption.setText(caption)
        self.layout().setContentsMargins(*([2]*4 if compared else [0]*4))
        self.setStyleSheet('QFrame#comparePane { border:2px solid %s; }'%('#f2f2f2' if marked else 'transparent') if compared else '')


class CompareLayout(QObject):
    def __init__(self,w):
        super().__init__(w)
        self.w=w;self.on=False;self.mode='before';self.vertical=False;self.swapped=False
        self.other_id=None;self.shown=None;self.version=0;self.full=False;self.colour_linked=False
        self.area=QWidget();box=QVBoxLayout(self.area);box.setContentsMargins(0,0,0,0);box.setSpacing(0)
        self.bar=QWidget();row=QHBoxLayout(self.bar);row.setContentsMargins(12,8,12,8);row.addStretch(1)
        self.choice=QComboBox();self.choice.setAccessibleName(tr('비교 방식'))
        for title,key in MODES:self.choice.addItem(tr(title),key)
        self.choice.currentIndexChanged.connect(lambda _:self.set_mode(self.choice.currentData()))
        row.addWidget(self.choice);row.addSpacing(18);row.addWidget(QLabel(tr('방향')))
        self.side_button=QPushButton(tr('좌우'));self.stack_button=QPushButton(tr('상하'))
        for control,value in ((self.side_button,False),(self.stack_button,True)):
            control.setCheckable(True);control.clicked.connect(lambda _=False,v=value:self.set_vertical(v));row.addWidget(control)
        row.addSpacing(18)
        self.swap_button=QPushButton(tr('바꾸기'));self.swap_button.setToolTip(tr('두 사진의 자리를 서로 바꿉니다.'))
        self.swap_button.clicked.connect(self.swap);row.addWidget(self.swap_button);row.addStretch(1)
        self.close_button=QPushButton(tr('비교 닫기'));self.close_button.setToolTip(tr('사진을 더블클릭해도 닫힙니다.'))
        self.close_button.clicked.connect(lambda:self.stop());row.addWidget(self.close_button)
        box.addWidget(self.bar);self.bar.hide()
        self.panes=QWidget();self.order=QBoxLayout(QBoxLayout.Direction.LeftToRight,self.panes)
        self.order.setContentsMargins(0,0,0,0);self.order.setSpacing(6)
        self.other=PhotoView();self.other.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.main_pane=Pane(w.view);self.other_pane=Pane(self.other)
        self.order.addWidget(self.main_pane,1);self.order.addWidget(self.other_pane,1);self.other_pane.hide()
        box.addWidget(self.panes,1)
        self.timer=QTimer(self);self.timer.setInterval(400);self.timer.timeout.connect(self.sync)
        for view in (w.view,self.other):view.viewport().installEventFilter(self)
        w.view.zoomChanged.connect(lambda _:self.follow());w.view.fullResolutionRequested.connect(self.sharpen)
        for scroll in (w.view.horizontalScrollBar(),w.view.verticalScrollBar()):scroll.valueChanged.connect(lambda _:self.follow())

    def toggle(self):
        if self.on:self.stop()
        else:self.start('photos' if len(self.w.selected_ids())>1 else None)

    def start(self,mode=None):
        """Show the comparison; `mode` 'before' or 'photos', or the one used last."""
        w=self.w
        if w.current_id is None:
            w.compare_button.setChecked(False);return
        if not self.colour_linked:
            self.colour_linked=True
            w.display_color.main.changed.connect(lambda:w.display_color.refresh_view(self.other,w.display_color.main))
        w.set_mode(0)
        if mode:
            self.mode=mode;self.other_id=None;self.swapped=False
        self.on=True;self.shown=None;self.full=not w.view.fit_mode
        self.bar.show();self.other_pane.show();w.compare_button.setChecked(True);w.screen_modes.apply()
        self.sync();self.timer.start()

    def stop(self,show_other=False):
        """Back to one picture: the current photo, or the other one with `show_other`."""
        if not self.on:return
        other=self.other_id if self.mode=='photos' and show_other else None
        self.on=False;self.timer.stop();self.version+=1;self.shown=None;self.full=False
        self.bar.hide();self.other_pane.hide();self.other.clear_photo();self.main_pane.dress(False)
        self.w.compare_button.setChecked(False)
        if other is not None:self.select(other)
        self.w.view.fit_photo()

    def set_mode(self,mode):
        if mode==self.mode or not self.on:return
        self.mode=mode;self.swapped=False;self.shown=None;self.sync()

    def set_vertical(self,vertical):
        self.vertical=bool(vertical);self.arrange()

    def swap(self):
        self.swapped=not self.swapped;self.arrange()

    def pick(self):
        """The photo beside the current one: the one already there, another selected photo, the photo seen before
        this one, or its neighbour in the list."""
        w=self.w;current=w.current_id;positions=w.photo_model.positions
        def usable(ident):return ident is not None and ident!=current and ident in positions
        if usable(self.other_id):return self.other_id
        ids=getattr(w,'visible_ids',[]);place=ids.index(current) if current in ids else -1
        near=[ids[place+1]] if 0<=place<len(ids)-1 else [ids[place-1]] if place>0 else []
        candidates=[i for i in w.selected_ids()]+[getattr(w.workflow,'previous_id',None)]+near
        self.other_id=next((i for i in candidates if usable(i)),None)
        return self.other_id

    def select(self,ident):
        w=self.w;positions=w.photo_model.positions
        if ident in positions:
            w.photo_selection.setCurrentIndex(w.photo_model.index(positions[ident]),QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if w.current_id!=ident:w.activate(ident)

    def activate_other(self):
        """The other photo becomes the current one; both stay where they are on screen."""
        if not self.on or self.mode!='photos' or self.other_id is None:return
        chosen,self.other_id=self.other_id,self.w.current_id
        self.swapped=not self.swapped;self.shown=None
        self.select(chosen);self.sync()

    def sync(self):
        """Keep the second picture and the captions in step with the main window."""
        if not self.on:return
        w=self.w
        if w.current_id is None:
            self.stop();return
        from .workflow_data import edit_signature
        record=None
        if self.mode=='photos':
            if self.pick() is None:
                self.mode='before';w.statusBar().showMessage(tr('비교할 다른 사진이 없어 보정 전·후로 표시합니다.'),5000)
            else:
                try:record=w.catalog.photo(self.other_id)
                except Exception:
                    self.other_id=None;return
        if self.mode=='before':
            current=w.catalog.photo(w.current_id);record={**current,'settings':before_settings(w.settings)}
        self.arrange(record)
        key=(self.mode,record['id'],record['path'],edit_signature(record['settings']),self.full)
        if key!=self.shown:self.show(record,key)

    def arrange(self,record=None):
        """Order, direction, captions and the frame around the current photo."""
        w=self.w;before=self.mode=='before'
        self.choice.blockSignals(True);self.choice.setCurrentIndex(self.choice.findData(self.mode));self.choice.blockSignals(False)
        self.side_button.setChecked(not self.vertical);self.stack_button.setChecked(self.vertical)
        # Before and after reads left to right; with two photos the current one comes first.
        first,second=(self.other_pane,self.main_pane) if before!=self.swapped else (self.main_pane,self.other_pane)
        if self.order.itemAt(0).widget() is not first:
            self.order.removeWidget(first);self.order.insertWidget(0,first,1)
        self.order.setDirection(QBoxLayout.Direction.TopToBottom if self.vertical else QBoxLayout.Direction.LeftToRight)
        if record is None:return
        name=w.catalog.photo(w.current_id)['name']
        self.main_pane.dress(True,not before,tr('보정 후') if before else name)
        self.other_pane.dress(True,False,tr('원본') if before else record['name'])

    def show(self,record,key):
        if self.shown is None or self.shown[1]!=key[1]:self.other.clear_photo(loading=True)
        self.shown=key;self.version+=1;version=self.version;size=None if self.full else SIZE;record=deepcopy(record)
        from .reference_view import render_photo
        def loaded(result):
            if not self.on or version!=self.version:return
            image,working=result;self.other.display_source=working
            self.other.set_image(image);self.w.display_color.refresh_view(self.other,self.w.display_color.main);self.follow()
        def failed(error):
            if self.on and version==self.version:self.other_pane.caption.setText(record['name']+'  ·  '+tr('원본을 읽을 수 없습니다.'))
        self.w.spawn(lambda:render_photo(record,size),loaded,failed)

    def sharpen(self):
        """The main view is zoomed in: the second picture is rendered at full size too."""
        if self.on and not self.full:
            self.full=True;self.sync()

    def follow(self):
        """The second picture shows the same part at the same relative size as the main view."""
        main,other=self.w.view,self.other
        if not self.on or not main.has_photo or not other.has_photo:return
        if main.fit_mode:
            other.fit_photo();return
        scale=main.transform().m11()*main.image_rect.width()/other.image_rect.width()
        centre=main.mapToScene(main.viewport().rect().center())
        other.fit_mode=False;other.resetTransform();other.scale(scale,scale)
        other.centerOn(centre.x()/main.image_rect.width()*other.image_rect.width(),centre.y()/main.image_rect.height()*other.image_rect.height())

    def eventFilter(self,target,event):
        if not self.on or getattr(self.w,'closing',False):return False
        try:kind=event.type();second=target is self.other.viewport();main=self.w.view
        except RuntimeError:return False      # the views are already gone while the window closes
        if kind==QEvent.Type.MouseButtonDblClick and event.button()==Qt.MouseButton.LeftButton:
            if second or not (main.crop_mode or main.tool_mode or main.sample_mode):
                QTimer.singleShot(0,lambda:self.stop(show_other=second));return True
        if second and kind in POINTER:
            if kind==QEvent.Type.MouseButtonPress and event.button()==Qt.MouseButton.LeftButton:QTimer.singleShot(0,self.activate_other)
            return True
        return False
