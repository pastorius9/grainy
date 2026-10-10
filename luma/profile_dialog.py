"""Camera-filtered profile browser and explicit RAW default management."""
from .i18n import tr
from copy import deepcopy
from pathlib import Path
from threading import Event
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QListWidget,QListWidgetItem,
    QPushButton,QCheckBox,QFileDialog,QMessageBox,QComboBox,QGridLayout,QInputDialog)
from . import profile_library,raw_defaults


def button(layout,text,callback):
    b=QPushButton(tr(text));b.clicked.connect(callback);layout.addWidget(b);return b


def note(layout,text):
    w=QLabel(tr(text));w.setTextFormat(Qt.TextFormat.PlainText);w.setWordWrap(True);layout.addWidget(w);return w


class ProfileDialog(QDialog):
    def __init__(self,w,metadata):
        super().__init__(w);self.w=w;self.metadata=deepcopy(metadata);self.record=None;self.rows=[]
        self.cancel=Event();self.active=True;self.scanning=False
        self.favorites=set(w.catalog.preference('dcp_favorites',[]))
        self.setWindowTitle(tr('카메라 프로파일'));self.resize(740,640)
        box=QVBoxLayout(self)
        note(box,'카메라 프로파일 · '+str(metadata.get('camera','모델 정보 없음')))
        note(box,'이 카메라와 호환되는 DCP를 보여줍니다. 선택한 프로파일은 보정과 함께 저장됩니다.')
        self.search=QLineEdit();self.search.setPlaceholderText(tr('프로파일 이름 검색'));box.addWidget(self.search)
        self.favorites_only=QCheckBox(tr('즐겨찾기만'));box.addWidget(self.favorites_only)
        self.list=QListWidget();box.addWidget(self.list,1)
        self.detail=note(box,'설치된 프로파일을 찾고 있습니다…');self.detail.setMinimumHeight(68)
        self.status=note(box,'')
        row=QHBoxLayout();box.addLayout(row)
        self.refresh_button=button(row,'다시 검색',self.refresh)
        self.folder_button=button(row,'프로파일 폴더 추가',self.add_folder)
        self.remove_folder_button=button(row,'추가 폴더 제외',self.remove_folder)
        self.favorite_button=button(row,'즐겨찾기 전환',self.toggle_favorite)
        row=QHBoxLayout();box.addLayout(row)
        button(row,'취소',self.reject);self.apply_button=button(row,'선택한 프로파일 적용',self.choose)
        self.apply_button.setEnabled(False)
        self.search.textChanged.connect(self.populate);self.favorites_only.toggled.connect(self.populate)
        self.list.currentItemChanged.connect(self.selection_changed);self.list.itemDoubleClicked.connect(self.choose)
        QTimer.singleShot(0,self.refresh)

    def refresh(self):
        if self.scanning or not self.active:return
        self.scanning=True;self.cancel=Event();cancel=self.cancel
        self.refresh_button.setEnabled(False);self.folder_button.setEnabled(False);self.remove_folder_button.setEnabled(False)
        self.status.setText(tr('설치된 프로파일을 검색하는 중…'));self.apply_button.setEnabled(False)
        roots=profile_library.profile_roots(self.w.catalog.directory,self.w.catalog.preference('dcp_folders',[]))
        self.refresh_button.setToolTip('\n'.join(map(str,roots)))
        def ready(result):
            if not self.active or cancel is not self.cancel:return
            self.scanning=False;self.rows=result['profiles'];self.refresh_button.setEnabled(True);self.folder_button.setEnabled(True)
            self.remove_folder_button.setEnabled(True)
            problems=sum(bool(r['error']) for r in self.rows)
            self.status.setText(tr('{0}개 파일 확인 · {1}개 갱신 · 미지원/읽기 오류 {2}개', f"{result['files']:,}", f"{result['parsed']:,}", f'{problems:,}')
                +(tr(' · 검색 상한에 도달했습니다. 폴더 범위를 줄이세요.') if result['truncated'] else '')
                +(tr(' · 폴더 읽기 오류 {0}개', f"{len(result['errors'])}") if result['errors'] else ''))
            self.status.setToolTip('\n'.join(result['errors']));self.populate()
        def failed(error):
            if not self.active:return
            self.scanning=False;self.refresh_button.setEnabled(True);self.folder_button.setEnabled(True);self.remove_folder_button.setEnabled(True);self.status.setText(str(error))
        self.w.spawn(lambda:profile_library.scan(roots,self.w.catalog.directory/'cache'/'dcp-index.sqlite',cancel),ready,failed)

    def populate(self):
        selected=self.list.currentItem();digest=selected.data(Qt.ItemDataRole.UserRole)['sha256'] if selected else None
        self.list.clear()
        rows=profile_library.compatible(self.rows,self.metadata,self.search.text(),self.favorites,self.favorites_only.isChecked())
        rows.sort(key=lambda r:(r['sha256'] not in self.favorites,r['name'].casefold()))
        for row in rows:
            item=QListWidgetItem(('★ ' if row['sha256'] in self.favorites else '')+row['name']+(' · 지원 안 됨' if row['error'] else ''))
            item.setData(Qt.ItemDataRole.UserRole,row);item.setToolTip(row['error'] or row['path']);self.list.addItem(item)
            if row['sha256']==digest:self.list.setCurrentItem(item)
        if self.list.currentRow()<0 and rows:self.list.setCurrentRow(0)
        if not rows:self.detail.setText(tr('일치하는 프로파일이 없습니다. 검색어를 지우거나 DCP 폴더를 추가하세요.'))
        self.selection_changed()

    def selection_changed(self,*_):
        item=self.list.currentItem();row=item.data(Qt.ItemDataRole.UserRole) if item else None
        self.apply_button.setEnabled(bool(row and not row['error'] and not self.scanning))
        self.favorite_button.setEnabled(bool(row and row['sha256']))
        if row:self.detail.setText(row['name']+' · '+(row['camera'] or '공용')+'\n'+row['copyright']+'\n'+(row['error'] or row['path']))

    def toggle_favorite(self):
        item=self.list.currentItem()
        if not item:return
        digest=item.data(Qt.ItemDataRole.UserRole)['sha256']
        if digest in self.favorites:self.favorites.remove(digest)
        elif digest:self.favorites.add(digest)
        self.w.catalog.save_preference('dcp_favorites',sorted(self.favorites));self.populate()

    def add_folder(self):
        folder=QFileDialog.getExistingDirectory(self,tr('DCP 프로파일이 있는 폴더'))
        if not folder:return
        folders=self.w.catalog.preference('dcp_folders',[])
        if folder not in folders:folders.append(folder);self.w.catalog.save_preference('dcp_folders',folders)
        self.refresh()

    def choose(self,*_):
        item=self.list.currentItem()
        if not item or not self.apply_button.isEnabled():return
        try:self.record=profile_library.load_selected(item.data(Qt.ItemDataRole.UserRole),self.metadata)
        except (ValueError,OSError) as e:QMessageBox.warning(self,tr('프로파일을 적용하지 못했습니다'),str(e));return
        self.accept()

    def remove_folder(self):
        folders=self.w.catalog.preference('dcp_folders',[])
        if not folders:self.status.setText(tr('추가한 폴더가 없습니다.'));return
        folder,ok=QInputDialog.getItem(self,tr('검색에서 제외할 폴더'),tr('파일은 그대로 두고 검색 목록에서 제외합니다.'),folders,0,False)
        if ok:
            self.w.catalog.save_preference('dcp_folders',[p for p in folders if p!=folder]);self.refresh()

    def done(self,result):
        self.active=False;self.cancel.set();super().done(result)


