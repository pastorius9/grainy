"""Live folder hierarchy and safe, non-destructive location repair."""
from pathlib import Path
from datetime import datetime
from threading import Event
from uuid import uuid4
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox
from .folders import folder_nodes,path_key,contains_folder
from .folder_locations import scan_folders,directory_identity,relink_plan,apply_relinks,rebase
from .i18n import tr


class FolderPanel:
    def __init__(self,w):
        self.w=w;self.photos=[];self.roots=[];self.signature=None
        self.children=[];self.missing=set();self.scanned=set();self.running=False
        self.cancel=Event();self.failed_moves=set()
        self.recovery_running=False;self.recovery_cancel=Event();self.recovery_signature=None
        self.timer=QTimer(w);self.timer.setInterval(5000);self.timer.timeout.connect(self.refresh);self.timer.start()

    def busy(self):
        w=self.w
        return w.closing or w.import_busy or w.import_scans or w.export_running or getattr(w,'command_running',False) or getattr(w,'maintenance_running',False)

    def nodes(self,photos,roots):
        signature=(tuple((p['path'],p.get('count',1)) for p in photos),tuple(roots))
        changed=signature!=self.signature
        self.photos,self.roots,self.signature=photos,list(roots),signature
        if changed:QTimer.singleShot(0,self.refresh)
        # Extra folders remain underneath registered roots; no automatic imports.
        extra=[p for p in self.children if any(contains_folder(root,p) for root in self.roots)]
        nodes=folder_nodes(photos,self.roots+extra)
        for node in nodes:
            node['missing']=node['key'] in self.missing
            node['unscanned']=not node['drive'] and node['key'] not in self.scanned
        return nodes

    def refresh(self):
        if self.running or self.busy() or QApplication.activeModalWidget() or QApplication.activePopupWidget():return
        w=self.w;signature=self.signature
        paths=list({str(Path(p['path']).parent) for p in self.photos}|set(self.children))
        roots=list(self.roots);expanded=set(w.expanded_folders)
        identities=w.catalog.preference('folder_identities',{})
        self.running=True;self.cancel=Event();cancel=self.cancel
        def finished(result):
            self.running=False
            if cancel.is_set() or self.busy() or signature!=self.signature:return
            saved=w.catalog.preference('folder_identities',{})
            updated={**saved,**result['identities']}
            if updated!=saved:w.catalog.save_preference('folder_identities',updated)
            if not QApplication.activeModalWidget() and not QApplication.activePopupWidget():
                for source,destination in result['moves']:
                    identity=saved.get(path_key(source),{}).get('id')
                    token=(source,destination,tuple(identity or []))
                    if token in self.failed_moves:continue
                    if Path(source).exists() or directory_identity(destination)!=identity:continue
                    try:
                        plan=relink_plan(w.catalog,source,destination)
                        self.apply(plan)
                        w.statusBar().showMessage(tr('폴더 이름 변경을 감지해 다시 연결했습니다: {0}',Path(destination).name),10000)
                        return
                    except Exception:
                        self.failed_moves.add(token)
                        w.statusBar().showMessage(tr('폴더 위치를 확인해 주세요. 폴더 우클릭 → 폴더 위치 다시 지정…'),10000)
            self.children=result['children'];self.missing=result['missing'];self.scanned=result['scanned']
            w.refresh_folder_tree(self.photos,self.roots)
            if self.missing:self.recover()
        def failed(error):
            self.running=False
            w.statusBar().showMessage(tr('폴더 목록을 갱신하지 못했습니다. 드라이브 연결을 확인하세요.'),6000)
        w.spawn(lambda:scan_folders(paths,roots,expanded,identities,cancel),finished,failed)

    def apply(self,plan):
        return self.apply_many([plan])

    def apply_many(self,plans):
        w=self.w;w.commit();w.save_keywords();w.save_folder_state()
        backup=w.catalog.directory/'backups'/f'folder-location-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.sqlite'
        w.catalog.backup(backup)
        ident=w.current_id;affected={ident for plan in plans for _,ident in plan['updates']}
        if ident in affected:w.clear_active_photo()
        try:count=apply_relinks(w.catalog,plans)
        except Exception:
            if ident in affected:w.activate(ident,force=True)
            raise
        for plan in plans:
            source,destination=plan['source'],plan['destination']
            if w.folder_filter:w.folder_filter=rebase(w.folder_filter,source,destination)
            w.expanded_folders={path_key(rebase(p,source,destination)) for p in w.expanded_folders}
        w.extras.watch_paths=w.catalog.preference('watch_folders',[])
        w.source_cache.clear();w.photo_model.invalidate_thumbnails(list(affected))
        self.children=[];self.missing=set();self.scanned=set();self.signature=None
        w.save_folder_state();w.refresh_lists()
        if ident in affected:w.activate(ident,force=True)
        return count

    def recover(self,explicit=False):
        if self.recovery_running:
            if explicit:self.recovery_cancel.set()
            return
        if self.busy() or QApplication.activeModalWidget() or QApplication.activePopupWidget():return
        key=(self.signature,tuple(sorted(self.children)))
        if not explicit and key==self.recovery_signature:return
        self.recovery_signature=key;self.recovery_running=True;self.recovery_cancel=Event()
        cancel=self.recovery_cancel;w=self.w
        w.statusBar().showMessage(tr('이름이 바뀐 폴더를 사진 정보·저장된 미리보기로 찾는 중…'))
        from .folder_recovery import recover_folders,validate_recovery
        def done(result):
            self.recovery_running=False
            if cancel.is_set() or w.closing:return
            if self.busy() or QApplication.activeModalWidget() or QApplication.activePopupWidget():
                self.recovery_signature=None;return
            plans=result['plans']
            if plans and not validate_recovery(w.catalog,plans):
                self.recovery_signature=None
                w.statusBar().showMessage(tr('검색 중 폴더 정보가 바뀌어 다시 확인합니다.'),7000);return
            if plans:
                count=self.apply_many(plans)
                report=dict(created=datetime.now().isoformat(),photos=count,
                    moves=[{k:p[k] for k in ('source','destination','proof','physical','anchors')} for p in plans],
                    unresolved=result['unresolved'],incomplete=result['incomplete'])
                w.catalog.save_preference('folder_recovery_last',report)
                w.statusBar().showMessage(tr('이름이 바뀐 폴더 {0}개 · 사진 {1}개를 자동으로 다시 연결했습니다.',len(plans),count),15000)
            elif explicit:
                w.statusBar().showMessage(tr('자동 연결할 폴더를 찾지 못했습니다. 후보가 중복되거나 사진 일치 근거가 부족합니다.'),15000)
        def failed(error):
            self.recovery_running=False
            w.statusBar().showMessage(tr('폴더 자동 찾기를 완료하지 못했습니다. 우클릭 메뉴에서 다시 시도하세요.'),12000)
        w.spawn(lambda:recover_folders(w.catalog.directory,cancel),done,failed)

    def locate(self,source):
        if self.busy():
            QMessageBox.information(self.w,tr('폴더 위치'),tr('사진 가져오기·내보내기와 카탈로그 작업을 마친 뒤 다시 시도하세요.'));return
        w=self.w;w.maintenance_running=True
        try:
            destination=QFileDialog.getExistingDirectory(w,tr('이 폴더의 새 위치 선택'),str(Path(source).parent))
            if not destination:return
            plan=relink_plan(w.catalog,source,destination)
            text=tr('기존 위치: {0}\n새 위치: {1}\n\n사진·가상 사본 {2}개를 다시 연결합니다. 찾은 원본 참조 {3}개, 찾지 못한 참조 {4}개.\n보정·별점·컬렉션은 유지하며 원본 파일을 이동하거나 삭제하지 않습니다. 변경 전 카탈로그를 자동 백업합니다.\n\n이 위치로 연결할까요?',
                source,destination,len(plan['updates']),plan['found'],plan['missing'])
            if QMessageBox.question(w,tr('폴더 위치 다시 지정'),text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
            count=self.apply(plan)
            w.statusBar().showMessage(tr('{0}개 원본 참조를 다시 연결했습니다.',count),10000)
        finally:
            w.maintenance_running=False
            QTimer.singleShot(0,self.refresh)

    def shutdown(self):
        self.timer.stop();self.cancel.set();self.recovery_cancel.set()
