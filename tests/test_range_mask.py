from copy import deepcopy
import numpy as np
import pytest
from luma.engine import defaults,develop,to_linear,geometry
from luma.range_mask import sample_selection,color_coverage
from luma.processing import layer_mask


def whole(edit,**kwargs):return dict(components=[dict(type='luma',range=[0,1],softness=.1)],adjustments=edit,**kwargs)


def test_range_sampling_precedes_own_and_later_edits_and_keeps_working_space():
    rgb=np.full((80,120,3),[.8,.2,.2],np.float32);source=to_linear(rgb);s=defaults();s['working_space']='ProPhoto'
    s['masks']=[whole(dict(hue=60)),whole(dict(hue=120)),whole(dict(exposure=2))]
    before=deepcopy(s);original=source.copy()
    sampled=sample_selection(source,s,[[.5,.5]],1)
    np.testing.assert_allclose(sampled['colors'],[[.8,.8,.2]],atol=2e-6)
    assert np.max(abs(develop(source,s)[40,60]-sampled['colors'][0]))>.2
    s['masks'][1]['enabled']=False
    assert sample_selection(source,s,[[.5,.5]],1)==sampled
    np.testing.assert_array_equal(source,original);s['masks'][1].pop('enabled');assert s==before


def test_rectangle_selects_multiple_colors_and_exact_brightness_span_after_geometry():
    colors=np.array([[.8,.2,.2],[.2,.8,.2],[.2,.2,.8],[.7,.7,.7]],np.float32)
    rgb=np.repeat(colors,30,axis=0)[None].repeat(80,axis=0);source=to_linear(rgb);s=defaults()
    s.update(rotation=1,crop=[.1,.05,.9,.95]);image=develop(source,s,output_space=None)
    selected=sample_selection(source,s,[[0,0],[1,1]])
    assert len(selected['colors'])==4
    assert color_coverage(image,selected['colors'],.02).min()>.999
    expected=image@np.array([.2126,.7152,.0722],np.float32)
    np.testing.assert_allclose(selected['range'],[expected.min(),expected.max()],atol=1e-6)


def test_legacy_color_exact_and_multiple_color_union_respects_mask_operations():
    image=np.random.default_rng(33).random((80,120,3),dtype=np.float32);a=[.8,.2,.2];b=[.2,.8,.2]
    original=image.copy();single=dict(type='color',rgb=a,tolerance=.4)
    old=layer_mask(image,image.shape,dict(components=[single]),lambda x:x)
    np.testing.assert_array_equal(old,color_coverage(image,[a],.4))
    both=color_coverage(image,[a,b],.4)
    np.testing.assert_array_equal(both,np.maximum(old,color_coverage(image,[b],.4)))
    luminance=dict(type='luma',range=[.3,.6],softness=.1)
    base=layer_mask(image,image.shape,dict(components=[luminance]),lambda x:x)
    actual=layer_mask(image,image.shape,dict(components=[luminance,dict(type='color',samples=[a,b],tolerance=.4,operation='intersect')]),lambda x:x)
    np.testing.assert_array_equal(actual,base*both);np.testing.assert_array_equal(image,original)


@pytest.mark.parametrize('values,tolerance',[([], .3),([[1,2]],.3),([[1,2,3]]*6,.3),([[1,2,float('nan')]],.3),([[.1,.2,.3]],0),([[.1,.2,.3]],float('nan'))])
def test_bad_samples_rejected(values,tolerance):
    with pytest.raises(ValueError):color_coverage(np.zeros((2,3,3),np.float32),values,tolerance)


def test_blank_geometry_and_invalid_points_rejected_without_changing_settings():
    source=np.full((120,120,3),.2,np.float32);s=defaults();s['straighten']=35;before=deepcopy(s)
    with pytest.raises(ValueError):sample_selection(source,s,[[0,0]])
    for points in ([],[[float('nan'),.5]],[[2,.5]],[[.5]]):
        with pytest.raises(ValueError):sample_selection(source,s,points)
    assert s==before


def test_range_samples_persist_and_export_and_cache_follow_changed_input(tmp_path):
    import tifffile
    from PIL import Image
    from luma.catalog import Catalog
    from luma.library import write_sidecar,read_sidecar
    from luma.engine import load_image,export_image
    from luma.render_cache import DevelopmentCache
    path=tmp_path/'range.png';rgb=np.zeros((100,150,3),np.uint8);rgb[:,:75]=[180,40,40];rgb[:,75:]=[40,170,40];Image.fromarray(rgb).save(path)
    source=load_image(path)[0];s=defaults();s['masks']=[whole(dict(hue=20)),whole(dict(exposure=.8))]
    samples=sample_selection(source,s,[[.2,.3],[.8,.6]],1)['colors']
    s['masks'][1]['components']=[dict(type='color',samples=samples,tolerance=.12)]
    catalog=Catalog(tmp_path/'catalog')
    try:
        ident=catalog.add(path);catalog.edit(ident,s);sidecar=tmp_path/'range.xmp';write_sidecar(catalog.photo(ident),sidecar)
        restored=read_sidecar(sidecar)['settings'];assert restored['masks']==s['masks'];cache=DevelopmentCache()
        for hue in (20,85,-10):
            restored['masks'][0]['adjustments']['hue']=hue
            np.testing.assert_array_equal(develop(source,restored,cache=cache),develop(source,restored))
        output=tmp_path/'range.tif';export_image(path,output,restored,'TIFF 16-bit')
        np.testing.assert_array_equal(tifffile.imread(output),np.uint16(develop(source,restored)*65535+.5))
    finally:catalog.close()
