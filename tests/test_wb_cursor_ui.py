from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGraphicsView
from PySide6.QtTest import QTest
from test_studio_ui import app,window,wait


def test_wb_cursor_sampling_escape_and_tool_switch(window):
    w=window;v=w.view
    w.wb_button.click()
    assert v.sample_mode and v.viewport().cursor().shape()==Qt.CursorShape.BitmapCursor
    assert v.viewport().cursor().hotSpot().x()==4 and v.viewport().cursor().hotSpot().y()==28
    assert v.dragMode()==QGraphicsView.DragMode.NoDrag
    QTest.keyClick(v,Qt.Key.Key_Escape)
    assert not v.sample_mode and not w.wb_button.isChecked()
    assert v.dragMode()==QGraphicsView.DragMode.ScrollHandDrag
    w.wb_button.click();w.render();wait(lambda:not w.render_running)
    assert v.viewport().cursor().shape()==Qt.CursorShape.BitmapCursor
    position=v.mapFromScene(v.image_rect.center())
    QTest.mouseClick(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position)
    wait(lambda:not w.render_running)
    assert not v.sample_mode and not w.wb_button.isChecked() and v.fit_mode
    w.studio.new_layer();w.studio.mode('brush');assert v.tool_mode=='brush'
    w.wb_button.click();assert not v.tool_mode and v.sample_mode
    w.crop_button.click();assert not v.sample_mode and v.crop_mode
    assert v.viewport().cursor().shape()!=Qt.CursorShape.BitmapCursor
