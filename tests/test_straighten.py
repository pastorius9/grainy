from copy import deepcopy
import cv2
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QRectF
from test_studio_ui import app, window, wait
from test_optics import grid_fixture
from luma import straighten
from luma.engine import defaults, geometry, develop, to_srgb
from luma.upright import estimate

W, H = 600, 400


def idle(w):
    wait(lambda: not w.jobs and not w.render_running and not w.preview_timer.isActive() and not w.refine_timer.isActive())


def covered(angle):
    """Where the turned photo covers its canvas (engine.geometry on a white frame)."""
    return geometry(np.ones((H, W, 3), np.float32), {**defaults(), 'straighten': angle}, False)[..., 0]


def pixels(crop):
    x0, y0, x1, y1 = crop
    return slice(round(y0*H), max(round(y0*H)+1, round(y1*H))), slice(round(x0*W), max(round(x0*W)+1, round(x1*W)))


@pytest.mark.parametrize('angle', [-44, -12.5, -3, 2, 9.7, 30])
def test_valid_crops_lie_on_the_turned_photo_and_the_largest_one_touches_its_edge(angle):
    white = covered(angle)
    best = straighten.largest(angle, W, H)
    assert straighten.valid(best, angle, W, H) and white[pixels(best)].min() > .97
    assert straighten.ratio(best, W, H) == pytest.approx(W/H, rel=1e-6)
    grown = straighten._scaled(best, 1.06)
    assert not straighten.valid(grown, angle, W, H) and white[pixels(grown)].min() < .5
    # The direction of the turn matters for crops away from the centre.
    rng = np.random.default_rng(int(abs(angle)*10)); accepted = refused_clean = total = 0
    for _ in range(400):
        x0, y0 = rng.uniform(0, .8, 2); crop = [x0, y0, x0+rng.uniform(.05, .2), y0+rng.uniform(.05, .2)]
        if crop[2] > 1 or crop[3] > 1:continue
        inside = white[pixels(crop)].min(); total += 1
        if straighten.valid(crop, angle, W, H):
            accepted += 1; assert inside > .97              # an accepted crop never shows an empty corner
        elif inside > .999:refused_clean += 1               # refused only for the 2 px margin
    assert total > 150 and accepted > 20 and refused_clean < total*.06
    for aspect in (1, 16/9, 2/3):
        shaped = straighten.largest(angle, W, H, aspect)
        assert straighten.valid(shaped, angle, W, H) and straighten.ratio(shaped, W, H) == pytest.approx(aspect, rel=1e-6)


def test_no_angle_means_the_whole_canvas():
    assert straighten.largest(0, W, H) == straighten.FULL and straighten.valid(straighten.FULL, 0, W, H)
    assert straighten.refit(None, 5, 0, W, H) is None and straighten.refit(None, 0, 0, W, H) is None


def test_the_crop_follows_the_angle():
    first = straighten.refit(None, 0, 5, W, H)
    assert first == pytest.approx(straighten.largest(5, W, H))
    more = straighten.refit(first, 5, 12, W, H)                     # automatic crop: follows at full size
    assert more == pytest.approx(straighten.largest(12, W, H)) and more[2]-more[0] < first[2]-first[0]
    assert straighten.refit(more, 12, 3, W, H) == pytest.approx(straighten.largest(3, W, H))    # and grows back
    assert straighten.refit(more, 12, 0, W, H) is None
    square = straighten.refit(None, 0, 8, W, H, aspect=1)           # the chosen aspect
    assert straighten.ratio(square, W, H) == pytest.approx(1) and straighten.valid(square, 8, W, H)
    placed = [.55, .2, .95, .6]                                     # a crop the user placed
    kept = straighten.refit(placed, 0, 2, W, H)
    assert straighten.valid(kept, 2, W, H) and straighten.ratio(kept, W, H) == pytest.approx(straighten.ratio(placed, W, H))
    centre = lambda c: ((c[0]+c[2])/2, (c[1]+c[3])/2)
    assert centre(kept) == pytest.approx(centre(placed)) and kept[2]-kept[0] <= placed[2]-placed[0]
    small = [.4, .4, .6, .6]
    assert straighten.refit(small, 0, 10, W, H) == pytest.approx(small)      # already inside: untouched
    assert straighten.refit(small, 10, 20, W, H) == pytest.approx(small)     # and it never grows on its own


