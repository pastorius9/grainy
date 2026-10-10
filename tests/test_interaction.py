"""Regressions for continuous slider feedback and a persistent crop overlay."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication,QStyle,QStyleOptionSlider
from PySide6.QtCore import Qt,QPoint,QPointF,QEventLoop,QTimer
from PySide6.QtTest import QTest
import luma.app as application
from luma.app import MainWindow,configure_application
from luma.engine import develop


@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([])
    configure_application(app)
    return app


def wait_for(predicate,timeout=12):
    if predicate():
        return
    loop=QEventLoop()
    poll=QTimer()
    poll.timeout.connect(lambda:loop.quit() if predicate() else None)
    poll.start(5)
    deadline=QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(int(timeout*1000))
    loop.exec()
    poll.stop()
    deadline.stop()
    assert predicate(),'UI timed out'


@pytest.fixture
def window(app,tmp_path):
    y,x=np.mgrid[0:1200,0:1800]
    rgb=np.stack([x/1799,y/1199,(x+y)/2998],axis=-1)
    path=tmp_path/'gradient.png'
    Image.fromarray(np.uint8(rgb*255)).save(path)
    w=MainWindow(tmp_path/'catalog')
    w.show()
    QTest.qWait(30)
    errors=[]
    w.show_error=lambda message:errors.append(message)
    photo_id=w.catalog.add(path)
    w.refresh_lists()
    w.activate(photo_id)
    wait_for(lambda:w.source is not None and not w.render_running)
    yield w
    for adjustment in w.adjustments.values():
        adjustment.slider.setSliderDown(False)
    wait_for(lambda:not w.render_running)
    w.close()
    QApplication.processEvents()
    assert not errors,errors


def drag(view,start,end,steps=8):
    start,end=start.toPoint(),end.toPoint()
    QTest.mousePress(view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,start)
    for i in range(1,steps+1):
        point=start+(end-start)*i/steps
        QTest.mouseMove(view.viewport(),point,5)
    QTest.mouseRelease(view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,end)


def enter_crop(w):
    w.crop_button.click()
    wait_for(lambda:not w.render_running)
    assert w.view.crop_mode


def test_histogram_streams_during_real_slider_drag_and_refines(window,monkeypatch):
    w=window
    # A slow full-quality frame must not block the dedicated live preview worker.
    real_develop=application.develop
    def slow_quality(source,*args,**kwargs):
        if max(source.shape[:2])>720:
            time.sleep(.18)
        return real_develop(source,*args,**kwargs)
    monkeypatch.setattr(application,'develop',slow_quality)
    w.set_setting('exposure',.01)
    w.render_quality()
    frames=[]
    slider=w.adjustments['exposure'].slider
    start_time=time.perf_counter()
    w.previewPresented.connect(lambda version,quality,duration:frames.append(
        {'version':version,'quality':quality,'render_ms':duration,'time':time.perf_counter()-start_time,
         'held':slider.isSliderDown(),'bins':np.array(w.histogram.bins).copy()}))
    option=QStyleOptionSlider()
    slider.initStyleOption(option)
    handle=slider.style().subControlRect(QStyle.ComplexControl.CC_Slider,option,QStyle.SubControl.SC_SliderHandle,slider)
    start=handle.center()
    QTest.mousePress(slider,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,start)
    assert slider.isSliderDown()
    # Run the real Qt event loop, rather than a tight Python/qWait loop that
    # starves Python worker threads of the GIL and mismeasures UI latency.
    movement=QTimer()
    loop=QEventLoop()
    moves=[0]
    def move_pointer():
        moves[0]+=1
        QTest.mouseMove(slider,QPoint(start.x()+moves[0]*2,start.y()))
        if moves[0]==40:
            movement.stop()
            loop.quit()
    movement.timeout.connect(move_pointer)
    movement.start(14)
    loop.exec()
    during=[f for f in frames if f['held'] and f['quality']=='live']
    assert len(during)>=5, f'Only {len(during)} live frames during continuous drag'
    assert during[0]['time']<.25, 'First feedback took longer than 250 ms'
    assert not np.array_equal(during[0]['bins'],during[-1]['bins'])
    QTest.mouseRelease(slider,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,QPoint(start.x()+80,start.y()))
    wait_for(lambda:not w.render_running)
    assert w.presented_quality=='quality' and w.presented_version==w.render_version
    assert np.allclose(w.view.on_screen,develop(w.source,w.settings),atol=1e-6)
    intervals=np.diff([f['time'] for f in during])*1000
    out=Path(__file__).resolve().parents[1]/'validation'
    out.mkdir(exist_ok=True)
    report={'live_frames_while_dragging':len(during),'first_feedback_ms':round(during[0]['time']*1000,1),
            'median_frame_interval_ms':round(float(np.median(intervals)),1),
            'median_render_ms':round(float(np.median([f['render_ms'] for f in during])),1),
            'final_matches_export_pipeline':True,'slow_quality_worker_simulated_ms':180}
    (out/'interaction-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


def test_mask_overlay_and_colour_conversion_stay_off_ui_thread(window,monkeypatch):
    import threading
    import luma.processing as processing
    w=window;main_thread=threading.get_ident();calls=[]
    real_develop=application.develop;real_mask=processing.layer_mask
    def record_develop(*args,**kwargs):
        calls.append(('develop',threading.get_ident()))
        return real_develop(*args,**kwargs)
    def record_mask(*args,**kwargs):
        calls.append(('mask',threading.get_ident()))
        return real_mask(*args,**kwargs)
    monkeypatch.setattr(application,'develop',record_develop)
    monkeypatch.setattr(processing,'layer_mask',record_mask)
    w.tabs.setCurrentIndex(5);w.studio.new_layer()
    w.studio.mode('brush');w.studio.stroke([[.4,.5],[.6,.55]],False)
    slider=w.studio.local_controls['exposure'].slider
    slider.setSliderDown(True)
    slider.setValue(65)
    wait_for(lambda:w.presented_version==w.render_version)
    assert w.interacting() and w.presented_quality=='live'
    assert w.view.tool_overlay is not None
    slider.setSliderDown(False);w.finish_interaction()
    wait_for(lambda:not w.render_running)
    assert w.presented_quality=='quality'
    assert w.view.on_screen.shape==(1200,1800,3)
    assert w.view.tool_overlay.width()==720
    assert calls and {kind for kind,_ in calls}=={'develop','mask'}
    assert all(thread!=main_thread for _,thread in calls)
    assert np.allclose(w.view.on_screen,real_develop(w.source,w.settings),atol=1e-6)


def test_leaving_mask_tab_drops_pending_overlay(window):
    w=window;w.tabs.setCurrentIndex(5);w.studio.new_layer()
    w.studio.mode('brush');w.studio.stroke([[.5,.5]],False)
    w.studio.local_controls['exposure'].slider.setValue(90)
    w.render_live()
    w.tabs.setCurrentIndex(0)
    wait_for(lambda:not w.render_running)
    assert w.view.tool_overlay is None
    assert w.presented_quality=='quality' and w.presented_version==w.render_version


def test_crop_handles_move_apply_reopen_and_cancel(window):
    w=window
    enter_crop(w)
    rect=w.view.crop_screen_rect()
    drag(w.view,rect.bottomRight(),rect.topLeft()+QPointF(rect.width()*.65,rect.height()*.65))
    assert w.view.crop_mode and w.settings['crop'] is None
    assert .63<w.view.crop_rect.width()<.67
    rect=w.view.crop_screen_rect()
    before=w.view.crop_values()
    drag(w.view,rect.center(),rect.center()+QPointF(60,35))
    after=w.view.crop_values()
    assert after[0]>before[0] and after[1]>before[1]
    assert after[2]-after[0]==pytest.approx(before[2]-before[0])
    screenshot=Path(__file__).resolve().parents[1]/'validation'/'04-crop-interface.png'
    w.grab().save(str(screenshot))
    QTest.keyClick(w.view,Qt.Key.Key_Return)
    wait_for(lambda:not w.render_running)
    assert not w.view.crop_mode
    saved=list(w.settings['crop'])
    enter_crop(w)
    assert w.view.crop_values()==pytest.approx(saved)
    rect=w.view.crop_screen_rect()
    drag(w.view,rect.topLeft(),rect.topLeft()+QPointF(30,25))
    assert w.view.crop_values()!=saved
    QTest.keyClick(w.view,Qt.Key.Key_Escape)
    wait_for(lambda:not w.render_running)
    assert w.settings['crop']==saved
    assert w.catalog.photo(w.current_id)['settings']['crop']==saved
    assert w.view.on_screen.shape[:2]==(round(saved[3]*1200)-round(saved[1]*1200),round(saved[2]*1800)-round(saved[0]*1800))


def test_crop_fixed_ratio_handles_and_boundaries(window):
    w=window
    # Setting a ratio before entering crop must apply it when the full image arrives.
    w.aspect.setCurrentIndex(w.aspect.findText('4 : 5'))
    enter_crop(w)
    def assert_valid():
        r=w.view.crop_rect
        assert r.width()*1800/(r.height()*1200)==pytest.approx(.8,abs=.001)
        assert r.left()>=-1e-8 and r.top()>=-1e-8 and r.right()<=1+1e-8 and r.bottom()<=1+1e-8
    assert_valid()
    for handle in ('se','n','e','nw','w','s','ne','sw'):
        rect=w.view.crop_screen_rect()
        point=w.view.handle_points(rect)[handle]
        drag(w.view,point,rect.center()+(point-rect.center())*.77)
        assert_valid()
    rect=w.view.crop_screen_rect()
    drag(w.view,rect.center(),rect.center()+QPointF(2000,-2000))
    assert_valid()
    w.crop_apply.click()
    wait_for(lambda:not w.render_running)
    assert not w.view.crop_mode and w.settings['crop']


def test_stale_preview_cannot_replace_new_photo_or_original_compare(window,tmp_path):
    w=window
    path=tmp_path/'second.png'
    Image.new('RGB',(1400,900),(20,190,60)).save(path)
    ident=w.catalog.add(path)
    for value in [.4,.8,1.2]:
        w.set_setting('exposure',value)
        w.render_live()
    w.activate(ident)
    wait_for(lambda:w.source is not None and not w.render_running)
    assert w.current_id==ident
    assert np.allclose(w.view.on_screen,develop(w.source,w.settings))
    w.set_setting('exposure',1.5)
    w.render_live()
    w.before_button.click()
    wait_for(lambda:not w.render_running)
    assert np.allclose(w.view.on_screen,develop(w.source,application.defaults()))


def test_overlay_stays_stable_between_live_and_quality_previews(window):
    w=window
    enter_crop(w)
    rect=w.view.crop_screen_rect()
    drag(w.view,rect.bottomRight(),rect.center()+QPointF(90,70))
    draft=w.view.crop_values()
    screen=w.view.crop_screen_rect()
    w.set_setting('exposure',.7)
    w.render_live()
    wait_for(lambda:not w.render_running)
    assert w.view.crop_values()==draft
    assert w.view.crop_screen_rect()==screen
    w.cancel_crop()


def test_auto_tone_button_leaves_histogram_headroom_and_undo_restores(window,tmp_path):
    w=window
    gray=np.tile(np.linspace(65,165,1800,dtype=np.uint8),(1200,1))
    path=tmp_path/'low-contrast.png'
    Image.fromarray(gray).convert('RGB').save(path)
    photo_id=w.catalog.add(path)
    w.refresh_lists()
    w.activate(photo_id)
    wait_for(lambda:w.source is not None and not w.render_running)
    original=w.view.on_screen.copy()
    assert original.min()>.2 and original.max()<.7
    QTest.mouseClick(w.auto_button,Qt.MouseButton.LeftButton)
    wait_for(lambda:not w.render_running)
    assert w.last_auto_tone['status']=='applied'
    assert np.percentile(w.view.on_screen,.1)==pytest.approx(.03,abs=.005)
    assert np.percentile(w.view.on_screen,99.9)==pytest.approx(.97,abs=.005)
    assert w.histogram.bins[0][:4].sum()==0 and w.histogram.bins[0][124:].sum()==0
    assert w.histogram.bins[0][4]>0 and w.histogram.bins[0][123]>0
    assert '8–247 / 255' in w.auto_tone_note.text() and '3%' in w.auto_tone_note.text()
    assert w.catalog.histories(photo_id)[0]['label']=='자동톤'
    out=Path(__file__).resolve().parents[1]/'validation'
    w.grab().save(str(out/'07-auto-tone-margin.png'))
    w.undo()
    wait_for(lambda:not w.render_running)
    assert np.allclose(w.view.on_screen,original)
