import base64
import hashlib
import struct
from copy import deepcopy
import numpy as np
import pytest
from luma import dcp,rawcolor
from luma.engine import defaults,develop,resize_float
from luma.render_cache import DevelopmentCache


def make_dcp(extra=(),endian='<'):
    tags=[(50708,2,'Test Camera\0'),(50936,2,'Luma test\0'),(50942,2,'Luma test data\0'),
          (50721,12,np.eye(3).ravel()),(50778,3,[21]),(50941,4,[3])]
    overrides={t[0] for t in extra};tags=[t for t in tags if t[0] not in overrides]+list(extra)
    table=bytearray();payload=bytearray();offset=8+2+12*len(tags)+4
    formats={1:'B',3:'H',4:'I',5:'I',7:'B',10:'i',11:'f',12:'d'}
    for tag,kind,values in sorted(tags):
        if kind==2:data=values.encode('utf-8');count=len(data)
        elif kind==7:data=values;count=len(data)
        else:
            data=struct.pack(endian+str(len(values))+formats[kind],*values)
            count=len(values)//2 if kind in (5,10) else len(values)
        table+=struct.pack(endian+'HHI',tag,kind,count)
        if len(data)<=4:table+=data.ljust(4,b'\0')
        else:table+=struct.pack(endian+'I',offset+len(payload));payload+=data
    return (b'II' if endian=='<' else b'MM')+struct.pack(endian+'HIH',0x4352,8,len(tags))+table+b'\0'*4+payload


