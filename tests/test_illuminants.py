import hashlib
import json
import struct
from pathlib import Path
from copy import deepcopy
import numpy as np
import pytest
from luma import dcp,rawcolor,illuminants as light,engine
from test_rawcolor import make_dcp,record


def xy_payload(xy,endian='<'):
    return struct.pack(endian+'H4I',0,round(xy[0]*1000000),1000000,round(xy[1]*1000000),1000000)


def spectrum_payload(start=360,step=470,samples=(1,1),endian='<'):
    return struct.pack(endian+'HI4I',1,len(samples),start,1,step,1)+struct.pack(
        endian+str(2*len(samples))+'I',*[v for sample in samples for v in (sample,1)])


def triple_data(extra=(),endian='<'):
    values=[(50778,3,[17]),(50779,3,[21]),(52529,3,[2]),
            (50721,12,[.9,.03,.01,.02,1,.02,.01,.03,.9]),
            (50722,12,[.85,.04,.02,.01,1.02,.01,.01,.01,.95]),
            (52531,12,[.87,.03,.015,.01,1.01,.02,.01,.02,.91])]
    overrides={v[0] for v in extra}
    return make_dcp([v for v in values if v[0] not in overrides]+list(extra),endian)


@pytest.mark.parametrize('kind',['weights','temperature','spectrum','xy'])
def test_matches_independent_compiled_sdk_reference(kind):
    fixture=json.loads((Path(__file__).parent/'fixtures/illuminants-sdk-reference.json').read_text())
    assert fixture['provenance']['status']=='passed'
    for c in fixture['cases']:
        if c['kind']!=kind:continue
        if kind=='weights':actual=light.triple_weights(c['anchors'],c['white'])
        elif kind=='temperature':actual=light.temperature_tint(c['xy'])
        elif kind=='xy':actual=light.temperature_xy(c['kelvin'],c['tint'])
        else:actual=light.custom_xy(spectrum_payload(c['start'],c['step'],c['samples']),'<')
        np.testing.assert_allclose(actual,c['expected'],atol=1e-10 if kind=='temperature' else 1e-12,rtol=0,
                                   err_msg=str(c))


def test_iso_studio_illuminant_uses_sdk_3200k_white():
    fixture=json.loads((Path(__file__).parent/'fixtures/illuminants-sdk-reference.json').read_text())
    c=next(c for c in fixture['cases'] if c['kind']=='xy' and c['kelvin']==3200 and c['tint']==0)
    profile=dcp.parse(triple_data([(52529,3,[24])]))
    np.testing.assert_allclose(profile['xy3'],c['expected'],atol=1e-12,rtol=0)
    np.testing.assert_allclose(dcp.weight(profile,3200,light.xy_to_xyz(c['expected'])),[0,0,1],atol=1e-12)


@pytest.mark.parametrize('endian',['<','>'])
@pytest.mark.parametrize('spectrum',[False,True])
def test_custom_single_dual_and_triple_roundtrip(endian,spectrum):
    payload=spectrum_payload(endian=endian) if spectrum else xy_payload([.31,.33],endian)
    p=dcp.parse(make_dcp([(50778,3,[255]),(52533,7,payload)],endian))
    np.testing.assert_allclose(p['xy1'],[1/3,1/3] if spectrum else [.31,.33],atol=1e-12)
    assert 1000<p['t1']<100000
    p=dcp.parse(make_dcp([(50778,3,[255]),(52533,7,payload),(50779,3,[17]),
                         (50722,12,np.eye(3).ravel())],endian))
    assert dcp.weight(p,p['t1'])==1 and dcp.weight(p,p['t2'])==0
    data=triple_data([(52529,3,[255]),(52535,7,payload)],endian)
    p=dcp.compiled(record(data))
    np.testing.assert_allclose(dcp.weight(p,5000,light.xy_to_xyz(p['xy3'])),[0,0,1],atol=1e-12)


