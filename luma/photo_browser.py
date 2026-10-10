"""Sparse Qt photo views: visible rows and a shared, bounded thumbnail cache."""
from .i18n import tr
from collections import OrderedDict
from pathlib import Path
import time
from PySide6.QtCore import (Qt,QAbstractListModel,QModelIndex,QSize,QRect,QItemSelection,
    QItemSelectionModel,QTimer,Signal)
from PySide6.QtGui import QPixmap,QImage,QColor,QPen
from PySide6.QtWidgets import QListView,QStyledItemDelegate,QStyle,QApplication
from .library_query import read_page


class PhotoModel(QAbstractListModel):
    PAGE_SIZE=128
    MAX_PAGES=16
    MAX_THUMB_BYTES=32*1024**2

    def __init__(self,window,pool):
        super().__init__(window);self.w=window;self.pool=pool;self.ids=[];self.positions={}
        self.pages=OrderedDict();self.thumbnails=OrderedDict();self.thumbnail_bytes=0
        self.epoch=0;self.pending_pages=set();self.pending_thumbs=set();self.thumbnail_reads=0
        self.thumb_versions={};self.closed=False;self.thumb_checked={};self.thumb_status={};self.rebuild_errors=OrderedDict()
        self.check_timer=QTimer(self);self.check_timer.setInterval(5000)
        self.check_timer.timeout.connect(lambda:self.changed(0,len(self.ids)-1) if self.ids and not self.closed else None)
        self.check_timer.start()

    @property
    def cached_rows(self):return sum(len(p) for p in self.pages.values())

    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.ids)

    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0<=index.row()<len(self.ids):return None
        if role==Qt.ItemDataRole.UserRole:return self.ids[index.row()]
        row=self.entry(index.row(),request=False)
        if role==Qt.ItemDataRole.DisplayRole:return self.caption(row) if row else tr('불러오는 중…')
        if role==Qt.ItemDataRole.ToolTipRole:return row['path']+'\n'+self.thumb_status.get(self.ids[index.row()],'') if row else None
        return None

    @staticmethod
    def caption(row):
        prefix='✓ ' if row['flag']==1 else '× ' if row['flag']==-1 else ''
        return f'{prefix}{row["name"]}\n'+('★'*row['rating'])

    def replace(self,ids):
        if hasattr(self.w,'preview_queue'):self.w.preview_queue.clear_visible()
        self.beginResetModel();self.epoch+=1;self.ids=ids;self.positions={i:n for n,i in enumerate(ids)}
        self.pages.clear();self.pending_pages.clear();self.pending_thumbs.clear();self.endResetModel()

    def changed(self,first,last=None):
        if self.ids:self.dataChanged.emit(self.index(first),self.index(min(len(self.ids)-1,first if last is None else last)))

    def entry(self,row,request=True):
        page=row//self.PAGE_SIZE;entries=self.pages.get(page)
        if entries is not None:
            self.pages.move_to_end(page);return entries.get(self.ids[row])
        if request and page not in self.pending_pages and len(self.pending_pages)<8 and not self.closed:
            self.pending_pages.add(page);epoch=self.epoch;start=page*self.PAGE_SIZE
            ids=self.ids[start:start+self.PAGE_SIZE];path=self.w.catalog.directory/'catalog.sqlite'
            def work():return {} if epoch!=self.epoch or self.closed else read_page(path,ids)
            def ready(values):
                if epoch!=self.epoch or self.closed:return
                self.pending_pages.discard(page);self.pages[page]=values
                while len(self.pages)>self.MAX_PAGES:self.pages.popitem(last=False)
                self.changed(start,start+len(ids)-1)
            self.w.spawn(work,ready,lambda _:ready({}),self.pool)
        return None

    def thumbnail(self,ident):
        cached=None
        if ident in self.thumbnails:
            self.thumbnails.move_to_end(ident);cached=self.thumbnails[ident][0]
            if time.monotonic()-self.thumb_checked.get(ident,0)<4:return cached
        if ident not in self.pending_thumbs and len(self.pending_thumbs)<64 and not self.closed:
            self.pending_thumbs.add(ident);epoch=self.epoch;version=self.thumb_versions.get(ident,0)
            directory=self.w.catalog.directory
            monitor=self.w.display_color.main.profile
            def work():
                if epoch!=self.epoch or self.closed:return None
                from .preview_store import inspect
                from .widgets import qimage
                import numpy as np
                result=inspect(directory,ident);source=result.pop('image')
                image=qimage(np.asarray(source,dtype=np.float32)/255,display=False) if source is not None else QImage()
                source_space=result.get('color_space','sRGB')
                if not image.isNull() and (monitor is not None or source_space!='sRGB'):
                    import numpy as np
                    from .colorio import convert,profile
                    from .widgets import qimage
                    image=image.convertToFormat(QImage.Format.Format_RGB888)
                    pixels=np.frombuffer(image.constBits(),np.uint8).reshape(image.height(),image.bytesPerLine())[:,:image.width()*3].reshape(image.height(),image.width(),3)
                    image=qimage(convert(pixels.astype(np.float32)/255,profile(source_space),monitor or profile('sRGB')),display=False)
                return image,result
            def ready(packet):
                if epoch!=self.epoch or self.closed:return
                self.pending_thumbs.discard(ident)
                if version!=self.thumb_versions.get(ident,0):
                    if ident in self.positions:self.changed(self.positions[ident])
                    return
                self.thumbnail_reads+=1
                image,result=packet if packet is not None else (None,dict(status='읽기 오류',rebuild=False))
                self.thumb_checked[ident]=time.monotonic();self.thumb_status[ident]=result['status']
                if result['rebuild']:
                    error=self.rebuild_errors.get(ident)
                    if error and time.monotonic()-error[1]<30:self.thumb_status[ident]=error[0]
                    else:self.w.preview_queue.request(ident)
                pix=QPixmap.fromImage(image) if image is not None and not image.isNull() else None
                size=pix.width()*pix.height()*4 if pix is not None else 1
                if ident in self.thumbnails:self.thumbnail_bytes-=self.thumbnails.pop(ident)[1]
                self.thumbnails[ident]=(pix,size);self.thumbnail_bytes+=size
                while self.thumbnail_bytes>self.MAX_THUMB_BYTES or len(self.thumbnails)>256:
                    old,(_,cost)=self.thumbnails.popitem(last=False);self.thumbnail_bytes-=cost
                    self.thumb_checked.pop(old,None);self.thumb_status.pop(old,None)
                if ident in self.positions:self.changed(self.positions[ident])
            self.w.spawn(work,ready,lambda _:ready(None),self.pool)
        return cached

    def invalidate_thumbnails(self,ids=None,retry=True):
        if ids is None:
            self.thumbnails.clear();self.thumbnail_bytes=0
            self.thumb_checked.clear();self.thumb_status.clear()
            if retry:self.rebuild_errors.clear()
            for i in self.pending_thumbs:self.thumb_versions[i]=self.thumb_versions.get(i,0)+1
            if self.ids:self.changed(0,len(self.ids)-1)
            return
        for ident in ids:
            self.thumb_checked.pop(ident,None);self.thumb_status.pop(ident,None)
            if retry:self.rebuild_errors.pop(ident,None)
            if ident in self.pending_thumbs:self.thumb_versions[ident]=self.thumb_versions.get(ident,0)+1
            else:self.thumb_versions.pop(ident,None)
            if ident in self.thumbnails:self.thumbnail_bytes-=self.thumbnails.pop(ident)[1]
            if ident in self.positions:self.changed(self.positions[ident])


