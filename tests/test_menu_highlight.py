"""Menus mark the entry under the pointer."""
from PySide6.QtWidgets import QApplication
from test_studio_ui import app, window
from luma.photo_actions import build_menu


def test_the_entry_under_the_pointer_is_marked(window):
    w = window; menu = build_menu(w)
    try:
        menu.popup(w.mapToGlobal(w.rect().center())); QApplication.processEvents()
        entries = [a for a in menu.actions() if not a.isSeparator() and a.isEnabled() and not a.menu()]
        under, other = entries[0], entries[1]
        colour = lambda action: menu.grab().toImage().pixelColor(menu.actionGeometry(action).left()+4, menu.actionGeometry(action).center().y()).name()
        plain = colour(other)
        assert colour(under) == plain
        menu.setActiveAction(under); QApplication.processEvents()
        assert colour(under) != plain and colour(other) == plain
        menu.setActiveAction(other); QApplication.processEvents()
        assert colour(under) == plain and colour(other) != plain
    finally:
        menu.close()
