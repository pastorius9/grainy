"""Two pictures side by side in the main window, and the eyedropper's magnifier."""
import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QApplication, QBoxLayout
from PySide6.QtTest import QTest
from test_studio_ui import app, window, wait
from luma.engine import develop, defaults
from luma.widgets import PhotoView


def idle(w):
    wait(lambda: w.source is not None and not w.jobs and not w.render_running and not w.preview_timer.isActive() and not w.refine_timer.isActive())


def second_photo(w, tmp_path):
    yy, xx = np.mgrid[:120, :180]; rgb = np.stack([.7-xx/360, np.full_like(xx, .3, dtype=float), .2+yy/240], -1)
    path = tmp_path/'other.png'; Image.fromarray(np.uint8(rgb*255)).save(path)
    ident = w.catalog.add(path); w.refresh_lists(); wait(lambda: not w.library_loading and ident in w.photo_model.positions); return ident


def shown(c):
    return c.other.on_screen is not None and not c.w.jobs


def test_before_and_after_side_by_side(window):
    w = window; c = w.compare; first = w.current_id
    assert not c.on and c.bar.isHidden() and c.other_pane.isHidden() and c.main_pane.caption.isHidden()
    w.adjustments['exposure'].changed.emit('exposure', 1.2); w.finish_interaction(); idle(w)
    w.apply_crop([.1, .1, .7, .9]); idle(w)
    w.compare_button.click(); wait(lambda: shown(c)); idle(w)
    assert c.on and c.mode == 'before' and not c.bar.isHidden() and not c.other_pane.isHidden() and w.compare_button.isChecked()
    assert (c.other_pane.caption.text(), c.main_pane.caption.text()) == ('원본', '보정 후') and not c.main_pane.marked
    assert c.order.itemAt(0).widget() is c.other_pane and c.order.direction() == QBoxLayout.Direction.LeftToRight      # before on the left
    before = c.other.on_screen; after = w.view.on_screen
    assert before.shape == after.shape and before.mean() < after.mean()-.05                  # same frame, unedited
    np.testing.assert_allclose(before, develop(w.source, {**defaults(), 'crop': w.settings['crop']}), atol=3e-3)
    w.adjustments['exposure'].changed.emit('exposure', 0.); w.finish_interaction(); idle(w)
    np.testing.assert_allclose(c.other.on_screen, w.view.on_screen, atol=3e-3)               # nothing but the frame is left
    c.stack_button.click(); assert c.order.direction() == QBoxLayout.Direction.TopToBottom and not c.side_button.isChecked()
    c.swap_button.click(); assert c.order.itemAt(0).widget() is c.main_pane
    QTest.mouseDClick(w.view.viewport(), Qt.MouseButton.LeftButton); QApplication.processEvents()
    assert not c.on and c.bar.isHidden() and c.other_pane.isHidden() and c.main_pane.caption.isHidden() and not w.compare_button.isChecked()
    assert w.current_id == first and w.view.fit_mode and c.other.on_screen is None


def test_two_photos_and_which_one_is_current(window, tmp_path):
    w = window; c = w.compare; first = w.current_id; other = second_photo(w, tmp_path)
    c.start('photos'); wait(lambda: shown(c)); idle(w)
    assert c.mode == 'photos' and c.other_id == other and w.current_id == first and c.main_pane.marked and not c.other_pane.marked
    assert c.order.itemAt(0).widget() is c.main_pane and c.main_pane.caption.text() == 'test.png' and c.other_pane.caption.text() == 'other.png'
    np.testing.assert_allclose(c.other.on_screen, np.asarray(Image.open(tmp_path/'other.png'), np.float32)/255, atol=4e-3)      # the other photo, unedited
    left = c.main_pane.x()
    QTest.mouseClick(c.other.viewport(), Qt.MouseButton.LeftButton); QApplication.processEvents()      # a click makes the other photo current
    wait(lambda: w.current_id == other and shown(c)); idle(w)
    assert c.other_id == first and c.main_pane.marked and c.main_pane.caption.text() == 'other.png'
    assert c.order.itemAt(0).widget() is c.other_pane and c.main_pane.x() > left                # both photos stayed in place
    assert w.selected_ids() == [other]
    c.swap_button.click(); assert c.order.itemAt(0).widget() is c.main_pane and w.current_id == other
    c.choice.setCurrentIndex(c.choice.findData('before')); wait(lambda: shown(c)); idle(w)
    assert c.mode == 'before' and c.other_pane.caption.text() == '원본' and not c.main_pane.marked
    c.choice.setCurrentIndex(c.choice.findData('photos')); wait(lambda: shown(c)); idle(w)
    assert c.other_id == first
    QTest.mouseDClick(c.other.viewport(), Qt.MouseButton.LeftButton); QApplication.processEvents()      # a double click shows that photo alone
    wait(lambda: w.current_id == first); idle(w)
    assert not c.on and c.other_pane.isHidden()


