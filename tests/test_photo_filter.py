import numpy as np
import pytest
from luma.engine import defaults, develop, to_linear, export_image, load_image
from luma.photo_filter import FILTERS, apply
from luma.render_cache import DevelopmentCache


def settings(**values):
    return {**defaults(),'photo_filter_enabled':True,**values}


def test_disabled_zero_unknown_identity_and_source_immutable():
    image=np.random.default_rng(82).random((32,48,3),dtype=np.float32);before=image.copy()
    for s in (defaults(),settings(photo_filter_density=0),settings(photo_filter='unknown')):
        np.testing.assert_array_equal(develop(image,s),develop(image,defaults()))
    for key,_,_ in FILTERS:develop(image,settings(photo_filter=key,photo_filter_density=100))
    np.testing.assert_array_equal(image,before)


def test_warm_cool_strength_and_linear_luminance():
    gray=np.full((12,16,3),.45,np.float32)
    results={key:apply(gray,settings(photo_filter=key,photo_filter_density=60)) for key in ('warming85','warming81','cooling80','cooling82')}
    assert results['warming85'][0,0,0]>results['warming85'][0,0,2]
    assert results['cooling80'][0,0,0]<results['cooling80'][0,0,2]
    assert np.ptp(results['warming85'][0,0])>np.ptp(results['warming81'][0,0])
    assert np.ptp(results['cooling80'][0,0])>np.ptp(results['cooling82'][0,0])
    image=np.random.default_rng(85).random((24,40,3),dtype=np.float32)
    weights=np.array([.2126,.7152,.0722])
    for key,_,_ in FILTERS:
        filtered=apply(image,settings(photo_filter=key,photo_filter_density=100))
        np.testing.assert_allclose(to_linear(filtered)@weights,to_linear(image)@weights,atol=6e-7)
        assert np.isfinite(filtered).all() and filtered.min()>=0 and filtered.max()<=1.000001
    dark=apply(gray,settings(photo_filter_density=100,photo_filter_luminosity=False))
    assert (to_linear(dark)@weights).mean()<(to_linear(gray)@weights).mean()


@pytest.mark.parametrize('space',['sRGB','ProPhoto'])
@pytest.mark.parametrize('hdr',[False,True])
def test_cache_updates_and_hdr_is_finite(space,hdr):
    image=np.random.default_rng(3).uniform(0,4 if hdr else 1,(40,60,3)).astype(np.float32)
    cache=DevelopmentCache();s=settings(working_space=space,hdr=hdr)
    previous=develop(image,s,cache=cache,output_space=None)
    for change in ({'photo_filter':'cooling80'},{'photo_filter_density':80},
                   {'photo_filter_luminosity':False},{'monochrome':True},{'photo_filter_enabled':False}):
        s.update(change);result=develop(image,s,cache=cache,output_space=None)
        np.testing.assert_allclose(result,develop(image,s,output_space=None),atol=1e-6)
        assert np.isfinite(result).all() and not np.allclose(result,previous);previous=result


def test_bw_red_filter_changes_relative_red_blue_brightness():
    colors=np.array([[[.7,.2,.2],[.2,.2,.7]]],np.float32)
    original=develop(colors,settings(photo_filter_enabled=False,monochrome=True))
    filtered=develop(colors,settings(photo_filter='red',photo_filter_density=80,monochrome=True))
    assert filtered[0,0,0]/filtered[0,1,0]>original[0,0,0]/original[0,1,0]
    np.testing.assert_array_equal(filtered[...,0],filtered[...,1])