class RawDefaultsDialog(QDialog):
    def __init__(self,w):
        super().__init__(w);self.w=w;w.commit()
        self.photo=w.catalog.photo(w.current_id) if w.current_id else None
        self.rules=deepcopy(w.catalog.preference(raw_defaults.PREFERENCE,[]))
        self.photo_ids=w.selected_ids()
        self.setWindowTitle(tr('RAW 가져오기 기본 보정'));self.resize(750,720)
        box=QVBoxLayout(self)
        note(box,'새로 가져오는 RAW의 기본 보정')
        note(box,'바디 일련번호 → 카메라 모델 → 모든 RAW 순서로 우선 적용합니다. 저장해도 기존 사진은 바뀌지 않습니다.')
        self.list=QListWidget();box.addWidget(self.list,1)
        row=QHBoxLayout();box.addLayout(row)
        button(row,'사용 / 해제',self.toggle);button(row,'선택 규칙 삭제',self.remove)
        self.scope=QComboBox();self.scope.addItem(tr('모든 RAW'),'master')
        info=self.photo['info'] if self.photo else {}
        if self.photo and raw_defaults.is_raw(info,self.photo['path']) and raw_defaults.camera_key(info):
            self.scope.addItem(tr('이 카메라 모델 · ')+info['camera'],'model')
            if raw_defaults.serial(info):self.scope.addItem(tr('이 바디 · ')+raw_defaults.serial(info),'serial')
            self.scope.setCurrentIndex(1)
        box.addWidget(self.scope)
        self.source=QComboBox();self.source.addItem(tr('현재 사진의 보정'),None)
        for name in w.all_presets:self.source.addItem(tr('프리셋 · ')+name,name)
        if not self.photo:self.source.setCurrentIndex(1)
        box.addWidget(self.source)
        self.name=QLineEdit();self.name.setPlaceholderText(tr('규칙 이름 (선택 사항)'));box.addWidget(self.name)
        grid=QGridLayout();box.addLayout(grid);self.groups={}
        for i,title in enumerate(raw_defaults.GROUPS):
            check=QCheckBox(tr(title));check.setChecked(title in ('RAW 프로파일','화이트 밸런스','디테일'))
            grid.addWidget(check,i//3,i%3);self.groups[title]=check
        self.iso=QCheckBox(tr('선택한 사진으로 ISO별 보정 만들기'));box.addWidget(self.iso)
        note(box,'같은 카메라에서 서로 다른 ISO로 촬영한 기준 RAW 2–64장을 선택하세요. 노이즈 제거·샤프닝·선명도·텍스처·안개 제거·검정값을 ISO 사이에서 연결합니다. 크롭·마스크·복구는 저장하지 않습니다.')
        button(box,'선택한 대상으로 저장 / 교체',self.save_rule)
        self.status=note(box,'규칙은 이 카탈로그에 저장되며 백업에 포함됩니다.')
        row=QHBoxLayout();box.addLayout(row)
        self.apply_button=button(row,'현재 사진에 저장된 항목 적용',self.apply_current)
        self.apply_button.setEnabled(bool(self.photo and raw_defaults.is_raw(info,self.photo['path'])))
        button(row,'닫기',self.accept);self.populate()

    def populate(self):
        self.list.clear()
        for r in self.rules:
            item=QListWidgetItem(('사용 · ' if r.get('enabled',True) else '해제 · ')+r['name']+'  /  '+
                ', '.join(r.get('groups',[]))+(f" · ISO {len(r.get('iso',[]))}개 기준" if r.get('iso') else ''))
            item.setData(Qt.ItemDataRole.UserRole,r['id']);self.list.addItem(item)

    def persist(self):
        self.w.catalog.save_preference(raw_defaults.PREFERENCE,self.rules);self.populate()

    def toggle(self):
        item=self.list.currentItem()
        if not item:return
        for r in self.rules:
            if r['id']==item.data(Qt.ItemDataRole.UserRole):r['enabled']=not r.get('enabled',True)
        self.persist()

    def remove(self):
        item=self.list.currentItem()
        if not item:return
        self.rules=[r for r in self.rules if r['id']!=item.data(Qt.ItemDataRole.UserRole)];self.persist()

    def save_rule(self):
        name=self.source.currentData()
        settings=self.w.all_presets[name] if name else (self.w.settings if self.photo and self.w.current_id==self.photo['id'] else None)
        if settings is None:return
        try:
            if self.iso.isChecked() and len(self.photo_ids)>64:raise ValueError('ISO 기준 사진은 2–64장으로 선택하세요.')
            photos=[self.w.catalog.photo(i) for i in self.photo_ids] if self.iso.isChecked() else []
            anchors=raw_defaults.iso_anchors(photos) if self.iso.isChecked() else []
            if anchors and self.scope.currentData()=='serial' and any(raw_defaults.serial(p['info'])!=raw_defaults.serial(self.photo['info']) for p in photos):
                raise ValueError('바디별 ISO 기본값에는 같은 일련번호의 사진만 선택하세요.')
            r=raw_defaults.make_rule(self.photo['info'] if self.photo else {},settings,self.scope.currentData(),
                [g for g,c in self.groups.items() if c.isChecked()],anchors,self.name.text().strip())
        except (ValueError,TypeError) as e:QMessageBox.warning(self,tr('기본 보정'),str(e));return
        self.rules=[old for old in self.rules if old['id']!=r['id']]+[r];self.persist()
        self.status.setText(r['name']+tr(' 저장 완료 · 다음에 새로 가져오는 RAW부터 적용됩니다.'))

    def apply_current(self):
        if not self.photo or self.w.current_id!=self.photo['id'] or self.w.source is None:return
        try:settings,notes=raw_defaults.resolve(self.photo['info'],self.rules,self.photo['path'],base=self.w.settings)
        except (ValueError,TypeError) as e:QMessageBox.warning(self,tr('기본 보정'),str(e));return
        if not notes:self.status.setText(tr('현재 사진에 적용할 규칙이 없습니다.'));return
        self.w.commit();self.w.settings=settings;self.w.commit('RAW 기본 보정 적용')
        self.w.load_controls();self.w.render_version+=1;self.w.render()
        self.w.manager.refresh_thumbnails([self.photo['id']]);self.status.setText(' · '.join(notes)+tr(' · 실행 취소 가능'))
