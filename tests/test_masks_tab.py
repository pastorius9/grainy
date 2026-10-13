"""The mask tab's order: Create New Mask -> kind, a mask list with Add / Subtract,
only the active tool's options, and the selected mask's adjustments."""
import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtTest import QTest
from test_studio_ui import app, wait, window


def at(w, x, y):
    return w.view.mapFromScene(QPointF(w.view.image_rect.width()*x, w.view.image_rect.height()*y))


def drag(w, start, end):
    QTest.mousePress(w.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(w, *start))
    QTest.mouseMove(w.view.viewport(), at(w, *end))
    QTest.mouseRelease(w.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(w, *end))
    wait(lambda: not w.render_running)


def kinds(button):
    return [a.text() for a in button.menu().actions() if not a.isSeparator()]


def test_create_add_subtract(window):
    w = window; s = w.studio; w.tabs.setCurrentIndex(5)
    assert kinds(s.create_button) == ['브러시', '선형 그레이디언트', '방사형 그레이디언트', '색상 범위', '광도 범위']
    assert not s.add_button.isEnabled() and s.adjust_host.isHidden() and s.mask_tree.isHidden() and s.tool_options.isHidden() and s.tool_hint.text()
    s.create_mask('brush')                                              # Create New Mask > Brush
    assert w.view.tool_mode == 'brush' and len(w.settings['masks']) == 1 and not s.tool_options.isHidden()
    assert not s.radius.isHidden() and not s.flow.isHidden() and not s.auto_mask.isHidden() and s.tolerance.isHidden()
    assert '마스크 1' in s.tool_hint.text() and '브러시' in s.tool_hint.text() and not s.done_button.isHidden()
    drag(w, (.3, .4), (.5, .5))
    assert [c['type'] for c in w.settings['masks'][0]['components']] == ['brush'] and w.view.tool_mode == 'brush'
    s.extend_mask('linear', 'subtract')                                 # Subtract > Linear Gradient
    assert w.view.tool_mode == 'linear' and s.tool_options.isHidden()   # a linear gradient has no options
    drag(w, (.2, .2), (.2, .7))
    components = w.settings['masks'][0]['components']
    assert [(c['type'], c['operation']) for c in components] == [('brush', 'add'), ('linear', 'subtract')]
    s.extend_mask('radial', 'add'); assert not s.feather.isHidden() and s.radius.isHidden()
    s.mode(''); assert s.tool_options.isHidden() and s.done_button.isHidden()
    mask = s.mask_tree.topLevelItem(0)
    assert mask.text(0) == '마스크 1' and [mask.child(i).text(0) for i in range(mask.childCount())] == ['+  브러시 1', '-  선형 그레이디언트 1']
    assert s.add_button.isEnabled() and not s.adjust_host.isHidden() and not s.mask_tree.isHidden()
    assert s.local_controls['exposure'].label.text() == w.adjustments['exposure'].label.text()     # named as in the main panel


def test_unused_new_mask_disappears_and_tree_drives_selection(window):
    w = window; s = w.studio; w.tabs.setCurrentIndex(5)
    s.create_mask('radial'); assert len(w.settings['masks']) == 1
    s.mode('')                                                          # nothing drawn: no empty mask is left behind
    assert not w.settings['masks'] and s.mask_tree.topLevelItemCount() == 0
    s.create_mask('linear'); drag(w, (.3, .3), (.6, .6)); s.mode('')
    s.create_mask('brush'); drag(w, (.6, .6), (.7, .7)); s.mode('')
    assert [m['name'] for m in w.settings['masks']] == ['마스크 1', '마스크 2'] and s.layer_index == 1
    first = s.mask_tree.topLevelItem(0)
    s.mask_tree.setCurrentItem(first.child(0))                          # clicking a gradient: its handles appear
    assert (s.layer_index, s.component_index) == (0, 0) and w.view.tool_mode == 'edit_component' and len(w.view.tool_handles) == 2
    s.mode('')
    first.setCheckState(0, Qt.CheckState.Unchecked)                    # the eye: hide the mask
    wait(lambda: w.settings['masks'][0].get('enabled', True) is False and not w.render_running)
    assert w.settings['masks'][0]['enabled'] is False and w.settings['masks'][1].get('enabled', True)
    assert s.mask_tree.topLevelItem(0).checkState(0) == Qt.CheckState.Unchecked


