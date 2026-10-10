from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QTabWidget
from PySide6.QtTest import QTest
from test_studio_ui import app,window,wait
from luma.develop_panels import Panel


def owner(widget):
    while widget is not None and not isinstance(widget,Panel):widget=widget.parentWidget()
    return widget.key if widget else None


def test_panel_order_solo_mode_and_navigation_do_not_edit_photo(window):
    w=window;p=w.tabs;before=deepcopy(w.settings);history=len(w.catalog.histories(w.current_id))
    assert not isinstance(p,QTabWidget)
    assert [key for key in p.sections if key not in ('crop','healing','masks')]==[
        'basic','curve','mixer','grading','detail','lens','transform','effects','photo_filter','calibration','metadata']
    assert p.is_open('basic')
    QTest.mouseClick(p.sections['curve'].header,Qt.MouseButton.LeftButton)
    assert p.is_open('curve') and not p.is_open('basic')
    p.set_solo(False);p.open_panel('effects')
    assert p.is_open('curve') and p.is_open('effects')
    p.set_solo(True)
    assert sum(v.header.isChecked() for k,v in p.sections.items() if k not in ('crop','healing','masks'))==1
    for key in p.sections:
        if key not in ('crop','healing','masks'):p.open_panel(key)
    assert w.settings==before and len(w.catalog.histories(w.current_id))==history
    assert w.catalog.preference('develop_panels')['expanded']==['metadata']


def test_related_controls_grouped_once_and_advanced_controls_reachable(window):
    w=window;s=w.studio;p=w.tabs
    expected={'exposure':'basic','temperature':'basic','texture':'basic','clarity':'basic','dehaze':'basic',
              'sharpen':'detail','noise_luma':'detail','distortion':'lens','perspective_v':'transform',
              'vignette':'effects','grain':'effects','photo_filter_density':'photo_filter','grading_balance':'grading',
              'vignette_midpoint':'effects','vignette_highlights':'effects','grain_size':'effects','grain_roughness':'effects'}
    assert all(owner(w.adjustments[key])==panel for key,panel in expected.items())
    assert owner(w.curve)=='curve' and owner(s.channel_curve)=='curve'
    assert owner(s.points)=='mixer' and owner(s.working)=='calibration'
    assert owner(w.keywords)=='metadata' and owner(w.auto_level_button)=='crop'
    assert len(w.adjustments)==51 and all(owner(c) for c in w.adjustments.values())
    assert not s.raw_mode.isVisible() and not w.adjustments['raw_kelvin'].isVisible()
    p.currentWidget().ensureWidgetVisible(w.adjustments['distortion']);QApplication.processEvents()
    assert p.is_open('lens') and w.adjustments['distortion'].isVisible()
    p.currentWidget().ensureWidgetVisible(s.channel_curve);QApplication.processEvents()
    assert p.is_open('curve') and s.channel_curve.isVisible()


def test_tool_strip_crop_healing_masks_and_exit_restore_pointer(window):
    w=window;p=w.tabs;v=w.view
    p.tool_buttons[1].click();assert v.crop_mode and p.is_open('crop')
    p.tool_buttons[6].click();assert not v.crop_mode and v.tool_mode=='heal' and p.is_open('healing')
    p.tool_buttons[5].click();assert not v.tool_mode and p.is_open('masks')
    w.studio.new_layer();w.studio.mode('brush')
    p.sections['basic'].header.click()
    assert not v.tool_mode and p.is_open('basic') and not p.sections['masks'].isVisible()
    p.tool_buttons[1].click();p.sections['crop'].header.click()
    assert not v.crop_mode and p.currentIndex()==0
    w.wb_button.click();assert v.sample_mode
    p.tool_buttons[5].click();assert not v.sample_mode and not w.wb_button.isChecked()
    p.currentWidget().ensureWidgetVisible(w.studio.local_curve);QApplication.processEvents()
    assert w.studio.local_curve.isVisible()


def test_compact_slider_number_reset_and_undo_keep_semantics(window):
    w=window;c=w.adjustments['exposure'];p=w.tabs
    assert c.property('compact') and c.layout().itemAt(0).layout().itemAt(1).widget() is c.slider
    c.value.setValue(.7);w.finish_interaction();wait(lambda:not w.render_running)
    assert w.settings['exposure']==.7
    QTest.mouseDClick(c.slider,Qt.MouseButton.LeftButton)
    wait(lambda:not w.render_running);assert w.settings['exposure']==0
    w.undo();wait(lambda:not w.render_running);assert w.settings['exposure']==.7
    w.resize(1120,740);QApplication.processEvents()
    assert c.value.geometry().right()<=c.width() and p.width()>=250


def test_panel_preference_and_language_navigation_state_restore(window):
    w=window;p=w.tabs;p.set_solo(False);p.open_panel('effects');p.open_panel('photo_filter')
    state=p.navigation_state();p.setCurrentIndex(5);p.restore_navigation(state)
    assert p.is_open('effects') and p.is_open('photo_filter') and not p.solo
    from luma.app import MainWindow
    restored=MainWindow(w.catalog.directory)
    try:
        assert not restored.tabs.solo
        assert restored.tabs.sections['effects'].header.isChecked()
        assert restored.tabs.sections['photo_filter'].header.isChecked()
    finally:restored.close()
