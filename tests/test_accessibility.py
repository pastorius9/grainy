"""Assistive programs on macOS see lists, trees and tables as one element each (luma/accessibility.py).

Qt's per-cell description of item views can leave a deleted cell in its cache on macOS, and the next request
for the selected items then quits Grainy. The tests ask for every platform what macOS would get."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PIL import Image
from PySide6.QtCore import Qt,QStringListModel,QTimer
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication,QAbstractItemView,QComboBox,QListWidget,QPushButton,QSlider,QTabBar,
                               QTableWidget,QTreeWidget,QTreeWidgetItem,QWidget)
from luma import accessibility
from luma.photo_browser import PhotoListView
from test_studio_ui import app,window,wait


@pytest.fixture
def host(app):
    widget=QWidget();yield widget
    widget.deleteLater();app.processEvents()


@pytest.fixture
def whole(monkeypatch):
    """Views described whole, as on macOS; the previous state comes back after the test."""
    monkeypatch.setattr(accessibility,'enabled',False)
    assert accessibility.install(platform='darwin')


def views(host):
    grid=PhotoListView(host);grid.setModel(QStringListModel([f'photo {n}' for n in range(30)],grid));grid.selectAll()
    listing=QListWidget(host);listing.addItems(['a','b','c']);listing.selectAll()
    tree=QTreeWidget(host);tree.addTopLevelItems([QTreeWidgetItem([f'folder {n}']) for n in range(5)]);tree.selectAll()
    table=QTableWidget(4,3,host);table.selectAll()
    combo=QComboBox(host);combo.addItems(['one','two'])
    return {'grid':(grid,QAccessible.Role.List),'list':(listing,QAccessible.Role.List),'tree':(tree,QAccessible.Role.Tree),
            'table':(table,QAccessible.Role.Table),'combo list':(combo.view(),QAccessible.Role.List)}


def test_views_are_one_element_without_rows_cells_or_selection(host,whole):
    for name,(view,role) in views(host).items():
        iface=QAccessible.queryAccessibleInterface(view)
        assert iface.role()==role,name
        # What -[QMacAccessibilityElement accessibilitySelectedChildren] reads, and what the macOS plugin mirrors as rows.
        assert iface.selectionInterface() is None,name
        assert not iface.interface_cast(QAccessible.InterfaceType.TableInterface),name
        children=[iface.child(n) for n in range(iface.childCount())]
        assert children and all(child.object() is not None for child in children),name     # widgets, no per-item entries


def test_other_controls_keep_qts_description(host,whole):
    button=QPushButton('Export',host);slider=QSlider(host);tabs=QTabBar(host);tabs.addTab('Basic');tabs.addTab('Masks')
    assert QAccessible.queryAccessibleInterface(button).role()==QAccessible.Role.Button
    assert QAccessible.queryAccessibleInterface(slider).valueInterface() is not None
    bar=QAccessible.queryAccessibleInterface(tabs)
    assert bar.selectionInterface() is not None and bar.child(0).role()==QAccessible.Role.PageTab


def test_other_systems_keep_qts_views(host,monkeypatch):
    monkeypatch.setattr(accessibility,'enabled',False)
    assert not accessibility.install(platform='win32') and not accessibility.install(platform='linux')
    for name,(view,_) in views(host).items():
        assert QAccessible.queryAccessibleInterface(view).selectionInterface() is not None,name


def test_the_factory_never_raises_into_qt(host,monkeypatch):
    monkeypatch.setattr(accessibility,'enabled',True)
    def broken(*args):raise RuntimeError('no interface')
    monkeypatch.setattr(accessibility,'QAccessibleWidget',broken)
    view=PhotoListView(host)
    assert accessibility.view_interface('QListView',view) is None
    assert accessibility.view_interface('QPushButton',view) is None and accessibility.view_interface('QListView',object()) is None


def test_photos_open_while_an_assistive_program_reads_selections(whole,window,tmp_path):
    """The crash happened here: an assistive program asks for the selected items while a photo opens."""
    w=window
    assert accessibility.verify(w).startswith('whole')
    ids=[w.current_id]
    for n in range(2):
        path=tmp_path/f'photo-{n}.png';Image.new('RGB',(600,400),(70+n*50,110,140)).save(path);ids.append(w.catalog.add(path))
    w.refresh_lists();wait(lambda:not w.library_loading)
    asked=[];listed=[]
    def ask():
        for widget in QApplication.allWidgets():
            if not widget.isVisible():continue
            iface=QAccessible.queryAccessibleInterface(widget)
            if iface is None or not iface.isValid():continue
            asked.append(1)
            selection=iface.selectionInterface()
            if selection is not None and isinstance(widget,QAbstractItemView):listed.append(type(widget).__name__)
    timer=QTimer();timer.timeout.connect(ask);QAccessible.setActive(True);timer.start(5)
    try:
        for ident in ids[1:]+ids[:1]:
            w.set_mode(1);QApplication.processEvents()
            model=w.grid.model();index=model.index(model.positions[ident]);w.grid.scrollTo(index);QApplication.processEvents()
            QTest.mouseClick(w.grid.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,w.grid.visualRect(index).center())
            wait(lambda:w.current_id==ident and w.source is not None and not w.render_running and not w.jobs)
    finally:
        timer.stop();QAccessible.setActive(False)
    assert asked and not listed,sorted(set(listed))
