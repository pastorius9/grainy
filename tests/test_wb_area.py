"""The white balance eyedropper over an area: a click still reads one point, a drag reads the average of its rectangle."""
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QPointF, QPoint, QEvent
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from test_studio_ui import app, wait
from luma.eyedropper import mean_colour, area_block
from luma.i18n import tr

NO = Qt.KeyboardModifier.NoModifier
WARM, COOL, WHITE = (144, 128, 112), (112, 128, 144), (255, 255, 255)


def photo(path, patches):
    """A 180 x 120 grey photo with coloured rectangles: [(x0, y0, x1, y1, colour)]."""
    pixels = np.full((120, 180, 3), 128, np.uint8)
    for x0, y0, x1, y1, colour in patches:pixels[y0:y1, x0:x1] = colour
    Image.fromarray(pixels).save(path)


def open_photo(app, tmp_path, patches):
    from luma.app import MainWindow
    photo(tmp_path/'chart.png', patches)
    w = MainWindow(tmp_path/'data'); ident = w.catalog.add(tmp_path/'chart.png'); w.refresh_lists(); w.activate(ident); w.show()
    wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
    w.activateWindow()
    return w


@pytest.fixture
def chart(app, tmp_path):
    """Warm and cool patches of the same strength on grey: their average is neutral."""
    w = open_photo(app, tmp_path, [(30, 30, 60, 90, WARM), (120, 30, 150, 90, COOL)])
    yield w
    wait(lambda: not w.render_running); w.close()


def at(w, x, y):
    return w.view.mapFromScene(QPointF(w.view.image_rect.width()*x, w.view.image_rect.height()*y))


def press(w, x, y):
    QTest.mousePress(w.view.viewport(), Qt.MouseButton.LeftButton, NO, at(w, x, y))


def release(w, x, y):
    QTest.mouseRelease(w.view.viewport(), Qt.MouseButton.LeftButton, NO, at(w, x, y))


def pick(w, start, end):
    """The eyedropper (W) dragged from one point of the photo to another."""
    w.wb_button.click(); assert w.view.sample_mode
    press(w, *start); QTest.mouseMove(w.view.viewport(), at(w, *end)); release(w, *end)
    wait(lambda: not w.render_running)


def test_the_average_ignores_clipped_and_black_pixels():
    pixels = np.array([[.4, .5, .6], [1., 1., 1.], [.99, .5, .5], [0., 0., 0.], [.4, 0., .6], [.2, .3, .4]], np.float32)
    np.testing.assert_allclose(mean_colour(pixels), [.3, .4, .5], atol=1e-6)
    np.testing.assert_allclose(mean_colour(pixels.reshape(2, 3, 3)), [.3, .4, .5], atol=1e-6)         # a block works the same
    assert mean_colour(pixels[[1, 2, 3, 4]]) is None and mean_colour(np.empty((0, 3))) is None
    np.testing.assert_allclose(mean_colour(pixels[[0, 3]], floor=-1), [.2, .25, .3], atol=1e-6)


def test_the_block_of_a_rectangle_keeps_at_least_one_pixel():
    frame = np.arange(20*30*3, dtype=np.float32).reshape(20, 30, 3)
    assert area_block(frame, (.0, .0, 1., 1.)).shape == (20, 30, 3)
    assert area_block(frame, (.5, .5, .5, .5)).shape == (1, 1, 3) and area_block(frame, (1., 1., 1., 1.)).shape == (1, 1, 3)
    np.testing.assert_array_equal(area_block(frame, (.1, .2, .5, .6)), frame[4:12, 3:15])


def test_a_click_reads_one_point_and_a_drag_reads_the_average(chart):
    w = chart; v = w.view; base = (w.settings['temperature'], w.settings['tint'])
    assert base == (0, 0)
    w.wb_button.click(); press(w, .25, .5); release(w, .25, .5)                                    # a click inside the warm patch
    wait(lambda: not w.render_running)
    assert not v.sample_mode and not w.wb_button.isChecked()
    clicked = w.settings['temperature']
    from luma.engine import to_linear
    r, b = to_linear(np.float32([144/255, 112/255]))
    assert clicked == pytest.approx(90*np.log2(b/r), abs=3)                                          # one point: the warm patch alone, in light
    w.undo(); wait(lambda: not w.render_running); assert w.settings['temperature'] == 0
    pick(w, (.1, .2), (.9, .8))                                                                     # both patches and the grey between
    assert not v.sample_mode and not w.wb_button.isChecked()
    assert abs(w.settings['temperature']) < 1.5 and abs(w.settings['tint']) < 1.5, (w.settings['temperature'], w.settings['tint'])
    w.undo(); wait(lambda: not w.render_running)                                                    # one undo step, like the click
    assert w.settings['temperature'] == 0


