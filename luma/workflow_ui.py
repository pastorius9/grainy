"""Photo workflow entry points; all asynchronous results are snapshot checked."""
from copy import deepcopy
from pathlib import Path
from threading import Event
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QHBoxLayout,QLabel,
    QCheckBox,QLineEdit,QDialogButtonBox,QInputDialog,QMessageBox,QTreeWidget,
    QTreeWidgetItem,QPushButton,QProgressDialog)
from .i18n import tr
from . import workflow_data as data


class WorkflowTools:
    def __init__(self,w):
        self.w=w;self.previous_id=None;self.cancel=None;self.windows=[]
        menu=w.menuBar().addMenu(tr('사진 작업'))
        for title,method in [
            ('자동 톤 (선택한 사진)',self.auto_tone),('자동 색 (선택한 사진)',self.auto_color),('자동 화이트 밸런스',self.auto_white),
            ('스캔 테두리 자동 자르기 (선택한 사진)',self.border_crop),
            ('최신 처리 방식으로 업데이트 (선택한 사진)',self.update_process),
            ('현재 사진에 총노출 맞추기',self.match_exposure),
            ('이전 사진 보정 붙이기',self.previous_edits),('카메라 방향으로 복원',self.camera_orientation),
            ('빠른 컬렉션에 추가·제외',self.quick_toggle),('빠른 컬렉션 보기',self.quick_show),
            ('소속 컬렉션으로 이동…',self.containing_collection),('현재 사진을 스택 대표로',self.stack_cover),
            ('스택 안에서 앞으로',lambda:self.stack_move(-1)),('스택 안에서 뒤로',lambda:self.stack_move(1)),
            ('메타데이터 프리셋 저장…',self.save_metadata),('메타데이터 프리셋 적용…',self.apply_metadata),
            ('메타데이터 프리셋 삭제…',self.delete_metadata),('바로가기 키워드 설정…',self.set_keyword),
            ('바로가기 키워드 적용',self.apply_keyword),('내보내기 기록…',self.show_exports),
            ('내보내기 상태 초기화',self.reset_exports),('현재 사진을 참조 사진으로 고정',self.reference),
            ('두 번째 화면 열기',self.second_window)]:w.manager.add(menu,title,method)

    def records(self):
        self.w.commit();self.w.save_keywords()
        return [self.w.catalog.photo(i) for i in self.w.selected_ids()]

    def apply_edits(self,records,values,label):
        from .command_batch import apply
        from .rawcolor import retarget
        if not records:return []
        checked=[retarget(v,r['info']) for r,v in zip(records,values)]
        ids=apply(self.w.catalog,records,checked,label,remember=False)
        self.w.refresh_command_edits(ids)
        self.w.statusBar().showMessage(tr('{0} · {1}장 변경', tr(label), f'{len(ids):,}'))
        return ids

    def auto_white(self):
        w=self.w;records=self.records()
        if not records or self.cancel is not None:return
        cancel=Event();self.cancel=cancel;w.maintenance_running=True
        dialog=QProgressDialog(tr('자동 화이트 밸런스 계산 중…'),tr('취소'),0,0,w)
        dialog.setWindowModality(Qt.WindowModality.WindowModal);dialog.setMinimumDuration(0)
        dialog.canceled.connect(cancel.set);dialog.show()
        def work():
            from .engine import load_image
            values=[]
            for r in records:
                if cancel.is_set():return None
                source,_=load_image(r['path'],640,r['settings']['working_space'],raw_options=r['settings'])
                values.append(data.automatic_white_balance(source,r['settings']))
            return values
        def clear():
            # QProgressDialog emits canceled when closed; detach it so completion is not treated as a cancel.
            dialog.canceled.disconnect(cancel.set)
            self.cancel=None;w.maintenance_running=False;dialog.close();dialog.deleteLater()
        def done(values):
            clear()
            if values is not None and not cancel.is_set():
                # Includes edits made while the computation was in flight.
                w.commit();w.manager.run(lambda:self.apply_edits(records,values,'자동 화이트 밸런스'))
        def failed(error):clear();w.show_error(error)
        w.spawn(work,done,failed)

    def auto_tone(self):
        """The develop panel's auto tone, computed separately for every selected photo; one undo step each."""
        records=self.records()
        if not records:return
        values=self.w.command_auto_values(records)
        self.apply_edits(records,values,'자동톤')

    def auto_color(self):
        """The develop panel's auto colour for every selected photo, at the saved strength; one undo step each."""
        records=self.records()
        if not records:return
        from .auto_color import auto_color_settings
        amount=float(self.w.catalog.preference('auto_color_amount',100))/100
        values=self.w.command_auto_values(records,lambda source,settings:auto_color_settings(source,settings,amount)[0],'자동 색')
        self.apply_edits(records,values,'자동 색')

    def update_process(self):
        """Switch selected photos to the current processes (highlight roll-off, luminance sharpening, wavelet noise reduction); undoable per photo."""
        from .engine import normalized
        records=[r for r in self.records() if normalized(r['settings'])['tone_version']<3 or r['settings'].get('detail_version',3)<3]
        if not records:
            self.w.statusBar().showMessage(tr('선택한 사진은 이미 최신 처리 방식입니다.'),5000);return
        self.apply_edits(records,[{**r['settings'],'tone_version':3,'detail_version':3} for r in records],'최신 처리 방식으로 업데이트')

    def border_crop(self):
        """scan_border.crop_for on every selected photo; photos without a film edge stay unchanged. One undo step."""
        w=self.w;records=self.records()
        if not records or self.cancel is not None:return
        cancel=Event();self.cancel=cancel;w.maintenance_running=True
        dialog=QProgressDialog(tr('스캔 테두리 분석 중…'),tr('취소'),0,len(records),w)
        dialog.setWindowModality(Qt.WindowModality.WindowModal);dialog.setMinimumDuration(0)
        dialog.canceled.connect(cancel.set);dialog.show()
        def work():
            from .engine import load_image
            from .scan_border import crop_for
            crops=[]
            for r in records:
                if cancel.is_set():return None
                source,_=load_image(r['path'],1200,r['settings']['working_space'],raw_options=r['settings'])
                crops.append(crop_for(source,r['settings']))
            return crops
        def clear():
            dialog.canceled.disconnect(cancel.set)
            self.cancel=None;w.maintenance_running=False;dialog.close();dialog.deleteLater()
        def done(crops):
            clear()
            if crops is None or cancel.is_set():return
            found=[(r,{**r['settings'],'crop':list(c)}) for r,c in zip(records,crops) if c is not None]
            w.commit()
            if found:w.manager.run(lambda:self.apply_edits([r for r,_ in found],[v for _,v in found],'스캔 테두리 자동 자르기'))
            w.statusBar().showMessage(tr('스캔 테두리: {0}장 자름 · {1}장은 테두리를 찾지 못함', f'{len(found):,}', f'{len(records)-len(found):,}'),8000)
        def failed(error):clear();w.show_error(error)
        w.spawn(work,done,failed)

    def match_exposure(self):
        records=self.records();reference=self.w.catalog.photo(self.w.current_id)
        if not reference:return
        targets=[r for r in records if r['id']!=reference['id']]
        if not targets:raise ValueError(tr('기준 사진과 맞출 사진을 함께 선택하세요.'))
        values=[data.match_exposure(reference,r) for r in targets]
        self.apply_edits(targets,values,'총노출 맞추기')

    def previous_edits(self):
        records=self.records();source=self.w.catalog.photo(self.previous_id)
        if source is None:raise ValueError(tr('먼저 기준 사진을 열고 다음 사진으로 이동하세요.'))
        targets=[r for r in records if r['id']!=source['id']]
        values=[]
        for r in targets:
            value=deepcopy(r['settings']);value.update({k:deepcopy(source['settings'][k]) for k in self.w.manager.copy_keys})
            if 'lensfun' in self.w.manager.copy_keys:
                from .optics import retarget
                value=retarget(value,r['info'])
            values.append(value)
        self.apply_edits(targets,values,'이전 사진 보정')

    def camera_orientation(self):
        # Only rotated/flipped photos lose their crop; the crop is defined in the rotated frame.
        records=[r for r in self.records() if r['settings']['rotation'] or r['settings']['flip']]
        self.apply_edits(records,[{**r['settings'],'rotation':0,'flip':False,'crop':None} for r in records],'카메라 방향 복원')
        self.w.view.fit_photo()

    def quick_toggle(self):
        ids=self.w.selected_ids()
        if not ids:return
        _,added=data.toggle_quick(self.w.catalog,ids);self.w.manager.refresh_collections();self.w.refresh_lists()
        self.w.statusBar().showMessage(tr('빠른 컬렉션에 {0}장 추가', f'{len(ids):,}') if added else tr('빠른 컬렉션에서 {0}장 제외', f'{len(ids):,}'))

    def select_collection(self,ident):
        w=self.w;w.manager.collection=ident;w.manager.rules={};w.manager.refresh_collections()
        w.search.blockSignals(True);w.search.clear();w.search.blockSignals(False)
        w.set_filter('all')

    def quick_show(self):self.select_collection(data.quick_collection(self.w.catalog))

    def containing_collection(self):
        w=self.w;choices=[c for c in w.catalog.collections() if w.current_id in w.catalog.collection_ids(c['id'])]
        if not choices:raise ValueError(tr('이 사진이 속한 컬렉션이 없습니다.'))
        labels=[f'{c["id"]} · {c["name"]}' for c in choices]
        selected,ok=QInputDialog.getItem(w,tr('소속 컬렉션'),tr('이동할 컬렉션'),labels,editable=False)
        if ok:self.select_collection(choices[labels.index(selected)]['id'])

    def stack_cover(self):
        data.stack_order(self.w.catalog,self.w.current_id,0);self.w.refresh_lists()

    def stack_move(self,delta):
        order=data.stack_order(self.w.catalog,self.w.current_id)
        if order:data.stack_order(self.w.catalog,self.w.current_id,order.index(self.w.current_id)+delta);self.w.refresh_lists()

    def save_metadata(self):
        w=self.w;w.save_keywords();photo=w.catalog.photo(w.current_id)
        if not photo:return
        dialog=QDialog(w);dialog.setWindowTitle(tr('메타데이터 프리셋 저장'));form=QFormLayout(dialog)
        name=QLineEdit();form.addRow(tr('프리셋 이름'),name);fields={}
        current={**photo['info'],**photo['user_metadata'],'keywords':photo['keywords']}
        titles=['제목','설명','촬영자','저작권','장소','도시','국가','위도','경도','촬영 시각','키워드']
        for key,title in zip(data.METADATA_KEYS,titles):
            row=QHBoxLayout();enabled=QCheckBox(tr(title));value=QLineEdit(str(current.get(key,'')))
            enabled.setChecked(key in ('creator','copyright') and bool(value.text()))
            row.addWidget(enabled);row.addWidget(value);form.addRow(row);fields[key]=(enabled,value)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);form.addRow(buttons)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        values={k:v.text() for k,(check,v) in fields.items() if check.isChecked()}
        if not values:raise ValueError(tr('저장할 메타데이터 항목을 선택하세요.'))
        data.save_metadata_preset(w.catalog,name.text(),values)

    def choose_metadata(self):
        choices=data.metadata_presets(self.w.catalog)
        if not choices:raise ValueError(tr('저장된 메타데이터 프리셋이 없습니다.'))
        name,ok=QInputDialog.getItem(self.w,tr('메타데이터 프리셋'),tr('프리셋 선택'),list(choices),editable=False)
        return (name,choices[name]) if ok else (None,None)

    def apply_metadata(self):
        name,values=self.choose_metadata()
        if name:
            self.w.save_keywords()
            data.apply_metadata_preset(self.w.catalog,self.w.selected_ids(),values)
            self.w.load_controls();self.w.refresh_lists()

    def delete_metadata(self):
        name,_=self.choose_metadata()
        if name:
            values=data.metadata_presets(self.w.catalog);values.pop(name);self.w.catalog.save_preference('metadata_presets',values)

    def set_keyword(self):
        w=self.w;text,ok=QInputDialog.getText(w,tr('바로가기 키워드'),tr('키워드 하나 · 계층은 |로 구분'),text=w.catalog.preference('shortcut_keyword',''))
        if ok:
            if ',' in text or not text.strip():raise ValueError(tr('키워드 하나를 입력하세요.'))
            w.catalog.save_preference('shortcut_keyword',text.strip())

    def apply_keyword(self):
        w=self.w;keyword=w.catalog.preference('shortcut_keyword','')
        if not keyword:self.set_keyword();keyword=w.catalog.preference('shortcut_keyword','')
        if keyword:
            w.save_keywords();ids=w.selected_ids();data.add_keyword(w.catalog,ids,keyword);w.load_controls();w.refresh_lists()
            w.statusBar().showMessage(tr('키워드 “{0}”를 {1}장에 추가', keyword, f'{len(ids):,}'))

    def show_exports(self):
        w=self.w;dialog=QDialog(w);dialog.setWindowTitle(tr('내보내기 기록'));dialog.resize(1000,500)
        box=QVBoxLayout(dialog);tree=QTreeWidget();tree.setHeaderLabels([tr('사진'),tr('저장 시각'),tr('상태'),tr('파일')]);box.addWidget(tree)
        for record in data.export_history(w.catalog,w.selected_ids()):
            status=tr('내보낸 뒤 보정 변경') if record['edited_since'] else tr('현재 보정과 일치')
            if not Path(record['path']).is_file():status=tr('출력 파일 없음')+' · '+status
            tree.addTopLevelItem(QTreeWidgetItem([w.catalog.photo(record['photo_id'])['name'],record['created'],status,record['path']]))
        for i in range(3):tree.resizeColumnToContents(i)
        dialog.exec()

    def reset_exports(self):
        ids=set(self.w.selected_ids());values=self.w.catalog.preference('export_history',[])
        self.w.catalog.save_preference('export_history',[r for r in values if r.get('photo_id') not in ids])

    def reference(self):
        self.w.commit()
        if self.w.current_id is None:return
        from .reference_view import ReferenceWindow
        dialog=ReferenceWindow(self.w,reference=deepcopy(self.w.catalog.photo(self.w.current_id)))
        self.windows.append(dialog);dialog.show()

    def second_window(self):
        from .reference_view import ReferenceWindow
        dialog=ReferenceWindow(self.w);self.windows.append(dialog);dialog.show()

    def shutdown(self):
        if self.cancel:self.cancel.set()
        for dialog in list(self.windows):dialog.close()
