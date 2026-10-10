from copy import deepcopy
import threading
import numpy as np
import pytest
from PySide6.QtCore import Qt,QPointF
from PySide6.QtTest import QTest
from test_studio_ui import app,window,wait


def mask(window):
    studio=window.studio;window.tabs.setCurrentIndex(5);studio.new_layer()
    studio.add_component(dict(type='brush',points=[[.5,.5]],radius=.3,feather=0))
    window.finish_interaction();wait(lambda:not window.render_running);return studio


def test_local_color_controls_picker_overlay_and_single_step_undo(window,monkeypatch):
    import luma.point_color as module
    w=window;s=mask(w);s.local_hsl_controls[1].slider.setValue(-35);w.finish_interaction()
    assert w.settings['masks'][0]['adjustments']['hsl'][0][1]==-35
    s.local_hsl_channel.setCurrentIndex(5);assert s.local_hsl_controls[1].slider.value()==0
    before=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id));threads=[];real=module.sample
    def capture(*args,**kwargs):threads.append(threading.current_thread());return real(*args,**kwargs)
    monkeypatch.setattr(module,'sample',capture)
    s.mode('local_point_color');wait(lambda:not w.render_running)
    p=w.view.mapFromScene(QPointF(w.view.image_rect.width()*.5,w.view.image_rect.height()*.5))
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p)
    wait(lambda:bool(s.local_point_values()) and not s.color_pick_running and not w.render_running)
    assert threads and all(t is not threading.current_thread() for t in threads)
    assert s.local_point_values()[0]['version']==2 and len(w.catalog.histories(w.current_id))==history+1
    added=deepcopy(w.settings);w.undo();wait(lambda:not w.render_running);assert w.settings==before
    w.redo();wait(lambda:not w.render_running);assert w.settings==added
    s.local_point_controls['lightness_range'].slider.setValue(20);s.local_point_controls['saturation'].slider.setValue(70)
    w.finish_interaction();wait(lambda:not w.render_running);pixels=w.view.on_screen.copy();saved=deepcopy(w.settings)
    coverage_threads=[];real_preview=module.preview
    def coverage(*args,**kwargs):coverage_threads.append(threading.current_thread());return real_preview(*args,**kwargs)
    monkeypatch.setattr(module,'preview',coverage);s.local_point_preview.setChecked(True)
    wait(lambda:not w.render_running and w.view.tool_overlay is not None)
    overlay=w.view.tool_overlay;rgba=np.frombuffer(overlay.bits(),np.uint8).reshape(overlay.height(),overlay.bytesPerLine())
    assert np.any(rgba[:,3:overlay.width()*4:4]>0) and np.any(rgba[:,3:overlay.width()*4:4]==0)
    assert coverage_threads and all(t is not threading.current_thread() for t in coverage_threads)
    np.testing.assert_array_equal(w.view.on_screen,pixels);assert w.settings==saved
    count=len(coverage_threads);s.local_point_controls['hue'].slider.setValue(40);w.finish_interaction();wait(lambda:not w.render_running)
    assert len(coverage_threads)==count
    s.local_point_controls['range'].slider.setValue(6);w.finish_interaction();wait(lambda:not w.render_running)
    assert len(coverage_threads)>count
    s.local_point_preview.setChecked(False);s.overlay.setChecked(False);wait(lambda:not w.render_running)
    assert w.view.tool_overlay is None
    s.remove_local_point();wait(lambda:not w.render_running);assert not s.local_point_values()
    w.undo();wait(lambda:not w.render_running);assert s.local_point_values() and s.local_point_controls['lightness_range'].slider.value()==20


@pytest.mark.parametrize('change',['settings','layer','tool','tab'])
def test_late_sample_cannot_edit_a_changed_context(window,monkeypatch,change):
    import luma.point_color as module
    w=window;s=mask(w);entered=threading.Event();release=threading.Event();real=module.sample
    def delayed(*a,**k):entered.set();assert release.wait(8);return real(*a,**k)
    monkeypatch.setattr(module,'sample',delayed)
    s.pick_point_color([.5,.5],local=True);wait(entered.is_set)
    try:
        if change=='settings':w.set_setting('contrast',23)
        elif change=='layer':s.new_layer()
        elif change=='tool':s.mode('brush')
        else:w.tabs.setCurrentIndex(3)
    finally:release.set()
    wait(lambda:not s.color_pick_running and not w.jobs and not w.render_running)
    assert all(not layer.get('adjustments',{}).get('point_colors') for layer in w.settings['masks'])


def test_outside_mask_pick_reports_reason_without_creating_an_edit(window):
    w=window;s=mask(w);before=deepcopy(w.settings);s.pick_point_color([.03,.03],local=True)
    wait(lambda:not s.color_pick_running and not w.jobs and not w.render_running)
    assert w.settings==before and '마스크' in s.local_point_note.text()


def test_legacy_global_point_keeps_settings_until_new_range_is_explicitly_changed(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(3)
    old=dict(rgb=[.6,.35,.3],range=.12,hue=30,saturation=10,lightness=-5)
    w.set_setting('point_colors',[old]);w.finish_interaction();w.load_controls();wait(lambda:not w.render_running)
    assert w.settings['point_colors']==[old] and s.point_controls['lightness_range'].slider.value()==100
    s.point_controls['lightness_range'].slider.setValue(30);w.finish_interaction();wait(lambda:not w.render_running)
    assert w.settings['point_colors'][0]['version']==2 and w.settings['point_colors'][0]['lightness_range']==.3
    w.undo();wait(lambda:not w.render_running);assert w.settings['point_colors']==[old]
    s.mode('point_color');s.pick_point_color([.5,.5]);wait(lambda:not s.color_pick_running and len(w.settings['point_colors'])==2 and not w.render_running)
    s.point_controls['range'].reset();assert w.settings['point_colors'][-1]['range']==.12
