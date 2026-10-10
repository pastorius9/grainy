from copy import deepcopy
import numpy as np
import pytest
from luma.engine import defaults,develop,geometry,to_linear
from luma.processing import layer_mask,_local_region,IDENTITY
from luma.render_cache import DevelopmentCache


def gate_settings():
    s=defaults();s['masks']=[dict(components=[dict(type='brush',points=[[.2,.5],[.8,.5]],seed=[.2,.5],radius=.4,
        feather=0,auto_mask=True,auto_tolerance=.15,paint_version=2,flow=100,density=100)],adjustments={'exposure':1})]
    return s


def test_color_boundary_uses_source_and_keeps_seed_outside_crop():
    rgb=np.full((120,180,3),[.2,.4,.65],np.float32);rgb[:,90:]=[.7,.2,.1];source=to_linear(rgb)
    s=gate_settings();masks={};full=develop(source,s,on_mask=lambda i,m:masks.setdefault(i,m.copy()))
    assert masks[0][60,65]>.99 and masks[0][60,120]==0
    s.update(crop=[.4,.2,.9,.8],rotation=1)
    after={};develop(source,s,on_mask=lambda i,m:after.setdefault(i,m.copy()))
    np.testing.assert_array_equal(after[0],geometry(masks[0][...,None],s)[...,0])
    assert after[0].max()>.9
    # Global and this layer's colour changes must not move the source gate.
    s.update(temperature=50,exposure=.8);s['masks'][0]['adjustments'].update(hue=120,exposure=-.5)
    shifted={};develop(source,s,on_mask=lambda i,m:shifted.setdefault(i,m.copy()))
    np.testing.assert_array_equal(after[0],shifted[0])


def test_auto_mask_soft_boundary_erase_and_cache_changes():
    y,x=np.mgrid[:100,:180];rgb=np.stack([x/200+.05,y*0+.3,y*0+.4],-1).astype(np.float32);source=to_linear(rgb)
    settings=gate_settings();cache=DevelopmentCache()
    for tolerance in (.1,.4,.2):
        settings['masks'][0]['components'][0]['auto_tolerance']=tolerance
        np.testing.assert_array_equal(develop(source,settings,cache=cache),develop(source,settings))
    component=settings['masks'][0]['components'][0]
    painted=layer_mask(rgb,rgb.shape,dict(components=[component]),lambda a:a,source_image=rgb)
    erased=layer_mask(rgb,rgb.shape,dict(components=[component,{**component,'operation':'subtract'}]),lambda a:a,source_image=rgb)
    assert np.any((painted>0)&(painted<1)) and np.all(erased<=painted)


@pytest.mark.parametrize('channel',[0,1,2,3])
def test_local_curves_independent_expected_mapping_and_no_spill(channel):
    image=np.full((21,21,3),[.2,.4,.6],np.float32);mask=np.zeros((21,21),np.float32);mask[5:16,5:16]=.5
    curves=deepcopy([IDENTITY]*4);curves[channel]=[[0,0],[.5,1],[1,1]]
    # The base local operation includes the established linear/sRGB round trip.
    neutral=_local_region(image,np.ones_like(mask),{})
    adjusted=neutral.copy()
    if channel==0:adjusted=np.minimum(adjusted*2,1)
    else:adjusted[...,channel-1]=np.minimum(adjusted[...,channel-1]*2,1)
    expected=image*(1-mask[...,None])+adjusted*mask[...,None]
    result=_local_region(image,mask,dict(local_curves=curves))
    np.testing.assert_allclose(result,expected,atol=1e-7)
    np.testing.assert_array_equal(result[0],image[0])


def test_hue_red_to_green_and_legacy_identity_exact():
    image=np.full((12,12,3),[.8,.1,.1],np.float32);mask=np.ones((12,12),np.float32)
    actual=_local_region(image,mask,dict(hue=120))
    np.testing.assert_allclose(actual[6,6],[.1,.8,.1],atol=2e-6)
    np.testing.assert_array_equal(_local_region(image,mask,{}),_local_region(image,mask,dict(hue=0,local_curves=[IDENTITY]*4)))


def test_component_cache_handles_legacy_first_reorder_disable_and_color_dependencies():
    source=np.random.default_rng(518).uniform(.04,.65,(120,180,3)).astype(np.float32)
    settings=gate_settings();components=settings['masks'][0]['components']
    legacy=dict(type='brush',points=[[.45,.45]],radius=.2,feather=70)
    components.insert(0,legacy)
    components.append(dict(type='luma',range=[.2,.6],operation='intersect'))
    cache=DevelopmentCache(max_bytes=20*1024*1024)
    for change in (lambda:None,lambda:components[1].update(flow=40),lambda:components.reverse(),
            lambda:components[0].update(enabled=False),lambda:settings.update(exposure=.8),lambda:components.pop()):
        change();np.testing.assert_array_equal(develop(source,settings,cache=cache),develop(source,settings))
        assert cache.bytes<=cache.max_bytes
    assert cache.hits>0
