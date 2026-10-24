"""The keyboard shortcuts: one table (luma/shortcut_list.TABLE) is what is bound, and what the shortcut list shows."""
import json
import re
from copy import deepcopy
from pathlib import Path
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QTextEdit
from test_studio_ui import app, window, wait
from luma.i18n import language, set_language, tr
from luma.photo_actions import build_menu
from luma.shortcut_list import TABLE, GROUPS, ShortcutDialog, written, key_hint

NO, SHIFT, CTRL = Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.ControlModifier
EXISTING = {'Ctrl+O', 'Ctrl+Shift+E', 'Ctrl+E', 'Ctrl+Z', 'Ctrl+Shift+Z', 'Ctrl+Y', 'Ctrl+Shift+C', 'Ctrl+Shift+V', 'Ctrl+Alt+V',
            '\\', 'C', 'G', 'D', 'R', 'P', 'X', 'U', 'B', '[', ']', 'O', '0', '1', '2', '3', '4', '5'}
NEW = {'W', 'K', 'M', 'Shift+M', 'Q', 'Shift+/'}
SCREEN = {'J', 'Z', 'F', 'Tab', 'Shift+Tab', 'L', 'Ctrl+/', 'Ctrl+U', 'Ctrl+Shift+U', 'Ctrl+[', 'Ctrl+]', '6', '7', '8', '9', "Ctrl+'"}
FOCUS = {'Tab', 'Shift+Tab'}                       # the keys Qt uses to move the focus: in a text field they still do


def press(w, key, modifier=NO):
    QTest.keyClick(w, key, modifier); QApplication.processEvents()


def at(w, x, y):
    return w.view.mapFromScene(QPointF(w.view.image_rect.width()*x, w.view.image_rect.height()*y))


def drag(w, start, end):
    QTest.mousePress(w.view.viewport(), Qt.MouseButton.LeftButton, NO, at(w, *start))
    QTest.mouseMove(w.view.viewport(), at(w, *end))
    QTest.mouseRelease(w.view.viewport(), Qt.MouseButton.LeftButton, NO, at(w, *end))
    wait(lambda: not w.render_running)


def mask_state(w):
    s = w.studio
    return (w.tabs.currentIndex(), w.view.tool_mode, [m['name'] for m in w.settings['masks']], s.operation.currentData(),
            s.layer_index, s.fresh_layer, w.stack.currentIndex())


def bound(w):
    return [s for s in w.findChildren(QShortcut, options=Qt.FindChildOption.FindDirectChildrenOnly)]


def test_the_table_is_what_is_bound(window):
    w = window
    keys = [key for entry in TABLE for key in entry.keys]
    assert len(keys) == len(set(keys)), 'a key belongs to one row only'
    assert set(keys) == EXISTING | NEW | SCREEN, 'a key was added or dropped: update this list and the shortcut list texts'
    assert {k for e in TABLE if e.focus for k in e.keys} == FOCUS
    assert sorted(s.key().toString() for s in bound(w)) == sorted(QKeySequence(k).toString() for k in keys)
    repeats = {s.key().toString(): s.autoRepeat() for s in bound(w)}
    assert not any(repeats[QKeySequence(k).toString()] for k in ('W', 'K', 'M', 'Shift+M', 'Q', 'Shift+/', 'J', 'Z', 'F', 'L', 'Ctrl+/', 'Ctrl+U', 'Ctrl+Shift+U', "Ctrl+'", '6', '9')), 'a held key must not flip a tool or pile up masks'
    assert all(repeats[QKeySequence(k).toString()] for k in ('[', ']', 'Ctrl+Z', 'Ctrl+Shift+Z', 'Ctrl+Y', 'C', 'B'))        # as before
    assert [g for g in GROUPS if any(e.group == g for e in TABLE)] == list(GROUPS) and all(e.group in GROUPS for e in TABLE)
    table = json.loads((Path(__file__).resolve().parents[1]/'assets/i18n/en.json').read_text(encoding='utf-8'))
    assert not [t for t in [e.text for e in TABLE]+list(GROUPS) if table.get(t, t) == t], 'every text of the list has an English translation'