class PhotoDelegate(QStyledItemDelegate):
    def __init__(self,view):super().__init__(view);self.view=view
    def sizeHint(self,option,index):return self.view.gridSize()

    def paint(self,painter,option,index):
        model=index.model();row=model.entry(index.row());ident=index.data(Qt.ItemDataRole.UserRole)
        pix=model.thumbnail(ident);rect=option.rect.adjusted(4,3,-4,-3)
        selected=bool(option.state&QStyle.StateFlag.State_Selected)
        hover=bool(option.state&QStyle.StateFlag.State_MouseOver)
        painter.save();painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#d3b68d' if selected else '#373e47' if hover else '#15181d'),1))
        painter.setBrush(QColor('#35322e' if selected else '#29313a' if hover else '#15181d'))
        painter.drawRect(rect)
        photo=rect.adjusted(7,7,-7,-36)
        if pix:
            size=pix.size();size.scale(photo.size(),Qt.AspectRatioMode.KeepAspectRatio)
            target=QRect(0,0,size.width(),size.height());target.moveCenter(photo.center())
            painter.drawPixmap(target,pix)
        else:
            painter.fillRect(photo,QColor('#222930'))
        status=model.thumb_status.get(ident,'')
        if status:
            badge=QRect(photo.left(),photo.top(),photo.width(),18)
            painter.fillRect(badge,QColor(22,27,33,210));painter.setPen(QColor('#d3b68d'))
            painter.drawText(badge,Qt.AlignmentFlag.AlignCenter,painter.fontMetrics().elidedText(status,Qt.TextElideMode.ElideRight,badge.width()))
        colour={'빨강':'#ec998c','노랑':'#eed08e','초록':'#95c9aa','파랑':'#95bce9','보라':'#c6a2e2'}
        painter.setPen(QColor(colour.get(row['label'],'#f4e7d4' if selected else '#aeb6bf') if row else '#89929d'))
        caption=model.caption(row).split('\n') if row else [tr('불러오는 중…'),'']
        label=QRect(rect.left()+5,rect.bottom()-33,rect.width()-10,17)
        painter.drawText(label,Qt.AlignmentFlag.AlignCenter,painter.fontMetrics().elidedText(caption[0],Qt.TextElideMode.ElideMiddle,label.width()))
        label.translate(0,16);painter.drawText(label,Qt.AlignmentFlag.AlignCenter,caption[1]);painter.restore()


