"""Physical filter scale, cropped mask boundaries, caches and preview consumers."""
from copy import deepcopy
import json
import numpy as np
import pytest
from PIL import Image
from luma.engine import defaults,develop,resize_float,to_linear,load_image
from luma.processing import _local_region
from luma.render_cache import DevelopmentCache
from luma.catalog import Catalog
from luma import preview_store,raw_defaults


def photograph(height=384,width=576):
    y,x=np.mgrid[:height,:width].astype(np.float32)
    luminance=.35+.1*x/width+.06*np.sin(x*.085)*np.cos(y*.063)
    luminance+=(x>width*.43)*.16-(y>height*.64)*.13
    rgb=np.stack([luminance+.03*np.sin(x*.6),luminance,luminance+.04*np.cos(y*.4)],-1)
    rgb+=np.random.default_rng(41).normal(0,.018,rgb.shape).astype(np.float32)
    return np.clip(rgb,.02,.98)


@pytest.mark.parametrize('scale',[.5,.25])
@pytest.mark.parametrize('edit',[
    dict(texture=70),dict(texture=-70),dict(sharpen=85,sharpen_radius=2.1,sharpen_detail=100),
    dict(sharpen=65,sharpen_radius=1.5,sharpen_mask=55),dict(noise_luma=80),dict(noise_color=80)])
def test_reduced_detail_effect_is_closer_to_full_render(edit,scale):
    source=to_linear(photograph());size=source.shape[1],source.shape[0];longest=round(max(size)*scale)
    small=resize_float(source,longest);settings={**defaults(),**edit}
    full_effect=resize_float(develop(source,settings),longest)-resize_float(develop(source,defaults()),longest)
    neutral=develop(small,defaults())
    before=develop(small,settings)-neutral
    actual=develop(small,settings,original_size=size)-neutral
    # Independent full-image reference, rather than checking the radius arguments.
    if edit.get('noise_color'):
        # The colour-guided filter depends little on its radius: both are close, the scaled one closer.
        assert np.mean(abs(actual-full_effect))<min(np.mean(abs(before-full_effect)),np.mean(abs(full_effect))*.05)
    else:
        assert np.mean(abs(actual-full_effect))<np.mean(abs(before-full_effect))*.7


@pytest.mark.parametrize('scale',[1.,.7,.5,.25,.08,.01])
@pytest.mark.parametrize('bounds',[(0,20,0,30),(33,68,25,89)])
def test_scaled_mask_halo_matches_full_frame_and_preserves_outside(scale,bounds):
    image=photograph(96,128);before=image.copy();mask=np.zeros(image.shape[:2],np.float32)
    y0,y1,x0,x1=bounds;mask[y0:y1,x0:x1]=.65
    edit=dict(clarity=45,texture=65,dehaze=35,sharpen=70,noise_luma=50,noise_color=65)
    full=_local_region(image,np.ones_like(mask),edit,pixel_scale=scale)
    actual=_local_region(image,mask,edit,pixel_scale=scale)
    # 3e-6: chroma_guided's region-size dependent rounding (see test_mask_tools partial controls).
    np.testing.assert_allclose(actual,image*(1-mask[...,None])+full*mask[...,None],atol=3e-6)
    np.testing.assert_array_equal(actual[mask==0],image[mask==0])
    np.testing.assert_array_equal(image,before)


def test_scale_change_invalidates_detail_and_following_range_mask_not_color(monkeypatch):
    # CPU stage cache; with the GPU the colour result stays on the device (test_native_gpu: resident tests).
    monkeypatch.setenv('GRAINY_GPU','0')
    source=to_linear(photograph(150,220));original=source.copy();cache=DevelopmentCache()
    settings={**defaults(),**dict(texture=65,sharpen=80,sharpen_radius=2.1,noise_color=50)}
    settings['masks']=[dict(components=[dict(type='luma',range=[.4,.55],softness=.01)],
        adjustments=dict(clarity=55,texture=60,noise_luma=50))]
    develop(source,settings,cache=cache)
    color=cache.entries['color'][1];masks=[];results=[]
    for size in [(880,600),(440,300),(220,150),(880,600)]:
        result=develop(source,settings,cache=cache,original_size=size,on_mask=lambda i,m:masks.append(m.copy()))
        np.testing.assert_array_equal(result,develop(source,settings,original_size=size))
        assert cache.entries['color'][1] is color
        results.append(result)
    np.testing.assert_array_equal(results[0],results[-1])
    assert not np.array_equal(results[0],results[2])
    assert not np.array_equal(masks[0],masks[2])
    np.testing.assert_array_equal(source,original)