def test_every_key_runs_its_row(window, monkeypatch):
    """Each QShortcut calls the run of its own row with its own key."""
    w = window; calls = []
    import luma.shortcut_list as module
    patched = tuple(e._replace(run=lambda window, key, name=e.text: calls.append((name, key))) for e in TABLE)
    monkeypatch.setattr(module, 'TABLE', patched)
    for s in bound(w): s.setParent(None)
    module.bind(w)
    for s in bound(w): s.activated.emit()
    assert sorted(calls) == sorted((e.text, k) for e in patched for k in e.keys)


def test_mask_keys_do_what_the_new_mask_list_does(window):
    w = window; s = w.studio; w.activateWindow()
    for key, modifier, kind, position in ((Qt.Key.Key_K, NO, 'brush', 0), (Qt.Key.Key_M, NO, 'linear', 1), (Qt.Key.Key_M, SHIFT, 'radial', 2)):
        w.tabs.setCurrentIndex(0)
        press(w, key, modifier)
        by_key = mask_state(w)
        assert w.view.tool_mode == kind and w.tabs.currentIndex() == 5 and len(w.settings['masks']) == 1      # the Masks tool opened
        s.mode('')                                                                  # nothing drawn: the empty mask goes
        assert not w.settings['masks']
        w.tabs.setCurrentIndex(5)
        entries = [a for a in s.create_button.menu().actions() if not a.isSeparator() and not a.text().startswith('하늘')]
        entries[position].trigger()                                                   # New Mask > the same kind
        assert mask_state(w) == by_key
        s.mode('')


def test_mask_keys_draw_like_the_buttons_and_do_not_pile_up_empty_masks(window):
    w = window; s = w.studio; w.activateWindow()
    press(w, Qt.Key.Key_K); press(w, Qt.Key.Key_K)                                     # again before drawing: the same mask
    assert len(w.settings['masks']) == 1 and w.view.tool_mode == 'brush'
    press(w, Qt.Key.Key_M); assert len(w.settings['masks']) == 1 and w.view.tool_mode == 'linear'
    drag(w, (.2, .3), (.7, .6))
    assert [c['type'] for c in w.settings['masks'][0]['components']] == ['linear']
    press(w, Qt.Key.Key_K)                                                            # after drawing: a second mask
    assert len(w.settings['masks']) == 2 and w.view.tool_mode == 'brush' and s.layer_index == 1
    drag(w, (.3, .4), (.5, .5))
    assert [c['type'] for c in w.settings['masks'][1]['components']] == ['brush']
    press(w, Qt.Key.Key_M, SHIFT); assert len(w.settings['masks']) == 3 and w.view.tool_mode == 'radial'
    s.mode(''); assert len(w.settings['masks']) == 2


def test_heal_key_opens_the_healing_tool_like_its_button(window):
    w = window; w.activateWindow()
    press(w, Qt.Key.Key_Q)
    keyed = (w.tabs.currentIndex(), w.view.tool_mode)
    assert keyed == (6, 'heal') and w.tabs.tool_buttons[6].isChecked()
    press(w, Qt.Key.Key_Q); assert (w.tabs.currentIndex(), w.view.tool_mode) == keyed            # the button would close it; the key keeps it
    w.studio.mode(''); press(w, Qt.Key.Key_Q); assert w.view.tool_mode == 'heal'                 # after Esc: the brush is back in hand
    w.tabs.setCurrentIndex(0); w.tabs.tool_buttons[6].click()
    assert (w.tabs.currentIndex(), w.view.tool_mode) == keyed
    assert key_hint('Q') in w.tabs.tool_buttons[6].toolTip()


def test_eyedropper_key_toggles_and_goes_to_develop(window):
    w = window; w.activateWindow()
    press(w, Qt.Key.Key_W); assert w.view.sample_mode and w.wb_button.isChecked()
    press(w, Qt.Key.Key_W); assert not w.view.sample_mode and not w.wb_button.isChecked()
    press(w, Qt.Key.Key_G); assert w.stack.currentIndex() == 1                                   # library
    press(w, Qt.Key.Key_W); assert w.stack.currentIndex() == 0 and w.view.sample_mode
    press(w, Qt.Key.Key_W)
    press(w, Qt.Key.Key_K); assert w.tabs.currentIndex() == 5                                    # from a tool: back to the Basic panel
    press(w, Qt.Key.Key_W)
    assert w.tabs.currentIndex() == 0 and w.view.sample_mode and not w.view.tool_mode and w.tabs.is_open('basic')
    assert not w.settings['masks']                                                               # the unused mask is gone
    press(w, Qt.Key.Key_W); assert not w.view.sample_mode
    w.negative_base_button.setChecked(True)                                                      # a film base pick is not left on
    press(w, Qt.Key.Key_W)
    assert not w.negative_base_button.isChecked() and w.view.sample_mode
    assert key_hint('W') in w.wb_button.toolTip()