def test_the_second_picture_follows_the_zoom_and_a_single_photo_falls_back(window):
    w = window; c = w.compare
    c.start('photos'); wait(lambda: shown(c)); idle(w)                                       # no other photo in the library
    assert c.on and c.mode == 'before' and c.other_id is None
    assert c.other.fit_mode and w.view.fit_mode
    w.view.actual_size(); w.view.zoom_by(2.); QApplication.processEvents(); wait(lambda: shown(c))
    assert not c.other.fit_mode and c.other.transform().m11()*c.other.image_rect.width() == \
        __import__('pytest').approx(w.view.transform().m11()*w.view.image_rect.width(), rel=1e-6)
    w.view.fit_photo(); QApplication.processEvents(); assert c.other.fit_mode
    c.stop(); assert not c.on


def test_menu_entries_open_the_comparison_in_the_window(window, tmp_path, monkeypatch):
    from luma.photo_actions import build_menu
    w = window; first = w.current_id; other = second_photo(w, tmp_path); monkeypatch.setattr(w, 'selected_ids', lambda: [first, other])
    menu = build_menu(w); actions = {a.data(): a for sub in [menu]+[a.menu() for a in menu.actions() if a.menu()] for a in sub.actions() if a.data()}
    dialogs = len(w.manager.dialogs)
    actions['compare'].trigger(); wait(lambda: shown(w.compare))
    assert w.compare.on and w.compare.mode == 'photos' and w.compare.other_id == other and len(w.manager.dialogs) == dialogs
    actions['before-after'].trigger(); wait(lambda: shown(w.compare))
    assert w.compare.mode == 'before'
    w.compare.stop(); menu.deleteLater()


def test_the_eyedropper_enlarges_what_is_under_it(app):
    view = PhotoView(); view.resize(600, 400); view.show()
    try:
        image = np.zeros((200, 300, 3), np.float32); image[:, 150:] = 1.                       # black left, white right
        view.set_image(image); QApplication.processEvents()
        edge = view.mapFromScene(150., 100.); pointer = QPoint(edge.x()-10, edge.y())
        look = lambda dx: view.viewport().grab().toImage().pixelColor(pointer.x()+dx, pointer.y()).lightness()
        QTest.mouseMove(view.viewport(), pointer); QApplication.processEvents()
        assert look(30) > 200                                                                 # no eyedropper: the photo as it is
        view.sample_mode = True; QTest.mouseMove(view.viewport(), pointer+QPoint(1, 0)); QTest.mouseMove(view.viewport(), pointer); QApplication.processEvents()
        assert view.viewport().cursor().shape() == Qt.CursorShape.BlankCursor
        assert look(30) < 60 and look(view.LOUPE_ZOOM*10+12) > 200                              # the edge is six times farther away
        assert look(view.LOUPE_RADIUS) > 150 and look(-view.LOUPE_RADIUS) > 150                 # the white rim of the circle
        QTest.mouseMove(view.viewport(), QPoint(3, 3)); QApplication.processEvents()          # off the photo: the pointer is back
        assert view.viewport().cursor().shape() != Qt.CursorShape.BlankCursor
        view.sample_mode = False; QTest.mouseMove(view.viewport(), pointer); QApplication.processEvents()
        assert look(30) > 200
    finally:
        view.close()
