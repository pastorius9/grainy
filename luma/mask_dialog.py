"""Edit a stored manual mask component without rebuilding its selection."""
from .i18n import tr
from copy import deepcopy
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QDialogButtonBox,
    QDoubleSpinBox,QComboBox,QCheckBox,QLabel)

NAMES={'brush':'브러시','linear':'선형 그레이디언트','radial':'방사형 그레이디언트','luma':'광도 범위','color':'색상 범위'}


class MaskComponentDialog(QDialog):
    def __init__(self,parent,component):
        super().__init__(parent)
        self.component=deepcopy(component);self.fields={}
        self.setWindowTitle(tr('선택 영역 편집'));self.setMinimumWidth(360)
        box=QVBoxLayout(self);form=QFormLayout();box.addLayout(form)
        form.addRow(tr('종류'),QLabel(tr(NAMES.get(component.get('type'),'선택 영역'))))
        self.enabled=QCheckBox(tr('선택 영역 사용'));self.enabled.setChecked(component.get('enabled',True));form.addRow(self.enabled)
        self.invert=QCheckBox(tr('이 선택 영역 반전'));self.invert.setChecked(component.get('invert',False));form.addRow(self.invert)
        self.operation=QComboBox()
        for title,key in [('더하기','add'),('빼기','subtract'),('교차','intersect')]:self.operation.addItem(tr(title),key)
        self.operation.setCurrentIndex(max(0,self.operation.findData(component.get('operation','add'))));form.addRow(tr('결합'),self.operation)
        def number(key,title,value,low=0,high=100,decimals=1):
            control=QDoubleSpinBox();control.setRange(low,high);control.setDecimals(decimals);control.setValue(value)
            form.addRow(tr(title),control);self.fields[key]=control
        kind=component.get('type')
        if kind=='brush':
            number('radius','크기',component.get('radius',.04)*100,.2,25,2)
            number('flow','칠하는 강도',component.get('flow',100))
            number('density','최대 농도',component.get('density',100))
            self.auto_mask=QCheckBox(tr('시작점과 비슷한 색 안에서만'));self.auto_mask.setChecked(component.get('auto_mask',False));form.addRow(self.auto_mask)
            number('auto_tolerance','색 경계 허용량',component.get('auto_tolerance',.18),.02,1,2)
        if kind in ('brush','radial'):number('feather','경계 흐림',component.get('feather',70))
        if kind=='luma':
            low,high=component.get('range',[.25,.75]);number('low','최소 밝기',low*100);number('high','최대 밝기',high*100)
            number('softness','범위 경계 흐림',component.get('softness',.1)*100,.1,100)
        if kind=='color':number('tolerance','색상 범위 허용량',component.get('tolerance',.3),.02,1.7,2)
        if kind in ('linear','radial'):
            points=component.get('points',[[.25,.25],[.75,.75]])
            for i,point in enumerate((points[0],points[-1])):
                for axis,value in zip(('x','y'),point):number(f'{axis}{i}',f'{"시작" if i==0 else "끝"} {axis.upper()} (%)',value*100)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);box.addWidget(buttons)

    def result_component(self):
        result=deepcopy(self.component);values={key:field.value() for key,field in self.fields.items()}
        result.update(enabled=self.enabled.isChecked(),invert=self.invert.isChecked(),operation=self.operation.currentData())
        for key in ('feather','flow','density','tolerance'):
            if key in values:result[key]=values[key]
        if 'radius' in values:
            result['radius']=values['radius']/100
            if self.auto_mask.isChecked():result.update(auto_mask=True,auto_tolerance=values['auto_tolerance'],seed=result.get('seed',result['points'][0]))
            else:result.pop('auto_mask',None)
            # Preserve legacy stroke blending unless flow/density actually changed.
            if any(values[k]!=self.component.get(k,100) for k in ('flow','density')):result['paint_version']=2
        if 'low' in values:result.update(range=sorted([values['low']/100,values['high']/100]),softness=values['softness']/100)
        if 'x0' in values:result['points']=[[values[f'x{i}']/100,values[f'y{i}']/100] for i in (0,1)]
        return result