class PhotoListView(QListView):
    photoClicked=Signal(QModelIndex)

    def __init__(self,parent=None):
        super().__init__(parent);self.setUniformItemSizes(True)
        self.photo_press=None
        # Uniform geometry is inexpensive and lets End/scrollTo address every
        # row immediately. Batched Qt layout silently drops distant jumps.
        self.setLayoutMode(QListView.LayoutMode.SinglePass)
        self.setItemDelegate(PhotoDelegate(self));self.setMouseTracking(True)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)

    def mousePressEvent(self,event):
        self.photo_press=None
        index=self.indexAt(event.position().toPoint())
        if event.button()==Qt.MouseButton.RightButton and index.isValid():
            if not self.selectionModel().isSelected(index):
                self.selectionModel().select(index,QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.selectionModel().setCurrentIndex(index,QItemSelectionModel.SelectionFlag.NoUpdate)
            event.accept();return
        if event.button()==Qt.MouseButton.LeftButton and not event.modifiers() and index.isValid():
            self.photo_press=(event.position().toPoint(),index)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self,event):
        press=self.photo_press;self.photo_press=None
        super().mouseReleaseEvent(event)
        if press and event.button()==Qt.MouseButton.LeftButton and not event.modifiers():
            point,index=press
            if (point-event.position().toPoint()).manhattanLength()<QApplication.startDragDistance() and self.indexAt(event.position().toPoint())==index:
                self.photoClicked.emit(index)

    def selected_ids(self):return [index.data(Qt.ItemDataRole.UserRole) for index in self.selectionModel().selectedRows()]

    def restore_selection(self,ids,current=None):
        model=self.model();rows=sorted(model.positions[i] for i in ids if i in model.positions)
        selection=QItemSelection();start=last=None
        for row in rows:
            if last is None:start=last=row
            elif row==last+1:last=row
            else:selection.select(model.index(start),model.index(last));start=last=row
        if last is not None:selection.select(model.index(start),model.index(last))
        self.selectionModel().select(selection,QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.selectionModel().setCurrentIndex(model.index(model.positions[current]) if current in model.positions else QModelIndex(),QItemSelectionModel.SelectionFlag.NoUpdate)

    # Small compatibility facade for existing photo-list integrations. Rows do
    # not allocate widgets/items until a caller explicitly asks for one.
    def count(self):return self.model().rowCount()
    def item(self,row):return PhotoItem(self,row) if 0<=row<self.count() else None
    def selectedItems(self):return [PhotoItem(self,i.row()) for i in self.selectionModel().selectedRows()]
    def setCurrentRow(self,row):self.setCurrentIndex(self.model().index(row))


class PhotoItem:
    def __init__(self,view,row):self.view=view;self.index=view.model().index(row)
    def data(self,role):return self.index.data(role)
    def setSelected(self,value):
        flag=QItemSelectionModel.SelectionFlag.Select if value else QItemSelectionModel.SelectionFlag.Deselect
        self.view.selectionModel().select(self.index,flag)