def test_dragging_stops_at_the_edge_of_the_photo():
    angle = 10; start = straighten.largest(angle, W, H)
    moved = [start[0]+.2, start[1], start[2]+.2, start[3]]
    stopped = straighten.limit(moved, start, angle, W, H)
    assert straighten.valid(stopped, angle, W, H) and start[0] <= stopped[0] < moved[0]
    inside = straighten._scaled(start, .5)
    assert straighten.limit(inside, start, angle, W, H) == inside
    assert straighten.limit(moved, [0, 0, 1, 1], angle, W, H) == moved        # an old crop off the photo does not trap the frame
    assert straighten.valid(straighten.shrink([0, 0, 1, 1], angle, W, H), angle, W, H)
    assert straighten.valid(straighten.shrink([.0, .0, .1, .1], 30, W, H), 30, W, H)   # centre off the photo


def test_auto_angle_levels_a_tilted_photo():
    image = grid_fixture()
    for tilt in (-7, 4):
        tilted = cv2.warpAffine(image, cv2.getRotationMatrix2D((399.5, 299.5), tilt, 1), (800, 600), borderValue=(.65, .65, .65))
        angle = -estimate(tilted, 'level')['rotation']
        level = geometry(tilted, {**defaults(), 'straighten': angle}, False)
        assert abs(estimate(level, 'level')['rotation']) < .2 and abs(abs(angle)-abs(tilt)) < .2


def tilted_photo(w, tmp_path, tilt=-7):
    image = cv2.warpAffine(grid_fixture(), cv2.getRotationMatrix2D((399.5, 299.5), tilt, 1), (800, 600), borderValue=(.65, .65, .65))
    path = tmp_path/'tilted.png'; Image.fromarray(np.uint8(to_srgb(image)*255+.5)).save(path)
    ident = w.catalog.add(path); w.activate(ident); wait(lambda: w.source is not None and w.current_id == ident); idle(w)
    return ident


def test_auto_button_sets_the_slider_opens_the_crop_tool_and_fits_the_frame(window, tmp_path):
    w = window; ident = tilted_photo(w, tmp_path)
    w.tabs.setCurrentIndex(1); w.auto_level_button.click()
    assert not w.auto_level_button.isEnabled()
    wait(lambda: w.settings['straighten'] != 0); idle(w)
    angle = w.settings['straighten']
    assert abs(angle+7) < .2 and w.settings['upright'] is None and w.auto_level_button.isEnabled()
    assert w.adjustments['straighten'].slider.value() == round(angle*10) and f'{angle:+.1f}' in w.auto_level_note.text()
    assert w.view.crop_mode and w.crop_button.isChecked()
    expected = straighten.largest(angle, 800, 600)
    assert w.view.crop_values() == pytest.approx(expected) and w.settings['crop'] == pytest.approx(expected)
    # The whole turned photo stays visible while editing: the shown frame is larger than the photo's canvas.
    pad_x, pad_y = straighten.padding(angle, 800, 600)
    assert pad_x > 0 and pad_y > 0 and w.view.on_screen.shape[:2] == (600+2*pad_y, 800+2*pad_x)
    canvas = w.view.canvas_rect()
    assert (canvas.left(), canvas.top(), canvas.width(), canvas.height()) == (pad_x, pad_y, 800, 600)
    corners = geometry(np.ones((600, 800, 3), np.float32), {**defaults(), 'straighten': angle}, 'all')[..., 0]
    assert corners.sum() == pytest.approx(800*600, rel=.01)         # nothing of the photo is cut off
    w.view.accept_crop(); idle(w)
    assert not w.view.crop_mode and w.settings['crop'] == pytest.approx(expected) and w.view.canvas is None
    shown = w.view.on_screen
    assert shown.shape[1]/shown.shape[0] == pytest.approx(800/600, rel=.01)
    assert shown[[0, 0, -1, -1], [0, -1, 0, -1]].min() > .5          # no empty corners: the grey background reaches them
    assert w.catalog.photo(ident)['settings']['straighten'] == angle
    w.undo(); idle(w); w.undo(); idle(w)
    assert w.settings['straighten'] == 0 and w.settings['crop'] is None


