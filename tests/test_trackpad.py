"""macOS pointing devices on the photo: a trackpad scrolls and pinches, a wheel mouse zooms."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt,QPoint,QPointF,QEvent
from PySide6.QtGui import QWheelEvent,QNativeGestureEvent,QPointingDevice
from PySide6.QtWidgets import QApplication
from luma import widgets
from test_studio_ui import app,window,wait


def wheel(view,pixels,angle,phase):
    centre=QPointF(view.viewport().rect().center())
    event=QWheelEvent(centre,view.viewport().mapToGlobal(centre),pixels,angle,Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,phase,False)
    QApplication.sendEvent(view.viewport(),event)


def gesture(view,kind,value=0.):
    centre=QPointF(view.viewport().rect().center())
    event=QNativeGestureEvent(kind,QPointingDevice.primaryPointingDevice(),2,centre,centre,view.viewport().mapToGlobal(centre),value,QPointF(),1)
    QApplication.sendEvent(view.viewport(),event)


def test_trackpad_moves_the_photo_and_pinch_zooms_on_macos(window,monkeypatch):
    monkeypatch.setattr(widgets,'MAC',True)
    view=window.view;view.zoom_by(8/view.transform().m11())           # larger than the view: there is somewhere to scroll to
    zoom=view.transform().m11();x,y=view.horizontalScrollBar().value(),view.verticalScrollBar().value()
    wheel(view,QPoint(-30,-20),QPoint(-60,-40),Qt.ScrollPhase.ScrollUpdate)
    assert view.transform().m11()==zoom
    assert view.horizontalScrollBar().value()==x+30 and view.verticalScrollBar().value()==y+20
    wheel(view,QPoint(),QPoint(0,120),Qt.ScrollPhase.NoScrollPhase)     # a wheel mouse: one notch, as on Windows
    assert view.transform().m11()==pytest.approx(zoom*1.18)
    zoom=view.transform().m11()
    gesture(view,Qt.NativeGestureType.ZoomNativeGesture,.25)
    assert view.transform().m11()==pytest.approx(zoom*1.25) and not view.fit_mode
    gesture(view,Qt.NativeGestureType.SmartZoomNativeGesture)
    assert view.fit_mode
    gesture(view,Qt.NativeGestureType.SmartZoomNativeGesture)
    assert not view.fit_mode and view.transform().m11()==1


def test_other_systems_keep_the_wheel_zoom_for_every_device(window,monkeypatch):
    monkeypatch.setattr(widgets,'MAC',False)
    view=window.view;zoom=view.transform().m11()
    wheel(view,QPoint(0,-20),QPoint(0,-40),Qt.ScrollPhase.ScrollUpdate)
    assert view.transform().m11()==pytest.approx(zoom/1.18)
    zoom=view.transform().m11()
    gesture(view,Qt.NativeGestureType.ZoomNativeGesture,.25)
    assert view.transform().m11()==zoom
