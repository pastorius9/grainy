import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from luma.lua_data import parse,sequence,DataError
from luma.adobe_develop import convert,from_lua,from_xmp,from_xmp_bytes,summary
from luma.engine import defaults,develop


def test_lua_data_unicode_comments_mixed_arrays_and_exact_integer():
    text=r'''-- Lightroom data
    s = { name="서울", encoded="\236\132\156\236\154\184", -- same UTF-8 bytes
      large=9007199254740993, hex=0xff, exp=-1.25e-2, yes=true, no=false,
      ["nested"]={ {0,0}, {255,255} }, [7]=[[
line one
line two]], --[=[ ignored { } ]=]
    };'''
    data=parse(text)
    assert data['encoded']==data['name']=='서울'
    assert data['large']==9007199254740993 and data['hex']==255 and data['exp']==-.0125
    assert data['yes'] is True and data['no'] is False
    assert data[7]=='line one\nline two'
    assert [sequence(point) for point in sequence(data['nested'])]==[[0,0],[255,255]]
    assert parse('return {foo=[==[a ]] b]==], empty=nil}')=={'foo':'a ]] b','empty':None}


@pytest.mark.parametrize('text',[
    's={Exposure2012=os.execute("bad")}', 's={1+2}', 's={};os.execute("bad")',
    's={a=1,a=2}', 's={[1]=2,3}', 'return {a=1e999}', 'return {a="\\999"}',
    's={a="\\255"}', 's={a=unknown}', 's={a=function() end}', 's={a=1 b=2}',
    's={[{}]=3}', 's={a=[=[unclosed}',
])
def test_lua_rejects_executable_ambiguous_or_broken_data(text):
    with pytest.raises(ValueError):parse(text)


def test_lua_limits_and_non_contiguous_arrays():
    with pytest.raises(DataError):parse('s={name="서울"}',max_bytes=14)
    with pytest.raises(DataError):parse('{{{{{}}}}}',max_depth=2)
    with pytest.raises(DataError):parse('{1,2,3}',max_items=3)
    with pytest.raises(DataError):sequence({1:'a',3:'b'})


def test_develop_mapping_preserves_base_and_reports_unsupported():
    base=defaults();base.update(exposure=-1,texture=14)
    original=deepcopy(base)
    result=convert(from_lua('''s={Exposure2012=1.25,Blacks2012=-70,Whites2012=80,
       HueAdjustmentRed=22,LuminanceAdjustmentBlue=-18,RedHue=5,
       ToneCurvePV2012={0,8,128,150,255,247},ConvertToGrayscale=false,
       ParametricShadows=8,ColorGradeMidtoneHue=170,ColorGradeMidtoneSat=20,
       CameraProfile="Adobe Color",LensProfileEnable=1,Sharpness=150}'''),base=base)
    s=result['settings']
    assert s['exposure']==1.25 and s['blacks']==-70 and s['whites']==80 and s['texture']==14
    assert s['hsl'][0][0]==22 and s['hsl'][5][2]==-18 and s['mixer_mode']=='hsl'
    assert s['calibration'][0][0]==5 and s['grading'][1]==[170,20,0]
    assert np.allclose(s['curve'],[[0,8/255],[128/255,150/255],[1,247/255]])
    # Sharpness 150 is in range since 0.5.57 (Grainy's slider is 0–150 like Lightroom's).
    assert s['parametric'][0]==8 and set(result['omitted'])=={'CameraProfile','LensProfileEnable'} and s['sharpen']==150
    assert base==original
    pixels=np.full((48,64,3),.18,np.float32)
    output=develop(pixels,s)
    assert np.isfinite(output).all() and not np.array_equal(output,develop(pixels,base))
    assert 'CameraProfile' in summary(result)


def test_raw_white_balance_and_crop_are_target_aware():
    base={**defaults(),'temperature':50,'tint':12,'kelvin_enabled':True}
    s=convert({'WhiteBalance':'As Shot','Temperature':5555,'Tint':10},base=base,is_raw=True)['settings']
    assert s['raw_mode']=='as_shot' and s['temperature']==s['tint']==0 and not s['kelvin_enabled']
    s=convert({'WhiteBalance':'Custom','Temperature':4800,'Tint':-7},is_raw=True)['settings']
    assert (s['raw_mode'],s['raw_kelvin'],s['raw_tint'])==('custom',4800,-7)
    assert convert({'Temperature':4800},is_raw=False)['omitted']['Temperature']
    crop={'HasCrop':True,'CropLeft':.1,'CropTop':.2,'CropRight':.9,'CropBottom':.8,'CropAngle':0}
    assert convert(crop)['settings']['crop']==[.1,.2,.9,.8]
    for kwargs in ({'orientation':'BC'},{'orientation':6}):
        result=convert(crop,**kwargs)
        assert result['settings']['crop'] is None and 'HasCrop' in result['omitted']
    assert convert({**crop,'CropAngle':2})['settings']['crop'] is None
    assert convert({'HasCrop':False},base={'crop':[.1,.1,.8,.8]})['settings']['crop'] is None