def test_slider_opens_the_tool_keeps_the_frame_filled_and_cancel_restores(window, tmp_path):
    w = window; tilted_photo(w, tmp_path)
    slider = w.adjustments['straighten']
    assert slider.slider.minimum() == -450 and slider.slider.maximum() == 450
    before = deepcopy(w.settings)
    slider.changed.emit('straighten', 5.); idle(w)
    assert w.view.crop_mode and w.settings['straighten'] == 5
    assert w.view.crop_values() == pytest.approx(straighten.largest(5, 800, 600))
    slider.changed.emit('straighten', 12.); idle(w)
    twelve = straighten.largest(12, 800, 600)
    assert w.view.crop_values() == pytest.approx(twelve) and w.settings['crop'] == pytest.approx(twelve)
    # Dragging the frame cannot leave the photo.
    view = w.view; view.drag_handle = 'move'; view.drag_rect = QRectF(view.crop_rect); view.drag_start = view.crop_rect.center()
    view.crop_rect = view.limited_crop(QRectF(view.crop_rect).translated(.3, 0), view.crop_values()); view.drag_handle = None
    assert straighten.valid(view.crop_values(), 12, 800, 600) and view.crop_values()[0] > twelve[0]
    w.aspect.setCurrentIndex(1); idle(w)                             # 1 : 1
    assert straighten.valid(view.crop_values(), 12, 800, 600)
    assert straighten.ratio(view.crop_values(), 800, 600) == pytest.approx(1, rel=1e-3)
    w.cancel_crop(); idle(w)
    assert not w.view.crop_mode and w.settings == before and slider.slider.value() == 0
    w.aspect.setCurrentIndex(0)
    slider.changed.emit('straighten', -4.); slider.changed.emit('straighten', 0.); idle(w)   # back to level: whole photo
    assert w.settings['crop'] is None and w.view.crop_values() == [0, 0, 1, 1]
    w.cancel_crop(); idle(w)


def test_a_placed_crop_keeps_its_place_and_perspective_alignment_is_left_alone(window, tmp_path):
    w = window; tilted_photo(w, tmp_path)
    w.set_setting('crop', [.5, .2, .9, .6]); w.finish_interaction(); idle(w)
    w.adjustments['straighten'].changed.emit('straighten', 3.); idle(w)
    crop = w.view.crop_values()
    assert straighten.valid(crop, 3, 800, 600) and ((crop[0]+crop[2])/2, (crop[1]+crop[3])/2) == pytest.approx((.7, .4))
    w.view.accept_crop(); idle(w)
    upright = estimate(grid_fixture(), 'level'); upright['mode'] = 'vertical'
    w.set_setting('upright', upright); w.set_setting('crop', None); w.finish_interaction(); idle(w)
    w.adjustments['straighten'].changed.emit('straighten', 6.); idle(w)
    assert w.settings['crop'] is None and w.view.crop_limit[0] == 0      # its own framing: no automatic crop
    w.cancel_crop(); idle(w)


def test_dragging_outside_the_frame_turns_the_photo(window, tmp_path):
    import math
    from PySide6.QtCore import Qt, QPoint, QPointF
    from PySide6.QtTest import QTest
    w = window; ident = tilted_photo(w, tmp_path)
    w.tabs.setCurrentIndex(1); w.start_crop(); idle(w)
    view = w.view; view.show_crop([.3, .3, .7, .7]); idle(w)
    frame = view.crop_screen_rect(); centre = frame.center(); radius = frame.width()/2+40
    at = lambda degrees: QPoint(round(centre.x()+radius*math.cos(math.radians(degrees))), round(centre.y()+radius*math.sin(math.radians(degrees))))
    assert view.hit_handle(QPointF(at(0))) == 'rotate' and view.hit_handle(centre) == 'move' and view.hit_handle(frame.topLeft()) == 'nw'
    port = view.viewport()
    QTest.mousePress(port, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0))
    assert view.drag_handle == 'rotate'
    QTest.mouseMove(port, at(4)); QTest.mouseMove(port, at(10))            # clockwise on screen
    assert w.settings['straighten'] == pytest.approx(10, abs=.4) and w.settings['straighten'] > 0
    assert w.adjustments['straighten'].slider.value() == round(w.settings['straighten']*10)
    QTest.mouseRelease(port, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(10)); idle(w)
    angle = w.settings['straighten']
    assert view.drag_handle is None and view.crop_mode and w.catalog.photo(ident)['settings']['straighten'] == angle
    crop = view.crop_values()                                             # the placed frame kept its centre and stays on the photo
    assert straighten.valid(crop, angle, 800, 600) and ((crop[0]+crop[2])/2, (crop[1]+crop[3])/2) == pytest.approx((.5, .5))
    frame = view.crop_screen_rect(); centre = frame.center(); radius = frame.width()/2+40
    QTest.mousePress(port, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(180))     # from the other side, the other way
    QTest.mouseMove(port, at(170)); QTest.mouseMove(port, at(100))
    assert w.settings['straighten'] == -45                               # limited to the slider's range
    QTest.mouseRelease(port, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(100)); idle(w)
    QTest.mousePress(port, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0))       # a click alone changes nothing
    QTest.mouseRelease(port, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(0)); idle(w)
    assert w.settings['straighten'] == -45
    w.cancel_crop(); idle(w)
    assert w.settings['straighten'] == 0 and w.settings['crop'] is None


