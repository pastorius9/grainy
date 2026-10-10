from pathlib import Path
import numpy as np
from test_studio_ui import app,window,wait
from luma.engine import develop,export_image,load_image,to_srgb
from luma.library import write_sidecar,read_sidecar
from luma.photo_filter import FILTERS,KEYS
from luma.manager import EDIT_GROUPS


def test_filter_controls_pixels_histogram_undo_sidecar_export(window,tmp_path):
    w=window;s=w.studio;before=w.view.on_screen.copy()
    assert s.photo_filter_choice.count()==17 and tuple(EDIT_GROUPS['컬러 필터'])==KEYS
    s.photo_filter_choice.setCurrentIndex(s.photo_filter_choice.findData('cooling80'))
    wait(lambda:not w.render_running)
    assert s.photo_filter_enabled.isChecked() and not np.allclose(w.view.on_screen,before)
    w.adjustments['photo_filter_density'].slider.setValue(72);w.finish_interaction()
    wait(lambda:not w.render_running)
    np.testing.assert_allclose(w.view.on_screen,develop(w.source,w.settings),atol=1e-6)
    w.undo();wait(lambda:not w.render_running);assert w.settings['photo_filter_density']==25
    w.redo();wait(lambda:not w.render_running);assert w.settings['photo_filter_density']==72
    record=w.catalog.photo(w.current_id);sidecar=tmp_path/'filter.xmp';write_sidecar(record,sidecar)
    restored=read_sidecar(sidecar)['settings'];assert all(restored[k]==w.settings[k] for k in KEYS)
    out=tmp_path/'filtered.tif';export_image(record['path'],out,restored,'TIFF 16-bit')
    actual,_=load_image(out)
    np.testing.assert_allclose(to_srgb(actual),w.view.on_screen,atol=3e-5)
    s.photo_filter_enabled.setChecked(False);wait(lambda:not w.render_running)
    np.testing.assert_allclose(w.view.on_screen,before,atol=1e-6)
    assert w.settings['photo_filter']=='cooling80' and w.settings['photo_filter_density']==72
