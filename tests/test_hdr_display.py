from copy import deepcopy
import numpy as np
import pytest
from luma.engine import to_srgb,defaults
from luma.hdr import display_linear,sdr_rendition
from luma.native_hdr import Display
from test_studio_ui import app,window,wait

def test_scrgb_keeps_superwhite_and_wide_gamut():
    linear=np.array([[[.18,1,4],[16,.5,2]]],np.float32)
    rgba=display_linear(to_srgb(linear),'sRGB')
    np.testing.assert_allclose(rgba[...,:3],linear,rtol=3e-6)
    assert np.all(rgba[...,3]==1) and rgba.dtype==np.float32
    wide=display_linear(to_srgb(np.array([[[0,1,0]]],np.float32)),'ProPhoto')
    assert wide[...,:3].min()<0 and wide[...,:3].max()>1

def test_headroom_matches_sdr_white_and_disabled_hdr():
    assert Display(active=True,white=200,peak=800).stops==2
    assert Display(active=False,white=80,peak=270).stops==0
    assert Display(active=True,white=400,peak=400).stops==0

def test_sdr_preview_is_explicit_and_never_changes_edits(window):
    w=window;w.hdr_button.click();w.set_setting('exposure',1);w.finish_interaction()
    wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert not w.hdr_sdr_preview.isChecked()
    assert not w.hdr_sdr_controls.isVisible()
    np.testing.assert_allclose(w.view.on_screen,np.clip(w.view.hdr_pixels,0,1),atol=1e-6)
    bright=w.view.on_screen.copy();extended=w.view.hdr_pixels.copy()
    settings=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id))
    w.hdr_sdr_preview.click();wait(lambda:not w.render_running)
    np.testing.assert_allclose(w.view.on_screen,sdr_rendition(extended,settings),atol=1e-6)
    assert w.view.on_screen.mean()<bright.mean() and w.hdr_sdr_controls.isVisible()
    assert w.settings==settings and len(w.catalog.histories(w.current_id))==history
    assert w.catalog.preference('hdr_sdr_preview') is True
    w.hdr_sdr_preview.click();wait(lambda:not w.render_running)
    np.testing.assert_array_equal(w.view.on_screen,bright)
    np.testing.assert_array_equal(w.view.hdr_pixels,extended)

def test_native_request_visualization_and_error_fallback(window):
    w=window;w.hdr_display.state=Display(active=True,supported=True,white=200,peak=800)
    w.hdr_display.update_ui();w.hdr_button.click()
    wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert w.view.hdr_surface.pixels is not None
    assert w.histogram.display_stops==2
    w.hdr_visualize.click();wait(lambda:not w.render_running)
    assert w.view.hdr_surface.pixels is None
    w.hdr_visualize.click();wait(lambda:not w.render_running)
    assert w.view.hdr_surface.pixels is not None
    w.hdr_display.failed('Simulated device removal');wait(lambda:not w.render_running and w.view.hdr_surface.pixels is None)
    assert not w.hdr_display.active and w.hdr_display.failure
    assert np.isfinite(w.view.on_screen).all()

def test_hdr_white_is_not_involuntarily_compressed():
    # A neutral white used to become ~212/255 whenever HDR was enabled.
    white=np.ones((1,1,3),np.float32)
    assert sdr_rendition(white,defaults())[0,0,0]<.85
    np.testing.assert_array_equal(display_linear(white,'sRGB')[...,:3],white)

def test_hdr_curve_edits_superwhite_and_undo(window):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    w=window;w.hdr_button.click();w.tabs.open_panel('curve');curve=w.curve
    white=to_srgb(np.float32(4));lower=to_srgb(np.float32(2))
    QTest.mouseClick(curve,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ControlModifier,curve.point_xy([white,lower]).toPoint())
    w.finish_interaction();wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert any(x>1 and y>1 for x,y in w.settings['curve'])
    from luma.hdr import curve_extended
    assert curve_extended(np.array([white]),w.settings['curve'])[0]==pytest.approx(lower,abs=.07)
    assert curve.axis(1)==.5 and curve.axis_value(1)==pytest.approx(to_srgb(np.float32(16)),rel=1e-6)
    added=deepcopy(w.settings['curve']);point=next(p for p in added if p[0]>1)
    QTest.mouseClick(curve,Qt.MouseButton.RightButton,Qt.KeyboardModifier.NoModifier,curve.point_xy(point).toPoint())
    wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert w.settings['curve']==defaults()['curve']
    w.undo();wait(lambda:not w.render_running);assert w.settings['curve']==added
    w.undo();wait(lambda:not w.render_running)
    assert w.settings['curve']==defaults()['curve']

def test_display_clipping_distinguishes_visible_and_outside(app):
    from luma.widgets import Histogram
    histogram=Histogram();histogram.hdr=True;histogram.display_stops=2
    histogram.set_hdr_ranges(to_srgb(np.array([[[1,2,4]]],np.float32)))
    assert histogram.hdr_inside and not histogram.hdr_outside
    histogram.display_stops=0;histogram.set_hdr_ranges(to_srgb(np.array([[[1,2,4]]],np.float32)))
    assert not histogram.hdr_inside and histogram.hdr_outside
