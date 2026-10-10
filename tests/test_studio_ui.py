import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from copy import deepcopy
import json
import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt,QPointF,QEventLoop,QTimer
from PySide6.QtTest import QTest
from luma.app import MainWindow,configure_application
from luma.engine import geometry,develop


def wait(predicate,timeout=15000):
    loop=QEventLoop();poll=QTimer();poll.setInterval(10);poll.timeout.connect(lambda:loop.quit() if predicate() else None)
    deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(loop.quit)
    poll.start();deadline.start(timeout)
    # A paint/timer in the same event batch can queue a new thumbnail after
    # the poll requested exit. Recheck without resetting the original timeout.
    while not predicate() and deadline.isActive():loop.exec()
    poll.stop();deadline.stop();assert predicate()


@pytest.fixture(scope='module')
def app():
    a=QApplication.instance() or QApplication([]);configure_application(a);return a


@pytest.fixture
def window(app,tmp_path):
    yy,xx=np.mgrid[:120,:180];rgb=np.stack([.2+xx/360,.2+yy/240,np.full_like(xx,.4,dtype=float)],-1)
    path=tmp_path/'test.png';Image.fromarray(np.uint8(rgb*255)).save(path)
    w=MainWindow(tmp_path/'data');ident=w.catalog.add(path);w.refresh_lists();w.activate(ident);w.show()
    wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
    yield w
    wait(lambda:not w.render_running);w.close()


