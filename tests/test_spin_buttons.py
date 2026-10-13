from pathlib import Path
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QSpinBox, QStyle, QStyleOptionSpinBox
from test_studio_ui import app

UI = Path(__file__).resolve().parents[1]/'assets'/'ui'


def rects(box):
    option = QStyleOptionSpinBox()
    box.initStyleOption(option)
    return [box.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, part, box)
            for part in (QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown)]


def test_the_number_field_leaves_both_spin_buttons_free(app):
    """Feedback 0.5.88: in the export window the up arrow of 'quality' and 'longest edge' did nothing.
    The Windows 11 style put the two buttons side by side under the number field."""
    from luma.app import configure_application
    configure_application(app)
    assert 'spin-up.png' in app.styleSheet() and '{UI}' not in app.styleSheet()
    for name in ('spin-up', 'spin-down', 'spin-up-off', 'spin-down-off'):
        assert (UI/f'{name}.png').is_file() and (UI/f'{name}@2x.png').is_file()
    box = QSpinBox()
    box.setRange(0, 30000)
    box.setSingleStep(500)
    box.setValue(1000)
    box.resize(box.sizeHint())
    box.show()
    app.processEvents()
    up, down = rects(box)
    field = box.lineEdit().geometry()
    assert not field.intersects(up) and not field.intersects(down) and not up.intersects(down)
    assert up.width() >= 16 and down.width() >= 16
    assert up.bottom() < down.top() and up.left() == down.left()             # stacked on the right edge
    for rect, step in ((up, 500), (down, -500)):
        for point in (rect.center(), QPoint(rect.left()+1, rect.center().y()), QPoint(rect.right()-1, rect.center().y())):
            before = box.value()
            QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=point)
            assert box.value() == before+step
    box.close()