def test_invalid_edits_never_silently_clamp_or_reset():
    data={'Exposure2012':9,'Blacks2012':True,'ToneCurvePV2012':{1:255,2:255,3:0,4:0},
          'ParametricShadowSplit':12,'ParametricShadows':20,'Texture':'NaN'}
    result=convert(data,base={'exposure':1,'texture':12})
    assert result['settings']['exposure']==1 and result['settings']['texture']==12
    assert set(result['omitted'])==set(data) and not result['mapped']
    result=convert({'AlreadyApplied':True,'Exposure2012':3},base={'exposure':1})
    assert result['settings']['exposure']==1 and result['mapped']==[]


XMP='''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/">
 <rdf:RDF><rdf:Description crs:Exposure2012="0.75" crs:WhiteBalance="Custom" crs:Temperature="4800" crs:Tint="-7">
 <crs:ToneCurvePV2012><rdf:Seq><rdf:li>0, 5</rdf:li><rdf:li>255, 250</rdf:li></rdf:Seq></crs:ToneCurvePV2012>
 <crs:PaintBasedCorrections><rdf:Seq><rdf:li rdf:parseType="Resource"><crs:LocalExposure2012>1</crs:LocalExposure2012></rdf:li></rdf:Seq></crs:PaintBasedCorrections>
 </rdf:Description></rdf:RDF></x:xmpmeta>'''


def test_xmp_variants_and_unsafe_xml(tmp_path):
    path=tmp_path/'preset.xmp';path.write_text(XMP,encoding='utf-16')
    result=convert(from_xmp(path),is_raw=True)
    assert result['settings']['exposure']==.75 and result['settings']['raw_kelvin']==4800
    assert result['settings']['curve'][0]==[0,5/255]
    assert 'PaintBasedCorrections' in result['omitted']
    for data in (b'<!DOCTYPE a [<!ENTITY a "boom">]><a>&a;</a>',b'x'*(16*1024*1024+1)):
        with pytest.raises(DataError):from_xmp_bytes(data)
    with pytest.raises(DataError):from_xmp_bytes(XMP.replace('crs:Exposure2012="0.75"','crs:Exposure2012="0.75"').replace('</rdf:Description>','<crs:Exposure2012>3</crs:Exposure2012></rdf:Description>').encode())


@pytest.mark.parametrize('extension',['xmp','lrtemplate'])
def test_partial_preset_portability_raw_target_and_source_integrity(tmp_path,extension):
    from luma.adobe_preset import read,resolve,KEY
    from luma.catalog import Catalog
    text=XMP if extension=='xmp' else 's={value={settings={Exposure2012=.75,Temperature=4800,Tint=-7,WhiteBalance="Custom"}}}'
    path=tmp_path/f'test.{extension}';path.write_text(text,encoding='utf-8')
    original=path.read_bytes();preset=read(path)
    with_catalog=tmp_path/'catalog'
    cat=Catalog(with_catalog);cat.save_adobe_preset('Adobe',preset);cat.close()
    cat=Catalog(with_catalog)
    try:
        stored=cat.presets()['Adobe']
        assert base64.b64decode(stored[KEY]['data'])==original
        base={**defaults(),'shadows':24,'crop':[.1,.1,.9,.9],'temperature':14}
        output=resolve(stored,base,'test.nef')
        assert output['exposure']==.75 and output['shadows']==24 and output['crop']==base['crop']
        assert output['raw_mode']=='custom' and output['raw_kelvin']==4800
        jpeg=resolve(stored,base,'test.jpg')
        assert jpeg['raw_mode']=='legacy' and jpeg['temperature']==14 and jpeg['exposure']==.75
        assert path.read_bytes()==original
        bad=deepcopy(stored);bad[KEY]['sha256']='0'*64
        with pytest.raises(DataError):resolve(bad,base,'test.jpg')
    finally:cat.close()
