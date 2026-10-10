"""Optional D3D11 compute path for develop stages. The numpy stages stay the reference.

Every entry point returns None when the GPU cannot serve the request, so callers keep
their CPU code as the fallback. One device is shared; its context is serialised.
"""
import ctypes as ct
from pathlib import Path
from threading import Lock,Timer
import os
import time
import weakref
import numpy as np
from .user_paths import NATIVE_SUFFIX


class _ToneParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (ToneParams).
    _fields_=[('gain',ct.c_float*4),('shadows',ct.c_float),('highlights',ct.c_float),('black',ct.c_float),
              ('whitesGain',ct.c_float),('contrast',ct.c_float),('vignette',ct.c_float),('curveCount',ct.c_uint),
              ('flags',ct.c_uint),('width',ct.c_uint),('height',ct.c_uint),('curveSlope',ct.c_float),('localMaps',ct.c_uint),
              ('shoulderKnee',ct.c_float),('shoulderWhite',ct.c_float),('shoulderSlope',ct.c_float),('localFloor',ct.c_float)]


class _LocalParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (LocalParams).
    _fields_=[('exposureGain',ct.c_float),('contrast',ct.c_float),('saturation',ct.c_float),('shadows',ct.c_float),
              ('highlights',ct.c_float),('black',ct.c_float),('whiteMinusBlack',ct.c_float),('hueShift',ct.c_float),
              ('tempTint',ct.c_float*4),('width',ct.c_uint),('height',ct.c_uint),('flags',ct.c_uint),('pad',ct.c_uint),
              ('curveOffset',ct.c_uint*4),('curveCount',ct.c_uint*4),('hsl',(ct.c_float*4)*8)]


class _ColorParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (ColorParams).
    _fields_=[('saturation',ct.c_float),('vibrance',ct.c_float),('width',ct.c_uint),('height',ct.c_uint),
              ('flags',ct.c_uint),('pad0',ct.c_uint*3),('curveOffset',ct.c_uint*4),('curveCount',ct.c_uint*4),
              ('hsl',(ct.c_float*4)*8),('parametric',ct.c_float*4),('calibration',(ct.c_float*4)*3),
              ('grading',(ct.c_float*4)*3),('gradingBalance',ct.c_float),('pad1',ct.c_float*3),('matrix',(ct.c_float*4)*3),
              ('pointCount',ct.c_uint),('pad2',ct.c_uint*3)]


class _PointParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (PointParams).
    _fields_=[('reference',ct.c_float*4),('widths',ct.c_float*4),('amounts',ct.c_float*4),('flags',ct.c_uint*4)]


class _DetailParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (DetailParams).
    _fields_=[('width',ct.c_uint),('height',ct.c_uint),('flags',ct.c_uint),('stage',ct.c_uint),
              ('texture',ct.c_float),('clarity',ct.c_float),('sharpen',ct.c_float),('threshold',ct.c_float),
              ('maskDivisor',ct.c_float),('pixelScale',ct.c_float),('vignette',ct.c_float),('pad0',ct.c_float),
              ('radius',ct.c_uint),('kernelOffset',ct.c_uint),('pad1',ct.c_uint),('pad2',ct.c_uint),
              ('channel',ct.c_uint),('lutOffset',ct.c_uint),('spaceOffset',ct.c_uint),('spaceCount',ct.c_uint),
              ('scaleIndex',ct.c_float),('strength',ct.c_float),('constant',ct.c_float),('pad3',ct.c_float),
              ('dehaze',ct.c_float),('defringe',ct.c_float),('edgeScale',ct.c_float),('dehazeRadius',ct.c_uint),
              ('vignetteRound',ct.c_float),('vignetteP',ct.c_float),('vignetteScale',ct.c_float),('vignetteExponent',ct.c_float),
              ('vignetteProtect',ct.c_float),('vignetteAspectX',ct.c_float),('vignetteAspectY',ct.c_float),('pad5',ct.c_float)]


class _OpticalParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (OpticalParams).
    _fields_=[('width',ct.c_uint),('height',ct.c_uint),('pad0',ct.c_uint),('pad1',ct.c_uint),
              ('halfWidth',ct.c_float),('halfHeight',ct.c_float),('scale',ct.c_float),('perspectiveH',ct.c_float),
              ('perspectiveV',ct.c_float),('zoom',ct.c_float),('aspect',ct.c_float),('shiftX',ct.c_float),
              ('shiftY',ct.c_float),('distortion1',ct.c_float),('distortion2',ct.c_float),('distortion3',ct.c_float),
              ('channelScale',ct.c_float*4)]


class _RotateParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (RotateParams).
    _fields_=[('width',ct.c_uint),('height',ct.c_uint),('pad0',ct.c_uint),('pad1',ct.c_uint),('matrix',ct.c_double*6)]