def test_enter_applies_and_escape_cancels_from_the_slider_and_the_number_box(window, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    w = window; ident = tilted_photo(w, tmp_path); w.tabs.setCurrentIndex(1)
    control = w.adjustments['straighten']
    control.slider.setFocus(); control.changed.emit('straighten', 4.); idle(w)
    assert w.view.crop_mode and w.focusWidget() is control.slider
    QTest.keyClick(control.slider, Qt.Key.Key_Return); idle(w)
    assert not w.view.crop_mode and w.settings['straighten'] == 4
    assert w.catalog.photo(ident)['settings']['crop'] == pytest.approx(straighten.largest(4, 800, 600))
    control.value.setFocus(); control.value.selectAll()
    QTest.keyClicks(control.value, '-6'); QTest.keyClick(control.value, Qt.Key.Key_Enter); idle(w)     # typed value, then applied
    assert not w.view.crop_mode and w.settings['straighten'] == -6
    assert w.settings['crop'] == pytest.approx(straighten.largest(-6, 800, 600))
    control.slider.setFocus(); control.changed.emit('straighten', 9.); idle(w)
    assert w.view.crop_mode
    QTest.keyClick(control.slider, Qt.Key.Key_Escape); idle(w)
    assert not w.view.crop_mode and w.settings['straighten'] == -6
    w.crop_button.setFocus(); w.start_crop(); idle(w)
    QTest.keyClick(w.crop_button, Qt.Key.Key_Return); idle(w)
    assert not w.view.crop_mode


def lined(segments, size=(900, 1350)):
    """A grey frame with dark straight edges: (x0, y0, x1, y1) in fractions of the frame."""
    image = np.full((*size, 3), .62, np.float32); h, w = size
    for x0, y0, x1, y1 in segments:
        cv2.line(image, (round(x0*w), round(y0*h)), (round(x1*w), round(y1*h)), (.03, .03, .03), 3, cv2.LINE_AA)
    return image


def slanted(x0, y0, length, degrees):
    import math
    return (x0, y0, x0+length*math.cos(math.radians(degrees)), y0+length*math.sin(math.radians(degrees))*1350/900)


def test_level_follows_the_horizon_and_uprights_not_a_wall_of_slanted_boards():
    from luma.upright import AlignmentError
    shore = [slanted(.03+.16*i, .30+.01*(i % 3), .13, 2) for i in range(6)]+[slanted(.05, .5, .3, 2), slanted(.5, .56, .3, 2)]
    boards = [slanted(.30, .70+.012*i, .26, 14) for i in range(18)]              # siding seen at an angle: long, many, one place
    tilt = lambda image: -estimate(image, 'level')['rotation']
    scene = lined([s for s in shore]+[slanted(x, .35, .23*900/1350, 92) for x in (.28, .45, .62)]+boards)
    assert tilt(scene) == pytest.approx(-2, abs=.6)                               # not -14
    for only in (boards+[slanted(.1, .2, .7, 12)],                                # a table edge or decking: one long slanted boundary and clutter
                 [slanted(.1+.2*i, .2, .3*900/1350, 99) for i in range(4)]+boards):   # uprights 9 degrees off with no horizon
        with pytest.raises(AlignmentError):estimate(lined(only), 'level')
    uprights = lined([slanted(.1+.2*i, .2, .5*900/1350, 91.5) for i in range(5)])
    assert tilt(uprights) == pytest.approx(-1.5, abs=.3)                          # a small turn on uprights alone
    with pytest.raises(AlignmentError):estimate(lined([slanted(.1+.2*i, .2, .5*900/1350, 94) for i in range(5)]), 'level')