def test_a_rectangle_over_a_patch_does_what_a_click_does_and_neutralises_it(chart):
    w = chart
    def cast():
        frame = w.view.on_screen; h, wide = frame.shape[:2]
        r, g, b = frame[int(.3*h):int(.7*h), int(.2*wide):int(.28*wide)].mean(axis=(0, 1))
        return abs(r-b)
    before = cast(); assert before > .1                                                             # warm
    w.wb_button.click(); press(w, .24, .5); release(w, .24, .5); wait(lambda: not w.render_running)
    by_click = (w.settings['temperature'], w.settings['tint'])
    w.undo(); wait(lambda: not w.render_running); assert cast() == pytest.approx(before, abs=1e-3)
    pick(w, (.19, .3), (.31, .7))
    assert (w.settings['temperature'], w.settings['tint']) == pytest.approx(by_click, abs=.5)         # a uniform patch: the same as a click
    assert w.settings['temperature'] < -20 and w.catalog.photo(w.current_id)['settings']['temperature'] == w.settings['temperature']
    once = cast(); assert once < before*.15                                                         # one sample neutralises the patch
    pick(w, (.19, .3), (.31, .7)); assert cast() <= once + .01                                      # and a second one keeps it there


def test_clipped_pixels_in_the_rectangle_are_left_out(app, tmp_path):
    w = open_photo(app, tmp_path, [(30, 30, 60, 90, WARM), (60, 30, 90, 90, WHITE)])
    try:
        pick(w, (.19, .3), (.32, .7)); warm_only = w.settings['temperature']; assert warm_only < -20
        w.undo(); wait(lambda: not w.render_running)
        pick(w, (.19, .3), (.45, .7))                                                                 # the clipped white next to it is in the rectangle too
        assert w.settings['temperature'] == pytest.approx(warm_only, abs=1.5)
        w.undo(); wait(lambda: not w.render_running)
        w.wb_button.click(); w.statusBar().clearMessage()
        press(w, .36, .3); QTest.mouseMove(w.view.viewport(), at(w, .46, .7)); release(w, .46, .7)  # only white: nothing to read
        assert w.view.sample_mode and w.wb_button.isChecked() and w.settings['temperature'] == 0
        assert w.statusBar().currentMessage() == tr('이 영역은 모두 너무 밝거나 어둡습니다. 색이 남아 있는 중성 회색이나 흰색 영역을 고르세요.')
        press(w, .36, .5); release(w, .36, .5)                                                      # a click on the white reads it as before
        wait(lambda: not w.render_running); assert not w.view.sample_mode
    finally:
        wait(lambda: not w.render_running); w.close()


def test_small_moves_are_clicks_and_larger_ones_rectangles(chart):
    w = chart; v = w.view; clicks, areas = [], []
    v.sampled.connect(lambda x, y: clicks.append((x, y))); v.sampledArea.connect(lambda *a: areas.append(a))
    start = at(w, .25, .5)
    def drag_by(dx, dy, release_at=None):
        w.wb_button.click()
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, NO, start)
        QTest.mouseMove(v.viewport(), start+QPoint(dx, dy))
        rectangle = v.sample_area
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, NO, start+QPoint(dx, dy))
        wait(lambda: not w.render_running); w.undo() if w.settings['temperature'] else None
        return rectangle
    assert drag_by(3, -2) is None and len(clicks) == 1 and not areas                                 # under 4 pixels: a click, at the press
    assert clicks[0] == pytest.approx((.25, .5), abs=.01)
    shape = drag_by(30, 20)
    assert len(clicks) == 1 and len(areas) == 1 and shape is not None                                # the rectangle was drawn while dragging
    x0, y0, x1, y1 = areas[0]
    assert (x0, y0) == pytest.approx((.25, .5), abs=.01) and x1 > x0 and y1 > y0
    scale = v.transform().m11()
    assert (x1-x0)*v.image_rect.width()*scale == pytest.approx(30, abs=2) and (y1-y0)*v.image_rect.height()*scale == pytest.approx(20, abs=2)
    drag_by(-30, -20)
    x0, y0, x1, y1 = areas[1]
    assert x0 < x1 and y0 < y1 and (x1, y1) == pytest.approx((.25, .5), abs=.01)                    # dragged up and to the left: still x0 < x1
    drag_by(3000, 3000)
    assert areas[2][2:] == (1., 1.)                                                                 # beyond the photo: kept on it
    assert not v.sample_mode and v.sample_area is None and v.sample_press is None