class _NoiseParams(ct.Structure):
    # Keep in sync with native/gpu_compute.cpp (NoiseParams). Channels: L, a, b.
    _fields_=[('active',ct.c_uint*3),('diameter',ct.c_uint),('strength',ct.c_float*3),
              ('sigmaColor',ct.c_float*3),('sigmaSpace',ct.c_float*3),
              ('guidedRadius',ct.c_uint),('guidedOffset',ct.c_uint),('guidedEps',ct.c_float),('guidedBlend',ct.c_float),
              ('guidedChromaEps',ct.c_float),('pad',ct.c_float*3),
              ('lumaWavelet',ct.c_uint),('waveletOffset',ct.c_uint*5),('boxOffset',ct.c_uint),('waveletLambda',ct.c_float)]


# Local edits that need the CPU path (spatial filters, point colour callbacks, HDR range).
CPU_ONLY_LOCAL=('point_colors','clarity','texture','dehaze','noise_luma','noise_color','sharpen','defringe','hdr')


def _load():
    if NATIVE_SUFFIX is None:return None
    path=Path(__file__).resolve().parents[1]/f'assets/native/grainy_gpu{NATIVE_SUFFIX}'   # D3D11 on Windows, Metal on macOS
    if not path.exists():return None
    lib=ct.CDLL(str(path))
    for name,(result,args) in {'grainy_gpu_abi':(ct.c_int,[]),
            'grainy_gpu_create':(ct.c_void_p,[ct.c_int,ct.POINTER(ct.c_int),ct.c_wchar_p,ct.c_uint]),
            'grainy_gpu_destroy':(None,[ct.c_void_p]),
            'grainy_gpu_tone':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(_ToneParams),ct.c_void_p]),
            'grainy_gpu_local':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(_LocalParams),ct.c_void_p,ct.c_uint]),
            'grainy_gpu_color':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(_ColorParams),ct.c_void_p,ct.c_uint,ct.c_void_p]),
            'grainy_gpu_detail':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(_DetailParams),ct.c_void_p,ct.c_uint,
                                           ct.POINTER(ct.c_uint),ct.POINTER(ct.c_uint),ct.c_int,ct.c_int,ct.POINTER(_NoiseParams)]),
            'grainy_gpu_develop':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(_ToneParams),ct.c_void_p,
                                            ct.POINTER(_ColorParams),ct.c_void_p,ct.c_uint,ct.c_void_p,ct.POINTER(_DetailParams),ct.c_void_p,ct.c_uint,
                                            ct.POINTER(ct.c_uint),ct.POINTER(ct.c_uint),ct.c_int,ct.POINTER(_NoiseParams)]),
            'grainy_gpu_resident':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_int,ct.c_uint,ct.POINTER(ct.c_int),ct.c_void_p,
                                             ct.POINTER(_ToneParams),ct.c_void_p,ct.POINTER(_ColorParams),ct.c_void_p,ct.c_uint,ct.c_void_p,
                                             ct.POINTER(_DetailParams),ct.c_void_p,ct.c_uint,ct.POINTER(ct.c_uint),ct.POINTER(ct.c_uint),
                                             ct.c_int,ct.POINTER(_NoiseParams),ct.c_void_p,ct.c_int,ct.c_uint,ct.POINTER(_LocalParams),
                                             ct.POINTER(ct.c_void_p),ct.POINTER(ct.c_int),ct.c_void_p,ct.c_uint,ct.c_int]),
            'grainy_gpu_release':(ct.c_int,[ct.c_void_p,ct.c_int]),
            'grainy_gpu_memory':(ct.c_ulonglong,[ct.c_void_p]),
            'grainy_gpu_optical':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(_OpticalParams)]),
            'grainy_gpu_rotate':(ct.c_int,[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.POINTER(_RotateParams)])}.items():
        getattr(lib,name).restype=result;getattr(lib,name).argtypes=args
    # 17 (0.5.83) added grainy_gpu_trim: the module keeps its large scratch buffers between calls and
    # frees them on request. The structures are those of 16; a module still at 16 frees them itself.
    abi=lib.grainy_gpu_abi()
    if abi not in (16,17):raise RuntimeError('Unexpected GPU module ABI')
    if abi>=17:lib.grainy_gpu_trim.restype=ct.c_int;lib.grainy_gpu_trim.argtypes=[ct.c_void_p]
    lib.keeps_scratch=abi>=17
    return lib


try:_lib=_load()
except (OSError,RuntimeError):_lib=None
TRIM_DELAY=5.      # seconds without GPU work before the large scratch buffers are freed
_last_use=0.
_trimmer=None
trims=0


