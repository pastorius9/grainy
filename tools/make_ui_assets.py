"""Small control images for the stylesheet (assets/ui): the spin box arrows, at 1x and 2x.

    QT_QPA_PLATFORM=offscreen python tools/make_ui_assets.py
"""
from pathlib import Path
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPolygonF

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'assets'/'ui'
WIDTH, HEIGHT = 9, 6
COLORS = {'': '#aeb6bf', '-off': '#4a525b'}


def arrow(up, color, scale):
    image = QImage(WIDTH*scale, HEIGHT*scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    w, h = WIDTH*scale, HEIGHT*scale
    tip, base = (0.5*scale, h-0.5*scale) if up else (h-0.5*scale, 0.5*scale)
    painter.drawPolygon(QPolygonF([QPointF(w/2, tip), QPointF(w-0.5*scale, base), QPointF(0.5*scale, base)]))
    painter.end()
    return image


def main():
    app = QGuiApplication.instance() or QGuiApplication([])
    OUT.mkdir(parents=True, exist_ok=True)
    for direction in ('up', 'down'):
        for suffix, color in COLORS.items():
            for scale in (1, 2):
                name = f'spin-{direction}{suffix}' + ('@2x' if scale == 2 else '') + '.png'
                arrow(direction == 'up', color, scale).save(str(OUT/name))
                print(OUT/name)


if __name__ == '__main__':
    main()
