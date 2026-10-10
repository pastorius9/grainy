import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_studio_ui import app,window,wait
from luma.widgets import Adjustment


def enter(control,text,key=Qt.Key.Key_Return):
    QTest.mouseClick(control.value,Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    control.value.selectAll()
    QTest.keyClicks(control.value,text)
    if key is not None:QTest.keyClick(control.value,key)


@pytest.mark.parametrize('low,high,scale,text,expected',[(-5,5,100,'-1.25',-1.25),(-200,200,1,'-175',-175),(-15,15,10,'2.7',2.7),(2000,50000,1,'7200',7200)])
def test_numeric_input_updates_slider_and_respects_precision(app,low,high,scale,text,expected):
    control=Adjustment('test','값',low,high,scale);control.show();events=[];commits=[]
    control.changed.connect(lambda key,value:events.append((key,value)))
    control.committed.connect(lambda:commits.append(True))
    enter(control,text)
    assert control.value.value()==pytest.approx(expected)
    assert control.slider.value()==round(expected*scale)
    assert events[-1]==('test',expected) and commits
    control.slider.setValue(round(low*scale))
    assert control.value.value()==pytest.approx(low)
    control.set_value(high);assert control.value.value()==pytest.approx(high)
    control.close()


def test_enter_and_focus_out_apply_to_photo_and_undo_as_one_edit(window):
    w=window;control=w.adjustments['exposure'];before=len(w.catalog.histories(w.current_id))
    enter(control,'1.37',key=None)
    assert w.settings['exposure']==0
    QTest.keyClick(control.value,Qt.Key.Key_Return)
    wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert w.settings['exposure']==pytest.approx(1.37)
    assert w.catalog.photo(w.current_id)['settings']['exposure']==pytest.approx(1.37)
    assert len(w.catalog.histories(w.current_id))==before+1
    w.undo();wait(lambda:not w.render_running)
    assert control.value.value()==0 and control.slider.value()==0
    enter(control,'-0.45',key=None)
    w.search.setFocus();QApplication.processEvents()
    assert w.settings['exposure']==pytest.approx(-.45)
    assert w.catalog.photo(w.current_id)['settings']['exposure']==pytest.approx(-.45)