class _Gate:
    """The device lock. The module keeps its frame-sized scratch buffers between calls (a batch export
    reuses them for every photo); leaving the lock restarts the idle timer that frees them."""
    def __init__(self):self.raw=Lock()
    def __enter__(self):self.raw.acquire();return self
    def __exit__(self,*error):
        global _last_use
        _last_use=time.monotonic();self.raw.release();_arm_trim()


def _arm_trim():
    global _trimmer
    if _trimmer is None and _device:
        _trimmer=Timer(TRIM_DELAY,_trim_idle);_trimmer.daemon=True;_trimmer.start()


def _trim_idle():
    global _trimmer
    wait=TRIM_DELAY-(time.monotonic()-_last_use)
    if wait>.05:
        _trimmer=Timer(wait,_trim_idle);_trimmer.daemon=True;_trimmer.start();return
    _trimmer=None
    trim()


def trim():
    """Free the large scratch buffers now."""
    global trims
    with _lock.raw:
        if _device:
            if _lib.keeps_scratch:_lib.grainy_gpu_trim(_device)
            trims+=1


_lock=_Gate()
_enabled=True
_device=None
_failed=None
adapter=''


def available():return _lib is not None


def enabled():
    """GPU stages run unless turned off in settings or GRAINY_GPU=0; GRAINY_GPU=warp forces the software device (tests)."""
    return _lib is not None and _enabled and os.environ.get('GRAINY_GPU','1')!='0'


def set_enabled(value):
    global _enabled
    _enabled=bool(value)


def _handle():
    global _device,_failed,adapter
    if _device is None and _failed is None:
        error=ct.c_int();name=ct.create_unicode_buffer(128)
        handle=_lib.grainy_gpu_create(int(os.environ.get('GRAINY_GPU')=='warp'),ct.byref(error),name,128)
        if handle:_device=handle;adapter=name.value
        else:_failed=f'0x{error.value&0xffffffff:08X}'
    return _device


def status():
    """Adapter name, or the reason the GPU path is unused."""
    if not enabled():return 'off'
    with _lock:
        return adapter if _handle() else f'unavailable ({_failed})'


def reset():
    """Release the device (tests switch between hardware and WARP)."""
    global _device,_failed,adapter
    with _lock:
        if _device:_lib.grainy_gpu_destroy(_device)
        _device=None;_failed=None;adapter=''
        _slot_owners[:]=[None]*len(_slot_owners);_slot_keys[:]=[{} for _ in _slot_keys]


def _frame(a):
    return a.ndim==3 and a.shape[2]==3 and a.size>0


def _call(function,*args):
    with _lock:
        handle=_handle()
        if not handle:return None
        return function(handle,*args)


def _tone_params(shape,s,frame=None,memo=None):
    """(params, curve) for engine._develop_tone_pixels, or None when the GPU cannot reproduce it.
    Process 3 Highlights/Shadows need `frame` (the linear input) for tone_response's analysis; memo is an
    optional (holder, key of the frame) that keeps the analysis while the two sliders move."""
    from .processing import white_balance
    curve=np.ascontiguousarray(s['curve'],dtype=np.float32)
    if curve.ndim!=2 or curve.shape[1]!=2 or len(curve)<2:return None
    local=None
    if s.get('tone_version',1)>=3 and not s.get('hdr') and (s['shadows'] or s['highlights']):
        if frame is None:return None
        from .engine import tone_local,tone_base,rolloff_white
        from .processing import white_balance as balance
        base=None
        if memo:
            # Everything the analysis reads besides the frame itself.
            key=(memo[1],frame.shape,float(s['exposure']),tuple(float(v) for v in balance(s)),float(s['lens_vignette']),rolloff_white(s))
            kept=getattr(memo[0],'_tone_base',None)
            if kept is not None and kept[0]==key:base=kept[1]
            else:
                base=tone_base(frame,s)
                try:memo[0]._tone_base=(key,base)
                except AttributeError:pass
        local=tone_local(frame,s,base)
        if local is None or max(local['a'].shape)>1024:return None
    identity=np.allclose(curve[:,0],curve[:,1])
    if not identity and np.any(np.diff(curve[:,0])<0):return None
    p=_ToneParams()
    wb=white_balance(s)
    p.gain[:]=[float(wb[0]),float(wb[1]),float(wb[2]),float(np.float32(2.**s['exposure']))]
    p.shadows=s['shadows'];p.highlights=s['highlights']
    p.black=float(np.clip(s['blacks'],-200,200))/250
    p.whitesGain=2.**(float(np.clip(s['whites'],-200,200))/100) if s['whites'] else 1.
    p.contrast=1+s['contrast']/125;p.vignette=s['lens_vignette']
    p.flags=(1 if s['shadows'] or s['highlights'] else 0)|(2 if s['contrast'] else 0)|(0 if identity else 4)|(8 if s['lens_vignette'] else 0)
    if not s.get('hdr') and s.get('tone_version',1)>=2:
        from .engine import rolloff_white,SHOULDER_KNEE
        white=rolloff_white(s)
        if white>1:
            p.flags|=32;p.shoulderKnee=SHOULDER_KNEE;p.shoulderWhite=white
            p.shoulderSlope=float(np.float32((white-SHOULDER_KNEE)/(1-SHOULDER_KNEE)))
    if s.get('hdr'):
        p.flags|=16
        # hdr.curve_extended's slope, rounded to float32 like its numpy expression.
        p.curveSlope=max(0.,float((curve[-1,1]-curve[-2,1])/max(curve[-1,0]-curve[-2,0],1e-6)))
    p.curveCount=len(curve);p.height,p.width=shape[:2]
    if local is not None:
        # After the curve points: tone_response's change table (value, 0) and its (a, b) coefficient maps.
        from .tone_response import FLOOR
        rows,columns=local['a'].shape
        table=np.zeros((len(local['curve']),2),np.float32);table[:,0]=local['curve']
        curve=np.ascontiguousarray(np.concatenate([curve,table,np.stack([local['a'],local['b']],-1).reshape(-1,2)]),dtype=np.float32)
        p.flags=(p.flags&~1)|64;p.localMaps=(columns<<16)|rows;p.localFloor=FLOOR
    return p,curve


