from copy import deepcopy
import json
import numpy as np
import cv2
import lensfunpy as lf
import pytest
from luma.optics import (lens_records,cameras,make_profile,automatic_profile,profile_lens,
    lens_shading,lens_geometry,match_metadata)
from luma.engine import defaults,develop,geometry,to_linear,export_image,load_image
from luma.rasterio import orient_pixels
from luma.render_cache import DevelopmentCache
from luma.upright import estimate,apply,AlignmentError


@pytest.fixture
def lens():return next(r for r in lens_records() if '18-105' in r['model'])


def settings_for(lens,orientation=1):
    settings=defaults();settings.update(lensfun=make_profile(lens,1.523,18,3.5,orientation=orientation),lensfun_enabled=True)
    return settings


def test_real_database_exact_match_and_missing_metadata(lens):
    assert len(cameras())>900 and len(lens_records())>1200
    info={'make':'NIKON CORPORATION','camera':'NIKON D7000','lens':lens['model'],'focal_length':18,'aperture':3.5}
    data,reason=automatic_profile(info)
    assert not reason and set(data['capabilities'])=={'distortion','tca','vignette'}
    assert data['crop']==pytest.approx(1.523,abs=1e-5)
    absent=deepcopy(info);absent.pop('lens')
    assert automatic_profile(absent)[0] is None
    ambiguous=deepcopy(info);ambiguous['lens']='18-105'
    assert automatic_profile(ambiguous)[0] is None
    missing_focal=deepcopy(info);missing_focal.pop('focal_length')
    assert automatic_profile(missing_focal)[0] is None
    no_aperture=deepcopy(info);no_aperture.pop('aperture')
    assert 'vignette' not in automatic_profile(no_aperture)[0]['capabilities']


@pytest.mark.parametrize('orientation',range(1,9))
def test_lens_correction_vs_native_and_orientation(lens,orientation):
    sensor=np.random.default_rng(5).uniform(.1,.4,(280,380,3)).astype(np.float32)
    original=orient_pixels(sensor,orientation).copy();before=original.copy()
    settings=settings_for(lens,orientation)
    actual=lens_geometry(lens_shading(original,settings),settings)
    native=lf.Modifier(profile_lens(lens),1.523,380,280)
    flags=lf.ModifyFlags.DISTORTION|lf.ModifyFlags.TCA|lf.ModifyFlags.VIGNETTING|lf.ModifyFlags.SCALE
    native.initialize(18,3.5,1000,scale=0,pixel_format=np.float32,flags=int(flags))
    reference=sensor.copy();assert native.apply_color_modification(reference)
    mapping=native.apply_subpixel_geometry_distortion()
    expected=np.stack([cv2.remap(reference[...,c],mapping[:,:,c,0],mapping[:,:,c,1],cv2.INTER_LINEAR)
        for c in range(3)],axis=-1)
    np.testing.assert_allclose(actual,orient_pixels(expected,orientation),atol=1e-6)
    np.testing.assert_array_equal(original,before)


def test_bounded_maps_use_native_coordinates_and_have_continuous_boundaries(lens,monkeypatch):
    import luma.optics as optics
    monkeypatch.setattr(optics,'MAP_BUDGET',0)
    source=np.random.default_rng(4).uniform(.1,.5,(520,700,3)).astype(np.float32)
    settings=settings_for(lens);actual=lens_geometry(source,settings)
    native=optics._modifier(settings,source.shape);expected=np.empty_like(source)
    for y in range(0,520,128):
        count=min(128,520-y);mapping=native.apply_subpixel_geometry_distortion(yu=y,width=700,height=count)
        for c in range(3):expected[y:y+count,:,c]=cv2.remap(source[...,c],mapping[:,:,c,0],mapping[:,:,c,1],cv2.INTER_LINEAR)
    np.testing.assert_array_equal(actual,expected)
    # Lensfun accumulates float coordinates along rows. Starting each strip at
    # an absolute position differs from a full-image call; check actual seams
    # against separately requested neighbouring rows, not a tolerance on RGB.
    native=optics._modifier(settings,(6000,4000,3));errors=[]
    for y in range(0,6000-128,128):
        block=native.apply_subpixel_geometry_distortion(xu=2000,yu=y,width=16,height=128)
        reference=native.apply_subpixel_geometry_distortion(xu=2000,yu=y+127,width=16,height=1)
        errors.append(np.abs(block[-1:]-reference).max())
    assert max(errors)<.02,errors


