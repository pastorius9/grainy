"""Single-worker rendering queue shared by visible and requested thumbnails."""
from .i18n import tr
from collections import OrderedDict,deque
from threading import Event
import time
from PySide6.QtCore import QObject,QTimer
from . import preview_store


class PreviewQueue(QObject):
    def __init__(self,w,pool):
        super().__init__(w);self.w=w;self.pool=pool;self.visible=OrderedDict();self.batch=deque()
        self.running=False;self.active_id=None;self.cancel=Event();self.total=0;self.completed=0;self.failures=0;self.closed=False

    def request(self,ident):
        if self.closed or ident==self.active_id:return
        self.visible[ident]=None;self.visible.move_to_end(ident)
        while len(self.visible)>64:self.visible.popitem(last=False)
        self.next()

    def start_batch(self,ids):
        self.stop();self.batch=deque(dict.fromkeys(ids));self.total=len(self.batch);self.completed=0;self.failures=0
        self.w.preview_cancel_button.setVisible(bool(self.total));self.next()

    def stop(self):
        self.cancel.set();self.cancel=Event();self.batch.clear();self.total=0
        if hasattr(self.w,'preview_cancel_button'):self.w.preview_cancel_button.hide()

    def clear_visible(self):self.visible.clear()

    def next(self):
        if self.running or self.closed:return
        if self.visible:ident,_=self.visible.popitem(last=False);batch=False
        elif self.batch:ident=self.batch.popleft();batch=True
        else:
            if self.total:
                self.w.statusBar().showMessage(tr('미리보기 {0}/{1}장 처리 · 원본 없음/오류 {2}장', f'{self.completed}', f'{self.total}', f'{self.failures}'))
                self.total=0;self.w.preview_cancel_button.hide()
            return
        self.running=True;self.active_id=ident;cancel=self.cancel
        cached=None
        if ident==self.w.current_id and self.w.source is not None:
            cached=(self.w.source,dict(source=getattr(self.w,'loaded_file_signature',None),
                offline=getattr(self.w,'loaded_offline_signature',None),
                working_space=getattr(self.w,'loaded_working_space',None),original_size=self.w.original_size))
        def ready(status):
            self.running=False;self.active_id=None
            if self.closed:return
            if cancel is self.cancel:
                if batch:
                    self.completed+=1;self.failures+=int(status.startswith(('오류','원본 없음')))
                    self.w.statusBar().showMessage(tr('미리보기 {0}/{1}장 처리 · {2}', f'{self.completed}', f'{self.total}', f'{status}'))
                errors=self.w.photo_model.rebuild_errors
                if status.startswith('오류'):errors[ident]=(status,time.monotonic())
                else:errors.pop(ident,None)
                while len(errors)>256:errors.popitem(last=False)
                self.w.photo_model.invalidate_thumbnails([ident],retry=False)
            QTimer.singleShot(0,self.next)
        self.w.spawn(lambda:preview_store.rebuild(self.w.catalog.directory,ident,cancel,force=batch,cached=cached),ready,
            lambda error:ready('오류: '+error.strip().splitlines()[-1]),self.pool)

    def shutdown(self):
        self.closed=True;self.cancel.set();self.visible.clear();self.batch.clear()