def tone(a,s):
    """GPU version of engine._develop_tone_pixels for SDR frames; None means use the CPU."""
    if not enabled() or not _frame(a):return None
    built=_tone_params(a.shape,s,a)
    if built is None:return None
    p,curve=built
    source=np.ascontiguousarray(a,dtype=np.float32);result=np.empty_like(source)
    code=_call(_lib.grainy_gpu_tone,source.ctypes.data,result.ctypes.data,ct.byref(p),curve.ctypes.data)
    return result if code is not None and code>=0 else None


def local(region,weights,edit):
    """GPU version of processing._local_region's colour work and blend; None means use the CPU."""
    if not enabled() or region.ndim!=3 or region.shape[2]!=3 or region.size==0 or weights.shape!=region.shape[:2]:return None
    built=local_params(edit)
    if built is None:return None
    p,points=built
    curve=np.ascontiguousarray(points or [[0,0],[1,1]],dtype=np.float32)
    p.height,p.width=region.shape[:2]
    source=np.ascontiguousarray(region,dtype=np.float32);mask=np.ascontiguousarray(weights,dtype=np.float32)
    result=np.empty_like(source)
    with _lock:
        handle=_handle()
        if not handle:return None
        code=_lib.grainy_gpu_local(handle,source.ctypes.data,mask.ctypes.data,result.ctypes.data,ct.byref(p),curve.ctypes.data,len(curve))
    return result if code>=0 else None


def local_params(edit):
    """(params without size, curve points) for a local edit the GPU reproduces, or None."""
    if any(edit.get(k) for k in CPU_ONLY_LOCAL):return None
    p=_LocalParams()
    p.exposureGain=float(np.float32(2**edit.get('exposure',0)))
    p.contrast=1+edit.get('contrast',0)/125;p.saturation=1+edit.get('saturation',0)/100
    p.shadows=edit.get('shadows',0)/300;p.highlights=edit.get('highlights',0)/300
    p.tempTint[:3]=[float(v) for v in np.array([edit.get('temperature',0),-edit.get('tint',0)*.5,-edit.get('temperature',0)],np.float32)/400]
    if edit.get('whites') or edit.get('blacks'):
        black=edit.get('blacks',0)/200
        p.flags|=1;p.black=black;p.whiteMinusBlack=(1+edit.get('whites',0)/200)-black
    if edit.get('hue'):p.flags|=2;p.hueShift=edit['hue']/360
    groups=edit.get('hsl',[])
    if any(any(group) for group in groups):
        if len(groups)!=8:return None
        p.flags|=4
        for g,values in enumerate(groups):p.hsl[g][:3]=[float(v) for v in values]
    points=[]
    for channel,values in enumerate(edit.get('local_curves',[])[:4]):
        curve=np.asarray(values,np.float32)
        if curve.ndim!=2 or curve.shape[1]!=2 or len(curve)<2:return None
        if np.array_equal(curve[:,0],curve[:,1]):continue
        if np.any(np.diff(curve[:,0])<0):return None
        p.curveOffset[channel]=len(points);p.curveCount[channel]=len(curve);points.extend(curve.tolist())
    return p,points