def test_copy_retargets_capture_conditions_and_does_not_guess_other_lens(lens):
    from luma.optics import retarget
    source=settings_for(lens,6);saved=deepcopy(source)
    metadata={'make':'NIKON CORPORATION','camera':'NIKON D7000','lens':lens['model'],
        'focal_length':70,'aperture':8,'source_orientation':8,'subject_distance':4}
    target=retarget(source,metadata)
    assert source==saved and target['lensfun_enabled']
    assert [target['lensfun'][key] for key in ('focal','aperture','orientation','distance')]==[70,8,8,4]
    metadata['lens']='unknown lens'
    target=retarget(source,metadata)
    assert not target['lensfun_enabled'] and target['lensfun']['note']
    target=retarget(source,{})
    assert target['lensfun_enabled'] and target['lensfun']['orientation']==1 and target['lensfun']['note']
    invalid=deepcopy(source);invalid['lensfun']['focal']=float('nan')
    with pytest.raises(ValueError):lens_geometry(np.ones((80,100,3),np.float32),invalid)


def test_profile_controls_masks_saved_data_and_cache(lens,monkeypatch):
    source=np.full((80,120,3),.2,np.float32);settings=settings_for(lens)
    shaded=lens_shading(source,settings)
    assert shaded[0,0,0]>source[0,0,0]*1.7 and shaded[40,60,0]==pytest.approx(.2,abs=.001)
    settings['lensfun_vignette']=False
    assert lens_shading(source,settings) is source
    yy,xx=np.mgrid[:80,:120].astype(np.float32)
    coords=np.stack([xx/119,yy/79,np.ones_like(xx),np.zeros_like(xx)],-1)
    transformed=geometry(coords,settings)
    assert transformed.shape==(80,120,4) and transformed[...,2].min()>.99
    assert np.all(transformed[...,3]==0)
    cache=DevelopmentCache(16*1024**2)
    first=develop(source,settings,cache=cache)
    settings['exposure']=.4
    np.testing.assert_array_equal(develop(source,settings,cache=cache),develop(source,settings))
    assert cache.hits>0
    settings['lensfun_vignette']=True
    np.testing.assert_array_equal(develop(source,settings,cache=cache),develop(source,settings))
    # Saved XML, not a newly looked-up database entry, controls future renders.
    monkeypatch.setattr('luma.optics.database',lambda:pytest.fail('global database lookup during rendering'))
    restored=json.loads(json.dumps(settings))
    np.testing.assert_array_equal(develop(source,restored),develop(source,settings))
    assert not np.array_equal(first,develop(source,settings))


def test_lens_preview_export_and_source_coordinates(lens,tmp_path):
    from PIL import Image
    source=tmp_path/'source.png'
    pixels=np.random.default_rng(7).integers(20,220,(100,160,3),dtype=np.uint8)
    Image.fromarray(pixels).save(source)
    settings=settings_for(lens);settings['lensfun_tca']=False
    linear,_=load_image(source);expected=develop(linear,settings)
    out=tmp_path/'out.png';export_image(source,out,settings,'PNG 16-bit')
    actual,_=load_image(out)
    from luma.engine import to_srgb
    np.testing.assert_allclose(to_srgb(actual),expected,atol=2/65535)
    assert source.read_bytes()!=out.read_bytes()
    with pytest.raises(ValueError):make_profile(lens,1.5,500,3.5)


def grid_fixture():
    image=np.full((600,800,3),.65,np.float32)
    for x in range(100,751,100):cv2.line(image,(x,45),(x,555),(.02,.02,.02),3)
    for y in range(100,551,90):cv2.line(image,(45,y),(755,y),(.02,.02,.02),3)
    return image


def pixel_matrix(report,shape,crop=False):
    h,w=shape[:2];n=np.diag([w-1,h-1,1]);m=n@np.asarray(report['matrix'])@np.linalg.inv(n)
    if crop:
        z=report['crop_scale'];m=np.array([[z,0,(w-1)*(1-z)/2],[0,z,(h-1)*(1-z)/2],[0,0,1]])@m
    return m