@pytest.mark.parametrize('payload',[None,b'',b'\0\0',struct.pack('<H',2),
    struct.pack('<H4I',0,1,0,1,2),xy_payload([0,.3]),xy_payload([1,.3]),
    spectrum_payload(samples=(1,)),spectrum_payload(samples=(0,0)),
    spectrum_payload(step=0),spectrum_payload(start=0),spectrum_payload()+b'x',
    struct.pack('<HI4I',1,1000000,360,1,1,1)])
def test_invalid_custom_illuminants_rejected(payload):
    extra=[(50778,3,[255])]
    if payload is not None:extra.append((52533,7,payload))
    with pytest.raises(ValueError):dcp.parse(make_dcp(extra))


@pytest.mark.parametrize('extra',[
    [(52529,3,[0])],[(52529,3,[17])],[(52529,3,[255])],
    [(52531,12,np.zeros(9))],[(52532,12,np.eye(3).ravel())],
    [(50937,4,[1,2,1]),(52537,11,[0,1,1,0,1,1])],
    [(52535,7,xy_payload([.3127,.329])),(52529,3,[255])],
])
def test_invalid_triple_combinations_fail_before_render(extra):
    with pytest.raises(ValueError):dcp.parse(triple_data(extra))


def test_partial_third_illuminant_never_silently_uses_two():
    for tag in (52529,52532,52535,52537):
        with pytest.raises(ValueError):dcp.parse(make_dcp([(tag,3,[1])]))
    data=make_dcp([(52531,12,np.eye(3).ravel()),(52529,3,[2])])
    with pytest.raises(ValueError):dcp.parse(data)


def test_custom_dual_uses_same_white_point_in_both_directions():
    xy=light.xyz_to_xy(rawcolor.temperature_xyz(3100,12))
    p=dcp.parse(make_dcp([(50778,3,[255]),(52533,7,xy_payload(xy)),
        (50779,3,[21]),(50722,12,(np.eye(3)*1.1).ravel())]))
    white=light.xy_to_xyz(p['xy1'])
    actual_t=rawcolor.xyz_temperature(white)[0]
    assert dcp.weight(p,actual_t,white)==1.
    neutral=p['cm1']@white
    recovered,_,_=rawcolor.neutral_white(p,neutral,{})
    np.testing.assert_allclose(light.xyz_to_xy(recovered),p['xy1'],atol=1e-7)


@pytest.mark.parametrize('temperature,tint',[(2500,0),(3200,12),(5000,-20),(6500,30),(9500,0)])
def test_three_matrices_white_recovery_calibration_and_forward(temperature,tint):
    profile=dcp.parse(triple_data([(50932,2,'body\0')]))
    info=dict(calibration_signature='body',analog=[.9,1,1.1],calibration=[
        [[1,.01,0],[0,1,0],[0,0,1]],[[.99,0,0],[.01,1.01,0],[0,0,1]],
        [[1,0,.01],[0,1,0],[.01,0,.98]]])
    white=rawcolor.temperature_xyz(temperature,tint)
    w=dcp.weight(profile,temperature,white)
    neutral=rawcolor.individual_matrix(profile,info,w)@dcp.interpolate(profile,'cm',w)@white
    recovered,_,_=rawcolor.neutral_white(profile,neutral,info)
    np.testing.assert_allclose(light.xyz_to_xy(recovered),light.xyz_to_xy(white),atol=2e-7,rtol=0)
    for forward in (False,True):
        if forward:
            for i in (1,2,3):profile[f'fm{i}']=np.diag(dcp.D50)
        matrix=dcp.camera_to_xyz(profile,neutral,temperature,info['calibration'],info['analog'],'body')
        np.testing.assert_allclose(matrix@neutral,dcp.D50,atol=1e-10)
    mismatch=rawcolor.individual_matrix(profile,{**info,'calibration_signature':'different'},w)
    np.testing.assert_array_equal(mismatch,np.diag(info['analog']))


