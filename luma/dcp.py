"""Bounded DNG camera-profile reader and color transforms (DNG 1.7.1, ch. 6).

This product includes DNG technology under license by Adobe.
Profile bytes/copyright are preserved; no third-party profiles ship with Luma.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from functools import lru_cache
from pathlib import Path
import numpy as np

MAX_PROFILE_BYTES = 4 * 1024 * 1024
D50 = np.array([.9642956764, 1., .8251046025])
_PRIMARIES = np.array([[.7347/.2653,.1596/.8404,.0366/.0001],
                      [1.,1.,1.],[0.,0.,(1-.0366-.0001)/.0001]])
PROPHOTO_XYZ = _PRIMARIES @ np.diag(np.linalg.solve(_PRIMARIES,D50))
XYZ_PROPHOTO = np.linalg.inv(PROPHOTO_XYZ)
BRADFORD = np.array([[.8951,.2664,-.1614],[-.7502,1.7135,.0367],[.0389,-.0685,1.0296]])
ILLUMINANTS = {1:5500.,2:4200.,3:2856.,4:5500.,9:5500.,10:6500.,11:7500.,
              12:6430.,13:5000.,14:4150.,15:3450.,16:2940.,17:2856.,18:4874.,
              19:6774.,20:5503.,21:6504.,22:7504.,23:5003.,24:3200.}


def read_tags(data,wanted=None):
    """Read a standalone DCP IFD; validate every range before allocating arrays."""
    if not 8 <= len(data) <= MAX_PROFILE_BYTES:
        raise ValueError('DCP 파일 크기가 잘못되었거나 4MB를 초과합니다.')
    endian = {b'II':'<', b'MM':'>'}.get(data[:2])
    if endian is None or struct.unpack_from(endian+'H',data,2)[0] not in (42,0x4352):
        raise ValueError('DCP/TIFF 카메라 프로파일이 아닙니다.')
    offset = struct.unpack_from(endian+'I',data,4)[0]
    if offset < 8 or offset+2 > len(data):raise ValueError('잘못된 DCP 디렉터리입니다.')
    count = struct.unpack_from(endian+'H',data,offset)[0]
    if count > 512 or offset+2+12*count+4 > len(data):raise ValueError('잘못된 DCP 태그입니다.')
    formats = {1:('B',1),2:('B',1),3:('H',2),4:('I',4),5:('I',8),
               7:('B',1),9:('i',4),10:('i',8),11:('f',4),12:('d',8)}
    tags = {};seen=set()
    for i in range(count):
        pos = offset+2+12*i
        tag,kind,n = struct.unpack_from(endian+'HHI',data,pos)
        if tag in seen:raise ValueError('DCP 태그가 중복되었습니다.')
        seen.add(tag)
        if kind not in formats:raise ValueError('지원하지 않는 DCP 태그 형식입니다.')
        code,width = formats[kind];size = n*width
        start = pos+8 if size <= 4 else struct.unpack_from(endian+'I',data,pos+8)[0]
        if not n or start+size > len(data):raise ValueError('DCP 데이터 범위가 잘못되었습니다.')
        if wanted is not None and tag not in wanted:continue
        if kind==7:
            value=data[start:start+size]
        elif kind==2 or kind==1 and tag in (50708,50932,50936,50942,52552):
            value = data[start:start+size].rstrip(b'\0').decode('utf-8',errors='replace')
        else:
            value = np.array(struct.unpack_from(endian+str(n*(2 if kind in (5,10) else 1))+code,data,start),dtype=np.float64)
            if kind in (5,10):
                value = value.reshape(-1,2)
                if np.any(value[:,1]==0):raise ValueError('DCP 분모가 0입니다.')
                value = value[:,0]/value[:,1]
            if not np.isfinite(value).all():raise ValueError('DCP에 유효하지 않은 수치가 있습니다.')
        tags[tag] = value
    return tags


def parse(data):
    return parse_tags(read_tags(data), '<' if data[:2]==b'II' else '>')


def parse_tags(tags,endian='<'):
    """Shared profile validation for standalone DCP and embedded DNG matrices."""
    from . import illuminants
    def number(tag,default=0):
        value = tags.get(tag)
        if value is None:return default
        value=np.asarray(value)
        if value.size!=1 or not np.issubdtype(value.dtype,np.number) or not np.isfinite(value).all():
            raise ValueError('DCP 단일 값 태그가 잘못되었습니다.')
        return float(value.ravel()[0])
    def matrix(tag):
        if tag not in tags:return None
        a = np.asarray(tags[tag],dtype=np.float64)
        if a.size != 9:raise ValueError('이 DCP는 3채널 RGB 카메라용 프로파일이 아닙니다.')
        a = a.reshape(3,3)
        if not np.isfinite(a).all() or np.max(np.abs(a))>100 or np.linalg.cond(a)>1e6:raise ValueError('DCP 색상 행렬이 유효하지 않습니다.')
        return a
    if any(tag in tags for tag in (52525,52543,52544)):
        raise ValueError('추가 RGB/공간 게인 테이블을 사용하는 DCP는 아직 지원하지 않습니다.')
    # HDR profiles need a different nonlinear encoding, never silently render as SDR.
    if 52551 in tags:
        payload=tags[52551]
        if not isinstance(payload,bytes) or len(payload)!=8:raise ValueError('DCP 출력 범위 태그가 잘못되었습니다.')
        version,mode,hint=struct.unpack(endian+'HHf',payload)
        if version!=1 or mode!=0:raise ValueError('HDR DCP는 아직 지원하지 않습니다.')
    result = {'name':str(tags.get(50936,'DCP')), 'camera':str(tags.get(50708,'')),
              'copyright':str(tags.get(50942,'')), 'signature':str(tags.get(50932,'')),
              'policy':int(number(50941)), 'exposure':number(51109),
              'cm1':matrix(50721),'cm2':matrix(50722),'cm3':matrix(52531),
              'fm1':matrix(50964),'fm2':matrix(50965),'fm3':matrix(52532)}
    if result['cm1'] is None:raise ValueError('DCP ColorMatrix1이 없습니다.')
    if abs(result['exposure'])>10:raise ValueError('DCP 기준 노출이 범위를 벗어났습니다.')
    triple=result['cm3'] is not None
    if triple and (result['cm2'] is None or any(tag not in tags for tag in (50778,50779,52529))):
        raise ValueError('DCP 3개 광원의 행렬 또는 광원 정보가 불완전합니다.')
    if not triple and any(tag in tags for tag in (52529,52532,52535,52537,52538)):
        raise ValueError('DCP 세 번째 광원의 ColorMatrix가 없습니다.')
    result['custom_illuminants']=False
    for i,tag in enumerate((50778,50779,52529),1):
        value=number(tag)
        if value!=int(value):raise ValueError('DCP 광원 번호가 잘못되었습니다.')
        code=int(value);xy=illuminants.light_xy(code,tags.get(52532+i),endian)
        if code==255:result['custom_illuminants']=True
        result[f'xy{i}']=xy
        result[f't{i}']=illuminants.temperature_tint(xy)[0] if code==255 else ILLUMINANTS.get(code)
    if triple:
        whites=[result[f'xy{i}'] for i in (1,2,3)]
        if any(xy is None for xy in whites):raise ValueError('DCP 3개 광원의 백색점을 해석할 수 없습니다.')
        if any(np.max(np.abs(whites[i]-whites[j]))<1e-12 for i,j in ((0,1),(0,2),(1,2))):
            raise ValueError('DCP 3개 광원의 백색점은 서로 달라야 합니다.')
    if not triple and result['cm2'] is not None and (result['t1'] is None or result['t2'] is None or result['t1']==result['t2']):
        raise ValueError('DCP 두 광원의 색온도를 해석할 수 없습니다.')
    if result['cm2'] is not None and (result['fm1'] is None)!=(result['fm2'] is None):
        raise ValueError('DCP ForwardMatrix 쌍이 불완전합니다.')
    if result['cm2'] is None and (result['fm2'] is not None or 50939 in tags):
        raise ValueError('DCP 두 번째 광원의 ColorMatrix가 없습니다.')
    if triple and len({result[f'fm{i}'] is None for i in (1,2,3)})!=1:
        raise ValueError('DCP 3개 광원의 ForwardMatrix가 불완전합니다.')
    def table(dims_tag,data_tag):
        if data_tag not in tags:return None
        dims=np.asarray(tags.get(dims_tag,[]))
        if dims.size!=3 or np.any(dims!=np.floor(dims)):raise ValueError('DCP 색상표 크기가 잘못되었습니다.')
        h,s,v=map(int,dims)
        if not (1<=h<=360 and 2<=s<=256 and 1<=v<=256) or h*s*v>250000:
            raise ValueError('DCP 색상표가 너무 크거나 잘못되었습니다.')
        a=np.asarray(tags[data_tag],dtype=np.float32)
        if a.size != h*s*v*3:raise ValueError('DCP 색상표 데이터 수가 맞지 않습니다.')
        a=a.reshape(v,h,s,3)
        if np.any(a[...,1:]<0) or np.any(a[...,1:]>100) or np.any(np.abs(a[...,0])>360):
            raise ValueError('DCP 색상표 값이 범위를 벗어났습니다.')
        if not np.allclose(a[:,:,0,2],1,atol=1e-5):raise ValueError('DCP 중성색의 밝기 배율이 1이 아닙니다.')
        return a
    result.update(hs1=table(50937,50938),hs2=table(50937,50939),hs3=table(50937,52537),look=table(50981,50982))
    if result['hs2'] is not None and result['hs1'] is None:raise ValueError('DCP 첫 번째 색상표가 없습니다.')
    if triple and len({result[f'hs{i}'] is None for i in (1,2,3)})!=1:
        raise ValueError('DCP 3개 광원의 색상표가 불완전합니다.')
    for name,tag in [('hs_encoding',51107),('look_encoding',51108)]:
        result[name]=int(number(tag))
        if result[name] not in (0,1):raise ValueError('지원하지 않는 DCP 색상표 인코딩입니다.')
    curve=np.asarray(tags.get(50940,[0.,0.,1.,1.]),dtype=np.float64)
    if curve.size<4 or curve.size%2 or curve.size>8192:raise ValueError('DCP 톤 곡선 크기가 잘못되었습니다.')
    curve=curve.reshape(-1,2)
    # Some shipping SDR profiles (including Panasonic Camera Matching) end
    # before x=1. Match the DNG SDK's valid-curve domain and constant extension
    # at either end; adding synthetic endpoints would change the spline itself.
    if np.any(np.diff(curve[:,0])<=0) or np.any(curve<0) or np.any(curve>1):
        raise ValueError('DCP 톤 곡선 좌표가 잘못되었습니다.')
    result['curve']=curve
    return result


def load_profile(path):
    path=Path(path)
    if path.stat().st_size>MAX_PROFILE_BYTES:raise ValueError('DCP는 4MB 이하여야 합니다.')
    data=path.read_bytes();p=parse(data)
    return {'name':p['name'],'camera':p['camera'],'copyright':p['copyright'],
            'sha256':hashlib.sha256(data).hexdigest(),'data':base64.b64encode(data).decode('ascii')}


@lru_cache(maxsize=8)
def _compiled(encoded,digest):
    if len(encoded)>((MAX_PROFILE_BYTES+2)//3)*4:raise ValueError('저장된 DCP가 너무 큽니다.')
    try:data=base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError) as error:raise ValueError('저장된 DCP 데이터가 손상되었습니다.') from error
    if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('저장된 DCP 검증값이 일치하지 않습니다.')
    return parse(data)


def compiled(record):
    if not isinstance(record,dict):raise ValueError('저장된 DCP 정보가 잘못되었습니다.')
    return _compiled(record.get('data',''),record.get('sha256',''))


def camera_matches(profile,metadata):
    key=lambda s:''.join(c for c in str(s).casefold() if c.isalnum())
    camera=key(profile.get('camera',''))
    if not camera:return True
    return camera in {key(metadata.get('camera','')),key(str(metadata.get('make',''))+' '+str(metadata.get('camera',''))),key(metadata.get('unique_camera',''))}


def weight(profile,temperature,white=None):
    if profile.get('cm3') is not None:
        from .illuminants import triple_weights
        if white is None:
            from .rawcolor import temperature_xyz
            white=temperature_xyz(temperature)
        return triple_weights([profile[f'xy{i}'] for i in (1,2,3)],white)
    if profile.get('cm2') is None:return 1.
    if profile.get('custom_illuminants') and white is not None:
        from .illuminants import temperature_tint,xyz_to_xy
        temperature=temperature_tint(xyz_to_xy(white))[0]
    t1,t2=profile['t1'],profile['t2']
    return float(np.clip((1/temperature-1/t2)/(1/t1-1/t2),0,1))


def interpolate(profile,name,w):
    a,b=profile.get(name+'1'),profile.get(name+'2')
    if a is None:return None
    if np.ndim(w):return a*w[0]+b*w[1]+profile[name+'3']*w[2]
    return a if b is None else a*w+b*(1-w)


def calibration_matrix(calibration,w):
    if not calibration:return np.eye(3)
    if np.ndim(w):
        return sum(np.asarray(calibration[i] if i<len(calibration) else np.eye(3))*w[i] for i in range(3))
    return np.asarray(calibration[0])*w+np.asarray(calibration[min(1,len(calibration)-1)])*(1-w)


def adapt_white(source,destination=D50):
    source=np.asarray(source,dtype=np.float64);source=source/source[1]
    return np.linalg.solve(BRADFORD,np.diag((BRADFORD@destination)/(BRADFORD@source))@BRADFORD)


def camera_to_xyz(profile,neutral,temperature,calibration=None,analog=None,signature='',white=None):
    if (profile.get('cm3') is not None or profile.get('custom_illuminants')) and white is None:
        from .rawcolor import neutral_white
        white,_,_=neutral_white(profile,neutral,dict(calibration=calibration,
            analog=analog if analog is not None else [1.,1.,1.],calibration_signature=signature))
    w=weight(profile,temperature,white)
    cc=np.eye(3)
    if signature==profile.get('signature','') and calibration:
        cc=calibration_matrix(calibration,w)
    ab=np.diag(analog if analog is not None else np.ones(3))
    individual=ab@cc
    cm=interpolate(profile,'cm',w);fm=interpolate(profile,'fm',w)
    if fm is not None:
        inv=np.linalg.inv(individual);ref=inv@neutral
        if np.any(ref<=0):raise ValueError('DCP 카메라 중립점이 유효하지 않습니다.')
        # Round-off in published DCP forward matrices must not tint a neutral.
        fm=np.diag(D50/(fm@np.ones(3)))@fm
        return fm@np.diag(1/ref)@inv
    inverse=np.linalg.inv(individual@cm)
    white=inverse@neutral
    return adapt_white(white)@inverse/white[1]


def table_map(rgb,table,encoding=0):
    import cv2
    from .engine import to_linear,to_srgb
    a=np.maximum(np.asarray(rgb,dtype=np.float32),0)
    hsv=cv2.cvtColor(np.ascontiguousarray(a),cv2.COLOR_RGB2HSV)
    h,s,v=hsv[...,0]/360,hsv[...,1],hsv[...,2]
    vd,hd,sd=table.shape[:3]
    encoded=to_srgb(v) if encoding and vd>1 else v
    coords=[np.clip(encoded,0,1)*(vd-1),h*hd,np.clip(s,0,1)*(sd-1)]
    from . import native_dcp
    values=native_dcp.interpolate(coords,table)
    if values is None:values=_table_values_numpy(coords,table)
    new_v=np.clip(encoded*values[...,2],0,1)
    if encoding and vd>1:new_v=to_linear(new_v)
    hsv[...,0]=(h*360+values[...,0])%360
    hsv[...,1]=np.clip(s*values[...,1],0,1);hsv[...,2]=new_v
    return cv2.cvtColor(hsv,cv2.COLOR_HSV2RGB)


def _table_values_numpy(coords,table):
    """Original interpolation retained as a portable fallback/reference."""
    vd,hd,sd=table.shape[:3]
    lo=[np.floor(c).astype(np.int32) for c in coords]
    frac=[(c-i).astype(np.float32) for c,i in zip(coords,lo)]
    hi=[np.minimum(lo[0]+1,vd-1),(lo[1]+1)%hd,np.minimum(lo[2]+1,sd-1)]
    lo[1]%=hd
    vf,hf,sf=[f[...,None] for f in frac]
    def plane(vi):
        left=table[vi,lo[1],lo[2]]*(1-sf)+table[vi,lo[1],hi[2]]*sf
        right=table[vi,hi[1],lo[2]]*(1-sf)+table[vi,hi[1],hi[2]]*sf
        return left*(1-hf)+right*hf
    values=plane(lo[0])
    if vd>1:values=values*(1-vf)+plane(hi[0])*vf
    return values


@lru_cache(maxsize=16)
def _curve_lut(points):
    """Natural cubic spline, evaluated on a dense LUT without a SciPy runtime."""
    p=np.asarray(points).reshape(-1,2);x,y=p.T;n=len(x);h=np.diff(x)
    diag=np.ones(n);upper=np.zeros(n-1);lower=np.zeros(n-1);rhs=np.zeros(n)
    for i in range(1,n-1):
        lower[i-1]=h[i-1];diag[i]=2*(h[i-1]+h[i]);upper[i]=h[i]
        rhs[i]=6*((y[i+1]-y[i])/h[i]-(y[i]-y[i-1])/h[i-1])
    for i in range(1,n):
        f=lower[i-1]/diag[i-1];diag[i]-=f*upper[i-1];rhs[i]-=f*rhs[i-1]
    second=np.zeros(n);second[-1]=rhs[-1]/diag[-1]
    for i in range(n-2,-1,-1):second[i]=(rhs[i]-upper[i]*second[i+1])/diag[i]
    values=np.clip(np.linspace(0,1,65536),x[0],x[-1])
    index=np.clip(np.searchsorted(x,values)-1,0,n-2)
    width=h[index];a=(x[index+1]-values)/width;b=(values-x[index])/width
    return np.clip(a*y[index]+b*y[index+1]+((a*a*a-a)*second[index]+(b*b*b-b)*second[index+1])*width*width/6,0,1).astype(np.float32)


def tone_map(rgb,curve):
    if curve[0,0]==0 and curve[-1,0]==1 and np.array_equal(curve[:,0],curve[:,1]):return rgb
    lut=_curve_lut(tuple(curve.ravel()))
    # Preserve hue by mapping ordered extremes and interpolating the middle.
    a=np.clip(rgb,0,1);low=a.min(axis=-1);high=a.max(axis=-1)
    def lookup(v):
        p=v*65535;i=np.minimum(p.astype(np.int32),65534);f=p-i
        return lut[i]*(1-f)+lut[i+1]*f
    low2,high2=lookup(low),lookup(high)
    f=(a-low[...,None])/np.maximum(high-low,1e-10)[...,None]
    return (low2[...,None]+f*(high2-low2)[...,None]).astype(np.float32)