def test_the_question_mark_opens_the_list(window, monkeypatch):
    w = window; w.activateWindow(); opened = []
    monkeypatch.setattr(ShortcutDialog, 'exec', lambda dialog: opened.append(dialog.parent()))
    press(w, Qt.Key.Key_Slash, SHIFT)
    assert opened == [w]
    press(w, Qt.Key.Key_Slash, CTRL)
    assert opened == [w, w]
    menu = w.findChild(QMenu, 'settingsMenu')
    entry = next(a for a in menu.actions() if a.text().startswith(tr('단축키 목록…')))
    assert entry.text().endswith('\t'+key_hint('Ctrl+/')); entry.trigger()
    assert opened == [w, w, w]


def test_list_shows_every_binding(window):
    w = window
    for lang in ('ko', 'en'):
        set_language(lang)
        try:
            dialog = ShortcutDialog(w)
            texts = [label.text() for label in dialog.findChildren(QLabel)]
            keys = [label.text() for label in dialog.findChildren(QLabel, 'shortcutKey')]
            assert keys == [written(e) for g in GROUPS for e in TABLE if e.group == g]                      # all of them, grouped and in order
            for entry in TABLE:
                assert written(entry) in texts and tr(entry.text) in texts
            assert all(tr(group) in texts for group in GROUPS)
            from luma.shortcut_list import shown                                              # macOS writes ⇧ and ⌘
            assert all(shown(key) in texts for key in ('W', 'K', 'M', 'Shift+M', 'Q', 'O', '[ / ]', '0 – 5', 'Ctrl+Shift+Z / Ctrl+Y',
                                                       'J', 'Z', 'F', 'Tab', 'Shift+Tab', 'L', '? / Ctrl+/', 'Ctrl+U', 'Ctrl+Shift+U', 'Ctrl+[ / Ctrl+]', '6 – 9', "Ctrl+'"))
            if lang == 'en':assert not [t for t in texts if re.search('[가-힣]', t)]
            dialog.deleteLater()
        finally:
            set_language('ko')
    assert language() == 'ko'


def test_the_keys_do_nothing_while_typing(window):
    w = window; w.activateWindow()
    w.set_mode(1)
    field = QLineEdit(w); field.show(); field.setFocus(); QApplication.processEvents()
    assert QApplication.focusWidget() is field
    for key, modifier in ((Qt.Key.Key_W, NO), (Qt.Key.Key_K, NO), (Qt.Key.Key_M, SHIFT), (Qt.Key.Key_Q, NO), (Qt.Key.Key_Slash, SHIFT), (Qt.Key.Key_5, NO), (Qt.Key.Key_P, NO)):
        QTest.keyClick(field, key, modifier)
    assert len(field.text()) == 7 and field.text().lower().startswith('wk')
    assert w.stack.currentIndex() == 1 and not w.view.sample_mode and not w.settings['masks'] and w.tabs.currentIndex() == 0
    assert w.catalog.photo(w.current_id)['rating'] == 0 and w.catalog.photo(w.current_id)['flag'] == 0
    # A key a text field does not take for itself is stopped by safe_shortcut: every shortcut passes it.
    for widget in (QLineEdit(w), QDoubleSpinBox(w), QTextEdit(w), QPlainTextEdit(w)):
        widget.show(); widget.setFocus(); QApplication.processEvents()
        for s in bound(w):
            if s.key().toString() in ('W', 'K', 'Q', 'Shift+/', 'M', 'Shift+M'):s.activated.emit()
        assert not w.view.sample_mode and not w.settings['masks'] and w.stack.currentIndex() == 1, type(widget).__name__
        widget.deleteLater()
    w.view.setFocus(); QApplication.processEvents()
    press(w, Qt.Key.Key_D); assert w.stack.currentIndex() == 0                                    # outside a text field they work


