"""Exact DCP table interpolation without gathered NumPy temporary planes.

RGB/HSV conversion, gamma encoding, table coefficients and operation order are
unchanged. Only interpolation runs in the bundled C++ library. It owns no state
or threads; ctypes releases the GIL and each caller holds its own arrays alive.
"""
import ctypes as ct
import os
from pathlib import Path
import numpy as np
from .user_paths import NATIVE_SUFFIX


def _load():
    if NATIVE_SUFFIX is None:return None
    path=Path(__file__).resolve().parents[1]/f'assets/native/luma_dcp{NATIVE_SUFFIX}'
    if not path.is_file():return None
    try:
        lib=ct.CDLL(str(path))
        lib.luma_dcp_version.argtypes=[];lib.luma_dcp_version.restype=ct.c_int
        if lib.luma_dcp_version()!=1:return None
        args=[ct.c_void_p]*3+[ct.c_size_t,ct.c_void_p]+[ct.c_uint32]*3+[ct.c_void_p]
        for name in ('luma_dcp_float','luma_dcp_double'):
            function=getattr(lib,name);function.argtypes=args;function.restype=ct.c_int
        return lib
    except (OSError,AttributeError):
        return None


_lib=_load()


def available():return _lib is not None


def interpolate(coords,table):
    """Return a fresh array, or None to use NumPy on unsupported platforms."""
    if _lib is None:return None
    table=np.asarray(table)
    if table.dtype not in (np.dtype('float32'),np.dtype('float64')):return None
    if table.ndim!=4 or table.shape[-1]!=3:
        raise ValueError('DCP 색상표 모양이 잘못되었습니다.')
    vd,hd,sd=table.shape[:3]
    if not (1<=vd<=256 and 1<=hd<=360 and 2<=sd<=256 and vd*hd*sd<=250000):
        raise ValueError('DCP 색상표 크기가 잘못되었습니다.')
    if len(coords)!=3:raise ValueError('DCP 색상표 좌표 수가 잘못되었습니다.')
    arrays=[np.asarray(c) for c in coords]
    if any(c.shape!=arrays[0].shape for c in arrays):
        raise ValueError('DCP 색상표 좌표 크기가 다릅니다.')
    # Preserve unusual caller types through the existing implementation.
    if any(c.dtype!=np.dtype('float32') for c in arrays):return None
    arrays=[np.ascontiguousarray(c) for c in arrays]
    table=np.ascontiguousarray(table)
    result=np.empty((*arrays[0].shape,3),dtype=table.dtype)
    function=_lib.luma_dcp_float if table.dtype==np.dtype('float32') else _lib.luma_dcp_double
    status=function(*[c.ctypes.data for c in arrays],arrays[0].size,table.ctypes.data,
                    vd,hd,sd,result.ctypes.data)
    if status:raise ValueError('DCP 색상표 좌표가 유효한 범위를 벗어났습니다.')
    return result