def test_brush_pointer_overlay_and_undo(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer();s.mode('brush')
    s.local_controls['exposure'].slider.setValue(100)
    point=w.view.mapFromScene(QPointF(w.view.image_rect.width()*.55,w.view.image_rect.height()*.5))
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    wait(lambda:not w.render_running)
    assert len(w.settings['masks'][0]['components'])==1
    assert w.view.tool_overlay is not None
    assert w.settings['masks'][0]['components'][0]['points'][0][0]==pytest.approx(.55,abs=.02)
    w.undo();wait(lambda:not w.render_running)
    assert not w.settings['masks'] or not w.settings['masks'][0]['components']


def test_source_coordinates_after_crop_and_rotation(window):
    w=window;w.set_setting('rotation',1);w.set_setting('crop',[.2,.1,.9,.8]);w.commit();w.render()
    wait(lambda:not w.render_running)
    original=w.studio.source_point([.5,.5])
    # Clockwise rotation maps displayed x/y back to source x=y, source y=1-x.
    assert original[0]==pytest.approx(.45,abs=.03)
    assert original[1]==pytest.approx(.45,abs=.03)


def test_brush_flow_erase_escape_size_and_release_point(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer();s.mode('brush')
    s.flow.slider.setValue(25);s.density.slider.setValue(70)
    view=w.view;start=view.mapFromScene(QPointF(view.image_rect.width()*.3,view.image_rect.height()*.4))
    end=view.mapFromScene(QPointF(view.image_rect.width()*.65,view.image_rect.height()*.55))
    QTest.mousePress(view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.AltModifier,start)
    QTest.mouseRelease(view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.AltModifier,end)
    component=s.current_component()
    assert component['operation']=='subtract' and component['flow']==25 and component['density']==70
    assert component['points'][-1][0]==pytest.approx(.65,abs=.02)
    before=deepcopy(w.settings['masks']);size=s.radius.slider.value()
    QTest.keyClick(view,Qt.Key.Key_BracketRight);assert s.radius.slider.value()>size
    QTest.mousePress(view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,start)
    QTest.keyClick(view,Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,end)
    assert w.settings['masks']==before


def test_stroke_renders_before_release_but_only_saves_once(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer();s.local_changed('exposure',1.5);w.finish_interaction()
    wait(lambda:not w.render_running);s.mode('brush');s.radius.slider.setValue(180)
    wait(lambda:not w.render_running);baseline=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id));base_pixels=w.view.on_screen.copy()
    point=w.view.mapFromScene(QPointF(w.view.image_rect.width()*.5,w.view.image_rect.height()*.5))
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    wait(lambda:s.pending_stroke is not None and w.presented_version==w.render_version)
    assert w.settings==baseline and w.catalog.photo(w.current_id)['settings']==baseline
    assert len(w.catalog.histories(w.current_id))==history
    assert w.view.on_screen[60,90].mean()>base_pixels[60,90].mean()+.1
    assert w.view.tool_overlay is not None
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    wait(lambda:not w.render_running)
    assert s.pending_stroke is None and len(s.current_layer()['components'])==1
    assert len(w.catalog.histories(w.current_id))==history+1
    w.undo();wait(lambda:not w.render_running);assert w.settings==baseline


def test_cancel_discards_inflight_preview_and_photo_switch(window,tmp_path,monkeypatch):
    import threading
    import luma.app as application
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer();s.local_changed('exposure',2);w.finish_interaction();wait(lambda:not w.render_running)
    s.mode('brush');s.radius.slider.setValue(200);wait(lambda:not w.render_running)
    baseline=deepcopy(w.settings);pixels=w.view.on_screen.copy();original=application.develop;entered=threading.Event();release=threading.Event()
    def delayed(source,settings,*args,**kwargs):
        if any(layer.get('components') for layer in settings.get('masks',[])):
            entered.set();assert release.wait(5)
        return original(source,settings,*args,**kwargs)
    monkeypatch.setattr(application,'develop',delayed)
    point=w.view.mapFromScene(QPointF(w.view.image_rect.width()*.5,w.view.image_rect.height()*.5))
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    try:
        wait(entered.is_set);QTest.keyClick(w.view,Qt.Key.Key_Escape)
        assert s.pending_stroke is None
    finally:release.set()
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    wait(lambda:not w.render_running and w.presented_version==w.render_version)
    assert w.settings==baseline;np.testing.assert_array_equal(w.view.on_screen,pixels)
    path=tmp_path/'other.png';Image.new('RGB',(80,60),'blue').save(path);ident=w.catalog.add(path)
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    w.activate(ident);wait(lambda:w.source is not None and not w.render_running)
    assert s.pending_stroke is None and w.settings['masks']==[]


def test_continuous_stroke_streams_pixels_and_histogram_without_saving(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.new_layer();s.local_changed('exposure',1.5)
    w.finish_interaction();wait(lambda:not w.render_running);s.mode('brush');s.radius.slider.setValue(90)
    wait(lambda:not w.render_running);baseline=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id))
    frames=[];w.previewPresented.connect(lambda *args:frames.append(np.array(w.histogram.bins).copy()) if s.pending_stroke else None)
    def position(x):return w.view.mapFromScene(QPointF(x*w.view.image_rect.width(),.5*w.view.image_rect.height()))
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position(.2))
    loop=QEventLoop();movement=QTimer();positions=iter(np.linspace(.22,.8,40))
    def move_pointer():
        x=next(positions,None)
        if x is None:movement.stop();loop.quit();return
        QTest.mouseMove(w.view.viewport(),position(x))
    movement.timeout.connect(move_pointer);movement.start(20);loop.exec()
    assert len(frames)>=4 and not np.array_equal(frames[0],frames[-1])
    assert w.settings==baseline and len(w.catalog.histories(w.current_id))==history
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position(.8))
    wait(lambda:not w.render_running)
    assert len(w.catalog.histories(w.current_id))==history+1


