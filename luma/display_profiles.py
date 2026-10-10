"""Read-only display profiles (Windows, macOS); no registry or calibration writes.

GetICMProfile returns the effective profile for a display DC. In Windows
Advanced Color mode its no-profile/sRGB answer must not be overridden by
enumerating installed HDR profiles (which would double-transform our surface).
"""
from dataclasses import dataclass,field
from functools import lru_cache
from pathlib import Path
import hashlib
import os
import re
import numpy as np
import imagecodecs
from . import colorio
from .user_paths import MAC

MAX_PROFILE_BYTES=16*1024**2
SYSTEM='macOS' if MAC else 'Windows'


@dataclass(frozen=True)
class DisplayProfile:
    mode:str='auto'
    screen:str=''
    path:str=''
    description:str='sRGB'
    message:str=''
    error:bool=False
    data:bytes|None=field(default=None,repr=False)
    digest:str='srgb'


def windows_profile_path(screen):
    if os.name!='nt' or not screen:return None
    # The Qt Windows platform exposes the GDI display device name.
    if not re.fullmatch(r'\\\\\.\\DISPLAY\d+',screen,re.IGNORECASE):
        raise ValueError('Windows 모니터 이름을 확인할 수 없습니다.')
    import ctypes as ct
    from ctypes import wintypes as wt
    gdi=ct.WinDLL('gdi32',use_last_error=True)
    create=gdi.CreateDCW;create.argtypes=[wt.LPCWSTR,wt.LPCWSTR,wt.LPCWSTR,ct.c_void_p];create.restype=wt.HDC
    query=gdi.GetICMProfileW;query.argtypes=[wt.HDC,ct.POINTER(wt.DWORD),wt.LPWSTR];query.restype=wt.BOOL
    delete=gdi.DeleteDC;delete.argtypes=[wt.HDC];delete.restype=wt.BOOL
    dc=create(screen,screen,None,None)
    if not dc:raise OSError(ct.get_last_error(),'모니터 정보를 읽을 수 없습니다.')
    try:
        size=wt.DWORD(0);ct.set_last_error(0)
        ok=query(dc,ct.byref(size),None)
        if not size.value:
            # A standard no-profile result is sRGB, including Advanced Color.
            code=ct.get_last_error()
            if ok or code in (0,2,1168,2015):return None
            raise OSError(code,'Windows 색상 프로파일을 읽을 수 없습니다.')
        if size.value>32768:raise ValueError('모니터 프로파일 경로가 너무 깁니다.')
        for _ in range(2):
            capacity=size.value;buffer=ct.create_unicode_buffer(capacity)
            if query(dc,ct.byref(size),buffer):return buffer.value or None
            code=ct.get_last_error()
            if code!=122 or not capacity<size.value<=32768:raise OSError(code,'Windows 색상 프로파일을 읽을 수 없습니다.')
        raise OSError('Windows 색상 프로파일이 변경 중입니다. 다시 확인해 주세요.')
    finally:delete(dc)


def mac_display_id(x,y,width,height):
    """The CoreGraphics display whose bounds are a Qt screen's geometry (both in points, origin top left)."""
    if not MAC:return None
    import ctypes as ct
    class Rect(ct.Structure):_fields_=[('x',ct.c_double),('y',ct.c_double),('w',ct.c_double),('h',ct.c_double)]
    cg=ct.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    cg.CGGetActiveDisplayList.argtypes=[ct.c_uint32,ct.POINTER(ct.c_uint32),ct.POINTER(ct.c_uint32)];cg.CGGetActiveDisplayList.restype=ct.c_int32
    cg.CGDisplayBounds.argtypes=[ct.c_uint32];cg.CGDisplayBounds.restype=Rect
    ids=(ct.c_uint32*32)();count=ct.c_uint32(0)
    if cg.CGGetActiveDisplayList(32,ids,ct.byref(count)):return None
    for ident in ids[:count.value]:
        bounds=cg.CGDisplayBounds(ident)
        if (round(bounds.x),round(bounds.y),round(bounds.w),round(bounds.h))==(x,y,width,height):return int(ident)
    return None