def test_the_existing_keys_still_work(window):
    w = window; w.activateWindow(); ident = w.current_id
    def key(code, field):                                    # the list reloads after a rating or a flag; the keys act on the selection once it is back
        press(w, code); wait(lambda: not w.library_loading); return w.catalog.photo(ident)[field]
    assert key(Qt.Key.Key_5, 'rating') == 5 and key(Qt.Key.Key_0, 'rating') == 0 and key(Qt.Key.Key_3, 'rating') == 3
    assert key(Qt.Key.Key_P, 'flag') == 1 and key(Qt.Key.Key_X, 'flag') == -1 and key(Qt.Key.Key_U, 'flag') == 0
    press(w, Qt.Key.Key_G); assert w.stack.currentIndex() == 1
    press(w, Qt.Key.Key_D); assert w.stack.currentIndex() == 0
    press(w, Qt.Key.Key_Backslash); assert w.before_button.isChecked()
    press(w, Qt.Key.Key_Backslash); assert not w.before_button.isChecked()
    press(w, Qt.Key.Key_R); assert w.crop_button.isChecked()
    press(w, Qt.Key.Key_R); assert not w.crop_button.isChecked()
    w.studio.create_mask('brush'); size = w.studio.radius.slider.value()
    press(w, Qt.Key.Key_BracketRight); assert w.studio.radius.slider.value() > size
    press(w, Qt.Key.Key_BracketLeft); assert w.studio.radius.slider.value() == size
    drag(w, (.3, .4), (.5, .5)); w.tabs.setCurrentIndex(5)
    shown = w.studio.overlay.isChecked(); press(w, Qt.Key.Key_O); assert w.studio.overlay.isChecked() != shown
    w.settings['exposure'] = .5; w.commit('노출'); press(w, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    wait(lambda: not w.render_running); assert w.settings['exposure'] == 0


# ---- view, panels and the other keys of the second batch (J Z F Tab Shift+Tab L 6-9 Ctrl+' Ctrl+[ Ctrl+] Ctrl+U Ctrl+Shift+U Ctrl+/) ----

SIDES = ('library_column', 'edit_column')
BARS = ('header_bar', 'view_bar', 'filmstrip')


def layout(w):
    """What is shown around the photo, and how wide the three columns are."""
    parts = (w.library_column, w.edit_column, w.header_bar, w.view_bar, w.filmstrip, w.library_toolbar, w.menuBar(), w.statusBar())
    return [p.isHidden() for p in parts], w.split.sizes()


def hidden(w, *names):
    return [getattr(w, name).isHidden() for name in names]


def tab(w, shift=False, target=None):
    QTest.keyClick(target or w, Qt.Key.Key_Tab, SHIFT if shift else NO); QApplication.processEvents()


def settled(w):
    wait(lambda: w.source is not None and not w.render_running and not w.jobs and not w.library_loading)


def menu_entry(w, menu, text):
    top = next(a.menu() for a in w.menuBar().actions() if a.text() == tr(menu))
    return next(a for a in top.actions() if a.text().startswith(tr(text)))


def photo_ids(w, tmp_path, count=2):
    """The current photo and `count` more, all in the list."""
    ids = [w.current_id]
    for n in range(count):
        path = tmp_path/f'more-{n}.png'; Image.new('RGB', (90, 60), (70+n*50, 110, 140)).save(path); ids.append(w.catalog.add(path))
    w.refresh_lists(); wait(lambda: not w.library_loading)
    return ids


def test_j_toggles_the_clipping_indicators_like_the_button(window):
    w = window; w.activateWindow()
    def state():
        return (w.clipping_button.isChecked(), w.histogram.shadow_button.isChecked(), w.histogram.highlight_button.isChecked())
    assert state() == (False,)*3
    press(w, Qt.Key.Key_J); keyed = state(); assert keyed == (True,)*3 and w.view.shadow_clipping and w.view.highlight_clipping
    press(w, Qt.Key.Key_J); assert state() == (False,)*3
    w.clipping_button.click(); assert state() == keyed
    assert key_hint('J') in w.clipping_button.toolTip()


def test_z_switches_between_one_to_one_and_fit_like_the_buttons(window):
    w = window; w.activateWindow()
    buttons = {b.text(): b for b in w.view_bar.findChildren(QPushButton)}; fit, actual = buttons[tr('맞춤')], buttons['1:1']
    def state():
        return (w.view.fit_mode, round(w.view.transform().m11(), 3), w.zoom_label.text())
    fit.click(); fitted = state(); actual.click(); one = state()
    assert fitted[0] and not one[0] and one[1] == 1 and fitted != one
    press(w, Qt.Key.Key_Z); assert state() == fitted                                               # from 1:1 to fit
    press(w, Qt.Key.Key_Z); assert state() == one                                                  # and from fit to 1:1
    w.view.zoom_by(1.5); assert not w.view.fit_mode
    press(w, Qt.Key.Key_Z); assert state() == fitted                                               # from some other zoom: to fit
    assert key_hint('Z') in fit.toolTip() and key_hint('Z') in actual.toolTip()


def test_f_toggles_full_screen_and_esc_leaves_it_only_then(window):
    w = window; w.activateWindow()
    assert not w.isFullScreen()
    press(w, Qt.Key.Key_F); assert w.isFullScreen()
    press(w, Qt.Key.Key_F); assert not w.isFullScreen()
    press(w, Qt.Key.Key_F); press(w, Qt.Key.Key_Escape); assert not w.isFullScreen()
    press(w, Qt.Key.Key_Escape); assert not w.isFullScreen()                                       # nothing to leave
    press(w, Qt.Key.Key_R); assert w.crop_button.isChecked()
    press(w, Qt.Key.Key_Escape); assert not w.crop_button.isChecked()                              # Esc still cancels the crop
    press(w, Qt.Key.Key_F); press(w, Qt.Key.Key_R); assert w.crop_button.isChecked() and w.isFullScreen()
    press(w, Qt.Key.Key_Escape); assert not w.crop_button.isChecked() and w.isFullScreen()         # the crop first
    press(w, Qt.Key.Key_Escape); assert not w.isFullScreen()
    press(w, Qt.Key.Key_F); w.view.setFocus(); w.studio.mask_shortcut('brush'); assert w.view.tool_mode == 'brush'
    QTest.keyClick(w.view, Qt.Key.Key_Escape); assert w.view.tool_mode == '' and w.isFullScreen()   # the tool first
    QTest.keyClick(w.view, Qt.Key.Key_Escape); assert not w.isFullScreen()
    entry = menu_entry(w, '보기·필터', '전체 화면'); assert entry.text().endswith('\tF')
    entry.trigger(); assert w.isFullScreen(); entry.trigger(); assert not w.isFullScreen()
    w.showMaximized(); QApplication.processEvents()
    if w.isMaximized():
        press(w, Qt.Key.Key_F); assert w.isFullScreen(); press(w, Qt.Key.Key_F); assert w.isMaximized() and not w.isFullScreen()
        w.showNormal()


def test_tab_hides_and_shows_the_side_panels_whatever_has_the_focus(window):
    w = window; w.activateWindow(); before = layout(w)
    assert not any(before[0][:2]) and all(before[1][:3]) and before[1][0] >= 230 and before[1][2] >= 290
    for focus in (w.adjustments['exposure'].slider, w.filmstrip, w.view, w.library_button, w.tabs.tool_buttons[0], w.histogram.shadow_button):
        focus.setFocus(); QApplication.processEvents(); assert QApplication.focusWidget() is focus, focus
        tab(w, target=focus)
        assert hidden(w, *SIDES) == [True, True] and hidden(w, *BARS) == [False]*3, focus      # the sides only
        assert w.split.sizes()[0] == 0 and w.split.sizes()[2] == 0
        tab(w, target=QApplication.focusWidget())
        assert layout(w) == before, focus                                                        # exactly as it was, widths too
    tab(w); w.resize(w.width()+100, w.height()); QApplication.processEvents(); tab(w)          # the window grew while the sides were away
    assert hidden(w, *SIDES) == [False, False] and 230 <= w.library_column.width() <= 360 and 290 <= w.edit_column.width() <= 360


def test_shift_tab_hides_all_panels_and_brings_them_back(window):
    w = window; w.activateWindow(); before = layout(w)
    for key in (Qt.Key.Key_Backtab, Qt.Key.Key_Tab):                                           # Windows reports Shift+Tab as Backtab
        w.adjustments['exposure'].slider.setFocus(); QApplication.processEvents()
        QTest.keyClick(QApplication.focusWidget(), key, SHIFT); QApplication.processEvents()
        assert hidden(w, *SIDES, *BARS) == [True]*5 and not w.statusBar().isHidden() and not w.menuBar().isHidden()
        QTest.keyClick(w, key, SHIFT); QApplication.processEvents()
        assert layout(w) == before
    tab(w); tab(w, True); assert hidden(w, *SIDES, *BARS) == [True]*5                        # Tab, then Shift+Tab: everything goes
    tab(w, True); assert layout(w) == before
    tab(w, True); tab(w); assert hidden(w, *SIDES) == [False, False] and hidden(w, *BARS) == [True]*3    # Tab: the sides come back alone
    tab(w, True); assert hidden(w, *SIDES, *BARS) == [True]*5                                  # something was shown: all of it goes
    tab(w, True); assert layout(w) == before
    for text, key in (('양옆 패널 숨기기·보이기', 'Tab'), ('모든 패널 숨기기·보이기', 'Shift+Tab')):
        entry = menu_entry(w, '보기·필터', text); assert entry.text().endswith('\t'+key)
        entry.trigger(); assert hidden(w, *SIDES) == [True, True]; entry.trigger(); assert layout(w) == before


def test_hidden_panels_stay_hidden_when_the_screen_changes(window):
    w = window; w.activateWindow()
    tab(w, True); press(w, Qt.Key.Key_G)
    assert w.stack.currentIndex() == 1 and hidden(w, 'library_toolbar', 'filmstrip', *SIDES, *BARS) == [True]*7
    tab(w, True); assert hidden(w, 'library_toolbar', 'filmstrip') == [False, True]            # the library has its toolbar, not the filmstrip
    press(w, Qt.Key.Key_D); assert hidden(w, 'library_toolbar', 'filmstrip') == [True, False]
    tab(w, True); press(w, Qt.Key.Key_G); press(w, Qt.Key.Key_D); assert hidden(w, 'library_toolbar', 'filmstrip', *SIDES, *BARS) == [True]*7
    tab(w, True); assert hidden(w, 'library_toolbar', 'filmstrip', *SIDES, *BARS) == [True, False, False, False, False, False, False]


def test_tab_and_shift_tab_are_ignored_in_a_text_field(window):
    w = window; w.activateWindow(); before = layout(w)
    def focused(widget):
        widget.show(); widget.setFocus(); QApplication.processEvents(); return QApplication.focusWidget()
    for widget in (QLineEdit(w), QDoubleSpinBox(w), QTextEdit(w), QPlainTextEdit(w)):
        for key, modifier in ((Qt.Key.Key_Tab, NO), (Qt.Key.Key_Backtab, SHIFT), (Qt.Key.Key_Tab, SHIFT)):
            target = focused(widget); assert target is widget or widget.isAncestorOf(target)
            QTest.keyClick(target, key, modifier); QApplication.processEvents()
            assert layout(w) == before and not w.screen_modes.sides and not w.screen_modes.bars, type(widget).__name__
        widget.deleteLater()
    field = QLineEdit(w); other = QLineEdit(w); other.move(0, 60); other.show()
    assert focused(field) is field; tab(w, target=field)
    moved = QApplication.focusWidget(); assert moved is not field and layout(w) == before     # Tab still moves the focus there
    QTest.keyClick(moved, Qt.Key.Key_Backtab, SHIFT); QApplication.processEvents()
    assert QApplication.focusWidget() is field and layout(w) == before                         # and Shift+Tab moves it back
    field.deleteLater(); other.deleteLater()


def test_other_tab_combinations_do_nothing(window):
    w = window; w.activateWindow(); before = layout(w)
    for modifier in (CTRL, Qt.KeyboardModifier.AltModifier, CTRL | SHIFT):
        QTest.keyClick(w.view, Qt.Key.Key_Tab, modifier); QApplication.processEvents()
    assert layout(w) == before and not w.screen_modes.sides and not w.screen_modes.bars


def test_l_is_lights_out_and_l_or_esc_ends_it(window):
    w = window; w.activateWindow(); w.set_view_background('dark_grey'); before = layout(w)
    grey = w.view.backgroundBrush().color().name(); assert grey != '#000000'
    press(w, Qt.Key.Key_L)
    assert w.screen_modes.lights and hidden(w, *SIDES, *BARS, 'library_toolbar') == [True]*6 and w.menuBar().isHidden() and w.statusBar().isHidden()
    assert not w.stack.isHidden() and not w.compare.area.isHidden()                             # the photo stays
    assert w.view.backgroundBrush().color().name() == '#000000' and w.compare.other.backgroundBrush().color().name() == '#000000'
    press(w, Qt.Key.Key_L); assert not w.screen_modes.lights and layout(w) == before and w.view.backgroundBrush().color().name() == grey
    press(w, Qt.Key.Key_L); press(w, Qt.Key.Key_Escape); assert layout(w) == before and w.view.backgroundBrush().color().name() == grey
    assert w.catalog.preference('view_background', 'black') == 'dark_grey'                      # the surround setting was not touched
    press(w, Qt.Key.Key_L); tab(w, True)
    assert hidden(w, *SIDES, *BARS) == [True]*5                                                 # the panel keys change nothing visible now
    press(w, Qt.Key.Key_G); assert hidden(w, 'library_toolbar', 'filmstrip') == [True, True]
    press(w, Qt.Key.Key_D); assert hidden(w, 'library_toolbar', 'filmstrip') == [True, True]
    press(w, Qt.Key.Key_L)
    assert hidden(w, *SIDES, *BARS) == [True]*5 and not w.menuBar().isHidden() and not w.statusBar().isHidden()   # the panels as Shift+Tab left them
    tab(w, True); assert layout(w) == before
    w.compare.start('before'); settled(w); assert not w.compare.bar.isHidden()
    press(w, Qt.Key.Key_L); assert w.compare.bar.isHidden() and not w.compare.panes.isHidden()
    press(w, Qt.Key.Key_L); assert not w.compare.bar.isHidden()
    w.compare.stop()
    entry = menu_entry(w, '보기·필터', '조명 끄기'); assert entry.text().endswith('\tL')
    entry.trigger(); assert w.screen_modes.lights; press(w, Qt.Key.Key_Escape); assert not w.screen_modes.lights
    press(w, Qt.Key.Key_F); press(w, Qt.Key.Key_L); press(w, Qt.Key.Key_Escape)
    assert not w.screen_modes.lights and w.isFullScreen()                                       # one step at a time: the lights, then the full screen
    press(w, Qt.Key.Key_Escape); assert not w.isFullScreen()


def test_number_keys_set_and_clear_the_color_labels(window, tmp_path):
    w = window; w.activateWindow(); ident = w.current_id
    def label(photo=None):
        wait(lambda: not w.library_loading); return w.catalog.photo(photo or ident)['label']
    for key, name in ((Qt.Key.Key_6, '빨강'), (Qt.Key.Key_7, '노랑'), (Qt.Key.Key_8, '초록'), (Qt.Key.Key_9, '파랑')):
        press(w, key); assert label() == name
        press(w, key); assert label() == ''                                                     # the same key again clears it
    press(w, Qt.Key.Key_6); assert label() == '빨강'
    press(w, Qt.Key.Key_9); assert label() == '파랑'                                            # another colour replaces it
    press(w, Qt.Key.Key_9); assert label() == ''
    entry = menu_entry(w, '사진 관리', '색 라벨').menu().actions()[1]                                    # the label menu: Red
    assert entry.text().endswith('\t6'); entry.trigger(); assert label() == '빨강'
    press(w, Qt.Key.Key_6); assert label() == ''
    assert [a.text().split('\t')[-1] for a in menu_entry(w, '사진 관리', '색 라벨').menu().actions()][1:5] == ['6', '7', '8', '9']
    ids = photo_ids(w, tmp_path); w.filmstrip.restore_selection(ids[:2], ids[0]); settled(w)
    w.catalog.update(ids[1], label='빨강'); w.refresh_lists(); wait(lambda: not w.library_loading)
    assert set(w.selected_ids()) == set(ids[:2])
    press(w, Qt.Key.Key_6); assert [label(i) for i in ids] == ['빨강', '빨강', '']              # not every selected photo had it: it is set on all
    press(w, Qt.Key.Key_6); assert [label(i) for i in ids] == ['', '', '']                      # all of them had it: cleared
    press(w, Qt.Key.Key_7); assert [label(i) for i in ids] == ['노랑', '노랑', '']


def test_ctrl_apostrophe_makes_a_virtual_copy_like_the_menu(window):
    w = window; w.activateWindow(); settled(w)
    def copies():
        wait(lambda: not w.library_loading); return w.catalog.db.execute('SELECT COUNT(*) FROM photos WHERE virtual_source IS NOT NULL').fetchone()[0]
    assert copies() == 0
    press(w, Qt.Key.Key_Apostrophe, CTRL); assert copies() == 1
    entry = menu_entry(w, '사진 관리', '가상 사본 만들기'); assert entry.text().endswith('\t'+key_hint("Ctrl+'"))
    entry.trigger(); assert copies() == 2


def test_rotation_keys_turn_the_selected_photos_like_the_menu(window, tmp_path):
    w = window; w.activateWindow(); settled(w)
    press(w, Qt.Key.Key_BracketRight, CTRL); settled(w); assert w.settings['rotation'] == 1
    keyed = deepcopy(w.settings)
    press(w, Qt.Key.Key_BracketLeft, CTRL); settled(w); assert w.settings['rotation'] == 0
    press(w, Qt.Key.Key_BracketLeft, CTRL); settled(w); assert w.settings['rotation'] == 3
    press(w, Qt.Key.Key_BracketRight, CTRL); settled(w); assert w.settings['rotation'] == 0
    menu = build_menu(w); actions = {a.data(): a for a in menu.actions()}
    actions['rotate-right'].trigger(); settled(w); assert w.settings == keyed                    # the menu's turn gives the same photo
    actions['rotate-left'].trigger(); settled(w); assert w.settings['rotation'] == 0
    menu.close()
    ids = photo_ids(w, tmp_path); w.filmstrip.restore_selection(ids[:2], ids[0]); settled(w)
    press(w, Qt.Key.Key_BracketRight, CTRL); settled(w)
    assert [w.catalog.photo(i)['settings']['rotation'] for i in ids] == [1, 1, 0]
    press(w, Qt.Key.Key_BracketLeft, CTRL); settled(w)
    assert [w.catalog.photo(i)['settings']['rotation'] for i in ids] == [0, 0, 0]
    assert key_hint('Ctrl+[') in next(b for b in w.findChildren(QPushButton) if b.text() == tr('시계 방향으로 90° 회전')).toolTip()


@pytest.mark.parametrize('modifiers,button,key', [(CTRL, 'auto_button', 'Ctrl+U'), (CTRL | SHIFT, 'auto_color_button', 'Ctrl+Shift+U')])
def test_ctrl_u_is_auto_tone_and_ctrl_shift_u_auto_color(window, modifiers, button, key):
    w = window; w.activateWindow(); settled(w); plain = deepcopy(w.settings)
    press(w, Qt.Key.Key_U, modifiers); settled(w); keyed = deepcopy(w.settings)
    assert keyed != plain
    w.undo(); settled(w); assert w.settings == plain
    getattr(w, button).click(); settled(w); assert w.settings == keyed                          # the button gives the same result
    assert key_hint(key) in getattr(w, button).toolTip()


def test_the_new_keys_do_nothing_while_typing(window):
    w = window; w.activateWindow(); ident = w.current_id
    keys = [s for s in bound(w) if s.key().toString() in ('J', 'Z', 'F', 'L', '6', '7', '8', '9', "Ctrl+'", 'Ctrl+[', 'Ctrl+]', 'Ctrl+U', 'Ctrl+Shift+U', 'Ctrl+/')]
    assert len(keys) == 14
    for widget in (QLineEdit(w), QDoubleSpinBox(w), QTextEdit(w), QPlainTextEdit(w)):
        widget.show(); widget.setFocus(); QApplication.processEvents()
        for s in keys: s.activated.emit()
        assert not w.isFullScreen() and not w.screen_modes.lights and not w.clipping_button.isChecked() and w.view.fit_mode, type(widget).__name__
        photo = w.catalog.photo(ident)
        assert photo['label'] == '' and photo['settings']['rotation'] == 0 and not w.catalog.db.execute('SELECT 1 FROM photos WHERE virtual_source IS NOT NULL').fetchone()
        widget.deleteLater()
    field = QLineEdit(w); field.show(); field.setFocus(); QApplication.processEvents()
    for key in (Qt.Key.Key_J, Qt.Key.Key_Z, Qt.Key.Key_F, Qt.Key.Key_L, Qt.Key.Key_6, Qt.Key.Key_9):
        QTest.keyClick(field, key)
    assert field.text() == 'jzfl69' and not w.isFullScreen() and not w.screen_modes.lights and w.catalog.photo(ident)['label'] == ''
    field.deleteLater()
    w.view.setFocus(); QApplication.processEvents(); press(w, Qt.Key.Key_J); assert w.clipping_button.isChecked()      # outside a text field they work
