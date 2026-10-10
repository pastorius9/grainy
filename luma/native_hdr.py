"""Narrow, validated ctypes ABI for our Windows HDR renderer."""
import ctypes as ct
from dataclasses import dataclass
from pathlib import Path
import math,os
import numpy as np

class _Info(ct.Structure):
    _fields_=[('size',ct.c_uint),('supported',ct.c_uint),('active',ct.c_uint),('bits',ct.c_uint),
              ('white',ct.c_float),('peak',ct.c_float),('full',ct.c_float),('minimum',ct.c_float),('name',ct.c_wchar*32)]

@dataclass(frozen=True)
class Display:
    supported:bool=False
    active:bool=False
    bits:int=8
    white:float=80.
    peak:float=80.
    name:str=''
    error:str=''

    @property
    def stops(self):return max(0.,math.log2(max(self.peak,self.white)/self.white)) if self.active else 0.

def _load():
    path=Path(__file__).resolve().parents[1]/'assets/native/grainy_hdr.dll'
    if os.name!='nt' or not path.exists():return None
    lib=ct.CDLL(str(path))
    signatures={'grainy_hdr_abi':(ct.c_int,[]),'grainy_hdr_probe':(ct.c_int,[ct.c_void_p,ct.POINTER(_Info)]),
        'grainy_hdr_create':(ct.c_void_p,[ct.c_void_p,ct.c_int,ct.POINTER(ct.c_int)]),
        'grainy_hdr_destroy':(None,[ct.c_void_p]),'grainy_hdr_upload':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_uint,ct.c_uint]),
        'grainy_hdr_draw':(ct.c_int,[ct.c_void_p,ct.c_uint,ct.c_uint,ct.c_void_p,ct.c_void_p,ct.c_uint,ct.c_float,ct.c_float,ct.c_int]),
        'grainy_hdr_readback':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_uint])}
    for name,(result,args) in signatures.items():getattr(lib,name).restype=result;getattr(lib,name).argtypes=args
    if lib.grainy_hdr_abi()!=1:raise RuntimeError('Unexpected HDR renderer ABI')
    return lib
try:_lib=_load()
except (OSError,RuntimeError):_lib=None

def available():return _lib is not None
def _check(code):
    if code<0:raise OSError(f'HDR renderer: 0x{code&0xffffffff:08X}')

def probe(hwnd=0):
    if _lib is None:return Display(error='HDR renderer unavailable')
    data=_Info();data.size=ct.sizeof(data)
    try:
        _check(_lib.grainy_hdr_probe(int(hwnd),ct.byref(data)))
        return Display(bool(data.supported),bool(data.active),data.bits,data.white,data.peak,data.name)
    except OSError as e:return Display(error=str(e))

class Renderer:
    def __init__(self,hwnd,warp=False):
        if _lib is None:raise OSError('HDR renderer unavailable')
        self.handle=None;error=ct.c_int()
        self.handle=_lib.grainy_hdr_create(int(hwnd),int(warp),ct.byref(error));_check(error.value)
        if not self.handle:raise OSError('HDR renderer initialization failed')
    def close(self):
        if self.handle:_lib.grainy_hdr_destroy(self.handle);self.handle=None
    def upload(self,pixels):
        data=np.ascontiguousarray(pixels,dtype=np.float32)
        if data.ndim!=3 or data.shape[2]!=4 or not np.isfinite(data).all():raise ValueError('Finite RGBA float pixels are required')
        _check(_lib.grainy_hdr_upload(self.handle,data.ctypes.data,data.shape[1],data.shape[0]))
    def draw(self,width,height,rect,overlay,white,peak,present=True):
        data=np.ascontiguousarray(overlay,dtype=np.uint8);bounds=np.ascontiguousarray(rect,dtype=np.float32)
        if data.shape!=(height,width,4) or bounds.shape!=(4,) or not np.isfinite(bounds).all() or np.any(bounds[2:]<=0):raise ValueError('Invalid HDR viewport geometry')
        if not math.isfinite(white) or not math.isfinite(peak) or white<=0 or peak<white:raise ValueError('Invalid display luminance')
        _check(_lib.grainy_hdr_draw(self.handle,width,height,bounds.ctypes.data,data.ctypes.data,data.strides[0],white,peak,int(present)))
    def readback(self,width,height):
        data=np.empty((height,width,4),np.float16)
        _check(_lib.grainy_hdr_readback(self.handle,data.ctypes.data,data.size));return data