def test_original_size_uses_uncropped_orientation_and_defaults_keep_full_pixels():
    source=to_linear(photograph(151,221));settings={**defaults(),**dict(texture=65,sharpen=40,
        sharpen_mask=50,noise_color=25,defringe=30,dehaze=20,clarity=15,rotation=1,crop=[.2,.1,.8,.9])}
    expected=develop(source,settings)
    np.testing.assert_array_equal(expected,develop(source,settings,original_size=(221,151)))
    assert not np.array_equal(expected,develop(source,settings,original_size=(884,604)))


@pytest.mark.parametrize('size',[(0,100),(-1,100),(100,float('nan')),(100,float('inf')),(100,),((100,200),)])
def test_invalid_original_dimensions_are_rejected(size):
    with pytest.raises(ValueError,match='Original image size'):
        develop(np.zeros((10,20,3),np.float32),defaults(),original_size=size)


@pytest.mark.parametrize('route',['file','cached','offline'])
def test_thumbnail_routes_use_fresh_original_dimensions(tmp_path,monkeypatch,route):
    path=tmp_path/'source.png';Image.fromarray(np.uint8(photograph(400,1920)*255)).save(path)
    cat=Catalog(tmp_path/'library');ident=cat.add(path)
    try:
        settings={**defaults(),**dict(sharpen=90,sharpen_radius=3,texture=80,noise_color=25)}
        cat.set_settings(ident,settings)
        pixels,info=load_image(path,1800)
        # Stale catalog metadata must not override a fresh file/cache/offline source.
        with cat.db:cat.db.execute('UPDATE photos SET metadata=? WHERE id=?',(json.dumps(dict(width=5,height=4)),ident))
        cached=None
        if route=='cached':
            state=preview_store.snapshot(cat.directory,ident)
            cached=(pixels,dict(source=state['source'],offline=None,working_space='sRGB',original_size=(1920,400)))
            monkeypatch.setattr('luma.engine.load_image',lambda *a,**k:pytest.fail('decoded an already loaded source'))
        elif route=='offline':
            folder=cat.directory/'previews';folder.mkdir()
            np.savez_compressed(folder/f'{ident}.npz',pixels=pixels,info=json.dumps(info))
            path.rename(path.with_suffix('.offline'))
        captured=[]
        monkeypatch.setattr(preview_store,'publish',lambda directory,state,image,*a,**k:captured.append(image.copy()) or True)
        assert preview_store.rebuild(cat.directory,ident,force=True,cached=cached)=='갱신 완료'
        expected=resize_float(develop(pixels,settings,original_size=(1920,400)),320)
        np.testing.assert_array_equal(captured[0],expected)
        assert np.max(abs(expected-resize_float(develop(pixels,settings),320)))>1e-4
    finally:cat.close()


def test_raw_import_preview_keeps_decode_dimensions(tmp_path,monkeypatch):
    path=tmp_path/'mock.nef';source=to_linear(photograph(96,144))
    settings={**defaults(),**dict(texture=60,sharpen=80)}
    monkeypatch.setattr('luma.engine.read_metadata',lambda p:{})
    monkeypatch.setattr('luma.engine.load_image',lambda *a,**k:(source,dict(width=1440,height=960)))
    monkeypatch.setattr(raw_defaults,'resolve',lambda *a:(deepcopy(settings),[]))
    preview,info,applied=raw_defaults.import_preview(path,[])
    expected=np.uint8(develop(source,settings,original_size=(1440,960))*255)
    np.testing.assert_array_equal(np.asarray(preview),expected)
    assert applied==settings and info['width']==1440
