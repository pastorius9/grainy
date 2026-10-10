from pathlib import Path
from PIL import Image
from test_studio_ui import app

BRAND = Path(__file__).resolve().parents[1]/'assets'/'brand'


def test_icon_set_has_every_windows_size_and_the_app_uses_it(app):
    icon = Image.open(BRAND/'grainy.ico')
    assert {(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)} <= set(icon.info['sizes'])
    svg = (BRAND/'grainy-logo.svg').read_text(encoding='utf-8')
    assert '<text' not in svg and '<path d="M' in svg                      # outlines, no font needed
    from luma.app import configure_application
    configure_application(app)
    assert not app.windowIcon().isNull()
    assert 'grainy.ico' in (Path(__file__).resolve().parents[1]/'Build Grainy.ps1').read_text(encoding='utf-8-sig')
