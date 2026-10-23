"""The keyboard shortcuts: one table (luma/shortcut_list.TABLE) is what is bound, and what the shortcut list shows."""
import json
import re
from pathlib import Path
import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QLabel, QLineEdit, QMenu, QPlainTextEdit, QTextEdit
from test_studio_ui import app, window, wait
from luma.i18n import language, set_language, tr
from luma.shortcut_list import TABLE, GROUPS, ShortcutDialog, written, key_hint

NO, SHIFT = Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier
EXISTING = {'Ctrl+O', 'Ctrl+Shift+E', 'Ctrl+E', 'Ctrl+Z', 'Ctrl+Shift+Z', 'Ctrl+Y', 'Ctrl+Shift+C', 'Ctrl+Shift+V', 'Ctrl+Alt+V',
            '\\', 'C', 'G', 'D', 'R', 'P', 'X', 'U', 'B', '[', ']', 'O', '0', '1', '2', '3', '4', '5'}
NEW = {'W', 'K', 'M', 'Shift+M', 'Q', 'Shift+/'}


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
    assert set(keys) == EXISTING | NEW, 'a key was added or dropped: update this list and the shortcut list texts'
    assert sorted(s.key().toString() for s in bound(w)) == sorted(QKeySequence(k).toString() for k in keys)
    repeats = {s.key().toString(): s.autoRepeat() for s in bound(w)}
    assert not any(repeats[QKeySequence(k).toString()] for k in ('W', 'K', 'M', 'Shift+M', 'Q', 'Shift+/')), 'a held key must not flip a tool or pile up masks'
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
    menu = w.findChild(QMenu, 'settingsMenu')
    entry = next(a for a in menu.actions() if a.text().startswith(tr('단축키 목록…')))
    assert entry.text().endswith('\t?'); entry.trigger()
    assert opened == [w, w]


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
            assert all(shown(key) in texts for key in ('W', 'K', 'M', 'Shift+M', 'Q', '?', 'O', '[ / ]', '0 – 5', 'Ctrl+Shift+Z / Ctrl+Y'))
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
