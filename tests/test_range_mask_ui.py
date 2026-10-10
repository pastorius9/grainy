from copy import deepcopy
import threading
import numpy as np
import pytest
from PySide6.QtCore import Qt,QPointF
from PySide6.QtTest import QTest
from test_studio_ui import app,window,wait


@pytest.fixture(autouse=True)
def no_application_errors(window,monkeypatch):
    errors=[];monkeypatch.setattr(window,'show_error',errors.append)
    yield
    assert not errors,'\n'.join(errors)


def click(w,x=.5,y=.5,shift=False):
    p=w.view.mapFromScene(QPointF(w.view.image_rect.width()*x,w.view.image_rect.height()*y))
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier,p)
    try:wait(lambda:not w.studio.color_pick_running and not w.render_running and not w.jobs)
    except AssertionError:
        pytest.fail(str(dict(picking=w.studio.color_pick_running,rendering=w.render_running,
            jobs=[getattr(job[0].function,'__qualname__',str(job[0].function)) for job in w.jobs.values()],
            sources=w.source_pool.activeThreadCount(),general=w.pool.activeThreadCount(),note=w.studio.range_pick_note.text())))


def test_actual_color_click_precedes_own_edit_append_refine_remove_and_undo(window,monkeypatch):
    import luma.range_mask as module
    from luma.engine import develop
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer()
    s.add_component(dict(type='luma',range=[0,1],softness=.1));s.local_changed('hue',120);w.finish_interaction()
    wait(lambda:not w.render_running);before=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id));real=module.sample_selection;threads=[]
    def capture(*a,**k):threads.append(threading.current_thread());return real(*a,**k)
    monkeypatch.setattr(module,'sample_selection',capture);s.mode('color_range');click(w)
    assert threads and all(t is not threading.current_thread() for t in threads)
    component=s.current_component();assert component['type']=='color' and len(component['samples'])==1
    expected=develop(w.source,{**before,'masks':[]},output_space=None)
    np.testing.assert_allclose(component['samples'][0],expected[60,90],atol=.006)
    assert len(w.catalog.histories(w.current_id))==history+1 and s.range_editor.isVisible()
    added=deepcopy(w.settings);w.undo();wait(lambda:not w.render_running);assert w.settings==before
    w.redo();wait(lambda:not w.render_running);assert w.settings==added
    s.select_component(1);s.mode('color_range');click(w,.2,.2,shift=True)
    assert len(s.current_component()['samples'])==2 and len(s.current_layer()['components'])==2
    s.range_controls['tolerance'].slider.setValue(12);w.finish_interaction();wait(lambda:not w.render_running)
    assert s.current_component()['tolerance']==.12 and w.view.tool_overlay is not None
    s.range_remove.click();assert len(s.current_component()['samples'])==1
    w.undo();wait(lambda:not w.render_running);assert len(s.current_component()['samples'])==2


def test_luminance_rectangle_single_undo_new_layer_and_continuous_refinement(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);before=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id))
    s.mode('luma_range');a=w.view.mapFromScene(QPointF(w.view.image_rect.width()*.2,w.view.image_rect.height()*.2));b=w.view.mapFromScene(QPointF(w.view.image_rect.width()*.7,w.view.image_rect.height()*.6))
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,a)
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,b)
    wait(lambda:bool(w.settings['masks']) and not s.color_pick_running and not w.render_running)
    component=s.current_component();assert component['type']=='luma' and component['range'][1]-component['range'][0]>.12
    assert len(w.catalog.histories(w.current_id))==history+1
    after=deepcopy(w.settings);w.undo();wait(lambda:not w.render_running);assert w.settings==before
    w.redo();wait(lambda:not w.render_running);assert w.settings==after
    s.select_layer(0);s.select_component(0);limits=component['range']
    s.range_controls['softness'].slider.setValue(275);w.finish_interaction();wait(lambda:not w.render_running)
    assert s.current_component()['range']==limits and s.current_component()['softness']==.275
    s.range_controls['low'].slider.setValue(60)
    wait(lambda:w.presented_version==w.render_version and not w.render_running)
    assert s.current_component()['range'][0]==.6 and s.current_component()['range'][1]>=.6
    assert w.view.tool_overlay is not None


@pytest.mark.parametrize('change',['escape','selection','settings','tab'])
def test_delayed_range_pick_cannot_modify_changed_context(window,monkeypatch,change):
    import luma.range_mask as module
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer();s.add_luma();w.finish_interaction();wait(lambda:not w.render_running)
    entered=threading.Event();release=threading.Event();real=module.sample_selection
    def delayed(*a,**k):entered.set();assert release.wait(8);return real(*a,**k)
    monkeypatch.setattr(module,'sample_selection',delayed);s.mode('color_range');s.pick_range([[.5,.5]],'color_range');wait(entered.is_set)
    try:
        if change=='escape':QTest.keyClick(w.view,Qt.Key.Key_Escape)
        elif change=='selection':s.select_component(-1)
        elif change=='settings':w.set_setting('exposure',.5)
        else:w.tabs.setCurrentIndex(3)
        saved=deepcopy(w.settings)
    finally:release.set()
    wait(lambda:not s.color_pick_running and not w.jobs and not w.render_running);assert w.settings==saved


def test_color_sample_limit_legacy_sample_upgrade_and_remove(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer();legacy=dict(type='color',rgb=[.2,.3,.4],tolerance=.3)
    s.add_component(legacy);w.finish_interaction();wait(lambda:not w.render_running);assert s.current_component()=={**legacy,'operation':'add'}
    for x,y in ((.1,.1),(.3,.3),(.6,.6),(.9,.9)):
        s.mode('color_range_add');click(w,x,y)
    assert len(s.current_component()['samples'])==5 and not s.range_add.isEnabled()
    previous=deepcopy(w.settings);click(w,.5,.5);assert w.settings==previous and '5개' in s.range_pick_note.text()
    s.range_samples.setCurrentIndex(3);s.range_remove.click();assert len(s.current_component()['samples'])==4