def test_same_temperature_different_tint_changes_triple_huesat():
    extras=[(50937,4,[2,2,1])]
    for tag,hue in [(50938,0),(50939,20),(52537,50)]:
        table=np.zeros((1,2,2,3));table[...,0]=hue;table[...,1:]=1
        extras.append((tag,11,table.ravel()))
    p=dcp.parse(triple_data(extras));maps=[]
    for tint in (-30,30):
        w=dcp.weight(p,5000,rawcolor.temperature_xyz(5000,tint));maps.append(dcp.interpolate(p,'hs',w))
        assert abs(float(w.sum())-1)<1e-12 and np.all(w>=0)
    assert np.max(np.abs(maps[0]-maps[1]))>.1


def write_triple_dng(path,endian='<'):
    import tifffile
    profile=dcp.parse(triple_data([(52529,3,[255]),(52535,7,spectrum_payload())]))
    white=rawcolor.temperature_xyz(4200,8);weights=dcp.weight(profile,4200,white)
    calibration=[np.eye(3),np.eye(3),np.array([[1,.01,0],[0,1,.015],[.01,0,1]])]
    neutral=dcp.calibration_matrix(calibration,weights)@dcp.interpolate(profile,'cm',weights)@white;neutral/=neutral[1]
    pixels=np.empty((100,150),np.uint16)
    for ys,xs,c in [(0,0,0),(0,1,1),(1,0,1),(1,1,2)]:pixels[ys::2,xs::2]=np.uint16(10000*neutral[c])
    rational=lambda a:tuple(v for x in np.ravel(a) for v in (round(float(x)*1000000),1000000))
    payload=spectrum_payload(endian=endian)
    tags=[(50706,'B',4,(1,6,0,0),False),(50707,'B',4,(1,1,0,0),False),
          (50708,'s',0,'Test Camera',False),(271,'s',0,'Test',False),(272,'s',0,'Test Camera',False),
          (33421,'H',2,(2,2),False),(33422,'B',4,(0,1,1,2),False),(50710,'B',3,(0,1,2),False),
          (50711,'H',1,1,False),(50714,'I',1,0,False),(50717,'I',1,65535,False),
          (50728,'2I',3,rational(neutral),False),(50778,'H',1,17,False),
          (50779,'H',1,21,False),(52529,'H',1,255,False),(52535,7,len(payload),payload,False),
          (50931,'s',0,'Luma fixture',False),(50932,'s',0,'Luma fixture',False)]
    tags += [(tag,'2i',9,rational(profile[f'cm{i}']),False) for i,tag in enumerate((50721,50722,52531),1)]
    tags += [(tag,'2i',9,rational(value),False) for tag,value in zip((50723,50724,52530),calibration)]
    tifffile.imwrite(path,pixels,photometric=32803,metadata=None,extratags=tags,byteorder=endian)
    return path


@pytest.mark.parametrize('endian',['<','>'])
def test_real_libraw_triple_dng_export_and_json_restore(tmp_path,endian):
    import tifffile
    path=write_triple_dng(tmp_path/'triple.dng',endian);digest=hashlib.sha256(path.read_bytes()).hexdigest()
    settings=engine.defaults();settings['raw_mode']='as_shot'
    source,metadata=engine.load_image(path,raw_options=settings)
    assert isinstance(source,rawcolor.CameraSource)
    p=rawcolor.default_profile(source.raw_info);assert p['cm3'] is not None
    assert source.raw_info['calibration_signature']=='Luma fixture'
    assert source.raw_info['calibration'][2][0][1]==.01
    np.testing.assert_allclose(p['xy3'],[1/3,1/3],atol=1e-12)
    pixels=engine.develop(source,settings)
    assert np.max(np.ptp(pixels[20:-20,20:-20],axis=-1))<.0003
    restored=rawcolor.CameraSource(np.asarray(source),json.loads(json.dumps(source.raw_info)))
    np.testing.assert_array_equal(engine.develop(restored,settings),pixels)
    export=tmp_path/'output.tif';engine.export_image(path,export,settings,'TIFF 16-bit')
    np.testing.assert_array_equal(tifffile.imread(export),np.uint16(pixels*65535+.5))
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