def test_escape_while_dragging_cancels_and_the_rectangle_is_drawn(chart):
    w = chart; v = w.view; areas = []
    v.sampledArea.connect(lambda *a: areas.append(a))
    flat = v.viewport().grab().toImage()
    w.wb_button.click(); press(w, .3, .3); QTest.mouseMove(v.viewport(), at(w, .7, .7))
    assert v.sample_area is not None and v.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    drawn = v.viewport().grab().toImage()
    edge = at(w, .3, .3).y()+1                                                                      # a pixel row beside the top edge
    row = [QColor(drawn.pixel(x, edge-1)).value() for x in range(at(w, .3, .3).x(), at(w, .7, .7).x())]
    assert max(row) > 200 and min(row) < 60, 'dashes of a light and a dark line along the edge'
    assert drawn != flat
    QTest.keyClick(v, Qt.Key.Key_Escape)
    assert not v.sample_mode and v.sample_area is None and not w.wb_button.isChecked()
    release(w, .7, .7)
    assert not areas and w.settings['temperature'] == 0


def hover(w, position, buttons=Qt.MouseButton.NoButton):
    """The pointer at `position` of the photo view, sent to it directly: where it ends up does not depend on the other windows."""
    viewport = w.view.viewport(); local = QPointF(position)
    QApplication.sendEvent(viewport, QMouseEvent(QEvent.Type.MouseMove, local, QPointF(viewport.mapToGlobal(position)), Qt.MouseButton.NoButton, buttons, NO))


def test_the_loupe_stays_for_clicks_and_makes_way_for_a_rectangle(chart):
    w = chart; v = w.view; loupes = []; draw = v.draw_loupe; centre = at(w, .5, .5); held = Qt.MouseButton.LeftButton
    v.draw_loupe = lambda painter: (loupes.append(1), draw(painter))
    def shown():
        loupes.clear(); v.viewport().grab(); return bool(loupes)
    w.wb_button.click(); hover(w, centre); assert shown()                                            # before the button goes down
    press(w, .5, .5); hover(w, centre+QPoint(2, 1), held)
    assert v.sample_area is None and shown()                                                         # still a click
    hover(w, centre+QPoint(40, 30), held)
    assert v.sample_area is not None and not shown()                                                 # a rectangle
    hover(w, centre+QPoint(1, 1), held)
    assert v.sample_area is None and shown()                                                         # back to where it started: a click again
    release(w, .5, .5)


def test_the_rectangle_is_carried_to_the_source_through_a_turned_photo(app, tmp_path):
    yy, xx = np.mgrid[:120, :180]
    Image.fromarray(np.uint8(np.stack([.2+xx/360, .2+yy/240, np.full_like(xx, .4, dtype=float)], -1)*255)).save(tmp_path/'ramp.png')
    from luma.app import MainWindow
    w = MainWindow(tmp_path/'data'); ident = w.catalog.add(tmp_path/'ramp.png'); w.refresh_lists(); w.activate(ident); w.show()
    wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
    try:
        straight = w.studio.area_pixels(w.source, (.2, .1, .4, .5))
        np.testing.assert_allclose(straight.mean(axis=0), w.source[12:60, 36:72].reshape(-1, 3).mean(axis=0), atol=.004)
        w.set_setting('rotation', 1); w.commit(); w.render(); wait(lambda: not w.render_running)
        turned = w.studio.area_pixels(w.source, (.2, .1, .4, .5))               # clockwise: shown x, y are source y, 1 - x
        np.testing.assert_allclose(turned.mean(axis=0), w.source[71:96, 18:90].reshape(-1, 3).mean(axis=0), atol=.004)
        assert turned.mean(axis=0)[1]-straight.mean(axis=0)[1] > .05            # and not the part of the photo it would be without the turn
        assert 1 < len(turned) <= 6400 and len(w.studio.area_pixels(w.source, (.5, .5, .5, .5))) == 1
    finally:
        wait(lambda: not w.render_running); w.close()
