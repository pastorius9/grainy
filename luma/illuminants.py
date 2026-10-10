"""DNG custom illuminants and three-light interpolation.

Interpolation and observer normalization follow Adobe DNG SDK 1.7.1.
Copyright 2006-2019 Adobe Systems Incorporated. All Rights Reserved.
Adobe permits use under the accompanying assets/licenses/Color/DNG-SDK.txt.
CIE observer data and its separate CC BY-SA 4.0 notice are bundled unchanged.
"""
from functools import lru_cache
from pathlib import Path
import hashlib
import io
import struct
import numpy as np


STANDARD_XY = {
    1:(.3324,.3474), 2:(.37208,.37529), 3:(.4476,.4074), 4:(.3324,.3474),
    9:(.3324,.3474), 10:(.3127,.3290), 11:(.2990,.3149),
    12:(.31310,.33727), 13:(.34588,.35875), 14:(.37417,.37281),
    15:(.40910,.39430), 16:(.44018,.40329), 17:(.4476,.4074),
    18:(.348483,.351747), 19:(.310061,.316150), 20:(.3324,.3474),
    21:(.3127,.3290), 22:(.2990,.3149), 23:(.3457,.3585),
}


def xyz_to_xy(xyz):
    a=np.asarray(xyz,dtype=np.float64)
    if a.shape!=(3,) or not np.isfinite(a).all() or a.sum()<=0:
        raise ValueError('카메라 백색점이 유효하지 않습니다.')
    return a[:2]/a.sum()


def xy_to_xyz(xy):
    a=np.asarray(xy,dtype=np.float64)
    if a.shape!=(2,) or not np.isfinite(a).all():
        raise ValueError('광원 색도 좌표가 잘못되었습니다.')
    a=np.clip(a,1e-6,.999999)
    if a.sum()>.999999:a=a*(.999999/a.sum())
    x,y=a
    return np.array([x/y,1.,(1-x-y)/y])


@lru_cache(maxsize=1)
def _sdk_temperature_table():
    from .rawcolor import LINES,DIRECTIONS
    lines=LINES.copy()
    # Adobe's published SDK table uses .24702 at 325 mired. Preserve its
    # profile interpolation while retaining the existing UI's corrected .24792.
    lines[lines[:,0]==325,1]=.24702
    lines.setflags(write=False)
    return lines,DIRECTIONS


def temperature_tint(xy):
    """SDK Robertson boundary behavior; separate from existing UI clamping."""
    LINES,DIRECTIONS=_sdk_temperature_table()
    xy=np.asarray(xy,dtype=np.float64)
    if xy.shape!=(2,) or not np.isfinite(xy).all() or 1.5-xy[0]+6*xy[1]<=0:
        raise ValueError('광원 색도에서 색온도를 계산할 수 없습니다.')
    x,y=xy
    uv=np.array([2*x,3*y])/(1.5-x+6*y)
    delta=uv-LINES[:,1:3]
    distances=delta[:,1]*DIRECTIONS[:,0]-delta[:,0]*DIRECTIONS[:,1]
    crossings=np.flatnonzero(distances[1:]<=0)
    i=int(crossings[0]+1) if len(crossings) else 30
    dt=max(-float(distances[i]),0.)
    f=0. if i==1 else dt/(float(distances[i-1])+dt)
    mired=LINES[i-1,0]*f+LINES[i,0]*(1-f)
    locus=LINES[i-1,1:3]*f+LINES[i,1:3]*(1-f)
    direction=DIRECTIONS[i-1]*f+DIRECTIONS[i]*(1-f)
    direction/=np.linalg.norm(direction)
    return float(1e6/mired),-float((uv-locus)@direction)*3000


def temperature_xy(kelvin,tint=0.):
    """SDK temperature-to-xy, including its historical 325-mired table entry."""
    if not np.isfinite([kelvin,tint]).all() or kelvin<=0:
        raise ValueError('광원 색온도는 유효한 양수여야 합니다.')
    lines,directions=_sdk_temperature_table()
    r=1e6/kelvin
    i=min(int(np.searchsorted(lines[1:,0],r,side='right')),29)
    f=(lines[i+1,0]-r)/(lines[i+1,0]-lines[i,0])
    uv=lines[i,1:3]*f+lines[i+1,1:3]*(1-f)
    direction=directions[i]*f+directions[i+1]*(1-f)
    uv+=direction/np.linalg.norm(direction)*(tint/-3000.)
    u,v=uv
    return np.array([1.5*u,v])/(u-4*v+2)


