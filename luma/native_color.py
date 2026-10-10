"""Bounded reusable LittleCMS transforms; float buffers retain their precision.

The bundled, unmodified MIT library is the same 2.19 engine as imagecodecs.
Transform contexts are private and immutable after creation. NOCACHE disables
the mutable one-pixel cache; disjoint chunks may safely share a transform.
Python references keep an evicted transform alive until all callers finish.
"""
import ctypes as ct
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
from threading import RLock
import os
import numpy as np
from .user_paths import NATIVE_SUFFIX

MAX_TRANSFORMS=16
MAX_PROFILE_BYTES=32*1024*1024
MAX_PROFILE_SIZE=16*1024*1024
PARALLEL_PIXELS=128*1024
# LittleCMS float transforms are compute bound: on 24 threads a 12 MP transform takes 81 ms, 150 ms on 8.
WORKERS=min(32,max(1,os.cpu_count() or 1))
_lock=RLock()
_cache=OrderedDict()
_profile_bytes=0
_hits=_misses=0
_pool=ThreadPoolExecutor(max_workers=WORKERS,thread_name_prefix='LumaColor')


def _load():
    if NATIVE_SUFFIX is None:return None
    path=Path(__file__).resolve().parents[1]/f'assets/native/luma_lcms2{NATIVE_SUFFIX}'
    if not path.is_file():return None
    # Absolute app-owned path; never search the working directory or PATH.
    lib=(ct.WinDLL if os.name=='nt' else ct.CDLL)(str(path))
    signatures={
        'cmsGetEncodedCMMversion':(ct.c_uint32,[]),
        'cmsCreateContext':(ct.c_void_p,[ct.c_void_p,ct.c_void_p]),
        'cmsDeleteContext':(None,[ct.c_void_p]),
        'cmsOpenProfileFromMemTHR':(ct.c_void_p,[ct.c_void_p,ct.c_void_p,ct.c_uint32]),
        'cmsCloseProfile':(ct.c_int,[ct.c_void_p]),
        'cmsGetColorSpace':(ct.c_uint32,[ct.c_void_p]),
        'cmsCreateTransformTHR':(ct.c_void_p,[ct.c_void_p,ct.c_void_p,ct.c_uint32,ct.c_void_p,ct.c_uint32,ct.c_uint32,ct.c_uint32]),
        'cmsDeleteTransform':(None,[ct.c_void_p]),
        'cmsDoTransform':(None,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.c_uint32]),
        'cmsDoTransformLineStride':(None,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.c_uint32,ct.c_uint32,
                                          ct.c_uint32,ct.c_uint32,ct.c_uint32,ct.c_uint32]),
    }
    for name,(restype,argtypes) in signatures.items():
        function=getattr(lib,name);function.restype=restype;function.argtypes=argtypes
    if lib.cmsGetEncodedCMMversion()!=2190:raise RuntimeError('Unexpected native color engine version')
    return lib


_lib=_load()


def available():return _lib is not None


# From the pinned lcms2.h: TYPE_RGB_8, and TYPE_RGB_FLT with PLANAR_SH(1).
RGB_8=(4<<16)|(3<<3)|1
RGB_FLT=(1<<22)|(4<<16)|(3<<3)|4
RGB_FLT_PLANAR=RGB_FLT|(1<<12)


class Transform:
    def __init__(self,source,destination,planar8=False):
        self.lib=_lib;self.context=None;self.handle=None
        incoming=outgoing=None
        try:
            self.context=self.lib.cmsCreateContext(None,None)
            if not self.context:raise MemoryError('Cannot create color context')
            incoming=self.lib.cmsOpenProfileFromMemTHR(self.context,source,len(source))
            outgoing=self.lib.cmsOpenProfileFromMemTHR(self.context,destination,len(destination))
            if not incoming or not outgoing:raise ValueError('Invalid ICC profile')
            if any(self.lib.cmsGetColorSpace(p)!=0x52474220 for p in (incoming,outgoing)):
                raise ValueError('RGB ICC profiles are required')
            # Float pipeline either way; planar8 reads 8-bit RGB and writes float planes (convert8_planar).
            self.handle=self.lib.cmsCreateTransformTHR(self.context,incoming,RGB_8 if planar8 else RGB_FLT,
                outgoing,RGB_FLT_PLANAR if planar8 else RGB_FLT,1,0x0040)
            if not self.handle:raise ValueError('Cannot create ICC color transform')
        finally:
            if incoming:self.lib.cmsCloseProfile(incoming)
            if outgoing:self.lib.cmsCloseProfile(outgoing)

    def __del__(self):
        if self.handle:self.lib.cmsDeleteTransform(self.handle)
        if self.context:self.lib.cmsDeleteContext(self.context)

    def chunk(self,packet):
        source,destination,start,count=packet
        self.lib.cmsDoTransform(self.handle,source.ctypes.data+start*12,
            destination.ctypes.data+start*12,count)

    def rows(self,packet):
        # 8-bit interleaved rows in, float planes out; the plane stride is the whole frame.
        source,destination,first,count=packet
        h,w=source.shape[:2]
        self.lib.cmsDoTransformLineStride(self.handle,source.ctypes.data+first*w*3,destination.ctypes.data+first*w*4,
            w,count,w*3,w*4,h*w*3,h*w*4)


