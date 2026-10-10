from copy import deepcopy
import cv2
import numpy as np
import pytest
from PIL import Image
from test_studio_ui import app,window,wait
from test_optics import grid_fixture
from luma.engine import develop,export_image,to_srgb
from luma.upright import estimate


def idle(w):
    wait(lambda:not w.jobs and not w.render_running and not w.preview_timer.isActive() and not w.refine_timer.isActive())


@pytest.mark.parametrize('editing',[False,True])
def test_reset_button_removes_auto_level_crop_and_angle_with_one_undo(window,tmp_path,editing):
    w=window;image=grid_fixture()
    tilted=cv2.warpAffine(image,cv2.getRotationMatrix2D((399.5,299.5),-7,1),(800,600),borderValue=(.65,.65,.65))
    path=tmp_path/'tilted.png';Image.fromarray(np.uint8(to_srgb(tilted)*255+.5)).save(path)
    ident=w.catalog.add(path);w.activate(ident);wait(lambda:w.source is not None);idle(w)
    w.tabs.setCurrentIndex(1);w.auto_level_button.click();wait(lambda:w.settings['straighten']!=0);idle(w)
    assert abs(w.settings['straighten']+7)<.2 and w.settings['upright'] is None and w.view.crop_mode
    w.view.accept_crop();idle(w)
    w.set_setting('exposure',.4);w.set_setting('crop',[.1,.15,.9,.85]);w.set_setting('straighten',1.5);w.finish_interaction();idle(w)
    saved=deepcopy(w.settings);history=len(w.catalog.histories(ident));original=w.source.copy()
    if editing:
        w.aspect.setCurrentIndex(1);w.crop_button.click();idle(w);assert w.view.crop_mode
    w.crop_reset_button.click();idle(w)
    expected={**saved,'crop':None,'straighten':0,'upright':None}
    assert w.settings==w.catalog.photo(ident)['settings']==expected
    assert len(w.catalog.histories(ident))==history+1
    assert not w.view.crop_mode and not w.crop_button.isChecked() and w.aspect.currentIndex()==0
    assert w.adjustments['straighten'].slider.value()==0 and '°' not in w.auto_level_note.text()
    np.testing.assert_array_equal(w.source,original)
    np.testing.assert_allclose(w.view.on_screen,develop(original,expected),atol=1e-6)
    import tifffile
    output=tmp_path/'reset.tif';export_image(path,output,expected,'TIFF 16-bit')
    np.testing.assert_array_equal(tifffile.imread(output),np.uint16(w.view.on_screen*65535+.5))
    w.undo();idle(w);assert w.settings==saved
    w.redo();idle(w);assert w.settings==expected
    w.crop_button.click();idle(w);assert w.view.crop_values()==[0,0,1,1]


@pytest.mark.parametrize('mode',['vertical','full'])
def test_crop_reset_keeps_other_geometry_and_tone_edits(window,mode):
    w=window;upright=estimate(grid_fixture(),'level');upright['mode']=mode
    for key,value in dict(upright=upright,rotation=1,flip=True,exposure=.8,crop=[.1,.1,.9,.9],straighten=3).items():w.set_setting(key,value)
    w.finish_interaction();idle(w);saved=deepcopy(w.settings)
    w.crop_reset_button.click();idle(w)
    assert w.settings=={**saved,'crop':None,'straighten':0}


@pytest.mark.parametrize('failure',[False,True])
def test_reset_invalidates_pending_alignment_even_when_settings_were_default(window,monkeypatch,failure):
    w=window;captured=[];w.source=grid_fixture();saved=deepcopy(w.settings)
    with monkeypatch.context() as patch:
        patch.setattr(w,'spawn',lambda work,ready,error:captured.append((work,ready,error)))
        w.auto_level_button.click()
    result=captured[0][0]();w.crop_reset_button.click();idle(w);note=w.auto_level_note.text()
    if failure:captured[0][2]('Outdated analysis failed')
    else:captured[0][1](result)
    idle(w)
    assert w.settings==saved and w.auto_level_note.text()==note and w.auto_level_button.isEnabled()