def test_empty_layer_draft_overlay_and_local_curve_persistence(window):
    w=window;s=w.studio;w.tabs.setCurrentIndex(5);s.mode('brush');s.auto_mask.setChecked(True)
    point=w.view.mapFromScene(QPointF(w.view.image_rect.width()*.5,w.view.image_rect.height()*.5))
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    wait(lambda:w.presented_version==w.render_version)
    assert not w.settings['masks'] and w.view.tool_overlay is not None
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
    s.local_curve_changed([[0,0],[.5,.65],[1,1]]);s.local_changed('hue',35);w.finish_interaction();wait(lambda:not w.render_running)
    assert s.current_component()['auto_mask']
    saved=w.catalog.photo(w.current_id)['settings']['masks'][0]
    assert saved['adjustments']['local_curves'][0][1]==[.5,.65] and saved['adjustments']['hue']==35
    w.load_controls();assert s.local_curve.points[1]==[.5,.65]


def test_component_edit_duplicate_reorder_layer_and_persistent_undo(window,tmp_path):
    from luma.mask_dialog import MaskComponentDialog
    w=window;s=w.studio;s.new_layer();s.mode('brush');s.stroke([[.4,.5]],False)
    dialog=MaskComponentDialog(w,s.current_component());dialog.fields['flow'].setValue(15);dialog.fields['density'].setValue(60)
    s.replace_component(dialog.result_component());saved=deepcopy(w.settings['masks'])
    s.duplicate_component();assert len(s.current_layer()['components'])==2
    s.move_component(-1);assert s.component_index==0
    s.remove_component();assert w.settings['masks']==saved
    s.duplicate_layer();assert len(w.settings['masks'])==2
    s.local_changed('blacks',-30);w.finish_interaction();s.move_layer(-1)
    assert w.settings['masks'][0]['adjustments']['blacks']==-30
    s.remove_layer();w.undo();wait(lambda:not w.render_running)
    assert len(w.settings['masks'])==2
    w.commit();persisted=w.catalog.photo(w.current_id)['settings']
    assert persisted['masks']==w.settings['masks']
    from luma.library import write_sidecar,read_sidecar
    photo=w.catalog.photo(w.current_id);sidecar=tmp_path/'masks.xmp';write_sidecar(photo,sidecar)
    assert read_sidecar(sidecar)['settings']['masks']==persisted['masks']


@pytest.mark.parametrize('kind',['linear','radial'])
def test_gradient_handles_after_crop_rotation_and_undo(window,kind):
    w=window;s=w.studio;w.set_setting('rotation',1);w.set_setting('crop',[.1,.1,.9,.9]);w.finish_interaction()
    wait(lambda:not w.render_running);w.tabs.setCurrentIndex(5);s.new_layer()
    s.add_component(dict(type=kind,points=[[.3,.35],[.7,.65]],feather=70));w.finish_interaction()
    wait(lambda:not w.render_running);before=deepcopy(s.current_component());s.edit_component_handles()
    assert len(w.view.tool_handles)==2
    handle=np.mean(w.view.tool_handles,axis=0);target=handle+[.06,.03]
    def position(p):return w.view.mapFromScene(QPointF(p[0]*w.view.image_rect.width(),p[1]*w.view.image_rect.height()))
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position(handle))
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,position(target))
    after=s.current_component();assert after['points']!=before['points']
    # Source deltas reflect the displayed clockwise rotation.
    delta=np.array(after['points'])-before['points']
    np.testing.assert_allclose(delta,[[.024,-.048]]*2,atol=.015)
    w.undo();wait(lambda:not w.render_running)
    assert w.settings['masks'][0]['components'][0]==before
    if w.view.tool_handles:
        np.testing.assert_allclose([s.source_point(p) for p in w.view.tool_handles],before['points'],atol=.02)


def test_auto_sync_selective_and_undo_survives_restart(window,tmp_path):
    w=window;first=w.current_id;p=tmp_path/'second.png';Image.new('RGB',(70,60),'gray').save(p)
    second=w.catalog.add(p);w.refresh_lists();wait(lambda:not w.library_loading)
    w.set_mode(1);w.grid.selectAll();w.set_mode(0)
    w.manager.copy_keys={'exposure'};w.manager.auto_sync=True
    w.set_setting('exposure',.7);w.set_setting('temperature',20);w.commit();wait(lambda:not w.render_running)
    assert w.catalog.photo(second)['settings']['exposure']==.7
    assert w.catalog.photo(second)['settings']['temperature']==0
    assert w.catalog.preference(f'undo:{first}')['undo']
    w.manager.auto_sync=False;w.activate(second);wait(lambda:w.source is not None and not w.render_running)
    w.undo();assert w.settings['exposure']==0