@lru_cache(maxsize=1)
def observer():
    data=(Path(__file__).resolve().parents[1]/'assets/color/CIE_xyz_1931_2deg.csv').read_bytes()
    if hashlib.sha256(data).hexdigest()!='fa663e3535a7e0763a745993a1f0a192eb0275ac46ad2d1befd7626841e713c1':
        raise ValueError('설치된 CIE 광원 자료가 손상되었습니다. 앱을 다시 설치하세요.')
    a=np.loadtxt(io.BytesIO(data),delimiter=',')
    if a.shape!=(471,4) or not np.array_equal(a[:,0],np.arange(360,831)):
        raise ValueError('설치된 CIE 광원 자료의 범위가 잘못되었습니다.')
    a.setflags(write=False)
    return a


def custom_xy(payload,endian):
    if not isinstance(payload,bytes) or not 2<=len(payload)<=8022:
        raise ValueError('사용자 지정 광원 데이터 크기가 잘못되었습니다.')
    kind=struct.unpack_from(endian+'H',payload)[0]
    def rational(offset,count):
        pairs=np.asarray(struct.unpack_from(endian+str(count*2)+'I',payload,offset),dtype=float).reshape(-1,2)
        if np.any(pairs[:,1]==0):raise ValueError('사용자 지정 광원 데이터 분모가 0입니다.')
        return pairs[:,0]/pairs[:,1]
    if kind==0:
        if len(payload)!=18:raise ValueError('사용자 지정 광원 색도 데이터 길이가 잘못되었습니다.')
        xy=rational(2,2)
        if np.any(xy<1e-6) or np.any(xy>.999999):raise ValueError('광원 색도 좌표가 범위를 벗어났습니다.')
        return xyz_to_xy(xy_to_xyz(xy))
    if kind!=1:raise ValueError('지원하지 않는 사용자 지정 광원 데이터 형식입니다.')
    if len(payload)<22:raise ValueError('광원 스펙트럼이 불완전합니다.')
    count=struct.unpack_from(endian+'I',payload,2)[0]
    if not 2<=count<=1000 or len(payload)!=22+8*count:
        raise ValueError('광원 스펙트럼 표본 수가 잘못되었습니다.')
    start,step=rational(6,2)
    if start<=0 or step<=0:raise ValueError('광원 파장과 간격은 양수여야 합니다.')
    samples=rational(22,count);table=observer()
    power=np.interp(table[:,0],start+np.arange(count)*step,samples)
    xyz=(table[:,1:]*power[:,None]).sum(axis=0)/table[:,1:].sum(axis=0)
    if np.any(xyz<=0):raise ValueError('광원 스펙트럼에서 유효한 백색점을 얻을 수 없습니다.')
    return xyz_to_xy(xyz)


def light_xy(code,payload=None,endian='<'):
    if code==255:return custom_xy(payload,endian)
    if code==24:
        return temperature_xy(3200.)
    value=STANDARD_XY.get(code)
    return np.asarray(value,dtype=float) if value is not None else None


@lru_cache(maxsize=32)
def _light_points(whites):
    result=np.asarray([temperature_tint(xy) for xy in whites])
    result[:,0]=np.minimum(1500/result[:,0],1.)
    result[:,1]/=200.
    result.setflags(write=False)
    return result


def triple_weights(whites,white):
    t,tint=temperature_tint(xyz_to_xy(white))
    points=_light_points(tuple(tuple(xy) for xy in whites))
    distance=np.sum((points-np.array([min(1500/t,1.),tint/200.]))**2,axis=1)
    weights=1/(distance+1e-8);weights/=weights.sum()
    weights=weights*weights*(3-2*weights)
    weights=np.clip((weights-.02)/.98,0,1);weights/=weights.sum()
    weights[2]=max(1-weights[0]-weights[1],0.)
    return weights
