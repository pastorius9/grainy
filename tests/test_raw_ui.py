import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from types import SimpleNamespace
from pathlib import Path
import json
import numpy as np
import pytest
import rawpy
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop,QTimer
from luma.app import MainWindow,configure_application
from luma import engine,rawcolor,dcp
from test_rawcolor import make_dcp


def wait(predicate,timeout=20000):
    loop=QEventLoop();timer=QTimer();timer.setInterval(10);timer.timeout.connect(lambda:loop.quit() if predicate() else None)
    end=QTimer();end.setSingleShot(True);end.timeout.connect(loop.quit);timer.start();end.start(timeout)
    if not predicate():loop.exec()
    timer.stop();end.stop();assert predicate()


@pytest.fixture
def window(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([]);configure_application(app)
    calls=[]
    class Raw:
        num_colors=3;color_desc=b'RGBG';sizes=SimpleNamespace(width=150,height=100,flip=0)
        camera_whitebalance=[2,1,1.5,1];daylight_whitebalance=[1.8,1,1.4,0]
        rgb_xyz_matrix=np.vstack([np.eye(3),np.zeros(3)])
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def postprocess(self,**kwargs):
            calls.append(kwargs)
            y,x=np.mgrid[:100,:150]
            a=np.stack([.04+x/1500,.04+y/1500,np.full_like(x,.08,dtype=float)],-1)
            return np.uint16(a*65535)
    monkeypatch.setattr(rawpy,'imread',lambda _:Raw())
    monkeypatch.setattr(engine,'read_metadata',lambda _:dict(camera='Test Camera',make='Test'))
    source=tmp_path/'test.NEF';source.write_bytes(b'Luma isolated RAW decoder fixture')
    w=MainWindow(tmp_path/'data');w.test_errors=[];w.show_error=lambda error:w.test_errors.append(str(error))
    ident=w.catalog.add(source);w.refresh_lists();w.activate(ident);w.show()
    wait(lambda:w.source is not None and not w.render_running and not w.library_loading)
    yield w,calls,source
    wait(lambda:not w.render_running);w.close();app.processEvents()
    assert not w.test_errors,'\n'.join(e.splitlines()[-1] for e in w.test_errors)


def test_raw_mode_profile_temperature_undo_and_persistence(window,tmp_path):
    w,calls,path=window;studio=w.studio
    studio.raw_mode.setCurrentIndex(studio.raw_mode.findData('as_shot'))
    wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    source=w.source;count=len(calls);before=w.view.on_screen.copy()
    assert calls[-1]['user_wb']==[1.,1.,1.,1.]
    studio.raw_mode.setCurrentIndex(studio.raw_mode.findData('custom'))
    w.adjustments['raw_kelvin'].slider.setValue(3500);w.finish_interaction()
    wait(lambda:not w.render_running)
    assert len(calls)==count and w.source is source and w.settings['raw_mode']=='custom'
    assert not np.allclose(before,w.view.on_screen)
    profile=tmp_path/'test.dcp';profile.write_bytes(make_dcp([(50964,12,np.diag(dcp.D50).ravel()),(51109,5,[1,2])]))
    studio.apply_dcp(profile);wait(lambda:not w.render_running)
    saved=w.settings['dcp_profile'];assert saved and len(calls)==count
    profile.unlink();w.undo();wait(lambda:not w.render_running)
    assert w.settings['dcp_profile'] is None
    w.redo();wait(lambda:not w.render_running)
    assert w.settings['dcp_profile']==saved
    assert w.catalog.photo(w.current_id)['settings']['dcp_profile']==saved
    output=tmp_path/'export.tif'
    engine.export_image(path,output,w.settings,'TIFF 16-bit')
    import tifffile
    actual=tifffile.imread(output)/65535
    expected=engine.develop(w.source,w.settings)
    np.testing.assert_allclose(actual,expected,atol=1/65535)
    ident=w.current_id;w.activate(ident,force=True)
    wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    np.testing.assert_allclose(w.view.on_screen,expected,atol=1e-6)


def test_native_offline_preview_and_camera_sample(window):
    w,calls,path=window
    w.studio.raw_mode.setCurrentIndex(w.studio.raw_mode.findData('as_shot'))
    wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    w.set_setting('temperature',20);w.commit();w.render();wait(lambda:not w.render_running)
    w.sample_wb(.5,.5);wait(lambda:not w.render_running)
    assert w.settings['raw_mode']=='sample' and w.settings['raw_neutral']
    assert w.settings['temperature']==0 and w.settings['tint']==0
    w.manager.smart_previews()
    preview=w.catalog.directory/'previews'/f'{w.current_id}.npz'
    wait(lambda:preview.is_file() and w.pool.activeThreadCount()==0)
    expected=w.view.on_screen.copy();path.rename(path.with_suffix('.offline'))
    w.activate(w.current_id,force=True)
    wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    assert w.catalog.photo(w.current_id)['info']['offline_preview']
    np.testing.assert_allclose(w.view.on_screen,expected,atol=.0005)
    w.adjustments['raw_kelvin'].slider.setValue(4500);w.finish_interaction();wait(lambda:not w.render_running)
    assert w.settings['raw_mode']=='custom'
    w.studio.raw_mode.setCurrentIndex(w.studio.raw_mode.findData('legacy'))
    wait(lambda:w.source is not None and not isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    w.studio.raw_mode.setCurrentIndex(w.studio.raw_mode.findData('as_shot'))
    wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)


def test_different_camera_profile_rejected_before_edit(window,tmp_path):
    w,_,_=window;old=w.settings.copy()
    p=tmp_path/'other.dcp';p.write_bytes(make_dcp([(50708,2,'Other Camera\0')]))
    with pytest.raises(ValueError,match='모델'):w.studio.apply_dcp(p)
    assert w.settings==old


def test_profile_browser_filters_favorites_and_applies_pinned_record(window,tmp_path,monkeypatch):
    from luma.profile_dialog import ProfileDialog
    from luma import profile_library
    from PySide6.QtWidgets import QDialog
    w,_,_=window;root=tmp_path/'profiles';root.mkdir()
    (root/'good.dcp').write_bytes(make_dcp([(50936,2,'Good Profile\0')]))
    (root/'other.dcp').write_bytes(make_dcp([(50708,2,'Other Camera\0')]))
    monkeypatch.setattr(profile_library,'profile_roots',lambda *a:[root])
    dialog=ProfileDialog(w,w.catalog.photo(w.current_id)['info']);dialog.show()
    wait(lambda:bool(dialog.rows) and not dialog.scanning)
    assert dialog.list.count()==1 and dialog.apply_button.isEnabled()
    dialog.toggle_favorite();assert len(w.catalog.preference('dcp_favorites'))==1
    dialog.favorites_only.setChecked(True);assert dialog.list.count()==1
    dialog.search.setText('missing');assert dialog.list.count()==0 and not dialog.apply_button.isEnabled()
    dialog.search.clear();dialog.choose();assert dialog.result()==QDialog.DialogCode.Accepted
    w.studio.apply_dcp_record(dialog.record)
    wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    assert w.settings['dcp_profile']['name']=='Good Profile'


def test_raw_default_dialog_new_import_and_explicit_apply_undo(window,tmp_path,monkeypatch):
    from luma.profile_dialog import RawDefaultsDialog
    from luma import raw_defaults as rd
    from PIL import Image
    w,_,path=window
    monkeypatch.setattr(engine,'read_metadata',lambda _:dict(camera='Test Camera',make='Test',iso=800,serial='000123'))
    # Save a model default with a real pinned profile and a tone adjustment.
    p=tmp_path/'default.dcp';p.write_bytes(make_dcp([(50964,12,np.diag(dcp.D50).ravel())]))
    w.studio.apply_dcp(p);wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    w.set_setting('exposure',.25);w.set_setting('noise_luma',15);w.commit();w.render();wait(lambda:not w.render_running)
    dialog=RawDefaultsDialog(w);dialog.groups['빛'].setChecked(True);dialog.save_rule();dialog.close()
    rule=w.catalog.preference(rd.PREFERENCE)[0];assert rule['scope']=='model'
    p.unlink();first=w.current_id
    w.set_setting('exposure',-.5);w.set_setting('crop',[.1,.1,.8,.8]);w.commit();w.render();wait(lambda:not w.render_running)
    old=w.settings.copy()
    second=tmp_path/'new.NEF';second.write_bytes(path.read_bytes());w.import_paths([str(second)])
    wait(lambda:not w.import_busy and not w.import_scans and w.catalog.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]==2)
    new_id=next(r['id'] for r in w.catalog.photos() if r['id']!=first);photo=w.catalog.photo(new_id)
    assert photo['settings']['exposure']==.25 and photo['settings']['noise_luma']==15 and photo['settings']['crop'] is None
    assert photo['settings']['dcp_profile']==rule['values']['dcp_profile']
    assert not w.catalog.histories(new_id) and photo['info']['raw_default_applied']
    assert w.catalog.photo(first)['settings']==old
    source,_=engine.load_image(second,240,raw_options=photo['settings'])
    expected=engine.develop(source,photo['settings'])
    from luma.preview_store import inspect
    wait(lambda:not inspect(w.catalog.directory,new_id)['rebuild'])
    thumb=np.asarray(Image.open(w.catalog.thumbs/f'{new_id}.jpg'))/255
    assert np.mean(np.abs(thumb-expected))<.01
    w.activate(new_id);wait(lambda:isinstance(w.source,rawcolor.CameraSource) and not w.render_running)
    assert w.catalog.photo(new_id)['info']['raw_default_applied']
    w.activate(first);wait(lambda:w.source is not None and not w.render_running)
    dialog=RawDefaultsDialog(w);dialog.apply_current();dialog.close();wait(lambda:not w.render_running)
    assert w.settings['exposure']==.25 and w.settings['crop']==old['crop']
    w.undo();wait(lambda:not w.render_running);assert w.settings==old


