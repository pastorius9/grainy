from copy import deepcopy
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt,QPointF
from PySide6.QtTest import QTest
from test_studio_ui import app,wait
from luma.app import MainWindow


@pytest.fixture
def window(app,tmp_path):
    image=np.full((180,240,3),170,np.uint8);image[86:95,116:125]=5;image[86:95,176:185]=5
    path=tmp_path/'healing.png';Image.fromarray(image).save(path)
    w=MainWindow(tmp_path/'library');ident=w.catalog.add(path)
    w.refresh_lists();w.activate(ident);w.show()
    wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
    w.tabs.setCurrentIndex(6);w.studio.retouch_radius.set_value(.07);w.studio.mode('heal')
    wait(lambda:not w.render_running)
    yield w
    w.view.cancel_stroke();wait(lambda:not w.render_running);w.close()


def point(w,x=.5,y=.5):
    return w.view.mapFromScene(QPointF(x*w.view.image_rect.width(),y*w.view.image_rect.height()))


def test_heal_marks_multiple_areas_then_executes_once_and_undo_redo(window,tmp_path):
    w=window;s=w.studio;baseline=deepcopy(w.settings);n=len(w.catalog.histories(w.current_id))
    version=w.render_version;context=w.preview_context()
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    assert s.pending_stroke['staged'] and w.view.healing_overlay is not None
    assert w.preview_context()==context and not w.interacting()
    assert w.view.on_screen[90,120].mean()<.05 and w.render_version==version
    assert s.preview_settings(deepcopy(baseline))==baseline
    assert w.settings==baseline and w.catalog.photo(w.current_id)['settings']==baseline
    assert len(w.catalog.histories(w.current_id))==n
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w,.55))
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w,.75))
    assert s.pending_stroke is None and len(s.healing_strokes)==2
    w.tabs.scroll.ensureWidgetVisible(s.healing_apply)
    assert w.view.tool_mode=='heal'
    color=w.view.viewport().grab().toImage().pixelColor(point(w))
    assert color.red()>color.green()+40
    assert w.settings==baseline and w.render_version==version
    assert w.catalog.photo(w.current_id)['settings']==baseline and len(w.catalog.histories(w.current_id))==n
    overlay=w.view.healing_overlay
    assert overlay.pixelColor(120,90).alpha()>0 and overlay.pixelColor(180,90).alpha()>0
    assert overlay.pixelColor(155,90).alpha()==0  # No line between separate strokes.
    QTest.mouseClick(s.healing_apply,Qt.MouseButton.LeftButton)
    wait(lambda:not w.render_running and w.presented_version==w.render_version)
    assert not s.healing_strokes and len(w.settings['retouch'])==2 and w.view.healing_overlay is None
    assert not s.healing_apply.isEnabled()
    assert w.view.on_screen[90,120].mean()>.6 and w.view.on_screen[90,180].mean()>.6
    op=w.settings['retouch'][0];assert op['type']=='heal' and op['source'] is None and op['version']==2
    assert len(op['points'])>=2 and op['points'][-1][0]==pytest.approx(.55,abs=.015)
    assert len(w.catalog.histories(w.current_id))==n+1
    saved=deepcopy(w.settings)
    w.undo();wait(lambda:not w.render_running);assert w.settings==baseline and max(w.view.on_screen[90,[120,180]].mean(-1))<.05
    w.redo();wait(lambda:not w.render_running);assert w.settings==saved and min(w.view.on_screen[90,[120,180]].mean(-1))>.6
    s.apply_healing();assert w.settings==saved and len(w.catalog.histories(w.current_id))==n+1
    from luma.library import write_sidecar,read_sidecar
    write_sidecar(w.catalog.photo(w.current_id),tmp_path/'saved.xmp')
    assert read_sidecar(tmp_path/'saved.xmp')['settings']['retouch']==saved['retouch']
    from luma.engine import export_image,load_image,to_srgb
    output=tmp_path/'healed.png';export_image(w.catalog.photo(w.current_id)['path'],output,saved,format='PNG')
    exported,_=load_image(output,1000)
    assert min(to_srgb(exported)[90,[120,180]].mean(-1))>.6