def _point_params(points):
    """point_color.apply's per-point constants (reference HLS from the CPU conversion), or None if invalid."""
    from .processing import rgb_to_hsl
    result=[]
    for point in points:
        version2=point.get('version',1)>=2
        if version2 and not any(point.get(k,0) for k in ('hue','saturation','lightness')):continue
        rgb=np.asarray(point.get('rgb',[.5,.5,.5]),np.float32)
        if rgb.shape!=(3,) or not np.isfinite(rgb).all():return None
        rh,rs,rl=(float(v[0,0]) for v in rgb_to_hsl(rgb.reshape(1,1,3)))
        q=_PointParams();q.reference[:3]=[rh,rs,rl];q.amounts[:3]=[float(point.get(k,0)) for k in ('hue','saturation','lightness')]
        if version2:
            widths=np.asarray([point.get('range',.12),point.get('saturation_range',.6),point.get('lightness_range',.5)],np.float32)
            if not np.isfinite(widths).all() or np.any(widths<=0) or np.any(widths>[.5,1,1]):return None
            q.widths[:3]=[float(v) for v in widths]
            q.flags[:]=[1,int(rs<.02 or widths[0]>=.5),int(widths[1]>=1),int(widths[2]>=1)]
        else:
            q.widths[0]=max(.005,float(point.get('range',.12)))
        result.append(q)
    return (_PointParams*max(1,len(result)))(*result),len(result)


def _color_params(shape,s):
    """(params, curves, points) for engine._develop_color, or None (photo filters, HDR, invalid points)."""
    if s.get('hdr') or s.get('photo_filter_enabled'):return None
    p=_ColorParams()
    if s['saturation'] or s['vibrance']:
        p.flags|=1;p.saturation=1+s['saturation']/100
        if s['vibrance']:p.flags|=2;p.vibrance=s['vibrance']/100
    groups=s['hsl']
    if any(any(x for x in group) for group in groups):
        if len(groups)!=8:return None
        p.flags|=4 if s['mixer_mode']=='hsl' else 8
        for g,values in enumerate(groups):p.hsl[g][:3]=[float(v) for v in values]
    points=[]
    for channel,values in enumerate(s['rgb_curves'][:3]):
        curve=np.asarray(values,np.float32)
        if curve.ndim!=2 or curve.shape[1]!=2 or len(curve)<2:return None
        if np.allclose(curve[:,0],curve[:,1]):continue
        if np.any(np.diff(curve[:,0])<0):return None
        p.curveOffset[channel]=len(points);p.curveCount[channel]=len(curve);points.extend(curve.tolist())
    if any(s['parametric']):p.flags|=16;p.parametric[:]=[float(v) for v in s['parametric']]
    if any(any(group) for group in s['calibration']):
        p.flags|=32
        for i,(hue,amount) in enumerate(s['calibration']):p.calibration[i][:2]=[float(hue),float(amount)]
    if any(any(group) for group in s['grading']):
        from .processing import hsl_to_rgb
        p.flags|=64;p.gradingBalance=s['grading_balance']/300
        for i,(hue,sat,light) in enumerate(s['grading']):
            # The CPU adds this per-zone RGB offset; computing it identically keeps the constants exact.
            tint=hsl_to_rgb(np.full((1,1),hue/360),np.ones((1,1)),np.full((1,1),.5))[0,0]
            p.grading[i][:3]=[float(v) for v in (tint-.5)*sat/200+light/400]
    if s.get('camera_matrix') is not None:
        matrix=np.asarray(s['camera_matrix'],np.float32)
        if matrix.shape==(3,3) and np.isfinite(matrix).all():
            p.flags|=128
            for i in range(3):p.matrix[i][:3]=[float(v) for v in matrix[i]]
    selected=_point_params(s.get('point_colors') or [])
    if selected is None:return None
    table,p.pointCount=selected
    if p.pointCount:p.flags|=256
    p.height,p.width=shape[:2]
    return p,np.ascontiguousarray(points or [[0,0],[1,1]],dtype=np.float32),len(points),table


def color(a,s):
    """GPU version of engine._develop_color for SDR frames without point colours or photo filters."""
    if not enabled() or not _frame(a):return None
    built=_color_params(a.shape,s)
    if built is None:return None
    p,curve,_,table=built
    source=np.ascontiguousarray(a,dtype=np.float32);result=np.empty_like(source)
    code=_call(_lib.grainy_gpu_color,source.ctypes.data,result.ctypes.data,ct.byref(p),curve.ctypes.data,len(curve),ct.byref(table))
    return result if code is not None and code>=0 else None


def detail_tools_on_gpu(s,pixel_scale=1.):
    """processing.detail_tools (noise reduction, dehaze, texture, defringe) runs entirely on the GPU."""
    return True


