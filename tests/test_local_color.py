from copy import deepcopy
import numpy as np
import pytest
from luma.engine import develop,defaults,to_linear
from luma.processing import hsl_to_rgb
from luma.point_color import weights,apply,sample,preview


def point(rgb,**edits):
    return dict(version=2,rgb=list(rgb),range=.12,saturation_range=.6,lightness_range=.5,hue=0,saturation=0,lightness=0,**edits) if not edits else {**point(rgb),**edits}


def layer(adjustments=None,**extra):
    return dict(name='Selected area',enabled=True,opacity=1,components=[dict(type='brush',points=[[.5,.5]],radius=.3,feather=0)],adjustments=adjustments or {},**extra)


def test_three_ranges_separate_same_hue_and_wrap_red_and_keep_neutrals_achromatic():
    h=np.array([[.995,.005,.3,.005,.005]],np.float32);s=np.array([[.8,.8,.8,.25,.8]],np.float32);l=np.array([[.5,.5,.5,.5,.9]],np.float32)
    rgb=hsl_to_rgb(h,s,l);p=point(rgb[0,1],range=.05,saturation_range=.2,lightness_range=.2)
    values=weights(rgb,p)[0]
    assert values[0]>.79 and values[1]>.99 and np.all(values[2:]==0)
    gray=np.repeat(np.linspace(.1,.9,9,dtype=np.float32)[None,:,None],3,-1)
    changed=apply(gray,[point([.5,.5,.5],hue=100,saturation=100,lightness=40)])
    np.testing.assert_array_equal(changed[...,0],changed[...,1]);np.testing.assert_array_equal(changed[...,0],changed[...,2])
    assert changed[0,4,0]>.5


def test_neutral_points_and_pixels_outside_color_range_stay_exact():
    image=np.random.default_rng(32501).uniform(.05,.95,(90,120,3)).astype(np.float32)
    p=point([.7,.1,.12],range=.04,saturation_range=.1,lightness_range=.12)
    np.testing.assert_array_equal(apply(image,[p]),image)
    changed=apply(image,[{**p,'hue':70,'saturation':30,'lightness':-25}]);outside=weights(image,p)==0
    assert np.any(~outside);np.testing.assert_array_equal(changed[outside],image[outside])


def test_sampling_precedes_later_grading_and_detail_but_follows_earlier_points():
    rgb=np.full((80,120,3),[.8,.2,.2],np.float32);source=to_linear(rgb);before=source.copy();s=defaults()
    s['grading']=[[120,90,20]]*3;s['monochrome']=True
    np.testing.assert_allclose(sample(source,s,[.5,.5]),[.8,.2,.2],atol=1e-6)
    visible=develop(source,s);assert abs(visible[40,60,0]-visible[40,60,1])<1e-5
    s['point_colors']=[point([.8,.2,.2],hue=100)]
    np.testing.assert_allclose(sample(source,s,[.5,.5]),[.8,.8,.2],atol=2e-6)
    np.testing.assert_array_equal(source,before)


def test_local_hsl_and_points_respect_coverage_opacity_disabled_and_outside():
    rgb=np.full((100,180,3),[.8,.2,.2],np.float32);source=to_linear(rgb);s=defaults();original=develop(source,s)
    groups=[[0,0,0] for _ in range(8)];groups[0]=[0,-80,20]
    s['masks']=[layer(dict(hsl=groups,point_colors=[point([.6,.5,.5],lightness=25)]))]
    mask=[];fixed=develop(source,s,on_mask=lambda _,value:mask.append(value))
    np.testing.assert_array_equal(fixed[mask[0]==0],original[mask[0]==0]);assert np.max(np.abs(fixed-original))>.1
    full=fixed.copy();s['masks'][0]['opacity']=.5;half=develop(source,s)
    np.testing.assert_allclose(half,(full+original)/2,atol=2e-7)
    s['masks'][0]['enabled']=False;np.testing.assert_array_equal(develop(source,s),original)


def test_local_sampling_second_layer_crop_rotation_and_overlay_agree():
    rgb=np.full((100,180,3),[.8,.2,.2],np.float32);source=to_linear(rgb);s=defaults()
    s.update(rotation=1,crop=[.1,.15,.9,.85],masks=[layer(dict(hue=60)),layer(dict(hue=60))])
    sampled=sample(source,s,[.5,.5],1);np.testing.assert_allclose(sampled,[.2,.8,.2],atol=2e-6)
    s['masks'][1]['adjustments']['point_colors']=[point(sampled,hue=80)]
    coverage=preview(source,s,(1,0));assert coverage.shape==develop(source,s).shape[:2] and coverage.max()>.99
    assert coverage[0,0]==0 and np.any(coverage>0)
    with pytest.raises(ValueError):sample(source,s,[.02,.02],1)
    s['masks'][1]['enabled']=False
    with pytest.raises(ValueError):sample(source,s,[.5,.5],1)


def test_wide_working_rgb_is_sampled_without_display_gamut_roundtrip():
    rgb=np.full((70,100,3),[.05,.85,.2],np.float32);source=to_linear(rgb);s=defaults();s['working_space']='ProPhoto'
    np.testing.assert_allclose(sample(source,s,[.5,.5]),rgb[0,0],atol=1e-6)
    visible=develop(source,s);assert np.max(np.abs(visible[0,0]-rgb[0,0]))>.05


@pytest.mark.parametrize('key,value',[('range',float('nan')),('range',0),('range',.6),('saturation_range',-1),('lightness_range',2),('rgb',[1,2])])
def test_invalid_range_or_reference_rejected(key,value):
    p=point([.5,.3,.2]);p[key]=value
    with pytest.raises(ValueError):weights(np.ones((2,3,3),np.float32),p)


def test_new_local_colors_persist_in_sidecar_full_output_and_cache(tmp_path):
    import tifffile
    from PIL import Image
    from luma.catalog import Catalog
    from luma.library import write_sidecar,read_sidecar
    from luma.engine import export_image,load_image
    from luma.render_cache import DevelopmentCache
    rgb=np.full((100,180,3),[.8,.2,.2],np.float32);path=tmp_path/'red.png';Image.fromarray(np.uint8(rgb*255)).save(path)
    groups=[[0,0,0] for _ in range(8)];groups[0]=[40,-25,15];s=defaults();s['masks']=[layer(dict(hsl=groups,point_colors=[point([.8,.2,.2],hue=80)]))]
    catalog=Catalog(tmp_path/'catalog')
    try:
        ident=catalog.add(path);catalog.edit(ident,s);xmp=tmp_path/'red.xmp';write_sidecar(catalog.photo(ident),xmp)
        restored=read_sidecar(xmp)['settings'];assert restored['masks']==s['masks']
        source=load_image(path)[0];cache=DevelopmentCache();a=develop(source,restored,cache=cache)
        restored['masks'][0]['adjustments']['hsl'][0][0]=70
        np.testing.assert_array_equal(develop(source,restored,cache=cache),develop(source,restored))
        restored['masks'][0]['adjustments']['point_colors'][0]['lightness_range']=.15
        np.testing.assert_array_equal(develop(source,restored,cache=cache),develop(source,restored))
        output=tmp_path/'local.tif';export_image(path,output,restored,format='TIFF 16-bit')
        np.testing.assert_array_equal(tifffile.imread(output),np.uint16(np.clip(develop(source,restored),0,1)*65535+.5))
    finally:catalog.close()