def record(data):
    p=dcp.parse(data)
    return dict(name=p['name'],camera=p['camera'],copyright=p['copyright'],data=base64.b64encode(data).decode(),sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize('endian',['<','>'])
def test_dcp_reader_roundtrip_and_reproducible_record(tmp_path,endian):
    data=make_dcp(endian=endian);path=tmp_path/'profile.dcp';path.write_bytes(data)
    saved=dcp.load_profile(path);path.unlink()
    p=dcp.compiled(saved)
    assert p['camera']=='Test Camera' and p['copyright']=='Luma test data'
    np.testing.assert_array_equal(p['cm1'],np.eye(3))
    assert dcp.camera_matches(p,{'make':'Test','camera':'Camera'})
    assert not dcp.camera_matches(p,{'camera':'Different Camera'})
    bad=deepcopy(saved);bad['sha256']='0'*64
    with pytest.raises(ValueError):dcp.compiled(bad)


@pytest.mark.parametrize('bad',[b'',b'IIxx'+b'\0'*8,make_dcp()[:-3],
    make_dcp([(50721,10,[1,0]*9)]),make_dcp([(50721,12,[0.]*9)]),
    make_dcp([(50721,12,[float('nan')]*9)]),make_dcp([(50940,11,[0,0,.4,.5,.4,.8,1,1])]),
    make_dcp([(52551,7,struct.pack('<HHf',1,1,8))])])
def test_malformed_profiles_fail_before_render(bad):
    with pytest.raises(ValueError):dcp.parse(bad)


@pytest.mark.parametrize('kelvin',[2000,2856,4000,5000,6500,12000,25000,50000])
@pytest.mark.parametrize('tint',[-10,0,10])
def test_temperature_roundtrip(kelvin,tint):
    actual,shade=rawcolor.xyz_temperature(rawcolor.temperature_xyz(kelvin,tint))
    assert abs(actual-kelvin)/kelvin<.0002
    assert abs(shade-tint)<.02


@pytest.mark.parametrize('temperature,tint',[(2000,150),(2000,-150),(50000,150),(50000,-150)])
def test_extreme_ui_white_balance_is_positive_finite(temperature,tint):
    xyz=rawcolor.temperature_xyz(temperature,tint)
    assert np.isfinite(xyz).all() and np.all(xyz>0)
    p=rawcolor.default_profile({'xyz_to_camera':[[.88,-.24,-.07],[-.48,1.26,.25],[-.06,.15,.75]]})
    n,_,_=rawcolor.resolve_white(p,{},dict(raw_mode='custom',raw_kelvin=temperature,raw_tint=tint))
    assert np.isfinite(n).all() and np.all(n>0)


def test_known_d65_temperature_and_neutral_mapping():
    t,tint=rawcolor.xyz_temperature([.95047,1,1.08883])
    assert abs(t-6504)<10 and 8<tint<12
    cm=np.array([[.88,-.24,-.07],[-.48,1.26,.25],[-.06,.15,.75]])
    p=dcp.parse(make_dcp([(50721,12,cm.ravel())]))
    for white in ([.95047,1,1.08883],[1.0985,1,.35585]):
        n=cm@white;n/=n[1]
        result=dcp.camera_to_xyz(p,n,rawcolor.xyz_temperature(white)[0])@n
        np.testing.assert_allclose(result,dcp.D50,rtol=2e-6)


def test_dual_illuminant_inverse_temperature_and_forward_matrix():
    p=dcp.parse(make_dcp([(50778,3,[17]),(50779,3,[21]),(50722,12,(np.eye(3)*2).ravel()),
        (50964,12,np.diag(dcp.D50).ravel()),(50965,12,np.diag(dcp.D50).ravel())]))
    assert dcp.weight(p,2000)==1 and dcp.weight(p,9000)==0
    middle=2/(1/2856+1/6504)
    assert dcp.weight(p,middle)==pytest.approx(.5)
    np.testing.assert_allclose(dcp.interpolate(p,'cm',.5),np.eye(3)*1.5)
    n=np.array([.45,1,.7]);matrix=dcp.camera_to_xyz(p,n,middle)
    np.testing.assert_allclose(matrix@n,dcp.D50)
    np.testing.assert_allclose(matrix,dcp.D50[:,None]*np.eye(3)/n)


def test_hsv_table_hue_wrap_trilinear_and_encoding():
    from luma.engine import hsv_to_rgb,to_srgb,to_linear
    table=np.zeros((2,4,3,3),np.float32);table[...,1:]=1
    table[1,...,0]=40;table[0,...,0]=20
    rgb=hsv_to_rgb(np.array([[.99,.01]]),np.array([[.5,.5]]),np.array([[.25,.25]]))
    for encoding in (0,1):
        v=float(to_srgb(np.array(.25))) if encoding else .25
        expected=hsv_to_rgb((np.array([[.99,.01]])+(20+20*v)/360)%1,np.array([[.5,.5]]),np.array([[.25,.25]]))
        np.testing.assert_allclose(dcp.table_map(rgb,table,encoding),expected,atol=2e-6)
    # A table varies on all axes and wraps its final hue node back to the first.
    table=np.zeros((2,2,2,3),np.float32);table[...,1:]=1
    table[...,0]=np.arange(8).reshape(2,2,2)*10
    pixel=hsv_to_rgb(np.array([[.75]]),np.array([[.5]]),np.array([[.5]]))
    expect=hsv_to_rgb(np.array([[(.75+35/360)%1]]),np.array([[.5]]),np.array([[.5]]))
    np.testing.assert_allclose(dcp.table_map(pixel,table),expect,atol=2e-6)


def test_tone_spline_matches_independent_scipy_and_preserves_hue():
    from scipy.interpolate import CubicSpline
    curve=np.array([[0,0],[.2,.3],[.5,.65],[.8,.9],[1,1]])
    x=np.linspace(0,1,1000);a=np.repeat(x[None,:,None],3,axis=-1).astype(np.float32)
    actual=dcp.tone_map(a,curve)
    expected=np.clip(CubicSpline(curve[:,0],curve[:,1],bc_type='natural')(x),0,1)
    np.testing.assert_allclose(actual[0,:,0],expected,atol=1e-7)
    pixel=np.array([[[.1,.25,.4]]],np.float32);b=dcp.tone_map(pixel,curve)
    assert (b[0,0,1]-b.min())/(b.max()-b.min())==pytest.approx(.5,abs=1e-6)


def test_camera_source_cache_live_resize_and_settings_change():
    cm=np.array([[.88,-.24,-.07],[-.48,1.26,.25],[-.06,.15,.75]])
    info=dict(camera='Test Camera',format='NEF',xyz_to_camera=cm.tolist(),neutral=[.45,1,.7],daylight_neutral=[.5,1,.85])
    a=np.random.default_rng(7).uniform(.01,.3,(40,60,3)).astype(np.float32)
    source=rawcolor.CameraSource(a,info);before=source.copy();settings=defaults();settings['raw_mode']='as_shot'
    small=resize_float(source,30)
    assert isinstance(small,rawcolor.CameraSource) and small.raw_info==info
    cache=DevelopmentCache();first=develop(source,settings,cache=cache)
    np.testing.assert_array_equal(first,develop(source,settings))
    settings['raw_mode']='custom';settings['raw_kelvin']=3200;settings['raw_tint']=15
    second=develop(source,settings,cache=cache)
    np.testing.assert_array_equal(second,develop(source,settings))
    assert np.max(abs(first-second))>.05
    hits=cache.hits;settings['exposure']=-.4;develop(source,settings,cache=cache)
    assert cache.hits>=hits+2
    np.testing.assert_array_equal(source,before)


def test_camera_profile_save_retarget_and_mismatch():
    p=record(make_dcp());settings=defaults();settings.update(raw_mode='as_shot',dcp_profile=p)
    assert rawcolor.retarget(settings,{'format':'NEF','camera':'Test Camera'})['dcp_profile']==p
    assert rawcolor.retarget(settings,{'format':'NEF','camera':'Other'})['dcp_profile'] is None
    copied=rawcolor.retarget(settings,{'format':'JPG'})
    assert copied['raw_mode']=='legacy' and copied['dcp_profile'] is None
    info=dict(camera='Other',xyz_to_camera=np.eye(3).tolist(),neutral=[1,1,1],daylight_neutral=[1,1,1])
    with pytest.raises(ValueError,match='모델'):
        rawcolor.develop_camera(rawcolor.CameraSource(np.ones((2,2,3)),info),settings)


def test_real_libraw_dng_metadata_and_sensor_neutral(tmp_path):
    import tifffile
    from luma.engine import load_image
    path=tmp_path/'calibrated.dng'
    cm=np.array([[.9,-.2,-.05],[-.4,1.2,.2],[-.05,.1,.8]])
    neutral=np.array([.5,1.,2/3]);raw=np.empty((100,150),np.uint16)
    for ys,xs,c in [(0,0,0),(0,1,1),(1,0,1),(1,1,2)]:raw[ys::2,xs::2]=np.uint16(10000*neutral[c])
    rational=lambda a:tuple(v for x in np.asarray(a).ravel() for v in (round(float(x)*10000),10000))
    tags=[(50706,'B',4,(1,4,0,0),False),(50707,'B',4,(1,1,0,0),False),
          (50708,'s',0,'Luma Test Camera',False),(271,'s',0,'Luma',False),(272,'s',0,'Test Camera',False),
          (33421,'H',2,(2,2),False),(33422,'B',4,(0,1,1,2),False),(50710,'B',3,(0,1,2),False),
          (50711,'H',1,1,False),(50714,'I',1,0,False),(50717,'I',1,65535,False),
          (50721,'2i',9,rational(cm),False),(50778,'H',1,21,False),(50728,'2I',3,(1,2,1,1,2,3),False)]
    tifffile.imwrite(path,raw,photometric=32803,metadata=None,extratags=tags)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    settings=defaults();settings['raw_mode']='as_shot'
    source,info=load_image(path,raw_options=settings)
    assert info['raw_native'] and source.raw_info['unique_camera']=='Luma Test Camera'
    np.testing.assert_allclose(rawcolor.default_profile(source.raw_info)['cm1'],cm)
    result=develop(source,settings)
    center=result[20:-20,20:-20]
    assert np.max(np.ptp(center,axis=-1))<.0003
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_camera_calibration_signature_and_analog_balance():
    profile=dcp.parse(make_dcp([(50964,12,np.diag(dcp.D50).ravel()),(50932,2,'Luma\0')]))
    calibration=[np.diag([1.1,.95,1.2]).tolist()]*2
    analog=[.9,1.,1.1];neutral=np.array([.5,1,.7])
    matrix=dcp.camera_to_xyz(profile,neutral,6504,calibration,analog,'Luma')
    np.testing.assert_allclose(matrix@neutral,dcp.D50)
    profile['fm1']=np.array([[.6,.3,.0643],[.2,.9,-.1],[.01,-.1,.9151]])
    a=dcp.camera_to_xyz(profile,neutral,6504,calibration,analog,'Luma')
    b=dcp.camera_to_xyz(profile,neutral,6504,calibration,analog,'Unrelated')
    # Diagonal calibration cancels under forward WB; non-diagonal calibration doesn't.
    calibration[0][0][1]=.06;calibration[1][0][1]=.06
    c=dcp.camera_to_xyz(profile,neutral,6504,calibration,analog,'Luma')
    assert not np.allclose(c,b)
    np.testing.assert_allclose(c@neutral,dcp.D50)