def _gaussian(sigma):
    import cv2
    sigma=max(.05,float(sigma))
    size=int(round(sigma*8+1))|1   # cv2.GaussianBlur's size for float images
    return cv2.getGaussianKernel(size,sigma,cv2.CV_32F)[:,0]


def _detail_params(shape,s,pixel_scale,tools):
    if s.get('hdr') or tools and not detail_tools_on_gpu(s,pixel_scale):return None
    h,w=shape[:2]
    sigmas=[1.3*pixel_scale if tools and s['texture'] else None,
            max(pixel_scale,min(h,w)/80) if s['clarity'] else None,
            s['sharpen_radius']*pixel_scale if s['sharpen'] else None]
    kernels=[];radii=(ct.c_uint*3)();offsets=(ct.c_uint*3)()
    for i,sigma in enumerate(sigmas):
        if sigma is None:radii[i]=0xffffffff;continue
        taps=_gaussian(sigma);offsets[i]=len(kernels);radii[i]=len(taps)//2;kernels.extend(taps.tolist())
    p=_DetailParams();p.height,p.width=h,w
    p.flags=(1 if tools and s['texture'] else 0)|(2 if s['monochrome'] else 0)
    p.texture=s['texture']/100;p.clarity=s['clarity']/140;p.sharpen=s['sharpen']/65;p.pixelScale=pixel_scale
    if s['sharpen']:
        if s.get('detail_version',1)>=2:p.flags|=64
        if s['sharpen_detail']<100:p.flags|=4;p.threshold=max((1-s['sharpen_detail']/100)*.012,1e-6)
        if s['sharpen_mask']:p.flags|=8;p.maskDivisor=.002+s['sharpen_mask']/200
    p.vignette=s['vignette']/220
    from .engine import vignette_shape
    (p.vignetteRound,p.vignetteP,p.vignetteScale,p.vignetteExponent,p.vignetteProtect,
     p.vignetteAspectX,p.vignetteAspectY)=vignette_shape(s,w,h)
    if tools and s['dehaze']:
        p.flags|=32;p.dehaze=np.float32(s['dehaze']/100*.8);p.dehazeRadius=round(3*pixel_scale)
    if tools and s['defringe']:
        p.flags|=16;p.defringe=s['defringe'];p.edgeScale=np.float32(pixel_scale**2)
    noise=_NoiseParams();noise.diameter=2*round(3*pixel_scale)+1
    if tools and noise.diameter>1:
        # Same strengths and filter sigmas as processing.detail_tools.
        if s['noise_luma'] and s.get('detail_version',1)>=3:
            # processing.luma_wavelet: level kernels and the 5x5 box after the Gaussians.
            from .processing import wavelet_kernel,wavelet_lambda,WAVELET_LEVELS
            noise.active[0]=1;noise.lumaWavelet=1;noise.waveletLambda=wavelet_lambda(s['noise_luma'],pixel_scale)
            for level in range(WAVELET_LEVELS):
                noise.waveletOffset[level]=len(kernels);kernels.extend(wavelet_kernel(level).tolist())
            noise.boxOffset=len(kernels);kernels.extend([.2]*5)
        elif s['noise_luma']:
            strength=s['noise_luma']/100
            noise.active[0]=1;noise.strength[0]=strength;noise.sigmaColor[0]=2+strength*16;noise.sigmaSpace[0]=(1+strength*3)*pixel_scale
        if s['noise_color']:
            # Same radius, eps and blend as processing.chroma_guided; the box taps follow the Gaussians.
            from .processing import chroma_guided_params
            radius,eps,chroma_eps,blend=chroma_guided_params(s['noise_color'],pixel_scale)
            noise.active[1]=noise.active[2]=1;noise.guidedRadius=radius;noise.guidedOffset=len(kernels)
            noise.guidedEps=eps;noise.guidedChromaEps=chroma_eps;noise.guidedBlend=blend;kernels.extend([1/(2*radius+1)]*(2*radius+1))
    return p,np.ascontiguousarray(kernels or [0.],dtype=np.float32),len(kernels),radii,offsets,noise


def detail(a,s,pixel_scale=1.,tools=True):
    """engine._develop_detail after the CPU-only filters, without grain; None means use the CPU.

    tools=True also performs processing.detail_tools (only when detail_tools_on_gpu is true).
    """
    if not enabled() or not _frame(a):return None
    built=_detail_params(a.shape,s,pixel_scale,tools)
    if built is None:return None
    p,table,count,radii,offsets,noise=built
    source=np.ascontiguousarray(a,dtype=np.float32);result=np.empty_like(source)
    code=_call(_lib.grainy_gpu_detail,source.ctypes.data,result.ctypes.data,ct.byref(p),table.ctypes.data,count,
               radii,offsets,int(tools),int(bool(s['vignette'])),ct.byref(noise))
    return result if code is not None and code>=0 else None


