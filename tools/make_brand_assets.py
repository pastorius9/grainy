"""Grainy logo and icon set (assets/brand): the 'Gr' tile in Century Gothic Bold, monochrome.

The letters are converted to outlines, so the SVG and bitmaps do not depend on installed fonts.
Sizes up to 32 px drop the inner border and enlarge the letters so they stay legible.

    QT_QPA_PLATFORM=offscreen QT_QPA_FONTDIR=C:/Windows/Fonts python tools/make_brand_assets.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'assets'/'brand'
TILE, BORDER, INK = '#0d0d0d', '#5e5e5e', '#f5f5f5'
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def letters():
    """'Gr' outline in a 1000-unit box, centred on the letters' own bounds."""
    from PySide6.QtGui import QFont, QPainterPath, QTransform
    font = QFont('Century Gothic', 200); font.setBold(True)
    if not font.exactMatch() and 'Century Gothic' not in font.family():
        raise SystemExit('Century Gothic is required to build the logo outlines.')
    path = QPainterPath(); path.addText(0, 0, font, 'Gr')
    box = path.boundingRect()
    return path.translated(-box.center()), box.width(), box.height()


def svg_path(path, scale, cx, cy):
    parts = []
    for i in range(path.elementCount()):
        e = path.elementAt(i); x, y = cx+e.x*scale, cy+e.y*scale
        if e.isMoveTo(): parts.append(f'M{x:.2f},{y:.2f}')
        elif e.isLineTo(): parts.append(f'L{x:.2f},{y:.2f}')
        elif e.isCurveTo():
            c2, end = path.elementAt(i+1), path.elementAt(i+2)
            parts.append(f'C{x:.2f},{y:.2f} {cx+c2.x*scale:.2f},{cy+c2.y*scale:.2f} {cx+end.x*scale:.2f},{cy+end.y*scale:.2f}')
    return ' '.join(parts)+' Z'


def layout(size):
    """(letter width as a fraction of the tile, border width in px or 0) for one icon size."""
    if size <= 32: return .74, 0
    return .56, max(2, round(size*.02))


def draw(size, path, width):
    from PySide6.QtGui import QImage, QPainter, QColor, QPen, QTransform
    from PySide6.QtCore import Qt, QRectF
    img = QImage(size, size, QImage.Format.Format_ARGB32); img.fill(QColor(TILE))
    p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    fraction, border = layout(size)
    if border:
        inset = size*.043+border/2
        pen = QPen(QColor(BORDER), border); pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(inset, inset, size-2*inset, size-2*inset))
    scale = size*fraction/width
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(INK))
    p.drawPath(QTransform().translate(size/2, size/2).scale(scale, scale).map(path))
    p.end()
    return img


def main():
    from PySide6.QtWidgets import QApplication
    from PIL import Image
    import io
    app = QApplication.instance() or QApplication([])
    OUT.mkdir(parents=True, exist_ok=True)
    path, width, height = letters()
    scale = 1024*.56/width
    border = round(1024*.02); inset = 1024*.043+border/2
    (OUT/'grainy-logo.svg').write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">'
        f'<rect width="1024" height="1024" fill="{TILE}"/>'
        f'<rect x="{inset:.2f}" y="{inset:.2f}" width="{1024-2*inset:.2f}" height="{1024-2*inset:.2f}" fill="none" stroke="{BORDER}" stroke-width="{border}"/>'
        f'<path d="{svg_path(path, scale, 512, 512)}" fill="{INK}" fill-rule="nonzero"/></svg>\n', encoding='utf-8')
    images = {}
    for size in ICO_SIZES+(512, 1024):
        img = draw(size, path, width)
        buffer = io.BytesIO(); from PySide6.QtCore import QBuffer, QIODevice
        qb = QBuffer(); qb.open(QIODevice.OpenModeFlag.WriteOnly); img.save(qb, 'PNG')
        images[size] = Image.open(io.BytesIO(bytes(qb.data()))).convert('RGBA')
    images[1024].save(OUT/'grainy-logo-1024.png'); images[256].save(OUT/'grainy-icon-256.png')
    largest = images[256]
    largest.save(OUT/'grainy.ico', format='ICO', sizes=[(s, s) for s in ICO_SIZES],
                 append_images=[images[s] for s in ICO_SIZES if s != 256])
    print('wrote', ', '.join(sorted(p.name for p in OUT.iterdir())))


if __name__ == '__main__':
    main()