def test_original_size_and_offline_preview(app,tmp_path):
    path=tmp_path/'large.jpg';Image.new('RGB',(2400,1200),'gray').save(path)
    w=MainWindow(tmp_path/'catalog');ident=w.catalog.add(path);w.refresh_lists();w.activate(ident);w.show()
    wait(lambda:w.source is not None and not w.render_running)
    assert w.view.image_rect.width()==2400 and w.source.shape[1]==1800
    w.view.actual_size();wait(lambda:w.full_source is not None and not w.render_running)
    assert w.view.on_screen.shape[1]==2400
    cache=w.catalog.directory/'previews';cache.mkdir()
    np.savez_compressed(cache/f'{ident}.npz',pixels=w.source.astype(np.float16),info=json.dumps(w.catalog.photo(ident)['info']))
    w.close();path.rename(path.with_suffix('.offline'))
    reopened=MainWindow(tmp_path/'catalog');reopened.activate(ident)
    wait(lambda:reopened.source is not None and not reopened.render_running)
    assert reopened.catalog.photo(ident)['info']['offline_preview']
    reopened.close()


def test_scaled_live_quality_compare_full_and_offline_detail(app,tmp_path):
    from test_preview_scale import photograph
    from luma.widgets import PhotoView
    from luma.engine import defaults
    path=tmp_path/'detail.png';Image.fromarray(np.uint8(photograph(400,1920)*255)).save(path)
    settings={**defaults(),**dict(texture=70,sharpen=75,sharpen_radius=2.2,noise_color=30,
                                 rotation=1,crop=[.13,.07,.87,.91])}
    w=MainWindow(tmp_path/'detail-catalog');ident=w.catalog.add(path,settings=settings)
    errors=[];w.show_error=errors.append;w.refresh_lists();w.activate(ident);w.show()
    try:
        wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
        np.testing.assert_array_equal(w.view.on_screen,develop(w.source,settings,original_size=(1920,400)))
        w.preview_timer.stop();w.refine_timer.stop();w.render_version+=1;w.live_running=True;w.start_preview('live')
        wait(lambda:not w.live_running)
        assert w.presented_quality=='live'
        np.testing.assert_array_equal(w.view.on_screen,develop(w.live_source,settings,original_size=(1920,400)))
        w.render_quality();wait(lambda:not w.render_running)
        np.testing.assert_array_equal(w.view.on_screen,develop(w.source,settings,original_size=(1920,400)))
        w.manager.compare(2);dialog=w.manager.dialogs[-1];view=dialog.findChildren(PhotoView)[0]
        wait(lambda:view.on_screen is not None)
        np.testing.assert_array_equal(view.on_screen,develop(w.source,settings,original_size=(1920,400)))
        dialog.close()
        w.view.actual_size();wait(lambda:w.full_source is not None and not w.render_running)
        np.testing.assert_array_equal(w.view.on_screen,develop(w.full_source,settings))
        folder=w.catalog.directory/'previews';folder.mkdir()
        np.savez_compressed(folder/f'{ident}.npz',pixels=w.source,info=json.dumps(w.catalog.photo(ident)['info']))
        assert not errors,errors
    finally:w.close()
    path.rename(path.with_suffix('.offline'));reopened=MainWindow(tmp_path/'detail-catalog')
    try:
        reopened.activate(ident);wait(lambda:reopened.source is not None and not reopened.render_running)
        np.testing.assert_array_equal(reopened.view.on_screen,develop(reopened.source,settings,original_size=(1920,400)))
    finally:reopened.close()