def develop(a,s,pixel_scale=1.):
    """Tone, colour and detail (without grain) in one GPU round trip; None means use the stage functions."""
    if not enabled() or not _frame(a):return None
    tone_built=_tone_params(a.shape,s,a);color_built=_color_params(a.shape,s);detail_built=_detail_params(a.shape,s,pixel_scale,True)
    if tone_built is None or color_built is None or detail_built is None:return None
    tp,tcurve=tone_built;cp,ccurve,ccount,points=color_built;dp,table,count,radii,offsets,noise=detail_built
    source=np.ascontiguousarray(a,dtype=np.float32);result=np.empty_like(source)
    code=_call(_lib.grainy_gpu_develop,source.ctypes.data,result.ctypes.data,ct.byref(tp),tcurve.ctypes.data,
               ct.byref(cp),ccurve.ctypes.data,ccount,ct.byref(points),ct.byref(dp),table.ctypes.data,count,radii,offsets,
               int(bool(s['vignette'])),ct.byref(noise))
    return result if code is not None and code>=0 else None


# Resident frames: each preview stage cache (live, quality) owns a group of device slots holding its geometry,
# tone, colour and detail+grain results, the grain field and shape masks, so a slider edit uploads nothing and
# reads back only the developed frame.
_GROUP=16
_GRAIN=4                    # slot of the grain field within a group
_MASKS=5                    # first mask slot; 11 masks per group
_slot_owners=[None,None]    # weak reference to the owning cache per group
_slot_keys=[{},{}]          # group -> {slot within the group: key of its content}
_slot_order=[0,1]           # least recently used group first


def _slot_group(owner):
    for group,reference in enumerate(_slot_owners):
        if reference is not None and reference() is owner:break
    else:
        group=_slot_order[0]
        _slot_owners[group]=weakref.ref(owner);_slot_keys[group]={}
    _slot_order.remove(group);_slot_order.append(group)
    return group


def develop_resident(owner,frame,keys,s,pixel_scale=1.,grain=None,layers=None):
    """develop() for a stage cache from frame, the geometry result; None means use the stage functions.

    keys are the cache keys of the geometry, tone, colour and detail+grain results; stages whose result is still
    on the device are skipped. owner is the cache (one slot group each, two groups). grain is (field, key) or None.
    layers=None returns the detail result with grain; otherwise layers lists (mask, key, edit) shape-mask edits
    the GPU reproduces (local_params), applied in order, and the result is also clipped like develop().
    """
    if not enabled() or not _frame(frame):return None
    layers=None if layers is None else list(layers)
    if layers is not None and len(layers)>_GROUP-_MASKS:return None
    tone_built=_tone_params(frame.shape,s,frame,(owner,keys[0]));color_built=_color_params(frame.shape,s);detail_built=_detail_params(frame.shape,s,pixel_scale,True)
    if tone_built is None or color_built is None or detail_built is None:return None
    tp,tcurve=tone_built;cp,ccurve,ccount,points=color_built;dp,table,count,radii,offsets,noise=detail_built
    h,w=frame.shape[:2]
    local=[];curves=[]
    for mask,key,edit in layers or []:
        built=local_params(edit)
        if built is None or mask.shape!=(h,w):return None
        p,curve=built;p.width,p.height=w,h
        for c in range(4):
            if p.curveCount[c]:p.curveOffset[c]+=len(curves)
        curves.extend(curve);local.append(p)
    if grain is not None and grain[0].shape!=(h,w):return None
    with _lock:
        handle=_handle()
        if not handle:return None
        # Kept frames must stay well inside video memory (software devices report none). The detail slot
        # is optional: without it, mask edits recompute detail from the colour result.
        budget=(_lib.grainy_gpu_memory(handle) or 2*1024**3)//5
        planes=frame.size*4//3*(9+(grain is not None)+len(local))
        if planes>budget:return None
        keep_detail=planes+frame.size*4<=budget
        group=_slot_group(owner);known=_slot_keys[group];base=group*_GROUP
        stages=(3,2,1,0) if keep_detail and layers is not None else (2,1,0)
        first=next((stage for stage in stages if known.get(stage)==keys[stage]),None)
        upload=first is None
        if upload:
            first=0
            for stage in range(4):known.pop(stage,None)
        source=np.ascontiguousarray(frame,dtype=np.float32) if upload else None
        result=np.empty(frame.shape,np.float32)
        keep=(ct.c_int*3)(base+1 if first==0 else -1,base+2 if first<=1 else -1,base+3 if first<=2 and keep_detail else -1)
        field=None;grain_slot=-1
        if grain is not None and first<=2:
            grain_slot=base+_GRAIN
            if known.get(_GRAIN)!=grain[1]:field=np.ascontiguousarray(grain[0],dtype=np.float32)
        masks=(ct.c_void_p*max(1,len(local)))();slots=(ct.c_int*max(1,len(local)))();arrays=[]
        for i,(mask,key,edit) in enumerate(layers or []):
            slots[i]=base+_MASKS+i
            if known.get(_MASKS+i)!=key:
                arrays.append(np.ascontiguousarray(mask,dtype=np.float32));masks[i]=arrays[-1].ctypes.data
        params=(_LocalParams*max(1,len(local)))(*local)
        curve=np.ascontiguousarray(curves or [[0,0]],dtype=np.float32)
        code=_lib.grainy_gpu_resident(handle,source.ctypes.data if upload else None,base+first,first,keep,result.ctypes.data,
                                      ct.byref(tp),tcurve.ctypes.data,ct.byref(cp),ccurve.ctypes.data,ccount,ct.byref(points),
                                      ct.byref(dp),table.ctypes.data,count,radii,offsets,int(bool(s['vignette'])),ct.byref(noise),
                                      field.ctypes.data if field is not None else None,grain_slot,len(local),params,masks,slots,
                                      curve.ctypes.data,len(curves),int(layers is not None))
        if code<0:
            known.clear();return None
        if upload:known[0]=keys[0]
        for stage in (1,2,3):
            if first<stage and keep[stage-1]>=0:known[stage]=keys[stage]
        if grain_slot>=0:known[_GRAIN]=grain[1]
        for i,(mask,key,edit) in enumerate(layers or []):known[_MASKS+i]=key
        return result


