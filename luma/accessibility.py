"""macOS accessibility: lists, trees and tables are described as one element each.

When a program reads Grainy's window through macOS accessibility (VoiceOver, or an AI assistant that operates
apps on the Mac), Qt mirrors every list, tree and table as rows and cells. While Qt refreshes that mirror it can
leave a deleted cell in its interface cache: QAccessibleCache::deleteInterface holds an iterator into the cache
across an ObjectDestroyed event, and on macOS handling that event creates elements and re-enters the cache. The
next request for a view's selected items reads the deleted cell and Grainy quits (EXC_BAD_ACCESS in
-[QMacAccessibilityElement accessibilitySelectedChildren]; seen with PySide6 6.11.2 on macOS 26, when a photo is
opened, and at start when the last photo reopens). Qt fixed it in its development branch (QTBUG-149629,
October 2026); the Qt of PySide6 6.11 and 6.12 does not have the fix.

Here the views get Qt's plain widget description instead: a list, tree or table element whose children are the
view's own widgets (viewport, scroll bars), with no element per row or cell. Qt then never creates or deletes
the per-cell entries the bug needs. Every other control (buttons, sliders, fields, tabs) keeps Qt's own
description. Assistive programs still find the views and can use them by pointer and keyboard; they no longer
list the photos and folders one by one.
Remove this once the bundled Qt has the fix (the commit "Accessibility: make the element cache safe against
re-entrancy").
"""
import sys

from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QAbstractItemView, QAccessibleWidget, QWidget

# Qt's interfaces for these classes, and so for their subclasses (QListWidget, QTreeWidget, QTableWidget, the
# photo grid and filmstrip, a combo box's list), keep rows and cells as interfaces without an object of their own.
VIEW_ROLES = {'QListView': QAccessible.Role.List, 'QTreeView': QAccessible.Role.Tree,
              'QTableView': QAccessible.Role.Table}
enabled = False         # views are described whole
_installed = False      # the factory is registered with Qt (it cannot be removed again)


def view_interface(classname, widget):
    """The accessibility factory. Qt asks it once per class of an object it has no interface for yet, the
    most derived class first, and keeps what it returns until the object is destroyed."""
    try:
        role = VIEW_ROLES.get(classname)
        if not enabled or role is None or not isinstance(widget, QWidget):
            return None
        return QAccessibleWidget(widget, role)
    except Exception:       # an exception raised here would stay pending inside Qt's call
        return None


def install(platform=sys.platform):
    """Describe views whole on macOS. Call once the QApplication exists and before any window is made: a view
    that already has an interface keeps it. Returns True when this call switched it on."""
    global enabled, _installed
    if platform != 'darwin' or enabled:
        return False
    if not _installed:
        QAccessible.installFactory(view_interface)
        _installed = True
    enabled = True
    return True


def verify(window):
    """For the self-test: how the window's item views are described; raises if one would still list rows."""
    views = window.findChildren(QAbstractItemView)
    if not enabled:
        return 'qt'
    for view in views:
        iface = QAccessible.queryAccessibleInterface(view)
        name = type(view).__name__
        assert iface is not None and iface.isValid(), f'{name} has no accessible interface'
        assert iface.selectionInterface() is None, f'{name} still lists selected rows to assistive programs'
        assert not iface.interface_cast(QAccessible.InterfaceType.TableInterface), f'{name} still describes rows and cells'
    return f'whole ({len(views)} views)'