def test_more_menu_overlay_key_and_intersect(window):
    w = window; s = w.studio; w.tabs.setCurrentIndex(5)
    assert [a.text() for a in s.mask_menu().actions()] == ['먼저 마스크를 만드세요.']
    s.create_mask('brush'); drag(w, (.4, .4), (.5, .5)); s.mode('')
    s.mask_tree.setCurrentItem(s.mask_tree.topLevelItem(0))
    menu = s.mask_menu(); titles = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert titles == ['이름 바꾸기…', '마스크 복제', '마스크 숨기기', '마스크 반전', '다음과 마스크 교차', '위로 이동', '아래로 이동', '마스크 삭제']
    next(a for a in menu.actions() if a.text() == '마스크 반전').trigger()
    assert w.settings['masks'][0]['invert'] is True and s.invert.isChecked()
    intersect = next(a for a in menu.actions() if a.text() == '다음과 마스크 교차').menu()
    next(a for a in intersect.actions() if a.text() == '방사형 그레이디언트').trigger()
    assert w.view.tool_mode == 'radial' and s.operation.currentData() == 'intersect'
    drag(w, (.3, .3), (.6, .6)); s.mode('')
    assert w.settings['masks'][0]['components'][-1]['operation'] == 'intersect'
    assert s.mask_tree.topLevelItem(0).child(1).text(0).startswith('∩')
    shown = s.overlay.isChecked(); s.toggle_overlay(); assert s.overlay.isChecked() != shown     # O
    w.tabs.setCurrentIndex(0); s.toggle_overlay(); assert s.overlay.isChecked() != shown         # only while masking
    w.tabs.setCurrentIndex(5)
    s.mask_tree.setCurrentItem(s.mask_tree.topLevelItem(0).child(0))
    assert '구성 요소 삭제' in [a.text() for a in s.mask_menu().actions()]
    next(a for a in s.mask_menu().actions() if a.text() == '마스크 삭제').trigger()
    assert not w.settings['masks'] and not s.add_button.isEnabled()


def test_brush_can_be_limited_to_a_tonal_range(window):
    import numpy as np
    from luma.processing import _layer_component
    w = window; s = w.studio; w.tabs.setCurrentIndex(5)
    shape = lambda: [(c['type'], c['operation']) for c in w.settings['masks'][s.layer_index]['components']]
    choose = lambda key: s.brush_range.setCurrentIndex(s.brush_range.findData(key))
    s.create_mask('brush')
    assert not s.brush_range.isHidden() and s.brush_range.currentData() == 'all'
    drag(w, (.3, .4), (.5, .5)); assert shape() == [('brush', 'add')]                      # all tones: nothing added
    history = len(w.catalog.histories(w.current_id))
    choose('shadows')                                                                    # limits the painted mask at once
    last = w.settings['masks'][0]['components'][-1]
    assert shape() == [('brush', 'add'), ('luma', 'intersect')] and last['brush_range'] == 'shadows' and last['range'] == [0., .2]
    assert len(w.catalog.histories(w.current_id)) == history+1
    mask = s.mask_tree.topLevelItem(0)
    assert [mask.child(i).text(0) for i in range(mask.childCount())] == ['+  브러시 1', '∩  밝기 제한: 어두운 곳']
    drag(w, (.6, .3), (.7, .6))                                                           # later strokes stay inside the range
    assert shape() == [('brush', 'add'), ('brush', 'add'), ('luma', 'intersect')] and s.component_index == 1
    choose('highlights'); assert shape()[-1] == ('luma', 'intersect') and w.settings['masks'][0]['components'][-1]['range'] == [.8, 1.]
    choose('all'); assert shape() == [('brush', 'add'), ('brush', 'add')]
    s.create_mask('brush'); assert s.brush_range.currentData() == 'all'                   # the range belongs to the mask
    choose('midtones'); assert w.settings['masks'][1]['components'] == []                 # chosen first: starts with the stroke
    drag(w, (.3, .6), (.5, .7))
    assert shape() == [('brush', 'add'), ('luma', 'intersect')] and w.settings['masks'][1]['components'][-1]['brush_range'] == 'midtones'
    s.select_layer(0); assert s.brush_range.currentData() == 'all'
    s.select_layer(1); assert s.brush_range.currentData() == 'midtones'
    s.mode('')
    ramp = np.repeat(np.linspace(0, 1, 101, dtype=np.float32)[None, :, None], 3, axis=2).repeat(2, axis=0)
    parts = {key: _layer_component(ramp, ramp.shape, s.range_component(key), None, None)[0] for key in ('shadows', 'midtones', 'highlights')}
    np.testing.assert_allclose(sum(parts.values()), 1., atol=1e-5)                       # every tone belongs somewhere, once
    assert parts['shadows'][0] == 1 and parts['shadows'][50] == 0 and parts['highlights'][-1] == 1 and parts['midtones'][50] == 1
