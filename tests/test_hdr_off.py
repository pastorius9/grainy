import numpy as np
import pytest
from PIL import Image
from test_studio_ui import app, wait
from luma import engine
from luma.engine import defaults, develop, normalized, to_linear


@pytest.fixture
def shipped(monkeypatch, hdr_feature):
    monkeypatch.setattr(engine, 'HDR_FEATURE', False)


def test_photos_saved_with_hdr_develop_as_ordinary_photos(shipped):
    ramp = to_linear(np.repeat(np.linspace(0, 1, 16, dtype=np.float32)[None, :, None], 3, axis=2).repeat(4, axis=0))
    stored = {**defaults(), 'hdr': True, 'sdr_compression': 75., 'exposure': .5}
    assert normalized(stored)['hdr'] is False
    assert np.array_equal(develop(ramp, stored), develop(ramp, {**stored, 'hdr': False}))
    assert abs(float(develop(ramp, {**defaults(), 'hdr': True})[0, -1, 0])-1) < 1e-4       # white stays white


def test_hdr_controls_and_export_format_are_gone(app, tmp_path, shipped):
    from luma.app import MainWindow, ExportDialog
    from luma.quick_export import PREFERENCE, previous
    path = tmp_path/'a.png'; Image.fromarray(np.full((40, 60, 3), 200, np.uint8)).save(path)
    w = MainWindow(tmp_path/'data')
    try:
        ident = w.catalog.add(path, settings={**defaults(), 'hdr': True}); w.refresh_lists(); w.activate(ident); w.show()
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        assert w.hdr_button.isHidden() and w.hdr_panel.isHidden() and not w.settings['hdr']
        w.catalog.save_preference(PREFERENCE, dict(folder=str(tmp_path), format='TIFF HDR 32-bit', quality=90, longest=0,
                                                   color_space='sRGB', keep_metadata=True, naming='{stem}'))
        assert previous(w.catalog) is None
        dialog = ExportDialog(1, w)
        formats = [dialog.format.itemData(i) for i in range(dialog.format.count())]
        assert 'TIFF HDR 32-bit' not in formats and 'JPEG' in formats and dialog.format.currentIndex() >= 0
        dialog.deleteLater()
    finally:
        wait(lambda: not w.render_running); w.close()
