"""Library workflows exposed through native menus and a collection sidebar."""
from .i18n import tr
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import json
import re
import numpy as np
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QColor,QAction
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QTreeWidget,
    QTreeWidgetItem,QDialog,QFormLayout,QLineEdit,QComboBox,QCheckBox,QDialogButtonBox,
    QFileDialog,QInputDialog,QMessageBox,QGridLayout,QScrollArea,QPlainTextEdit)
from .library import matches,move_files,move_folder,relink,duplicate_groups,restore_backup,write_sidecar,read_sidecar
from .engine import normalized,defaults,load_image,develop,resize_float
from .widgets import PhotoView


EDIT_GROUPS={
 '작업 색공간':('working_space',),
 '빛':('exposure','contrast','highlights','shadows','whites','blacks'),
 'HDR':('hdr','hdr_limit','hdr_brightness','sdr_exposure','sdr_compression'),
 '화이트 밸런스':('temperature','tint','kelvin','wb_reference','kelvin_enabled','raw_mode','raw_kelvin','raw_tint','raw_neutral'),
 'RAW 프로파일':('dcp_profile','dcp_huesat','dcp_look','dcp_tone','dcp_exposure'),
 '색상':('saturation','vibrance','monochrome','mixer_mode','hsl','grading','grading_balance','calibration','point_colors','camera_matrix'),
 '컬러 필터':('photo_filter','photo_filter_enabled','photo_filter_density','photo_filter_luminosity'),
 '커브':('curve','rgb_curves','parametric'),
 '디테일':('clarity','texture','dehaze','sharpen','sharpen_radius','sharpen_detail','sharpen_mask','noise_luma','noise_color'),
 '효과':('grain','vignette'),
 '렌즈·원근':('distortion','distortion_k2','distortion_k3','lens_vignette','ca_red','ca_blue','defringe','lens_profile','perspective_v','perspective_h','aspect_scale','shift_x','shift_y','transform_scale','guides',
             'lensfun','lensfun_enabled','lensfun_distortion','lensfun_tca','lensfun_vignette','lensfun_scale','upright','upright_crop'),
 '크롭·회전':('crop','rotation','straighten','flip'), '마스크':('masks',),'복구':('retouch',)}


class FilterDialog(QDialog):
    def __init__(self,parent,rules=None,title='상세 필터'):
        super().__init__(parent);self.setWindowTitle(tr(title));self.fields={}
        self.setMinimumWidth(480)
        form=QFormLayout(self);rules=rules or {}
        definitions=[('name','파일 이름'),('keywords','키워드 · 계층은 | 로 구분'),('folder','폴더 경로'),
            ('camera','카메라'),('lens','렌즈'),('extension','파일 확장자'),('rating_min','최소 별점'),('rating_max','최대 별점'),
            ('flag','선택 1 / 제외 -1 / 해제 0'),('label','색 라벨'),('iso_min','최소 ISO'),('iso_max','최대 ISO'),
            ('date_from','시작 날짜 YYYY-MM-DD'),('date_to','종료 날짜 YYYY-MM-DD')]
        for key,text in definitions:
            field=QLineEdit(str(rules.get(key,'')));form.addRow(tr(text),field);self.fields[key]=field
        self.missing=QCheckBox(tr('누락 원본만'));self.missing.setChecked(rules.get('missing',False));form.addRow(self.missing)
        self.virtual=QCheckBox(tr('가상 사본만'));self.virtual.setChecked(rules.get('virtual',False));form.addRow(self.virtual)
        self.orientation=QComboBox();self.orientation.addItem(tr('전체'),'')
        for text,value in [('가로','landscape'),('세로','portrait'),('정사각','square')]:self.orientation.addItem(tr(text),value)
        self.orientation.setCurrentIndex(max(0,self.orientation.findData(rules.get('orientation',''))));form.addRow(tr('방향'),self.orientation)
        known=set(self.fields)|{'missing','virtual','orientation'}
        self.extra_rules=deepcopy({k:v for k,v in rules.items() if k not in known})
        self.keep_complex=QCheckBox(tr('기존 복합 조건 유지 · 위 조건을 추가로 적용'))
        self.keep_complex.setChecked(True)
        if self.extra_rules:
            from .smart_rules import KEY,describe
            text=describe(self.extra_rules[KEY]) if KEY in self.extra_rules else json.dumps(self.extra_rules,ensure_ascii=False,indent=2)
            details=QPlainTextEdit(text);details.setReadOnly(True);details.setMaximumHeight(110)
            details.setAccessibleName(tr('기존 복합 검색 조건'))
            form.addRow(self.keep_complex);form.addRow(details)
        else:self.keep_complex.hide()
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate);buttons.rejected.connect(self.reject);form.addRow(buttons)

    def validate(self):
        try:self.rules();self.accept()
        except ValueError as e:QMessageBox.information(self,tr('필터'),str(e))

    def rules(self):
        rules=deepcopy(self.extra_rules) if self.keep_complex.isChecked() else {}
        rules.update({k:f.text().strip() for k,f in self.fields.items() if f.text().strip()})
        for key in ('rating_min','rating_max','flag','iso_min','iso_max'):
            if key in rules:rules[key]=int(rules[key])
        for key in ('date_from','date_to'):
            if key in rules:datetime.strptime(rules[key],'%Y-%m-%d')
        if self.missing.isChecked():rules['missing']=True
        if self.virtual.isChecked():rules['virtual']=True
        if self.orientation.currentData():rules['orientation']=self.orientation.currentData()
        return rules