def test_closed_profile_scan_cannot_update_dialog(window,tmp_path,monkeypatch):
    from luma.profile_dialog import ProfileDialog
    from luma import profile_library
    w,_,_=window
    monkeypatch.setattr(profile_library,'profile_roots',lambda *a:[tmp_path/'empty'])
    dialog=ProfileDialog(w,{});dialog.show();dialog.refresh();dialog.reject()
    wait(lambda:w.pool.activeThreadCount()==0)
    assert not dialog.active and dialog.cancel.is_set() and not dialog.rows


def test_raw_default_dialog_does_not_load_entire_selection(window,monkeypatch):
    from luma.profile_dialog import RawDefaultsDialog
    w,_,_=window;reads=[];original=w.catalog.photo
    def read(ident):reads.append(ident);return original(ident)
    monkeypatch.setattr(w,'selected_ids',lambda:list(range(1,100001)))
    monkeypatch.setattr(w.catalog,'photo',read)
    dialog=RawDefaultsDialog(w)
    assert reads==[w.current_id] and len(dialog.photo_ids)==100000
    dialog.close()


def test_unreadable_raw_says_what_to_do_instead_of_the_decoder_error():
    from luma.app import import_reason
    from luma.i18n import set_language
    decoder="Traceback (most recent call last):\n  File \"x.py\", line 1\nrawpy._rawpy.LibRawFileUnsupportedError: b'Unsupported file format or not RAW file'"
    nikon=import_reason('/photos/DSC_0001.NEF',decoder)
    assert '무손실 압축' in nikon and 'DNG' in nikon and 'LibRaw' not in nikon
    assert import_reason('/photos/a.CR3',decoder)=='지원하지 않는 RAW 형식입니다.'
    assert import_reason('/photos/a.jpg','Traceback\nOSError: cannot identify image file')=='OSError: cannot identify image file'
    set_language('en')
    try:assert 'Lossless compression' in import_reason('/photos/DSC_0001.nef',decoder)
    finally:set_language('ko')