def test_manual_donor_reset_before_view_size_and_feather(window):
    w=window;s=w.studio
    w.before_button.setChecked(True);s.mode('heal');assert not w.before_button.isChecked()
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.AltModifier,point(w,.2,.5))
    assert s.donor[0]==pytest.approx(.2,abs=.015) and not w.settings['retouch']
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    frozen=deepcopy(s.healing_strokes[0]);assert frozen['source']==s.donor
    s.retouch_feather.slider.setValue(25);assert w.view.brush_feather==25
    size=s.retouch_radius.slider.value();QTest.keyClick(w.view,Qt.Key.Key_BracketRight)
    assert s.retouch_radius.slider.value()>size
    assert w.view.brush_radius==pytest.approx(s.retouch_radius.slider.value()/1000)
    s.automatic_healing_source();assert s.donor is None
    assert s.healing_strokes[0]==frozen
    QTest.keyClick(w.view,Qt.Key.Key_Return);wait(lambda:not w.render_running)
    assert w.settings['retouch'][0]==frozen and w.view.on_screen[90,120].mean()>.6


def test_escape_and_photo_switch_discard_unsaved_healing(window,tmp_path):
    w=window;s=w.studio
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    wait(lambda:w.presented_version==w.render_version)
    QTest.keyClick(w.view,Qt.Key.Key_Escape)
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    wait(lambda:not w.render_running and w.presented_version==w.render_version)
    assert not w.settings['retouch'] and w.view.on_screen[90,120].mean()<.05
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    assert len(s.healing_strokes)==1
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    path=tmp_path/'next.png';Image.new('RGB',(200,150),'white').save(path);ident=w.catalog.add(path)
    w.activate(ident);wait(lambda:w.source is not None and not w.render_running)
    assert s.pending_stroke is None and not w.settings['retouch'] and s.donor is None
    assert not s.healing_strokes and w.view.healing_overlay is None and not s.healing_apply.isEnabled()
    s.apply_healing();assert not w.settings['retouch']


def test_clone_still_uses_explicit_donor_and_retouch_follows_crop_rotation(window):
    w=window;s=w.studio;s.mode('clone')
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    assert not w.settings['retouch']
    w.set_setting('rotation',1);w.set_setting('crop',[.1,.1,.9,.9]);w.finish_interaction()
    wait(lambda:not w.render_running);s.mode('heal')
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    assert w.view.healing_overlay is not None and not w.settings['retouch']
    s.apply_healing()
    wait(lambda:not w.render_running)
    op=w.settings['retouch'][0]
    assert op['points'][0]==pytest.approx([.5,.5],abs=.015)
    h,ww=w.view.on_screen.shape[:2];assert w.view.on_screen[h//2,ww//2].mean()>.6


def test_remove_last_clear_escape_and_panel_switch(window):
    w=window;s=w.studio;baseline=deepcopy(w.settings)
    for x in (.5,.75):QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w,x))
    s.mode('clone');assert w.view.healing_overlay is None and len(s.healing_strokes)==2
    s.mode('heal');assert w.view.healing_overlay is not None
    QTest.mouseClick(s.healing_remove,Qt.MouseButton.LeftButton)
    assert len(s.healing_strokes)==1 and w.view.healing_overlay.pixelColor(180,90).alpha()==0
    QTest.mousePress(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w,.75))
    QTest.keyClick(w.view,Qt.Key.Key_Escape)
    QTest.mouseRelease(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w,.75))
    assert len(s.healing_strokes)==1 and w.settings==baseline
    QTest.keyClick(w.view,Qt.Key.Key_Escape)
    assert not s.healing_strokes and w.view.healing_overlay is None and w.view.tool_mode=='heal'
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    QTest.mouseClick(s.healing_clear,Qt.MouseButton.LeftButton)
    assert not s.healing_strokes and not s.healing_clear.isEnabled() and w.settings==baseline


def test_pending_marks_reproject_with_geometry_and_hide_in_before_view(window):
    w=window;s=w.studio
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w,.75))
    frozen=deepcopy(s.healing_strokes)
    w.before_button.setChecked(True);wait(lambda:not w.render_running)
    assert w.view.healing_overlay is None
    w.before_button.setChecked(False);wait(lambda:not w.render_running)
    assert w.view.healing_overlay is not None
    w.set_setting('rotation',1);w.set_setting('crop',[.1,.1,.9,.9]);w.finish_interaction()
    wait(lambda:not w.render_running and w.presented_version==w.render_version)
    assert s.healing_strokes==frozen
    overlay=w.view.healing_overlay
    assert overlay.pixelColor(overlay.width()//2,round(overlay.height()*.8125)).alpha()>0
    s.apply_healing();wait(lambda:not w.render_running)
    assert w.settings['retouch']==frozen


def test_explicit_clone_donor_keeps_immediate_workflow(window):
    w=window;s=w.studio;s.mode('clone')
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.AltModifier,point(w,.2))
    QTest.mouseClick(w.view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(w))
    wait(lambda:not w.render_running)
    assert len(w.settings['retouch'])==1 and w.settings['retouch'][0]['type']=='clone'
    assert not s.healing_strokes and w.view.on_screen[90,120].mean()>.6