def clear_cache():
    global _profile_bytes,_hits,_misses
    with _lock:
        _cache.clear();_profile_bytes=0;_hits=_misses=0


def cache_info():
    with _lock:return dict(transforms=len(_cache),profile_bytes=_profile_bytes,hits=_hits,misses=_misses)


def _transform(source,destination,planar8=False):
    global _profile_bytes,_hits,_misses
    key=source,destination,planar8
    with _lock:
        if key in _cache:
            _hits+=1;_cache.move_to_end(key);return _cache[key]
        _misses+=1
        transform=Transform(source,destination,planar8)
        cost=len(source)+len(destination)
        while _cache and (len(_cache)>=MAX_TRANSFORMS or _profile_bytes+cost>MAX_PROFILE_BYTES):
            old,_=_cache.popitem(last=False);_profile_bytes-=len(old[0])+len(old[1])
        if cost<=MAX_PROFILE_BYTES:
            _cache[key]=transform;_profile_bytes+=cost
        return transform


def convert(rgb,source,destination):
    if _lib is None:raise RuntimeError('Native color engine is unavailable')
    if not isinstance(source,bytes) or not isinstance(destination,bytes):raise TypeError('ICC bytes required')
    if any(not 128<=len(p)<=MAX_PROFILE_SIZE for p in (source,destination)):raise ValueError('Invalid ICC profile size')
    array=np.asarray(rgb)
    if array.dtype!=np.float32 or array.ndim<2 or array.shape[-1]!=3:raise ValueError('Float32 RGB buffer required')
    array=np.ascontiguousarray(array)
    result=np.empty_like(array)
    pixels=array.size//3
    if not pixels:return result
    transform=_transform(source,destination)
    if pixels<PARALLEL_PIXELS or WORKERS==1:
        # cmsDoTransform takes a uint32 pixel count, even on 64-bit Windows.
        for start in range(0,pixels,0xffffffff):transform.chunk((array,result,start,min(pixels-start,0xffffffff)))
    else:
        size=min(128*1024,(pixels+WORKERS-1)//WORKERS)
        _run(transform.chunk,((array,result,start,min(size,pixels-start)) for start in range(0,pixels,size)))
    return result


def convert8_planar(rgb8,source,destination):
    """convert(rgb8.astype(float32)/255, ...) as float planes (3, h, w), without the float input frame.

    LittleCMS unpacks 8-bit samples to v/255 for its float pipeline; an exhaustive check over all
    16.7 million RGB8 values matched the float32 input path bit for bit (tests: every channel level).
    """
    if _lib is None:raise RuntimeError('Native color engine is unavailable')
    if not isinstance(source,bytes) or not isinstance(destination,bytes):raise TypeError('ICC bytes required')
    if any(not 128<=len(p)<=MAX_PROFILE_SIZE for p in (source,destination)):raise ValueError('Invalid ICC profile size')
    array=np.ascontiguousarray(rgb8)
    if array.dtype!=np.uint8 or array.ndim!=3 or array.shape[-1]!=3:raise ValueError('8-bit RGB buffer required')
    h,w=array.shape[:2]
    result=np.empty((3,h,w),np.float32)
    if not h*w:return result
    if h*w*4>0xffffffff:raise ValueError('Image is too large for one plane stride')
    transform=_transform(source,destination,True)
    if h*w<PARALLEL_PIXELS or WORKERS==1:
        transform.rows((array,result,0,h))
    else:
        size=max(1,min(128*1024//max(1,w),(h+WORKERS-1)//WORKERS))
        _run(transform.rows,((array,result,first,min(size,h-first)) for first in range(0,h,size)))
    return result


def _run(function,packets):
    # Submit a bounded rolling window: a full-resolution export must not
    # queue hundreds of tiles ahead of the next interactive preview.
    packets=iter(packets);pending=set()
    try:
        for _ in range(WORKERS):
            packet=next(packets,None)
            if packet is not None:pending.add(_pool.submit(function,packet))
        while pending:
            done,pending=wait(pending,return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
                packet=next(packets,None)
                if packet is not None:pending.add(_pool.submit(function,packet))
    finally:
        wait(pending)
