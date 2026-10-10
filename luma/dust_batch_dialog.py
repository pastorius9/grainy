"""Bounded sequential detection with per-photo, explicitly reviewed edits."""
from .i18n import tr
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt,QTimer,Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QPushButton,QComboBox,QSpinBox,QCheckBox,QTreeWidget,QTreeWidgetItem,QProgressBar,QHeaderView,QMessageBox,QGridLayout,QScrollArea,QWidget,QLayout,QFrame)

from .dust import Options,Cancelled,checkpoint,detect
from .dust_dialog import DustDialog,prepare_source,MAX_CANDIDATES
from .dust_batch import capture_scope_ids,capture_record,verify_source,apply_reviewed,restore,BATCH_KEY
from .engine import VIDEO_EXTENSIONS
from .preview_store import signature
from .dust_store import DustReviewStore,target_key,read_work

STATES={'pending':'검출 대기','scanning':'검출 중','ready':'검토 대기','reviewed':'검토 완료',
        'failed':'확인 필요','applied':'적용 완료'}


def plain(text=''):
    label=QLabel(tr(text));label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(True);return label


class DustBatchDialog(QDialog):
    scanProgress=Signal(int,int,int)
    loadProgress=Signal(int,int,int)

    def __init__(self,w):
        super().__init__(w);self.w=w;self.closed=False;self.busy=False;self.running=False
        self.cancel=Event();self.entries=[];self.active_index=-1;self.scan_indices=[]
        self.snapshot=capture_scope_ids(w.catalog,w.current_id,w.selected_ids(),w.folder_filter)
        self.current_snapshot=deepcopy(self.snapshot);self.store=DustReviewStore(w.catalog)
        self.session_id=None;self.revision=0;self.dirty=set();self.save_error=''
        self.loading=False;self.load_token=0
        self.setWindowTitle(tr('여러 사진의 먼지 감지'));self.resize(940,720);self.setMinimumSize(740,590)
        box=QVBoxLayout(self);title=plain('사진별로 찾고, 확인한 먼지만 제거');title.setStyleSheet('font-size:20px;font-weight:600;color:#ead4b1');box.addWidget(title)
        box.addWidget(plain('검출 → 사진별 검토 → 검토한 사진에 적용\n검출 결과와 완료한 검토는 자동 저장됩니다. 닫거나 앱을 다시 실행해도 이어갈 수 있습니다.'))
        saved_row=QHBoxLayout();box.addLayout(saved_row);saved_row.addWidget(plain('저장한 작업'))
        self.work_selector=QComboBox();saved_row.addWidget(self.work_selector,1)
        self.work_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);self.work_selector.setMinimumContentsLength(20)
        self.new_button=QPushButton(tr('현재 대상으로 새 작업'));saved_row.addWidget(self.new_button);self.new_button.clicked.connect(self.new_work)
        self.forget_button=QPushButton(tr('작업 기록 삭제'));saved_row.addWidget(self.forget_button);self.forget_button.clicked.connect(self.forget_work)
        top=QHBoxLayout();box.addLayout(top);top.addWidget(plain('대상'))
        self.scope=QComboBox()
        for title,key in [('선택한 사진','selection'),('현재 폴더 전체','folder')]:self.scope.addItem(tr(title),key)
        self.scope.setCurrentIndex(1 if w.folder_filter else 0);top.addWidget(self.scope,1)
        self.scope_note=plain();box.addWidget(self.scope_note)
        self.search_scroll=QScrollArea();self.search_scroll.setWidgetResizable(True)
        self.search_scroll.setFrameShape(QFrame.Shape.NoFrame);self.search_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.search_scroll.setMinimumHeight(72);self.search_scroll.setMaximumHeight(210);box.addWidget(self.search_scroll)
        search_controls=QWidget();self.search_scroll.setWidget(search_controls)
        search_box=QVBoxLayout(search_controls);search_box.setContentsMargins(0,0,0,0);search_box.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        settings=QHBoxLayout();search_box.addLayout(settings)
        saved=w.catalog.preference('dust_detection_options',{})
        try:initial=Options(**{k:v for k,v in saved.items() if k in asdict(Options())});initial.validate()
        except (AttributeError,TypeError,ValueError):initial=Options()
        self.sensitivity=QSpinBox();self.sensitivity.setRange(1,100);self.sensitivity.setValue(initial.sensitivity)
        self.minimum=QSpinBox();self.maximum=QSpinBox()
        for control,value in ((self.minimum,initial.minimum),(self.maximum,initial.maximum)):
            control.setRange(2,160);control.setValue(value);control.setSuffix(' px')
        self.polarity=QComboBox()
        for title,key in [('검은 점 + 흰 먼지','both'),('검은 점','dark'),('흰 먼지','bright')]:self.polarity.addItem(tr(title),key)
        self.polarity.setCurrentIndex(self.polarity.findData(initial.polarity))
        for title,control in [('민감도',self.sensitivity),('최소 점 크기',self.minimum),('최대 점 크기',self.maximum),('종류',self.polarity)]:
            form=QFormLayout();form.addRow(tr(title),control);settings.addLayout(form)
        row=QGridLayout();search_box.addLayout(row)
        self.detailed=QCheckBox(tr('경계·미세 먼지까지 정밀 검색'));self.detailed.setChecked(initial.detailed);row.addWidget(self.detailed,0,0)
        self.soft=QCheckBox(tr('입자 속 옅은 먼지 추가 검색'));self.soft.setChecked(initial.soft);row.addWidget(self.soft,0,1)
        self.soft.setToolTip(tr('옅은 점을 추가로 찾습니다. 시간이 더 걸리고 무늬도 후보에 포함될 수 있으므로 직접 확인해 주세요.'))
        self.reduce_patterns=QCheckBox(tr('무늬 후보 줄이기'));self.reduce_patterns.setChecked(initial.reduce_patterns);row.addWidget(self.reduce_patterns,1,0)
        self.suppress_grain=QCheckBox(tr('입자 후보 억제'));self.suppress_grain.setChecked(initial.suppress_grain);row.addWidget(self.suppress_grain,1,1)
        self.suppress_grain.setToolTip(tr('옅은 점의 검출 기준을 높여 검토 후보를 줄입니다. 옅은 먼지도 놓칠 수 있으므로 후보가 너무 많을 때 켜세요.'))
        self.scratches=QCheckBox(tr('긴 흠집·머리카락 후보 찾기'));self.scratches.setChecked(initial.scratches);row.addWidget(self.scratches,2,0)
        self.scratches.setToolTip(tr('전선·가지·글자도 잡힐 수 있어 추가 후보를 직접 확인해 주세요. 제거 범위는 가는 모양을 따라갑니다.'))
        self.scratch_width=QSpinBox();self.scratch_width.setRange(2,40);self.scratch_width.setValue(initial.scratch_width);self.scratch_width.setSuffix(' px')
        width_form=QFormLayout();width_form.addRow(tr('흠집 최대 굵기'),self.scratch_width);row.addLayout(width_form,2,1)
        self.reduce_patterns.setToolTip(tr('반복 무늬와 넓은 물체 주변의 후보를 줄입니다. 실제 먼지나 배경과 붙은 흠집도 놓칠 수 있습니다.'))
        search_box.addWidget(plain('조건·대상을 바꾸면 새 작업을 준비합니다. 이전 검토는 위 목록에서 다시 열 수 있습니다.'))
        self.list=QTreeWidget();self.list.setColumnCount(4);self.list.setHeaderLabels([tr('사진'),tr('상태'),tr('검출 후보'),tr('선택한 먼지')])
        self.list.setRootIsDecorated(False);self.list.setAlternatingRowColors(True)
        self.list.setMinimumHeight(120)
        self.list.setStyleSheet('QTreeWidget { alternate-background-color:#1e2329; }')
        self.list.header().setMinimumSectionSize(100)
        self.list.header().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        for i in (1,2,3):self.list.header().setSectionResizeMode(i,QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.list,1);self.list.currentItemChanged.connect(self.update_controls)
        self.list.itemDoubleClicked.connect(lambda *_:self.review_selected())
        row=QHBoxLayout();box.addLayout(row)
        self.scan_button=QPushButton(tr('검출 시작'));self.scan_button.clicked.connect(self.start);row.addWidget(self.scan_button)
        self.stop_button=QPushButton(tr('검출 중단'));self.stop_button.clicked.connect(self.stop);row.addWidget(self.stop_button)
        self.retry_button=QPushButton(tr('이 사진 다시 검출'));self.retry_button.clicked.connect(self.retry);row.addWidget(self.retry_button)
        row.addStretch();self.review_button=QPushButton(tr('선택한 사진 검토'));self.review_button.clicked.connect(self.review_selected);row.addWidget(self.review_button)
        self.next_button=QPushButton(tr('다음 미검토 사진'));self.next_button.clicked.connect(self.review_next);row.addWidget(self.next_button)
        self.progress=QProgressBar();self.progress.setRange(0,1);self.progress.setValue(0);box.addWidget(self.progress)
        self.summary=plain();box.addWidget(self.summary)
        self.status=plain('대상을 확인하고 검출 시작을 누르세요.');box.addWidget(self.status)
        save_row=QHBoxLayout();box.addLayout(save_row);self.save_note=plain();save_row.addWidget(self.save_note,1)
        self.save_button=QPushButton(tr('검토 저장 다시 시도'));save_row.addWidget(self.save_button);self.save_button.clicked.connect(lambda:self.persist(create=True))
        row=QHBoxLayout();box.addLayout(row)
        self.undo_button=QPushButton(tr('최근 먼지 일괄 적용 취소'));self.undo_button.clicked.connect(lambda:self.restore(False));row.addWidget(self.undo_button)
        self.redo_button=QPushButton(tr('다시 적용'));self.redo_button.clicked.connect(lambda:self.restore(True));row.addWidget(self.redo_button)
        row.addStretch();self.apply_button=QPushButton(tr('검토한 사진에 적용'));self.apply_button.setObjectName('primary');self.apply_button.clicked.connect(self.apply);row.addWidget(self.apply_button)
        close=QPushButton(tr('닫기'));close.clicked.connect(self.reject);row.addWidget(close)
        self.scanProgress.connect(self.scan_progress)
        self.loadProgress.connect(self.load_progress)
        self.scope.currentIndexChanged.connect(self.scope_changed)
        for control in (self.sensitivity,self.minimum,self.maximum):control.valueChanged.connect(self.reset_entries)
        self.polarity.currentIndexChanged.connect(self.reset_entries)
        self.detailed.toggled.connect(self.reset_entries);self.reduce_patterns.toggled.connect(self.reset_entries)
        self.soft.toggled.connect(self.reset_entries)
        self.suppress_grain.toggled.connect(self.reset_entries)
        self.scratches.toggled.connect(self.reset_entries);self.scratch_width.valueChanged.connect(self.reset_entries)
        self.reset_entries()
        self.work_selector.currentIndexChanged.connect(self.work_changed)
        key=target_key(self.snapshot,self.scope.currentData())
        match=next((s for s in self.store.sessions() if s['target_key']==key),None)
        if match:self.load_work(match['id'])

    def options(self):
        return Options(self.sensitivity.value(),self.minimum.value(),self.maximum.value(),self.polarity.currentData(),self.detailed.isChecked(),self.reduce_patterns.isChecked(),self.soft.isChecked(),self.suppress_grain.isChecked(),self.scratches.isChecked(),self.scratch_width.value())

    def reset_entries(self,*_):
        if self.busy:return
        self.session_id=None;self.revision=0;self.dirty.clear();self.save_error=''
        self.entries=[];self.list.clear()
        scope=self.scope.currentData();ids=self.snapshot['scopes'][scope]
        for ident in ids:
            row=self.w.catalog.photo(ident)
            if not row or Path(row['path']).suffix.lower() in VIDEO_EXTENSIONS:continue
            record=capture_record(self.w.catalog,ident)
            entry=dict(record=record,state='pending',result=None,options=None,operation=None,review=None,error='')
            self.entries.append(entry);self.list.addTopLevelItem(QTreeWidgetItem())
            self.update_row(len(self.entries)-1)
        self.scope_note.setText(tr('{0}장 · 영상 제외', f'{len(self.entries):,}')+(tr(' · 하위 폴더 제외 · 필터에 가려진 사진 포함\n{0}', f"{self.snapshot['folder'] or '폴더 없음'}") if scope=='folder' else ''))
        if self.entries:self.list.setCurrentItem(self.list.topLevelItem(0))
        self.progress.setRange(0,max(1,len(self.entries)));self.progress.setValue(0)
        self.status.setText(tr('대상을 확인하고 검출 시작을 누르세요.'));self.update_controls()
        self.refresh_saved()

    def scope_changed(self,*_):
        self.snapshot=deepcopy(self.current_snapshot);self.reset_entries()

    def refresh_saved(self):
        self.work_selector.blockSignals(True);self.work_selector.clear();self.work_selector.addItem(tr('새 작업 · 아직 검출 전'),None)
        for saved in self.store.sessions():
            folder=None
            try:
                snapshot=json.loads(saved['snapshot']);folder=snapshot.get('folder')
                title=Path(folder).name if saved['scope']=='folder' and folder else '선택한 사진'
                date=datetime.fromisoformat(saved['updated']).astimezone().strftime('%m/%d %H:%M')
            except (ValueError,TypeError):title='저장한 검토';date=''
            self.work_selector.addItem(tr('{0} · {1}장 · 검토 {2} · 적용 {3} · {4}', f'{title}', f"{saved['total']:,}", f"{saved['reviewed'] or 0}", f"{saved['applied'] or 0}", f'{date}'),saved['id'])
            self.work_selector.setItemData(self.work_selector.count()-1,folder or '',Qt.ItemDataRole.ToolTipRole)
        self.work_selector.setCurrentIndex(max(0,self.work_selector.findData(self.session_id)))
        self.work_selector.blockSignals(False)

    def work_changed(self,*_):
        ident=self.work_selector.currentData()
        if ident:self.load_work(ident)
        else:self.new_work()

    def new_work(self):
        if self.busy or self.running or self.save_error:return
        self.current_snapshot=capture_scope_ids(self.w.catalog,self.w.current_id,self.w.selected_ids(),self.w.folder_filter)
        self.snapshot=deepcopy(self.current_snapshot);self.scope.blockSignals(True)
        self.scope.setCurrentIndex(1 if self.w.folder_filter else 0);self.scope.blockSignals(False);self.reset_entries()

    def load_work(self,ident):
        if self.busy or self.running or self.save_error:return
        self.w.commit()
        # Keep the displayed queue until a complete, validated snapshot is ready.
        if self.session_id and not self.persist():return
        self.busy=True;self.loading=True;self.cancel=Event();cancel=self.cancel
        self.load_token+=1;token=self.load_token;directory=self.w.catalog.directory
        self.progress.setRange(0,0);self.status.setText(tr('저장한 검토를 불러오는 중…'));self.update_controls()
        def work():
            try:return read_work(directory,ident,cancel,lambda done,total:self.loadProgress.emit(token,done,total))
            except Cancelled:return None
        def ready(result):
            if self.closed or token!=self.load_token:return
            self.busy=False;self.loading=False
            if result is None or cancel.is_set():
                self.load_stopped('불러오기를 중단했습니다. 기존 작업은 유지됩니다.');return
            self.show_work(ident,*result)
        def failed(error):
            if self.closed or token!=self.load_token:return
            self.busy=False;self.loading=False;self.load_stopped(f'검토를 불러오지 못했습니다: {error}')
        self.w.spawn(work,ready,failed)

    def load_stopped(self,message):
        self.progress.setRange(0,max(1,len(self.entries)));self.progress.setValue(sum(e['state']!='pending' for e in self.entries))
        self.status.setText(message);self.refresh_saved();self.update_controls()

    def load_progress(self,token,done,total):
        if not self.closed and self.loading and token==self.load_token:
            self.progress.setRange(0,max(1,total));self.progress.setValue(done)

    def show_work(self,ident,header,entries,damaged):
        self.session_id=ident;self.revision=header['revision'];self.snapshot=header['snapshot'];self.entries=entries
        self.dirty=set(damaged);self.save_error=''
        options=Options(**header['options'])
        controls=((self.scope,self.scope.findData(header['scope'])),(self.sensitivity,options.sensitivity),
            (self.minimum,options.minimum),(self.maximum,options.maximum),(self.polarity,self.polarity.findData(options.polarity)),
            (self.detailed,options.detailed),(self.reduce_patterns,options.reduce_patterns),(self.soft,options.soft),(self.suppress_grain,options.suppress_grain),
            (self.scratches,options.scratches),(self.scratch_width,options.scratch_width))
        for control,value in controls:
            control.blockSignals(True)
            if isinstance(control,QComboBox):control.setCurrentIndex(value)
            elif isinstance(control,QCheckBox):control.setChecked(value)
            else:control.setValue(value)
            control.blockSignals(False)
        self.list.clear()
        for index,entry in enumerate(entries):
            self.list.addTopLevelItem(QTreeWidgetItem());self.update_row(index)
        self.scope_note.setText(tr('저장한 대상 {0}장 · 영상 제외', f'{len(entries):,}')+('\n'+(self.snapshot['folder'] or '')+tr(' · 하위 폴더 제외') if header['scope']=='folder' else tr(' · 선택한 사진')))
        if entries:self.list.setCurrentItem(self.list.topLevelItem(max(0,min(len(entries)-1,header['current_index']))))
        self.progress.setRange(0,max(1,len(entries)));self.progress.setValue(sum(e['state']!='pending' for e in entries))
        self.status.setText(tr('저장한 검토를 불러왔습니다. 확인 필요 {0}장.', f'{len(damaged):,}') if damaged else tr('저장한 검토를 불러왔습니다. 완료한 검출을 반복하지 않습니다.'))
        self.refresh_saved();self.update_controls()
        if self.dirty:self.persist()

    def persist(self,indices=(),*,create=False):
        self.dirty.update(indices)
        if self.session_id is None and not create:return True
        try:
            if self.session_id is None:
                self.session_id,self.revision=self.store.create(self.snapshot,self.scope.currentData(),asdict(self.options()),self.entries,self.current_index())
            else:self.revision=self.store.save(self.session_id,self.revision,[(i,self.entries[i]) for i in sorted(self.dirty)],self.current_index())
        except Exception as error:
            self.save_error=str(error);self.running=False;self.cancel.set();self.update_controls();return False
        self.dirty.clear();self.save_error='';self.refresh_saved();self.update_controls();return True

    def forget_work(self):
        if not self.session_id or self.busy or self.running or self.save_error:return
        answer=QMessageBox.question(self,tr('검토 기록 삭제'),tr('이 작업의 검출 후보와 검토 기록을 삭제할까요? 사진에 적용한 보정과 실행 취소 정보는 유지됩니다.'),QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer!=QMessageBox.StandardButton.Yes:return
        try:self.store.delete(self.session_id,self.revision)
        except Exception as error:self.status.setText(str(error));return
        self.new_work()

    def current_index(self):
        return self.list.indexOfTopLevelItem(self.list.currentItem())

    def update_row(self,index):
        entry=self.entries[index];item=self.list.topLevelItem(index);result=entry['result'];operation=entry['operation']
        item.setText(0,Path(entry['record']['path']).name);item.setToolTip(0,entry['record']['path'])
        item.setText(1,STATES[entry['state']]);item.setToolTip(1,entry['error'])
        item.setForeground(1,QColor('#e9c48b' if entry['state'] in ('ready','failed') else '#a6d1ba' if entry['state'] in ('reviewed','applied') else '#a7b1bc'))
        item.setText(2,f'{result["total"]:,}'+(' · 일부 표시' if result['truncated'] else '') if result else '—')
        item.setText(3,str(len(operation['spots'])) if operation is not None else '—')

    def update_controls(self,*_):
        if self.closed:return
        index=self.current_index();entry=self.entries[index] if 0<=index<len(self.entries) else None
        locked=self.busy or self.running or bool(self.save_error)
        self.work_selector.setEnabled(not locked);self.new_button.setEnabled(not locked)
        self.forget_button.setEnabled(not locked and self.session_id is not None)
        self.save_button.setVisible(bool(self.save_error));self.save_button.setEnabled(not self.busy)
        self.save_note.setText(tr('검토 저장 실패 · 창을 열어 둔 채 다시 시도하세요: ')+self.save_error if self.save_error else tr('검토 자동 저장됨 · 원본 파일 변경 없음') if self.session_id else tr('검출을 시작하면 작업을 자동 저장합니다.'))
        for control in (self.scope,self.sensitivity,self.minimum,self.maximum,self.polarity,self.detailed,self.scratches):control.setEnabled(not locked)
        self.scratch_width.setEnabled(not locked and self.scratches.isChecked())
        self.reduce_patterns.setEnabled(not locked and (self.detailed.isChecked() or self.scratches.isChecked()))
        self.soft.setEnabled(not locked and self.detailed.isChecked())
        self.suppress_grain.setEnabled(not locked and self.detailed.isChecked() and self.soft.isChecked())
        pending=sum(e['state']=='pending' for e in self.entries)
        reviewed=[e for e in self.entries if e['state']=='reviewed']
        changed=[e for e in reviewed if e['operation']['spots']]
        candidates=sum(len(e['operation']['spots']) for e in changed)
        self.scan_button.setText(tr('남은 {0}장 검출', f'{pending:,}'));self.scan_button.setEnabled(not locked and pending>0)
        self.stop_button.setText(tr('불러오기 중단') if self.loading else tr('검출 중단'))
        self.stop_button.setEnabled(locked and not self.cancel.is_set())
        self.retry_button.setEnabled(not locked and entry is not None)
        self.review_button.setEnabled(not locked and entry is not None and entry['state'] in ('ready','reviewed'))
        self.next_button.setEnabled(not locked and any(e['state']=='ready' for e in self.entries))
        self.apply_button.setEnabled(not locked and bool(changed));self.apply_button.setText(tr('검토한 {0}장 · 먼지 {1}개 적용', f'{len(changed)}', f'{candidates:,}'))
        group=self.w.catalog.preference(BATCH_KEY,{})
        self.undo_button.setEnabled(not locked and group.get('state')=='applied');self.redo_button.setEnabled(not locked and group.get('state')=='undone')
        counts={state:sum(e['state']==state for e in self.entries) for state in STATES}
        self.summary.setText(' · '.join(f'{title} {counts[state]:,}장' for state,title in STATES.items() if counts[state]) or '대상 사진이 없습니다.')
        if not locked and entry and entry['error']:self.status.setText(entry['error'])

    def start(self,*,only=None):
        if self.closed or self.busy or self.running or self.save_error:return
        try:self.options().validate()
        except ValueError as error:self.status.setText(str(error));return
        self.w.commit()
        if not self.persist(create=True):return
        self.scan_indices=[only] if only is not None else list(range(len(self.entries)))
        self.cancel=Event();self.running=True;self.update_controls();self.start_next()

    def start_next(self):
        if self.closed:return
        if self.cancel.is_set():self.running=False;self.update_controls();return
        index=next((i for i in self.scan_indices if self.entries[i]['state']=='pending'),None)
        if index is None:
            self.running=False;self.status.setText(tr('검출이 끝났습니다. 사진을 검토한 뒤 적용하세요.'));self.update_controls();return
        entry=self.entries[index];record=deepcopy(entry['record']);options=self.options();cancel=self.cancel
        try:verify_source(self.w.catalog,record)
        except ValueError as error:
            entry['state']='failed';entry['error']=str(error);self.update_row(index)
            self.persist([index])
            QTimer.singleShot(0,self.start_next);return
        self.active_index=index;self.busy=True;entry['state']='scanning';self.update_row(index);self.update_controls()
        self.status.setText(tr('{0} / {1} · {2} 검출 중…', f'{index + 1:,}', f'{len(self.entries):,}', f"{Path(record['path']).name}"))
        def work():
            try:
                prepared=prepare_source(record['path'],record['settings'],cancel)
                gray=prepared[1];del prepared
                result=detect(gray,options,cancel=cancel,limit=MAX_CANDIDATES,
                              progress=lambda done,total:self.scanProgress.emit(index,done,total))
                del gray;checkpoint(cancel)
                if signature(record['path'])!=record['fingerprint']:raise ValueError('분석 중 원본이 바뀌었습니다. 다시 검출해 주세요.')
                return result
            except Cancelled:return None
        def ready(result):
            if self.closed:return
            self.busy=False
            if result is None or cancel.is_set():
                entry['state']='pending';self.running=False;self.status.setText(tr('검출을 중단했습니다. 완료한 사진은 검토할 수 있습니다.'))
            else:
                try:verify_source(self.w.catalog,record)
                except ValueError as error:entry['state']='failed';entry['error']=str(error)
                else:
                    entry.update(state='ready',result=result,options=asdict(options))
                    self.w.catalog.save_preference('dust_detection_options',asdict(options))
            self.update_row(index);self.persist([index]);self.update_controls()
            self.progress.setRange(0,len(self.entries));self.progress.setValue(sum(e['state']!='pending' for e in self.entries))
            if self.running:QTimer.singleShot(0,self.start_next)
        def failed(error):
            if self.closed:return
            self.busy=False;entry['state']='failed';entry['error']=str(error);self.update_row(index);self.persist([index]);self.update_controls()
            if self.running:QTimer.singleShot(0,self.start_next)
        self.w.spawn(work,ready,failed)

    def scan_progress(self,index,done,total):
        if not self.closed and self.busy and self.active_index==index:
            self.progress.setRange(0,max(1,total));self.progress.setValue(done)

    def stop(self):
        self.cancel.set();self.running=False;self.stop_button.setEnabled(False)
        self.status.setText(tr('불러오기를 중단하는 중…') if self.loading else tr('검출을 중단하는 중… 완료한 사진의 결과는 유지됩니다.'))

    def retry(self):
        if self.busy or self.running:return
        self.w.commit()
        index=self.current_index()
        if not 0<=index<len(self.entries):return
        entry=self.entries[index]
        try:record=capture_record(self.w.catalog,entry['record']['id'])
        except ValueError as error:self.status.setText(str(error));return
        entry.update(record=record,state='pending',result=None,options=None,operation=None,review=None,error='')
        self.dirty.add(index)
        self.update_row(index);self.start(only=index)

    def review_selected(self):
        if self.busy or self.running or self.closed:return
        index=self.current_index()
        if not 0<=index<len(self.entries):return
        entry=self.entries[index]
        if entry['state'] not in ('ready','reviewed'):return
        self.w.commit()
        try:verify_source(self.w.catalog,entry['record'])
        except ValueError as error:
            entry.update(state='failed',error=str(error),operation=None);self.update_row(index);self.persist([index]);self.update_controls();return
        dialog=DustDialog(self.w,record=entry['record'],cached={'result':entry['result'],'options':entry['options']},review_state=entry['review'])
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        try:
            if dialog.exec()==QDialog.DialogCode.Accepted and dialog.valid_source():
                entry.update(state='reviewed',operation=deepcopy(dialog.operation),review=deepcopy(dialog.review_state),
                             result=deepcopy(dialog.result),options=deepcopy(dialog.detect_options))
                self.persist([index])
                self.status.setText(tr('검토 내용을 보관했습니다. 아래 적용 버튼을 눌러야 사진에 반영됩니다.'))
        finally:dialog.deleteLater()
        self.update_row(index);self.update_controls()

    def review_next(self):
        if self.busy or self.running:return
        current=self.current_index();order=list(range(current+1,len(self.entries)))+list(range(current+1))
        index=next((i for i in order if self.entries[i]['state']=='ready'),None)
        if index is not None:self.list.setCurrentItem(self.list.topLevelItem(index));self.review_selected()

    def apply(self):
        if self.busy or self.running or self.closed or self.save_error:return
        self.w.commit()
        if not self.persist(create=True):return
        updated={};revision=self.revision
        def checkpoint_applied(ids):
            nonlocal revision
            for i,entry in enumerate(self.entries):
                if entry['record']['id'] in ids:updated[i]={**entry,'state':'applied'}
            revision=self.store.save(self.session_id,self.revision,list(updated.items()),self.current_index(),nested=True)
        try:ids=apply_reviewed(self.w.catalog,self.entries,after_apply=checkpoint_applied)
        except Exception as error:self.status.setText(str(error));return
        self.revision=revision
        for i,entry in updated.items():self.entries[i]=entry;self.update_row(i)
        self.refresh_saved()
        self.w.refresh_command_edits(ids);self.w.statusBar().showMessage(tr('먼지 일괄 제거 · {0}장 적용', f'{len(ids):,}'))
        self.status.setText(tr('{0}장에 적용했습니다. 최근 먼지 일괄 적용 취소로 함께 되돌릴 수 있습니다.', f'{len(ids):,}'))
        self.update_controls()

    def restore(self,redo):
        if self.busy or self.running or self.save_error:return
        self.w.commit()
        updated={};revision=self.revision
        def checkpoint_restored(ids):
            nonlocal revision
            for i,entry in enumerate(self.entries):
                if entry['record']['id'] in ids:
                    updated[i]=dict(record=capture_record(self.w.catalog,entry['record']['id']),state='pending',result=None,options=None,operation=None,review=None,error='')
            if self.session_id:revision=self.store.save(self.session_id,self.revision,list(updated.items()),self.current_index(),nested=True)
        try:ids=restore(self.w.catalog,redo,after_restore=checkpoint_restored)
        except Exception as error:self.status.setText(str(error));return
        self.revision=revision
        self.w.refresh_command_edits(ids);self.w.statusBar().showMessage(tr('먼지 일괄 적용을 복원했습니다.'))
        for i,entry in updated.items():self.entries[i]=entry;self.update_row(i)
        self.refresh_saved()
        self.status.setText(tr('먼지 일괄 적용을 다시 실행했습니다.') if redo else tr('먼지 일괄 적용을 취소했습니다.'))
        self.update_controls()

    def done(self,result):
        if self.closed:return
        if self.session_id or self.save_error:
            if not self.persist(create=bool(self.save_error)):
                answer=QMessageBox.question(self,tr('검토 저장 실패'),tr('최근 검토 내용을 저장하지 못했습니다. 저장되지 않은 변경을 버리고 닫을까요? 이전에 저장된 작업은 유지됩니다.'),QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
                if answer!=QMessageBox.StandardButton.Yes:return
        self.closed=True;self.cancel.set();self.running=False
        super().done(result)
