"""Searchable offline lens selection with visible capture parameters."""
from .i18n import tr
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QComboBox,QDoubleSpinBox,
    QLabel,QDialogButtonBox)
from .optics import cameras,compatible_lenses,match_metadata,make_profile,description,profile_lens


class LensDialog(QDialog):
    def __init__(self,parent,metadata,current=None):
        super().__init__(parent)
        self.setWindowTitle(tr('렌즈 자동 보정'));self.resize(700,440)
        self.metadata=metadata;self.result_profile=None;self.initialising=True
        layout=QVBoxLayout(self);form=QFormLayout();layout.addLayout(form)
        self.camera=QComboBox();self.camera.addItem(tr('카메라 직접 선택 · 센서 배율 입력'),None)
        for camera in cameras():
            variant=getattr(camera,'variant','') or ''
            self.camera.addItem(camera.model+(f' ({variant})' if variant else ''),camera)
        self.camera.setEditable(True);self.camera.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.camera.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.camera.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        form.addRow(tr('카메라 찾기'),self.camera)
        self.lens=QComboBox();self.lens.setEditable(True);self.lens.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.lens.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.lens.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        form.addRow(tr('렌즈 찾기'),self.lens)
        self.crop=self.number(form,'센서 배율',.1,20,3,1)
        self.focal=self.number(form,'초점 거리 mm',.1,5000,2,metadata.get('focal_length') or 50)
        self.aperture=self.number(form,'조리개 f/',0,128,2,metadata.get('aperture') or 0)
        self.aperture.setSpecialValueText(tr('정보 없음 · 주변광량 보정 제외'))
        self.distance=self.number(form,'촬영 거리 m',.01,100000,2,metadata.get('subject_distance') or 1000)
        note=QLabel(tr('촬영 정보를 먼저 사용합니다. 필름 스캔처럼 정보가 없는 사진은 실제 촬영 조건을 입력하세요.\n거리 정보가 없으면 1,000m(원거리)를 기준으로 계산합니다. 센서 배율은 35mm 전체 화면 기준입니다.'))
        note.setWordWrap(True);layout.addWidget(note)
        self.status=QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr('보정 적용'))
        buttons.accepted.connect(self.apply);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.camera.currentIndexChanged.connect(self.camera_changed)
        self.lens.currentIndexChanged.connect(self.lens_changed)
        camera,matches,reason=match_metadata(metadata)
        if camera is not None:
            for i in range(1,self.camera.count()):
                candidate=self.camera.itemData(i)
                if (candidate.maker,candidate.model)==(camera.maker,camera.model):self.camera.setCurrentIndex(i);break
        self.camera_changed()
        if current:
            found=False
            for i in range(self.lens.count()):
                item=self.lens.itemData(i)
                if item and item['xml']==current['xml']:
                    self.lens.setCurrentIndex(i);found=True;break
            if not found:
                saved={'xml':current['xml'],'model':current['name'],'maker':current['maker'],
                    'crop':current['crop'],'file':current.get('database_file','')}
                self.lens.addItem(current['name']+tr(' · 저장된 프로파일'),saved)
                self.lens.setCurrentIndex(self.lens.count()-1)
            for key in ('crop','focal','aperture','distance'):getattr(self,key).setValue(current[key])
        elif len(matches)==1:
            for i in range(self.lens.count()):
                item=self.lens.itemData(i)
                if item and item['id']==matches[0]['id']:self.lens.setCurrentIndex(i);break
        self.initialising=False;self.status.setText(reason or '사용할 프로파일과 촬영 조건을 확인하세요.')

    def number(self,form,title,low,high,decimals,value):
        control=QDoubleSpinBox();control.setRange(low,high);control.setDecimals(decimals)
        try:control.setValue(float(value))
        except (ValueError,TypeError):control.setValue(low)
        form.addRow(tr(title),control);return control

    def camera_changed(self,*_):
        camera=self.camera.currentData()
        if camera:self.crop.setValue(camera.crop_factor)
        self.focal.setRange(.1,5000)
        self.lens.blockSignals(True);self.lens.clear();self.lens.addItem(tr('사용한 렌즈를 선택하세요'),None)
        for record in compatible_lenses(camera):
            self.lens.addItem(tr('{0}  ·  배율 {1}', f"{record['model']}", f"{record['crop']:g}"),record)
        self.lens.blockSignals(False)

    def lens_changed(self,*_):
        record=self.lens.currentData()
        if not record:return
        lens=profile_lens(record)
        if lens.min_focal:
            self.focal.setRange(lens.min_focal,lens.max_focal)
        if self.camera.currentData() is None:self.crop.setValue(record['crop'])
        caps=[name for name,values in [('왜곡',lens.calib_distortion),('색수차',lens.calib_tca),('주변광량',lens.calib_vignetting)] if values]
        self.status.setText(tr('프로파일 제공 항목: ')+' · '.join(tr(name) for name in caps))

    def apply(self):
        try:
            if self.lens.currentText()!=self.lens.itemText(self.lens.currentIndex()) or not self.lens.currentData():
                raise ValueError('검색 결과에서 실제 렌즈 항목을 선택해 주세요.')
            if self.camera.currentText()!=self.camera.itemText(self.camera.currentIndex()):
                raise ValueError('검색 결과에서 카메라를 선택하거나 직접 입력 항목을 선택해 주세요.')
            camera=self.camera.currentData()
            self.result_profile=make_profile(self.lens.currentData(),self.crop.value(),self.focal.value(),
                self.aperture.value(),self.distance.value(),int(self.metadata.get('source_orientation') or 1),camera.model if camera else '')
        except (ValueError,TypeError) as error:self.status.setText(str(error));return
        self.accept()
