from copy import deepcopy
import numpy as np
from luma.engine import defaults,develop,geometry,to_linear
from luma.processing import apply_retouch,component_mask,layer_mask


def test_dust_repair_restores_constant_field_without_touching_source():
    source=np.full((96,96,3),.4,np.float32);source[44:52,44:52]=0
    before=source.copy()
    result=apply_retouch(source,[dict(type='inpaint',points=[[.5,.5]],radius=.13)])
    np.testing.assert_array_equal(source,before)
    assert np.max(np.abs(result[46:50,46:50]-.4))<1e-4
    assert np.isfinite(result).all() and result.max()<.41


def test_clone_and_redeye_are_spatially_limited():
    source=np.zeros((100,100,3),np.float32);source[15:35,15:35]=[.8,.2,.1]
    cloned=apply_retouch(source,[dict(type='clone',source=[.25,.25],points=[[.75,.75]],radius=.08)])
    assert cloned[75,75,0]>.7 and cloned[50,50,0]==0
    fixed=apply_retouch(source,[dict(type='red_eye',points=[[.25,.25]],radius=.1)])
    assert fixed[25,25,0]<.2


def test_mask_follows_crop_rotation_and_perspective():
    source=np.full((100,150,3),.2,np.float32)
    layer=dict(components=[dict(type='brush',points=[[.7,.4]],radius=.08,feather=30)],adjustments={'exposure':1})
    s=defaults();s['masks']=[layer]
    neutral=develop(source,defaults());edited=develop(source,s)
    assert edited[40,105].mean()>neutral[40,105].mean()
    np.testing.assert_allclose(edited[5,5],neutral[5,5])
    t=deepcopy(s);t.update(rotation=1,crop=[.1,.1,.9,.9],perspective_h=10)
    mask=layer_mask(geometry(source,t),source.shape,layer,lambda a:geometry(a,t))
    diff=develop(source,t)-develop(source,{**t,'masks':[]})
    assert np.unravel_index(mask.argmax(),mask.shape)==np.unravel_index(diff[...,0].argmax(),mask.shape)


def test_subtract_intersect_invert_masks():
    source=np.full((60,60,3),.5,np.float32)
    components=[dict(type='brush',points=[[.5,.5]],radius=.3),dict(type='brush',points=[[.5,.5]],radius=.1,operation='subtract')]
    layer={'components':components}
    m=layer_mask(source,source.shape,layer,lambda a:a)
    assert m[30,30]<.15 and m[30,40]>.1
    inv=layer_mask(source,source.shape,{**layer,'invert':True},lambda a:a)
    np.testing.assert_allclose(m+inv,1)


def test_conventional_noise_reduction_and_color_controls():
    rng=np.random.default_rng(5)
    source=to_linear(np.clip(.45+rng.normal(0,.02,(80,90,3)),0,1).astype(np.float32))
    s=defaults();s.update(noise_luma=85,noise_color=90)
    denoised=develop(source,s)
    assert denoised.std()<develop(source,defaults()).std()*.8
    for key,value in [('texture',70),('dehaze',40),('distortion',40),('defringe',100),('kelvin_enabled',True),('lens_vignette',35)]:
        s=defaults();s[key]=value;s['kelvin']=4000
        out=develop(source,s)
        assert out.shape==source.shape and np.isfinite(out).all() and out.min()>=0 and out.max()<=1