def line_errors(matrix):
    from luma.upright import _points
    vertical=np.array([[[x,80],[x,520]] for x in range(100,751,100)],float)
    horizontal=np.array([[[100,y],[700,y]] for y in range(100,551,90)],float)
    v=_points(matrix,vertical);h=_points(matrix,horizontal)
    dv=v[:,1]-v[:,0];dh=h[:,1]-h[:,0]
    return np.max(np.rad2deg(np.arctan2(np.abs(dv[:,0]),np.abs(dv[:,1])))),np.max(np.rad2deg(np.arctan2(np.abs(dh[:,1]),np.abs(dh[:,0]))))


@pytest.mark.parametrize('angle',[-12,-5,4,11])
def test_auto_level_recovers_known_rotation(angle):
    image=grid_fixture();h,w=image.shape[:2]
    distortion=np.eye(3);distortion[:2]=cv2.getRotationMatrix2D(((w-1)/2,(h-1)/2),-angle,1)
    distorted=cv2.warpPerspective(image,distortion,(w,h),borderValue=(.65,.65,.65))
    report=estimate(distorted,'level')
    assert report['rotation']==pytest.approx(angle,abs=.2)
    assert max(line_errors(pixel_matrix(report,image.shape)@distortion))<.25
    np.testing.assert_array_equal(image,grid_fixture())


@pytest.mark.parametrize('mode',['vertical','full','auto'])
def test_auto_perspective_straightens_independent_projected_grid(mode):
    image=grid_fixture();h,w=image.shape[:2]
    distortion=cv2.getPerspectiveTransform(np.float32([[0,0],[w-1,0],[w-1,h-1],[0,h-1]]),
        np.float32([[120,35],[715,70],[785,555],[15,585]]))
    distorted=cv2.warpPerspective(image,distortion,(w,h),borderValue=(.65,.65,.65))
    report=estimate(distorted,mode);result=pixel_matrix(report,image.shape)@distortion
    v,horiz=line_errors(result)
    assert v<.4,(v,horiz,report)
    if mode in ('full','auto'):assert horiz<.4,(v,horiz,report)
    assert line_errors(distortion)[0]>5
    result=apply(np.ones_like(image),{'upright':report,'upright_crop':True})
    assert result.min()>.99


def test_upright_resolution_independence_and_blank_refusal():
    image=grid_fixture();distortion=np.eye(3)
    distortion[:2]=cv2.getRotationMatrix2D((399.5,299.5),-7,1)
    distorted=cv2.warpPerspective(image,distortion,(800,600),borderValue=(.65,.65,.65))
    report=estimate(distorted,'level')
    expected=pixel_matrix(report,distorted.shape)
    small=pixel_matrix(report,(300,400,3))
    scale=np.diag([799/399,599/299,1])
    np.testing.assert_allclose(scale@small@np.linalg.inv(scale),expected,atol=1e-10)
    with pytest.raises(AlignmentError):estimate(np.full((300,400,3),.3,np.float32))
    for matrix in [[[float('nan')]*3]*3,[[0]*3]*3]:
        with pytest.raises(ValueError):apply(image,{'upright':{'matrix':matrix}})


@pytest.mark.parametrize('angle',[-11,-3,0,6,13])
def test_single_horizon_without_building_lines(angle):
    yy,xx=np.mgrid[:600,:900]
    water=yy>300+np.tan(np.deg2rad(angle))*(xx-449.5)
    image=np.where(water[...,None],np.array([.04,.14,.22]),np.array([.38,.6,.8])).astype(np.float32)
    original=image.copy();report=estimate(image,'level')
    assert report['algorithm']=='lines-horizon-v2'
    assert report['rotation']==pytest.approx(angle,abs=.15)
    from luma.upright import _points
    endpoints=np.array([[80,300+np.tan(np.deg2rad(angle))*(80-449.5)],[820,300+np.tan(np.deg2rad(angle))*(820-449.5)]])
    corrected=_points(pixel_matrix(report,image.shape),endpoints)
    assert abs(corrected[1,1]-corrected[0,1])<2
    np.testing.assert_array_equal(image,original)


def test_horizon_rejects_short_border_and_conflicting_boundaries():
    from luma.upright import _horizon_roll
    for lines in (np.array([[[50,100],[200,110]]]),np.array([[[0,10],[899,60]]]),
                  np.array([[[10,200],[880,250]],[[10,400],[880,330]]])):
        with pytest.raises(AlignmentError):_horizon_roll(lines,np.linalg.norm(lines[:,1]-lines[:,0],axis=1),900,600)
