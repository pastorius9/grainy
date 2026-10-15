"""Tests for photo view background colour choice and window-level [ / ] brush shortcuts."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PIL import Image
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from test_studio_ui import app, window, wait
from luma.app import MainWindow, configure_application, BACKGROUNDS


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bg_color(view):
    """Return the QColor from view.backgroundBrush()."""
    return view.backgroundBrush().color()


def _menu_action(w, key):
    """Return the (key, action) pair from manager.bg_group for the given key."""
    return next(a for k, a in w.manager.bg_group if k == key)


# ---------------------------------------------------------------------------
# Background colour – default
# ---------------------------------------------------------------------------

def test_default_background_is_black(window):
    w = window
    assert _bg_color(w.view) == QColor(BACKGROUNDS['black'])
    assert _bg_color(w.compare.other) == QColor(BACKGROUNDS['black'])


# ---------------------------------------------------------------------------
# Background colour – menu change propagates to both views and preference
# ---------------------------------------------------------------------------

def test_menu_change_sets_background_and_saves_preference(window):
    w = window
    # Trigger "dark grey" from the 보기·필터 › 사진 배경 submenu
    action = _menu_action(w, 'dark_grey')
    action.trigger()

    assert _bg_color(w.view) == QColor(BACKGROUNDS['dark_grey'])
    assert _bg_color(w.compare.other) == QColor(BACKGROUNDS['dark_grey'])
    assert w.catalog.preference('view_background', 'black') == 'dark_grey'

    # Menu action for dark_grey should be checked; black should not
    assert _menu_action(w, 'dark_grey').isChecked()
    assert not _menu_action(w, 'black').isChecked()

    # Restore
    w.set_view_background('black')


# ---------------------------------------------------------------------------
# Background colour – new window picks up saved preference
# ---------------------------------------------------------------------------

def test_new_window_restores_saved_background(app, tmp_path):
    # Shared data dir so we can check the saved preference carries over
    data_dir = tmp_path / 'shared_data'
    yy, xx = np.mgrid[:60, :90]
    rgb = np.stack([.3 + xx / 180, .2 + yy / 120, np.full_like(xx, .5, dtype=float)], -1)
    img_path = tmp_path / 'img.png'
    Image.fromarray(np.uint8(rgb * 255)).save(img_path)

    # First window: choose mid_grey
    w1 = MainWindow(data_dir)
    w1.catalog.add(img_path)
    w1.refresh_lists()
    w1.show()
    wait(lambda: not w1.library_loading)
    w1.set_view_background('mid_grey')
    assert w1.catalog.preference('view_background', 'black') == 'mid_grey'
    wait(lambda: not w1.render_running)
    w1.close()

    # Second window on same data dir: must inherit mid_grey
    w2 = MainWindow(data_dir)
    w2.show()
    wait(lambda: not w2.library_loading)
    assert _bg_color(w2.view) == QColor(BACKGROUNDS['mid_grey'])
    assert _bg_color(w2.compare.other) == QColor(BACKGROUNDS['mid_grey'])
    wait(lambda: not w2.render_running)
    w2.close()


# ---------------------------------------------------------------------------
# Background colour – right-click context menu
# ---------------------------------------------------------------------------

def test_context_menu_actions_reflect_current_choice(window):
    w = window
    w.set_view_background('white')

    # Build the menu without showing it (avoids exec() blocking in headless env)
    menu = w._build_bg_context_menu()

    # _bg_context_actions should now exist and 'white' should be checked
    ctx = {k: a for k, a in w._bg_context_actions}
    assert ctx['white'].isChecked()
    assert not ctx['black'].isChecked()
    assert not ctx['mid_grey'].isChecked()
    menu.deleteLater()

    # Restore
    w.set_view_background('black')


def test_context_menu_action_changes_background(window):
    w = window
    w.set_view_background('black')

    # Build the menu without showing it (avoids exec() blocking in headless env)
    menu = w._build_bg_context_menu()
    mid_action = next(a for k, a in w._bg_context_actions if k == 'mid_grey')
    mid_action.trigger()
    menu.deleteLater()

    assert _bg_color(w.view) == QColor(BACKGROUNDS['mid_grey'])
    assert _bg_color(w.compare.other) == QColor(BACKGROUNDS['mid_grey'])

    # Restore
    w.set_view_background('black')


# ---------------------------------------------------------------------------
# [ / ] brush-size shortcuts – window level (focus on slider, not photo view)
# ---------------------------------------------------------------------------

def test_bracket_shortcuts_resize_brush_with_focus_elsewhere(window):
    w = window
    s = w.studio
    # Activate the Masks tab (index 5) and create a brush mask
    w.tabs.setCurrentIndex(5)
    s.create_mask('brush')
    wait(lambda: w.source is not None and not w.render_running)

    # Move keyboard focus to something other than the photo view
    slider = s.radius.slider
    slider.setFocus()
    assert w.focusWidget() is not w.view

    before = w.view.brush_radius
    # ] makes the brush bigger
    QTest.keyClick(w, Qt.Key.Key_BracketRight)
    assert w.view.brush_radius > before
    bigger = w.view.brush_radius
    # slider follows brushSizeChanged signal
    assert s.radius.slider.value() > 0  # just verify it didn't break

    # [ makes it smaller
    QTest.keyClick(w, Qt.Key.Key_BracketLeft)
    assert w.view.brush_radius < bigger


def test_bracket_shortcuts_do_nothing_without_brush_tool(window):
    w = window
    s = w.studio
    # Make sure no brush tool is active (go back to develop tab)
    w.tabs.setCurrentIndex(0)
    wait(lambda: not w.render_running)

    before = w.view.brush_radius
    QTest.keyClick(w, Qt.Key.Key_BracketRight)
    QTest.keyClick(w, Qt.Key.Key_BracketLeft)
    assert w.view.brush_radius == before
