import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from test_studio_ui import app, wait
from luma.engine import defaults, develop, auto_slider_value
from luma.targeted import curve_input, drag_curve, hsl_weights, drag_hsl, sample


def test_curve_target_finds_the_pressed_tone_and_keeps_the_curve_rising():
    points = [[0., 0.], [.5, .7], [1., 1.]]
    rgb = np.array([.7, .7, .7], np.float32)                   # displayed tone of input .5
    assert curve_input(points, rgb) == pytest.approx(.5, abs=1e-4)
    raised = drag_curve(points, .5, .1)
    assert raised[1] == [.5, pytest.approx(.8)] and len(raised) == 3      # an existing point is reused
    added = drag_curve(points, .25, -.2)
    assert [.25, pytest.approx(.15)] in added and len(added) == 4
    extreme = drag_curve(points, .25, 5.)
    assert [.25, 1.] in extreme and [p[0] for p in extreme] == sorted(p[0] for p in extreme)   # 0-1, inputs in order
    assert curve_input([[0., 0.], [.5, .9], [.6, .2], [1., 1.]], rgb) == pytest.approx(.7, abs=1e-3)   # not rising


def test_hsl_target_changes_the_colours_of_the_pressed_hue():
    weights = hsl_weights(np.array([.9, .1, .1], np.float32))         # red
    assert weights[0] == 1 and weights[1] > 0 and weights[3:].max() == 0
    groups = drag_hsl(defaults()['hsl'], weights, 1, 40)
    assert groups[0][1] == 40 and 0 < groups[1][1] < 40 and groups[3] == [0., 0., 0.]
    assert drag_hsl(groups, weights, 1, 400)[0][1] == 100              # slider range
    image = np.zeros((10, 10, 3), np.float32);image[4:7, 4:7] = (.2, .4, .6)
    assert np.allclose(sample(image, .5, .5), (.2, .4, .6))


def test_auto_whites_and_blacks_bring_the_ends_just_inside_clipping():
    rng = np.random.default_rng(3)
    source = (.12 + .5 * rng.random((200, 300, 3))).astype(np.float32)   # a dull, low-contrast photo
    s = defaults()
    whites = auto_slider_value(source, s, 'whites');blacks = auto_slider_value(source, s, 'blacks')
    assert whites > 0 and blacks < 0
    out = develop(source, {**s, 'whites': whites})
    assert np.percentile(out.max(axis=-1), 99.9) == pytest.approx(.99, abs=.02)
    out = develop(source, {**s, 'blacks': blacks})
    assert np.percentile(out.min(axis=-1), .1) == pytest.approx(.01, abs=.02)
    assert auto_slider_value(np.full((50, 50, 3), .5, np.float32), s, 'whites') is None   # no tonal range
    assert auto_slider_value(source, s, 'contrast') is None


def test_targeted_curve_and_hsl_drags_and_shift_double_click_auto(app, tmp_path):
    from luma.app import MainWindow
    path = tmp_path/'p.png'
    y, x = np.mgrid[:60, :90]
    rgb = np.stack([.2 + .6 * x / 90, .3 + .2 * y / 60, .5 + 0 * x], -1)
    Image.fromarray(np.uint8(rgb * 255)).save(path)
    w = MainWindow(tmp_path/'data');pid = w.catalog.add(path)
    try:
        w.refresh_lists();w.activate(pid);w.show()
        wait(lambda: w.source is not None and not w.render_running and w.view.on_screen is not None)
        undo = len(w.undo_stack)
        w.targeted.buttons['curve'].click()
        assert w.view.tool_mode == 'adjust'
        w.view.adjustStarted.emit(.5, .5);w.view.adjustDragged.emit(60.);w.view.adjustFinished.emit()
        curve = w.catalog.photo(pid)['settings']['curve']
        assert len(curve) >= 5 and any(p[1] > p[0] + .1 for p in curve)            # raised where pressed
        assert len(w.undo_stack) == undo + 1 and w.curve.points == curve
        w.targeted.buttons['hsl'].click()
        assert w.view.tool_mode == 'adjust' and not w.targeted.buttons['curve'].isChecked()
        w.hsl_target.setCurrentIndex(1)
        w.view.adjustStarted.emit(.95, .5);w.view.adjustDragged.emit(-40.);w.view.adjustFinished.emit()
        hsl = w.catalog.photo(pid)['settings']['hsl']
        assert min(g[1] for g in hsl) < 0 and w.hsl_color.currentIndex() == int(np.argmin([g[1] for g in hsl]))
        w.view.set_tool('')
        assert not w.targeted.buttons['hsl'].isChecked()
        slider = w.adjustments['whites'].slider
        QTest.mouseDClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        assert w.catalog.photo(pid)['settings']['whites'] > 0
        QTest.mouseDClick(slider, Qt.MouseButton.LeftButton)                    # plain double-click: reset
        assert w.settings['whites'] == 0
    finally:
        wait(lambda: not w.render_running);w.close()
