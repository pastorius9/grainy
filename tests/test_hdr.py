from copy import deepcopy
import numpy as np
import pytest
import tifffile
from luma.engine import defaults,develop,to_linear,to_srgb,load_image,export_image
from luma.hdr import histogram,sdr_rendition,visualize
from luma.render_cache import DevelopmentCache
from test_studio_ui import app,window,wait


def ramp():
    return np.repeat(np.linspace(.01,16,320,dtype=np.float32)[None,:,None],3,axis=2).repeat(8,axis=0)


def test_hdr_retains_distinct_values_above_white_and_sdr_is_unchanged():
    source=ramp();before=source.copy();s=defaults()
    sdr=develop(source,s,output_space=None)
    assert sdr.max()==1
    s['hdr']=True;hdr=develop(source,s,output_space=None)
    np.testing.assert_allclose(to_linear(hdr),source,rtol=3e-6,atol=2e-6)
    np.testing.assert_array_equal(source,before)
    assert len(np.unique(hdr[hdr>1]))>250
    bins=histogram(hdr);assert sum(bins[0][64:])>sum(bins[0][:64])
    assert visualize(hdr).shape==hdr.shape


def test_hdr_controls_cache_sdr_rendition_and_limit_are_separate():
    source=ramp();s={**defaults(),'hdr':True};cache=DevelopmentCache()
    hdr=develop(source,s,output_space=None,cache=cache)
    s.update(sdr_exposure=-1,sdr_compression=40)
    np.testing.assert_array_equal(develop(source,s,output_space=None,cache=cache),hdr)
    preview=develop(source,s,cache=cache)
    np.testing.assert_allclose(preview,sdr_rendition(hdr,s),atol=1e-7)
    assert preview.min()>=0 and preview.max()<=1
    assert np.all(np.diff(preview[0,:,0])>=-2e-7)
    s['hdr_limit']=2
    limited=develop(source,s,output_space=None,cache=cache)
    assert to_linear(limited).max()==pytest.approx(4,abs=1e-5)
    s.update(hdr_limit=4,hdr_brightness=-100)
    darker=to_linear(develop(source,s,output_space=None,cache=cache))
    np.testing.assert_allclose(darker[source<=1],source[source<=1],rtol=3e-6)
    assert darker.max()==pytest.approx(4,abs=1e-5)
    s['hdr']=False
    assert develop(source,s,output_space=None,cache=cache).max()==1


def test_color_detail_curve_and_local_edits_do_not_flatten_hdr():
    source=ramp();s={**defaults(),'hdr':True,'saturation':-30,'clarity':10,'noise_color':10}
    s['curve']=[[0,0],[.5,.45],[1,1]]
    hdr=develop(source,s,output_space=None)
    assert np.unique(hdr[hdr>1]).size>100
    # An all-frame local exposure must preserve and increase HDR values.
    from luma.processing import _local_region
    edited=_local_region(to_srgb(source/2),np.ones(source.shape[:2],np.float32),{'hdr':True,'exposure':1})
    np.testing.assert_allclose(to_linear(edited),source,rtol=4e-6,atol=1e-6)


@pytest.mark.parametrize('space',['sRGB','ProPhoto'])
def test_hdr_tiff_roundtrip_preserves_linear_highlights_and_sdr_export(tmp_path,space):
    source=ramp();path=tmp_path/'source.tif';tifffile.imwrite(path,source,photometric='rgb')
    s={**defaults(),'hdr':True,'working_space':space}
    output=tmp_path/'hdr.tif';export_image(path,output,s,format='TIFF HDR 32-bit')
    decoded,info=load_image(output,working_space=space)
    working,_=load_image(path,working_space=space)
    expected=to_linear(develop(working,s,output_space=None))
    np.testing.assert_allclose(decoded,expected,rtol=3e-4,atol=3e-4)
    assert decoded.max()>10 and tifffile.imread(output).dtype==np.float32
    with pytest.raises(FileExistsError):export_image(path,output,s,format='TIFF HDR 32-bit')
    s['hdr']=False
    with pytest.raises(ValueError):export_image(path,tmp_path/'invalid.tif',s,format='TIFF HDR 32-bit')
    assert not (tmp_path/'invalid.tif').exists()


def test_hdr_mode_ui_live_histogram_visualization_persistence_and_undo(window):
    w=window;original=deepcopy(w.settings)
    w.hdr_button.click();w.set_setting('exposure',3);w.finish_interaction()
    wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert w.settings['hdr'] and w.hdr_panel.isVisible() and w.histogram.hdr
    assert w.view.hdr_pixels.max()>1 and w.view.on_screen.max()<=1
    np.testing.assert_array_equal(w.histogram.bins,histogram(w.view.hdr_pixels))
    saved=deepcopy(w.settings);before=len(w.catalog.histories(w.current_id))
    w.hdr_visualize.click();wait(lambda:not w.render_running)
    assert w.settings==saved and len(w.catalog.histories(w.current_id))==before
    w.adjustments['hdr_limit'].value.setValue(1);w.finish_interaction()
    wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert to_linear(w.view.hdr_pixels).max()<=2.00001
    assert w.catalog.photo(w.current_id)['settings']['hdr_limit']==1
    w.undo();wait(lambda:not w.render_running)
    assert w.adjustments['hdr_limit'].value.value()==4
    w.reset_edits();wait(lambda:not w.render_running)
    assert w.settings==original and not w.histogram.hdr and w.view.hdr_pixels is None