def _warp(function,a,p):
    source=np.ascontiguousarray(a,dtype=np.float32);result=np.empty_like(source)
    code=_call(function,source.ctypes.data,result.ctypes.data,ct.byref(p))
    return result if code is not None and code>=0 else None


def optical(a,s):
    """processing.optical_geometry's distortion/perspective/CA map and remap (after Lensfun and guides)."""
    if not enabled() or not _frame(a):return None
    h,w=a.shape[:2];f=np.float32
    p=_OpticalParams();p.width,p.height=w,h
    # Each constant is the float32 value numpy uses for the corresponding Python scalar.
    p.halfWidth=f((w-1)/2);p.halfHeight=f((h-1)/2);p.scale=f(max(w,h)/2)
    p.perspectiveH=f(s.get('perspective_h',0)/160);p.perspectiveV=f(s.get('perspective_v',0)/160)
    p.zoom=f(max(.2,s.get('transform_scale',100)/100));p.aspect=f(2**(s.get('aspect_scale',0)/100))
    p.shiftX=f(s.get('shift_x',0)/100);p.shiftY=f(s.get('shift_y',0)/100)
    p.distortion1=f(s.get('distortion',0)/100);p.distortion2=f(s.get('distortion_k2',0)/100);p.distortion3=f(s.get('distortion_k3',0)/100)
    p.channelScale[:3]=[f(1+s.get('ca_red',0)/10000),f(1+0),f(1+s.get('ca_blue',0)/10000)]
    return _warp(_lib.grainy_gpu_optical,a,p)


def rotate(a,angle):
    """PIL Image.rotate(angle, BICUBIC, expand=False) per channel; None for PIL's fast paths or no double support."""
    import math
    if not enabled() or not _frame(a):return None
    h,w=a.shape[:2];angle=angle%360.0
    if angle in (0,180) or angle in (90,270) and w==h:return None
    cx,cy=w/2,h/2;r=-math.radians(angle)
    m=[round(math.cos(r),15),round(math.sin(r),15),0.0,round(-math.sin(r),15),round(math.cos(r),15),0.0]
    m[2],m[5]=m[0]*-cx+m[1]*-cy+m[2],m[3]*-cx+m[4]*-cy+m[5]
    m[2]+=cx;m[5]+=cy
    p=_RotateParams();p.width,p.height=w,h;p.matrix[:]=m
    return _warp(_lib.grainy_gpu_rotate,a,p)


def _single_channel(function,a,*args):
    """Masks are one channel: warp a 3-channel copy and keep the middle channel (no lateral CA)."""
    if a.ndim!=3 or a.shape[2]!=1 or a.size==0:return None
    result=function(np.repeat(np.asarray(a,np.float32),3,axis=2),*args)
    return None if result is None else np.ascontiguousarray(result[...,1:2])


def optical_any(a,s):
    return optical(a,s) if a.ndim==3 and a.shape[2]==3 else _single_channel(optical,a,s)


def rotate_any(a,angle):
    return rotate(a,angle) if a.ndim==3 and a.shape[2]==3 else _single_channel(rotate,a,angle)