class LibraryManager:
    def __init__(self,w):
        self.w=w;self.rules={};self.collection=None;self.collapsed=False;self.syncing=False;self.dialogs=[]
        self.copy_keys=set(defaults())-set(EDIT_GROUPS['크롭·회전'])
        self.auto_sync=False
        self.build_collections();self.build_menus()
        self.smart_timer=QTimer(w);self.smart_timer.setInterval(60000)
        self.smart_timer.timeout.connect(self.refresh_timed_collection);self.smart_timer.start()

    def refresh_timed_collection(self):
        w=self.w
        if (getattr(w,'closing',False) or getattr(w,'maintenance_running',False) or w.library_loading
                or w.import_busy or w.import_scans):return
        from .smart_rules import time_dependent
        rules=[self.rules]
        if self.collection is not None:
            row=w.catalog.db.execute('SELECT rules FROM collections WHERE id=?',(self.collection,)).fetchone()
            if row and row['rules']:rules.append(json.loads(row['rules']))
        if any(time_dependent(rule) for rule in rules):w.refresh_lists()

    def run(self,fn):
        try:fn()
        except Exception as e:QMessageBox.warning(self.w,tr('작업을 완료하지 못했습니다'),tr(str(e)))

    def add(self,menu,title,fn):
        action=menu.addAction(tr(title));action.triggered.connect(lambda _:self.run(fn));return action

    def build_menus(self):
        bar=self.w.menuBar()
        files=bar.addMenu(tr('파일 관리'))
        for text,fn in [('선택 사진 이름 변경',self.rename),('선택 사진 이동',self.move),('폴더 이름 변경·이동',self.move_selected_folder),
            ('카탈로그에서만 제거',lambda:self.remove(False)),('원본을 휴지통으로',lambda:self.remove(True)),
            ('누락 원본 다시 연결',self.relink),('중복 파일 검사',self.duplicates),
            ('오프라인 편집용 미리보기 만들기',self.smart_previews),('카탈로그 백업',self.backup),('구글 드라이브 백업…',self.cloud_backup),('카탈로그 복원',self.restore),
            ('카탈로그 최적화',self.optimize_catalog),('카탈로그 검사',self.integrity)]:self.add(files,text,fn)
        library=bar.addMenu(tr('사진 관리'))
        for text,fn in [('가상 사본 만들기',self.virtual_copy),('현재 보정 스냅샷 저장',self.snapshot),('스냅샷 불러오기',self.restore_snapshot),
            ('선택 사진 스택으로 묶기',self.stack),('선택 사진 스택 해제',self.unstack),('메타데이터 편집',self.metadata),
            ('촬영 시각 일괄 이동',self.shift_date),('XMP 저장',self.write_xmp),('XMP 불러오기',self.read_xmp),
            ('프리셋 파일 가져오기',self.import_preset),('프리셋 파일 내보내기',self.export_preset),('사용자 프리셋 삭제',self.delete_preset)]:self.add(library,text,fn)
        labels=library.addMenu(tr('색 라벨'))
        for name in ['','빨강','노랑','초록','파랑','보라']:self.add(labels,name or '해제',lambda n=name:self.set_label(n))
        view=bar.addMenu(tr('보기·필터'))
        for text,fn in [('상세 필터',self.filter_dialog),('필터·컬렉션 해제',self.clear_filter),('선택 사진 비교',lambda:self.compare(2)),
            ('선택 사진 모아보기',lambda:self.compare(12)),('현재 사진 전·후 비교',lambda:self.compare(2,before=True))]:self.add(view,text,fn)
        self.stack_action=self.add(view,'스택 접어서 보기',self.toggle_stacks);self.stack_action.setCheckable(True)
        self.add(view,'화면 색상 관리…',self.w.display_color.show_dialog)
        self.add(view,'모니터 색상 자동 감지',lambda:self.w.display_color.configure('auto'))
        self.add(view,'모니터 ICC 프로파일 선택',self.monitor_profile)
        self.add(view,'모니터 ICC 보정 해제',self.clear_monitor_profile)
        self.add(library,'전체 EXIF 메타데이터 새로 읽기',self.refresh_metadata)
        self.add(library,'RAW 가져오기 기본 보정',self.w.studio.raw_defaults)
        self.add(files,'선택 사진 미리보기 다시 만들기',lambda:self.w.preview_queue.start_batch(self.w.selected_ids()))
        self.add(files,'누락 폴더 다시 연결',self.relink_folder)
        self.add(library,'계층형 키워드 트리',self.keyword_tree)
        collections=bar.addMenu(tr('컬렉션'))
        self.add(collections,'선택 컬렉션 이름·규칙 변경',self.edit_collection)
        self.add(collections,'선택 사진을 컬렉션에서 제외',self.remove_from_collection)
        edits=bar.addMenu(tr('보정 동기화'))
        self.add(edits,'복사할 항목 선택',self.choose_copy)
        self.add(edits,'현재 보정 → 선택 사진',self.sync_selected)
        self.sync_action=self.add(edits,'Auto Sync · 선택한 사진 함께 보정',self.toggle_auto_sync);self.sync_action.setCheckable(True)

    def build_collections(self):
        sidebar=self.w.folder_tree.parentWidget().layout()
        container=QWidget();box=QVBoxLayout(container);box.setContentsMargins(0,0,0,0)
        self.tree=QTreeWidget();self.tree.setHeaderHidden(True);self.tree.setMaximumHeight(210);box.addWidget(self.tree)
        self.tree.currentItemChanged.connect(self.select_collection)
        row=QHBoxLayout();box.addLayout(row)
        for title,fn in [('+',lambda:self.create_collection(False)),('스마트',lambda:self.create_collection(True)),('추가',self.add_to_collection),('삭제',self.remove_collection)]:
            b=QPushButton(tr(title));b.clicked.connect(lambda _,f=fn:self.run(f));row.addWidget(b)
        self.w.add_foldout(sidebar,tr('컬렉션'),container)
        self.refresh_collections()

    def refresh_collections(self):
        self.tree.blockSignals(True);self.tree.clear();items={}
        collections=self.w.catalog.collections()
        for c in collections:
            item=QTreeWidgetItem([('◈ ' if c['rules'] is not None else '')+c['name']])
            item.setData(0,Qt.ItemDataRole.UserRole,c['id']);items[c['id']]=item
        for c in collections:
            parent=items.get(c['parent_id'])
            if parent:parent.addChild(items[c['id']])
            else:self.tree.addTopLevelItem(items[c['id']])
            if c['id']==self.collection:self.tree.setCurrentItem(items[c['id']])
        self.tree.expandAll();self.tree.blockSignals(False)

    def select_collection(self,item,*_):
        self.collection=item.data(0,Qt.ItemDataRole.UserRole) if item else None
        self.w.folder_filter=None;self.w.refresh_lists(ensure_current=True);self.w.set_mode(1)

    def create_collection(self,smart):
        selected=self.w.selected_ids()
        name,ok=QInputDialog.getText(self.w,tr('스마트 컬렉션') if smart else tr('컬렉션'),tr('이름'))
        if not ok or not name.strip():return
        rules=None
        if smart:
            dialog=FilterDialog(self.w,title='스마트 컬렉션 규칙')
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            rules=dialog.rules()
        self.collection=self.w.catalog.add_collection(name.strip(),rules,parent_id=self.collection)
        if not smart:self.w.catalog.collection_add(self.collection,selected)
        self.refresh_collections();self.w.refresh_lists(ensure_current=True)

    def add_to_collection(self):
        choices=[c for c in self.w.catalog.collections() if c['rules'] is None]
        if not choices:raise ValueError('일반 컬렉션을 먼저 만들어 주세요.')
        labels=[f'{c["id"]} · {c["name"]}' for c in choices]
        chosen,ok=QInputDialog.getItem(self.w,tr('컬렉션에 추가'),tr('선택한 사진을 추가할 컬렉션'),labels,editable=False)
        if ok:self.w.catalog.collection_add(choices[labels.index(chosen)]['id'],self.w.selected_ids());self.w.refresh_lists()

    def remove_collection(self):
        if self.collection is not None:
            self.w.catalog.remove_collection(self.collection);self.collection=None;self.refresh_collections();self.w.refresh_lists()

    def filtered(self,photos):
        members=self.w.catalog.collection_ids(self.collection) if self.collection is not None else None
        result=[p for p in photos if (members is None or p['id'] in members) and matches(p,self.rules)]
        if self.collapsed:
            seen=set();filtered=[]
            for p in result:
                stack=p.get('stack_id')
                if stack is not None and stack in seen:continue
                if stack is not None:seen.add(stack)
                filtered.append(p)
            result=filtered
        return result

    def filter_dialog(self):
        dialog=FilterDialog(self.w,self.rules)
        if dialog.exec()==QDialog.DialogCode.Accepted:self.rules=dialog.rules();self.w.refresh_lists(ensure_current=True)

    def clear_filter(self):
        self.rules={};self.collection=None;self.refresh_collections();self.w.set_filter('all')

    def toggle_stacks(self):self.collapsed=self.stack_action.isChecked();self.w.refresh_lists(ensure_current=True)
    def stack(self):self.w.catalog.stack(self.w.selected_ids());self.w.refresh_lists()
    def unstack(self):
        self.w.catalog.update_many(self.w.selected_ids(),'stack_id',None)
        self.w.refresh_lists()
    def set_label(self,label):
        self.w.catalog.update_many(self.w.selected_ids(),'label',label)
        self.w.refresh_lists()

    def virtual_copy(self):
        self.w.commit()
        for i in self.w.selected_ids():self.w.catalog.virtual_copy(i)
        self.w.refresh_lists()

    def snapshot(self):
        if self.w.current_id is None:return
        name,ok=QInputDialog.getText(self.w,tr('스냅샷'),tr('이름'))
        if ok and name.strip():self.w.commit();self.w.catalog.snapshot(self.w.current_id,name.strip())

    def restore_snapshot(self):
        snapshots=self.w.catalog.snapshots(self.w.current_id)
        if not snapshots:raise ValueError('저장된 스냅샷이 없습니다.')
        labels=[f'{s["id"]} · {s["name"]}' for s in snapshots]
        chosen,ok=QInputDialog.getItem(self.w,tr('스냅샷'),tr('복원할 보정'),labels,editable=False)
        if ok:
            self.w.commit();self.w.settings=normalized(snapshots[labels.index(chosen)]['settings'])
            self.w.commit('스냅샷 복원');self.w.load_controls();self.w.render_version+=1;self.w.render()

    def selected_photos(self):return [self.w.catalog.photo(i) for i in self.w.selected_ids()]

    def reload_current(self):
        ident=self.w.current_id;self.w.clear_active_photo();self.w.refresh_lists()
        if ident and self.w.catalog.photo(ident):self.w.activate(ident)

    def rename(self):
        template,ok=QInputDialog.getText(self.w,tr('파일 이름 변경'),tr('{stem}: 원래 이름 / {n:03d}: 연속 번호'),text='{stem}_{n:03d}')
        if not ok or not template:return
        paths=list(dict.fromkeys(p['path'] for p in self.selected_photos()));mapping={}
        for n,source in enumerate(paths,1):
            src=Path(source);name=template.format(stem=src.stem,n=n)+src.suffix
            if Path(name).name!=name or any(c in name for c in '<>:"/\\|?*'):raise ValueError('파일 이름에 경로나 특수문자를 넣을 수 없습니다.')
            mapping[source]=src.with_name(name)
        self.w.commit();count=move_files(self.w.catalog,mapping);self.reload_current()
        self.w.statusBar().showMessage(tr('{0}개 파일 이름을 변경했습니다.', f'{count}'))

    def move(self):
        folder=QFileDialog.getExistingDirectory(self.w,tr('선택 사진을 이동할 폴더'))
        if folder:
            self.w.commit();move_files(self.w.catalog,{p['path']:Path(folder)/Path(p['path']).name for p in self.selected_photos()});self.reload_current()

    def move_selected_folder(self):
        if not self.w.folder_filter:raise ValueError('왼쪽에서 이동할 사진 폴더를 선택하세요.')
        source=Path(self.w.folder_filter)
        destination,ok=QInputDialog.getText(self.w,tr('폴더 이름 변경·이동'),tr('새 폴더의 전체 경로'),text=str(source))
        if ok and destination and Path(destination)!=source:
            self.w.commit();move_folder(self.w.catalog,source,destination);self.w.folder_filter=str(Path(destination).resolve());self.reload_current()

    def remove(self,trash):
        ids=self.w.selected_ids()
        if not ids:return
        text=f'선택한 {len(ids)}장을 카탈로그에서 제거합니다.'
        if trash:text+='\n원본 파일은 휴지통으로 이동하며 같은 원본의 가상 사본도 제거됩니다.'
        else:text+='\n원본 파일은 그대로 남습니다.'
        if QMessageBox.question(self.w,tr('사진 제거'),text)!=QMessageBox.StandardButton.Yes:return
        self.w.commit();paths={self.w.catalog.photo(i)['path'] for i in ids}
        if trash:
            from send2trash import send2trash
            for path in paths:
                if Path(path).exists():send2trash(path)
            ids=[p['id'] for p in self.w.catalog.photos() if p['path'] in paths]
        self.w.clear_active_photo();self.w.catalog.remove_photos(ids)
        if not trash:self.w.folder_sync.exclude(paths-set(self.w.catalog.paths()))
        self.w.refresh_lists(ensure_current=True)

    def relink(self):
        if not self.w.current_id:return
        path,_=QFileDialog.getOpenFileName(self.w,tr('원본 사진 다시 연결'))
        if path:relink(self.w.catalog,self.w.current_id,path);self.reload_current()

    def duplicates(self):
        photos=self.w.catalog.photos();self.w.statusBar().showMessage(tr('파일 내용으로 중복 검사 중…'))
        def done(groups):
            if not groups:QMessageBox.information(self.w,tr('중복 검사'),tr('내용이 같은 파일이 없습니다.'));return
            dialog=QDialog(self.w);dialog.setWindowTitle(tr('중복 파일 · 내용 해시 일치'));dialog.resize(720,480)
            box=QVBoxLayout(dialog);tree=QTreeWidget();tree.setHeaderLabels([tr('파일'),tr('경로')]);box.addWidget(tree)
            for n,rows in enumerate(groups,1):
                group=QTreeWidgetItem(tree,[f'그룹 {n} · {len(rows)}개'])
                for p in rows:QTreeWidgetItem(group,[p['name'],p['path']])
            tree.expandAll();self.dialogs.append(dialog);dialog.show()
        self.w.spawn(lambda:duplicate_groups(photos),done)

    def smart_previews(self):
        photos=self.selected_photos()
        if not photos:return
        directory=self.w.catalog.directory/'previews';directory.mkdir(exist_ok=True)
        self.w.statusBar().showMessage(tr('오프라인 편집 미리보기 만드는 중…'))
        def work():
            for p in photos:
                source,info=load_image(p['path'],1800,p['settings']['working_space'],raw_options=p['settings'])
                from .rawcolor import CameraSource
                extra={}
                if isinstance(source,CameraSource):
                    legacy,_=load_image(p['path'],1800,p['settings']['working_space'])
                    extra['legacy_pixels']=legacy
                    info['raw_dual_preview']=True
                np.savez_compressed(directory/f'{p["id"]}.npz',pixels=source.astype(np.float32 if isinstance(source,CameraSource) else np.float16),info=json.dumps(info),**extra)
            return len(photos)
        self.w.spawn(work,lambda count:self.w.statusBar().showMessage(tr('{0}장: 원본 없이도 편집할 수 있습니다. 원본 출력에는 원본 연결이 필요합니다.', f'{count}')))

    def backup(self):
        path,_=QFileDialog.getSaveFileName(self.w,tr('카탈로그 백업'),f'Grainy-{datetime.now():%Y%m%d-%H%M%S}.sqlite','SQLite (*.sqlite)')
        if path:
            self.w.commit();self.w.save_keywords();self.w.catalog.backup(path)
            QMessageBox.information(self.w,tr('백업'),tr('카탈로그·보정·컬렉션을 백업했습니다. 원본 사진은 별도로 보관하세요.'))

    def cloud_backup(self):
        from .cloud_backup import dialog
        dialog(self.w)

    def restore(self):
        if self.w.import_busy or self.w.import_scans or self.w.export_running:raise ValueError('가져오기·내보내기 완료 후 복원하세요.')
        folder=self.w.cloud_backup.settings()['folder'] if hasattr(self.w,'cloud_backup') else ''
        path,_=QFileDialog.getOpenFileName(self.w,tr('카탈로그 백업 복원'),folder if folder and Path(folder).is_dir() else '','SQLite (*.sqlite)')
        if not path:return
        if QMessageBox.question(self.w,tr('카탈로그 복원'),tr('현재 카탈로그를 자동 백업한 뒤 선택한 백업으로 복원할까요?'))!=QMessageBox.StandardButton.Yes:return
        self.w.clear_active_photo();self.w.pool.waitForDone()
        self.w.preview_queue.stop();self.w.preview_queue.clear_visible()
        self.w.thumbnail_render_pool.waitForDone();self.w.thumbnail_pool.waitForDone()
        self.w.library_cancel.set();self.w.library_version+=1;self.w.browser_pool.waitForDone()
        self.w.photo_model.replace([]);self.w.photo_model.invalidate_thumbnails()
        restore_backup(self.w.catalog,path)
        self.collection=None;self.rules={};self.w.folder_filter=None
        self.refresh_collections();self.w.refresh_presets();self.w.refresh_lists(ensure_current=True)

    def integrity(self):
        status=self.w.catalog.db.execute('PRAGMA integrity_check').fetchone()[0]
        photos=self.w.catalog.photos()
        self.w.spawn(lambda:sum(not Path(p['path']).is_file() for p in photos),
            lambda missing:QMessageBox.information(self.w,tr('카탈로그 검사'),tr('데이터베이스: {0}\n사진 {1}장 / 누락된 원본 참조 {2}개', f'{status}', f'{len(photos)}', f'{missing}')))

    def optimize_catalog(self):
        w=self.w
        if getattr(w,'maintenance_running',False):return
        if w.import_busy or w.import_scans or w.export_running or w.jobs:
            raise ValueError('진행 중인 사진 처리 작업이 끝난 뒤 최적화를 시작하세요.')
        from .catalog_maintenance import optimize
        from .maintenance_dialog import MaintenanceDialog
        w.commit();w.save_keywords()
        w.maintenance_running=True
        dialog=MaintenanceDialog(w);self.maintenance_dialog=dialog
        timers=[w.photo_model.check_timer,w.extras.timer]
        active=[timer for timer in timers if timer.isActive()]
        for timer in active:timer.stop()
        def release():
            w.catalog.profiles.clear();w.maintenance_running=False
            dialog.finish()
            for timer in active:timer.start()
        def finished(result):
            self.last_optimization=result;release()
            text=('최적화를 취소했습니다. 완료된 정리는 유지됩니다.' if result['cancelled'] else '카탈로그 최적화를 마쳤습니다.')
            text+=f'\n정리한 기록: {result["converted"]}개 · 보관한 프로파일: {result["profiles"]}개'
            text+=f'\n카탈로그: {result["before_bytes"]/1024**2:.2f} → {result["after_bytes"]/1024**2:.2f} MiB (백업 제외)'
            text+='\n무결성: '+result['integrity']
            if result['backup']:text+='\n자동 백업: '+result['backup']
            if result['warnings']:text+='\n확인이 필요한 기록:\n'+'\n'.join(result['warnings'])
            QMessageBox.information(w,tr('카탈로그 최적화'),text)
        def failed(error):
            release();w.show_error(error)
        dialog.show()
        w.spawn(lambda:optimize(w.catalog.directory,dialog.cancel,dialog.updates.put),finished,failed)

    def metadata(self):
        photos=self.selected_photos()
        if not photos:return
        dialog=QDialog(self.w);dialog.setWindowTitle(tr('메타데이터 · {0}장', f'{len(photos)}'));form=QFormLayout(dialog);fields={}
        original=photos[0]['user_metadata']
        definitions=[('title','제목'),('caption','설명'),('creator','촬영자'),('copyright','저작권'),('location','장소'),
            ('city','도시'),('country','국가'),('DateTimeOriginal','촬영 시각 YYYY:MM:DD HH:MM:SS'),('latitude','위도'),('longitude','경도')]
        for key,title in definitions:
            field=QLineEdit(str(original.get(key,'')));fields[key]=field;form.addRow(title,field)
        keywords=QLineEdit(photos[0]['keywords']);form.addRow(tr('키워드 · 여행|일본, 필름'),keywords)
        note=QLabel(tr('변경한 항목만 선택한 사진에 적용합니다. 원본 파일은 수정하지 않습니다.'));note.setWordWrap(True);form.addRow(note)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);form.addRow(buttons)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        changes={k:f.text() for k,f in fields.items() if f.text()!=str(original.get(k,''))}
        for key,limit in [('latitude',90),('longitude',180)]:
            if changes.get(key) and not -limit<=float(changes[key])<=limit:raise ValueError('GPS 좌표 범위를 확인하세요.')
        if changes.get('DateTimeOriginal'):datetime.strptime(changes['DateTimeOriginal'],'%Y:%m:%d %H:%M:%S')
        for p in photos:
            self.w.catalog.set_user_metadata(p['id'],changes)
            if keywords.text()!=photos[0]['keywords']:self.w.catalog.update(p['id'],keywords=keywords.text())
        self.w.load_controls();self.w.refresh_lists()

    def shift_date(self):
        minutes,ok=QInputDialog.getInt(self.w,tr('촬영 시각 이동'),tr('이동할 분 (예: +540 / -60)'),0,-525600,525600)
        if not ok:return
        updates=[]
        for p in self.selected_photos():
            stamp=p['user_metadata'].get('DateTimeOriginal') or p['info'].get('date')
            if not stamp:continue
            dt=datetime.strptime(str(stamp),'%Y:%m:%d %H:%M:%S')+timedelta(minutes=minutes)
            updates.append((p['id'],dt.strftime('%Y:%m:%d %H:%M:%S')))
        for ident,stamp in updates:self.w.catalog.set_user_metadata(ident,{'DateTimeOriginal':stamp})
        self.w.statusBar().showMessage(tr('{0}장 촬영 시각을 변경했습니다.', f'{len(updates)}'))

    def write_xmp(self):
        self.w.commit();self.w.save_keywords()
        for p in self.selected_photos():
            suffix=f'.luma-copy-{p["id"]}.xmp' if p['virtual_source'] is not None else '.xmp'
            write_sidecar(p,Path(p['path']).with_suffix(suffix))
        self.w.statusBar().showMessage(tr('XMP에 별점·키워드·Grainy 보정값을 저장했습니다.'))

    def read_xmp(self):
        self.w.commit()
        for p in self.selected_photos():
            path=Path(p['path']).with_suffix(f'.luma-copy-{p["id"]}.xmp' if p['virtual_source'] is not None else '.xmp')
            if not path.exists():continue
            data=read_sidecar(path)
            if 'settings' in data:self.edit_photo(p['id'],data['settings'],'XMP 불러오기')
            if 'user_metadata' in data:self.w.catalog.set_user_metadata(p['id'],data['user_metadata'])
            self.w.catalog.update(p['id'],**{k:v for k,v in data.items() if k in ('rating','label','keywords')})
        self.reload_current()

    def import_preset(self):
        path,_=QFileDialog.getOpenFileName(self.w,tr('프리셋 가져오기'),'','사진 프리셋 (*.json *.xmp *.lrtemplate)')
        if not path:return
        suffix=Path(path).suffix.lower()
        if suffix=='.xmp':data=read_sidecar(path)
        elif suffix=='.lrtemplate':data={}
        else:data=json.loads(Path(path).read_text(encoding='utf-8'))
        settings=data.get('settings') or data.get('edits')
        if not isinstance(settings,dict) and suffix in ('.xmp','.lrtemplate'):
            from .adobe_preset import read,KEY
            preset=read(path)
            name=Path(path).stem;existing=set(self.w.catalog.presets());suffix=2
            while name in existing:
                name=f'{Path(path).stem} ({suffix})';suffix+=1
            review=QMessageBox(self.w);review.setWindowTitle(tr('Adobe 프리셋 가져오기'))
            review.setTextFormat(Qt.TextFormat.PlainText)
            review.setText(tr('“{0}” 프리셋을 추가합니다.\n', f'{name}')+preset[KEY]['preview'])
            if preset[KEY]['preview']!=preset[KEY]['report']:review.setDetailedText(preset[KEY]['report'])
            review.setStandardButtons(QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Cancel)
            review.button(QMessageBox.StandardButton.Save).setText(tr('프리셋 추가'))
            review.button(QMessageBox.StandardButton.Cancel).setText(tr('취소'))
            if review.exec()!=QMessageBox.StandardButton.Save:return
            develop(np.full((32,32,3),.2,np.float32),normalized(preset))
            self.w.catalog.save_adobe_preset(name,preset);self.w.refresh_presets();return
        if not isinstance(settings,dict):raise ValueError('Luma 보정값이 들어 있는 프리셋이 아닙니다. Adobe 현상값은 직접 호환되지 않습니다.')
        # Exercise the processing pipeline before accepting a foreign preset.
        settings=normalized(settings);develop(np.full((32,32,3),.2,np.float32),settings)
        self.w.catalog.save_preset(data.get('name',Path(path).stem),settings);self.w.refresh_presets()

    def export_preset(self):
        path,_=QFileDialog.getSaveFileName(self.w,tr('현재 보정 프리셋 내보내기'),'preset.luma.json','Grainy preset (*.json)')
        if path:Path(path).write_text(json.dumps({'name':Path(path).stem,'settings':self.w.settings},ensure_ascii=False,indent=2),encoding='utf-8')

    def delete_preset(self):
        names=list(self.w.catalog.presets())
        if not names:raise ValueError('삭제할 사용자 프리셋이 없습니다.')
        name,ok=QInputDialog.getItem(self.w,tr('프리셋 삭제'),tr('사용자 프리셋'),names,editable=False)
        if ok:self.w.catalog.delete_preset(name);self.w.refresh_presets()

    def choose_copy(self):
        dialog=QDialog(self.w);dialog.setWindowTitle(tr('복사·동기화 항목'));box=QVBoxLayout(dialog);checks={}
        from . import engine
        for title,keys in EDIT_GROUPS.items():
            c=QCheckBox(tr(title));c.setChecked(bool(set(keys)&self.copy_keys));box.addWidget(c);checks[title]=c
            if title=='HDR':c.setVisible(engine.HDR_FEATURE)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);box.addWidget(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.copy_keys={k for title,keys in EDIT_GROUPS.items() if checks[title].isChecked() for k in keys}
            self.w.copy_crop.setChecked(checks['크롭·회전'].isChecked());self.w.copy_edits()

    def edit_photo(self,ident,settings,label):
        from .rawcolor import retarget
        settings=retarget(settings,self.w.catalog.photo(ident)['info'])
        previous=self.w.catalog.photo(ident)['settings']
        if previous==settings:return
        state=self.w.catalog.preference(f'undo:{ident}',{'undo':[],'redo':[]})
        state['undo']=(state['undo']+[previous])[-80:];state['redo']=[]
        self.w.catalog.edit(ident,settings,label);self.w.catalog.save_preference(f'undo:{ident}',state)
        self.w.photo_model.invalidate_thumbnails([ident])

    def sync_selected(self):
        if self.w.current_id is None:return
        self.w.commit();values=deepcopy(self.w.settings)
        ids=[i for i in self.w.selected_ids() if i!=self.w.current_id]
        for ident in ids:
            settings=self.w.catalog.photo(ident)['settings'];settings.update({k:deepcopy(values[k]) for k in self.copy_keys})
            if 'lensfun' in self.copy_keys:
                from .optics import retarget
                settings=retarget(settings,self.w.catalog.photo(ident)['info'])
            self.edit_photo(ident,settings,'보정 동기화')
        self.refresh_thumbnails(ids);self.w.statusBar().showMessage(tr('{0}장에 선택한 보정 항목을 동기화했습니다.', f'{len(ids)}'))

    def toggle_auto_sync(self):
        self.auto_sync=self.sync_action.isChecked()
        self.w.statusBar().showMessage(tr('Auto Sync: 현재 선택한 사진들에 변경 항목을 함께 적용합니다.') if self.auto_sync else tr('Auto Sync 해제'))

    def after_commit(self,previous):
        if not self.auto_sync or self.syncing:return
        changes={k:deepcopy(v) for k,v in self.w.settings.items() if k in self.copy_keys and previous.get(k)!=v}
        if not changes:return
        self.syncing=True
        try:
            ids=[i for i in self.w.selected_ids() if i!=self.w.current_id]
            for ident in ids:
                values=self.w.catalog.photo(ident)['settings'];values.update(changes)
                if 'lensfun' in changes:
                    from .optics import retarget
                    values=retarget(values,self.w.catalog.photo(ident)['info'])
                self.edit_photo(ident,values,'Auto Sync')
            self.refresh_thumbnails(ids)
        finally:self.syncing=False

    def refresh_thumbnails(self,ids):
        self.w.photo_model.invalidate_thumbnails(ids)
        # Painting queues only the visible photographs; a selected batch may
        # contain hundreds of thousands of off-screen images.

    def compare(self,limit,before=False):
        self.w.commit();photos=self.selected_photos()[:limit]
        if not photos:return
        if before:photos=[self.w.catalog.photo(self.w.current_id)]*2
        dialog=QDialog(self.w);dialog.setWindowTitle(tr('전·후 비교') if before else tr('선택 사진 비교 / 모아보기'));dialog.resize(1200,750)
        target=self.w.display_color.watch(dialog);views=[]
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        def closed():
            self.w.display_color.release(target)
            if dialog in self.dialogs:self.dialogs.remove(dialog)
        dialog.finished.connect(closed)
        def recolor():
            for view in views:self.w.display_color.refresh_view(view,target)
        target.changed.connect(recolor)
        grid=QGridLayout(dialog)
        for index,p in enumerate(photos):
            cell=QWidget();box=QVBoxLayout(cell)
            label=QLabel((tr('원본') if index==0 else tr('보정 후')) if before else p['name']);box.addWidget(label)
            view=PhotoView();views.append(view);box.addWidget(view,1)
            row=QHBoxLayout();box.addLayout(row)
            fit=QPushButton(tr('맞춤'));fit.clicked.connect(view.fit_photo);row.addWidget(fit)
            remove=QPushButton(tr('보기에서 제외'));remove.clicked.connect(cell.hide);row.addWidget(remove)
            grid.addWidget(cell,index//(2 if len(photos)<=4 else 3),index%(2 if len(photos)<=4 else 3))
            settings=defaults() if before and index==0 else p['settings']
            def load(path=p['path'],s=settings):
                from .engine import output_rgb
                source,info=load_image(path,1800,s['working_space'],raw_options=s)
                working=develop(source,s,output_space=None,original_size=(info['width'],info['height']))
                return output_rgb(working,s['working_space']),(working,s['working_space']) if s['working_space']=='ProPhoto' else None
            def loaded(result,v=view):
                if target.closed or not dialog.isVisible():return
                image,working=result;v.display_source=working
                v.set_image(image);self.w.display_color.refresh_view(v,target)
            self.w.spawn(load,loaded,lambda error,l=label:l.setText(tr('원본을 읽을 수 없습니다.')) if not target.closed else None)
        self.dialogs.append(dialog);dialog.show()

    def monitor_profile(self):
        path,_=QFileDialog.getOpenFileName(self.w,tr('모니터 ICC 프로파일 선택'),'','ICC (*.icc *.icm)')
        if path:
            self.w.display_color.configure('manual',path)

    def clear_monitor_profile(self):
        self.w.display_color.configure('off')

    def refresh_metadata(self):
        from .engine import read_metadata,VIDEO_EXTENSIONS
        photos=self.w.catalog.photos()
        def work():
            results=[];failures=0
            for p in photos:
                if Path(p['path']).suffix.lower() in VIDEO_EXTENSIONS:continue
                try:results.append((p['id'],{**json.loads(p['metadata']),**read_metadata(p['path'])}))
                except Exception:failures+=1
            return results,failures
        def done(result):
            values,failures=result
            with self.w.catalog.db:
                self.w.catalog.db.executemany('UPDATE photos SET metadata=? WHERE id=?',[(json.dumps(info),ident) for ident,info in values])
            self.w.refresh_lists();self.w.statusBar().showMessage(tr('메타데이터 {0}장 갱신 · 읽을 수 없음 {1}장', f'{len(values)}', f'{failures}'))
        self.w.spawn(work,done)

    def relink_folder(self):
        if not self.w.folder_filter:raise ValueError('왼쪽에서 누락된 폴더를 선택하세요.')
        self.w.folder_panel.locate(self.w.folder_filter)

    def edit_collection(self):
        if self.collection is None:return
        c=next(x for x in self.w.catalog.collections() if x['id']==self.collection)
        name,ok=QInputDialog.getText(self.w,tr('컬렉션 편집'),tr('이름'),text=c['name'])
        if not ok or not name.strip():return
        rules=c['rules']
        if rules is not None:
            dialog=FilterDialog(self.w,json.loads(rules),'스마트 컬렉션 규칙')
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            rules=json.dumps(dialog.rules())
        self.w.catalog.db.execute('UPDATE collections SET name=?,rules=? WHERE id=?',(name.strip(),rules,self.collection))
        self.w.catalog.db.commit();self.refresh_collections();self.w.refresh_lists(ensure_current=True)

    def remove_from_collection(self):
        if self.collection is None:return
        with self.w.catalog.db:
            self.w.catalog.db.executemany('DELETE FROM collection_members WHERE collection_id=? AND photo_id=?',[(self.collection,i) for i in self.w.selected_ids()])
        self.w.refresh_lists(ensure_current=True)

    def keyword_tree(self):
        dialog=QDialog(self.w);dialog.setWindowTitle(tr('계층형 키워드'));dialog.resize(480,580);box=QVBoxLayout(dialog)
        tree=QTreeWidget();tree.setHeaderLabels([tr('키워드'),tr('사진')]);box.addWidget(tree);groups={};items={}
        for photo in self.w.catalog.photos():
            for keyword in photo['keywords'].split(','):
                parts=[k.strip() for k in keyword.strip().split('|') if k.strip()]
                for end in range(1,len(parts)+1):groups.setdefault('|'.join(parts[:end]),set()).add(photo['id'])
        for path,ids in sorted(groups.items()):
            parent=items.get(path.rpartition('|')[0]);item=QTreeWidgetItem([path.split('|')[-1],str(len(ids))])
            item.setData(0,Qt.ItemDataRole.UserRole,path)
            if parent:parent.addChild(item)
            else:tree.addTopLevelItem(item)
            items[path]=item
        def select(item,*_):
            self.rules={'keywords':item.data(0,Qt.ItemDataRole.UserRole)};self.collection=None
            self.w.folder_filter=None;self.w.refresh_lists(ensure_current=True);self.w.set_mode(1)
        tree.itemDoubleClicked.connect(select);tree.expandAll()
        box.addWidget(QLabel(tr('더블클릭으로 사진 필터 · 사진 관리 → 메타데이터 편집에서 키워드 수정')))
        self.dialogs.append(dialog);dialog.show()
