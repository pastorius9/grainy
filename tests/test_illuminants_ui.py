"""Actual LibRaw, Qt, catalog, offline and export path for triple illuminants."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import hashlib
import numpy as np
import tifffile
from PySide6.QtWidgets import QApplication
from luma.app import MainWindow,configure_application
from luma import engine,rawcolor
from test_raw_ui import wait
from test_illuminants import write_triple_dng,triple_data,spectrum_payload


def test_triple_profile_temperature_offline_undo_export_and_reopen(tmp_path):
    path=write_triple_dng(tmp_path/'three-lights.dng')
    original_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    app=QApplication.instance() or QApplication([]);configure_application(app)
    w=MainWindow(tmp_path/'catalog');errors=[];w.show_error=errors.append
    try:
        settings=engine.defaults();settings['raw_mode']='as_shot'
        ident=w.catalog.add(path,settings);w.refresh_lists();w.activate(ident);w.show()
        wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running and not w.library_loading)
        profile=tmp_path/'three.dcp'
        profile.write_bytes(triple_data([(52529,3,[255]),(52535,7,spectrum_payload()),(50932,2,'Luma fixture\0')]))
        w.studio.apply_dcp(profile);wait(lambda:not w.render_running)
        saved=w.settings['dcp_profile'];profile.unlink();buffer=w.source
        for name,value in [('raw_kelvin',3200),('raw_tint',25)]:
            w.adjustments[name].slider.setValue(value);w.finish_interaction();wait(lambda:not w.render_running)
        assert w.source is buffer
        before=w.view.on_screen.copy()
        w.undo();wait(lambda:not w.render_running);w.redo();wait(lambda:not w.render_running)
        np.testing.assert_array_equal(w.view.on_screen,before)
        assert w.catalog.photo(ident)['settings']['dcp_profile']==saved
        export=tmp_path/'three.tif';engine.export_image(path,export,w.settings,'TIFF 16-bit')
        np.testing.assert_array_equal(tifffile.imread(export),np.uint16(engine.develop(w.source,w.settings)*65535+.5))
        w.manager.smart_previews();preview=w.catalog.directory/'previews'/f'{ident}.npz'
        wait(lambda:preview.is_file() and w.pool.activeThreadCount()==0)
        offline=path.with_suffix('.offline');path.rename(offline)
        w.activate(ident,force=True);wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
        np.testing.assert_allclose(w.view.on_screen,before,atol=5e-4)
        assert w.catalog.photo(ident)['info']['offline_preview']
        w.close();app.processEvents()
        w=MainWindow(tmp_path/'catalog');w.show_error=errors.append;w.activate(ident);w.show()
        wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
        assert w.settings['dcp_profile']==saved
        np.testing.assert_allclose(w.view.on_screen,before,atol=5e-4)
        assert hashlib.sha256(offline.read_bytes()).hexdigest()==original_hash
        assert not errors,errors
    finally:
        w.close();app.processEvents()