def mac_profile_data(screen):
    """ICC bytes of the display named 'Name (id)' by DisplayTarget.screen_name, or None for the system default."""
    found=re.search(r'\((\d+)\)$',screen or '')
    if not MAC or not found:return None
    import ctypes as ct
    cg=ct.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    cf=ct.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cg.CGDisplayCopyColorSpace.argtypes=[ct.c_uint32];cg.CGDisplayCopyColorSpace.restype=ct.c_void_p
    cg.CGColorSpaceCopyICCData.argtypes=[ct.c_void_p];cg.CGColorSpaceCopyICCData.restype=ct.c_void_p
    cf.CFDataGetLength.argtypes=[ct.c_void_p];cf.CFDataGetLength.restype=ct.c_long
    cf.CFDataGetBytePtr.argtypes=[ct.c_void_p];cf.CFDataGetBytePtr.restype=ct.c_void_p
    cf.CFRelease.argtypes=[ct.c_void_p];cf.CFRelease.restype=None
    space=cg.CGDisplayCopyColorSpace(int(found.group(1)))
    if not space:return None
    data=None
    try:
        data=cg.CGColorSpaceCopyICCData(space)
        if not data:return None
        size=cf.CFDataGetLength(data)
        if size>MAX_PROFILE_BYTES:raise ValueError('모니터 프로파일이 너무 큽니다.')
        return ct.string_at(cf.CFDataGetBytePtr(data),size)
    finally:
        if data:cf.CFRelease(data)
        cf.CFRelease(space)


def _checked(data,name):
    """Bytes, description and digest of a usable display profile; raises for anything else."""
    imagecodecs.cms_profile_validate(data)
    info=imagecodecs.cms_info(data)
    if info['colorspace']!='rgb' or info['deviceclass'] not in ('display','colorspace'):
        raise ValueError('화면용 RGB ICC 프로파일을 선택하세요.')
    # Some syntactically valid profiles cannot create an output transform.
    sample=np.array([[[0.,0.,0.],[.2,.5,.8],[1.,1.,1.]]],np.float32)
    converted=colorio.convert(sample,colorio.profile('sRGB'),data)
    if not np.isfinite(converted).all():raise ValueError('ICC 색상 변환 결과가 올바르지 않습니다.')
    return data,str(info.get('description') or name),hashlib.sha256(data).hexdigest()


@lru_cache(maxsize=8)
def _read_profile(path,size,mtime):
    with Path(path).open('rb') as handle:data=handle.read(MAX_PROFILE_BYTES+1)
    if len(data)>MAX_PROFILE_BYTES:raise ValueError('16MB 이하의 모니터 ICC 프로파일을 선택하세요.')
    return _checked(data,Path(path).name)


@lru_cache(maxsize=8)
def _system_profile(data):return _checked(data,'')


def read_profile(path):
    path=Path(path).resolve();stat=path.stat()
    if stat.st_size>MAX_PROFILE_BYTES:raise ValueError('16MB 이하의 모니터 ICC 프로파일을 선택하세요.')
    return _read_profile(str(path),stat.st_size,stat.st_mtime_ns)


def resolve(mode,screen='',path=''):
    if mode=='off':return DisplayProfile(mode=mode,screen=screen,message='앱의 화면 색상 보정이 꺼져 있습니다.')
    selected=path
    try:
        if mode=='auto' and MAC:
            # macOS hands over the profile itself, not a file.
            selected='';system=mac_profile_data(screen)
            if system:
                data,description,digest=_system_profile(system)
                # The profile's own name comes back in whichever language it lists first; the display's name is clearer.
                return DisplayProfile(mode=mode,screen=screen,description=screen.rsplit(' (',1)[0] or description,data=data,digest=digest,
                    message=f'{SYSTEM}가 현재 모니터에 제공한 프로파일입니다.')
        elif mode=='auto':selected=windows_profile_path(screen)
        elif mode!='manual':raise ValueError('화면 색상 모드를 확인하세요.')
        if not selected:
            if mode=='manual':raise ValueError('ICC 프로파일 파일을 선택하세요.')
            return DisplayProfile(screen=screen,message=f'{SYSTEM} 기본 색상(sRGB)을 사용합니다.')
        data,description,digest=read_profile(selected)
        return DisplayProfile(mode=mode,screen=screen,path=str(selected),description=description,data=data,digest=digest,
            message=f'{SYSTEM}가 현재 모니터에 제공한 프로파일입니다.' if mode=='auto' else '직접 선택한 화면 프로파일입니다.')
    except Exception as error:
        return DisplayProfile(mode=mode,screen=screen,path=str(selected or ''),error=True,
            message=f'{error}\n현재 화면은 sRGB로 표시합니다. 파일을 다시 선택하거나 자동 모드를 사용하세요.')
