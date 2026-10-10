"""Scene-relative HDR editing; 1.0 linear is SDR reference white.

The display and SDR rendition are separate. Extended pixels feed the native
HDR viewport, histogram/range diagnostics and floating-point TIFF output.
"""
import numpy as np


def color_operation(pixels, operation):
    """Apply bounded chroma tools while carrying per-pixel highlight intensity."""
    # The per-pixel gain, division and product run on the pixel workers (same values); operation sees the whole frame.
    from .pixel_jobs import rows
    if pixels.ndim!=3:
        gain=np.maximum(1.,pixels.max(axis=-1,keepdims=True))
        return operation(pixels/gain)*gain
    h,w,channels=pixels.shape
    gain=rows(h,w,lambda first,stop:np.maximum(1.,pixels[first:stop].max(axis=-1,keepdims=True)),channels=1,
              dtype=np.result_type(pixels.dtype,np.float32))
    bounded=rows(h,w,lambda first,stop:pixels[first:stop]/gain[first:stop],channels=channels,dtype=np.result_type(pixels,gain))
    result=operation(bounded)
    return rows(h,w,lambda first,stop:result[first:stop]*gain[first:stop],channels=result.shape[-1],dtype=np.result_type(result,gain))


def curve_extended(pixels, points):
    # Keep the endpoint's linear slope above SDR white rather than plateauing.
    p=np.asarray(points,np.float32)
    result=np.interp(pixels,p[:,0],p[:,1]).astype(np.float32)
    slope=max(0.,float((p[-1,1]-p[-2,1])/max(p[-1,0]-p[-2,0],1e-6)))
    return np.where(pixels>p[-1,0],p[-1,1]+(pixels-p[-1,0])*slope,result)


def limit(pixels, settings):
    from .pixel_jobs import transform
    return transform(pixels,lambda tile:_limit(tile,settings)) if pixels.dtype==np.float32 else _limit(pixels,settings)


def _limit(pixels, settings):
    from .engine import to_linear,to_srgb
    linear=to_linear(np.maximum(pixels,0))
    stops=np.log2(np.maximum(linear,1))
    gain=2**(float(np.clip(settings.get('hdr_brightness',0),-100,100))/100)
    linear=np.where(linear>1,2**(stops*gain),linear)
    ceiling=2**float(np.clip(settings.get('hdr_limit',4),0,4))
    return to_srgb(np.minimum(linear,ceiling))


def to_srgb_extended(pixels, working_space):
    from .engine import to_linear,to_srgb
    if working_space!='ProPhoto':return pixels
    from .colorio import convert,profile
    return to_srgb(convert(to_linear(pixels),profile('Linear ProPhoto'),profile('Linear sRGB')))


def display_linear(pixels,working_space):
    """Linear scRGB, including negative wide-gamut components; no monitor ICC.

    Windows applies its advanced-color monitor transform at presentation time.
    Applying the SDR monitor profile here would apply calibration twice.
    """
    from .engine import to_linear
    linear=to_linear(pixels)
    if working_space=='ProPhoto':
        from .colorio import convert,profile
        linear=convert(linear,profile('Linear ProPhoto'),profile('Linear sRGB'))
    rgba=np.ones((*linear.shape[:2],4),np.float32);rgba[...,:3]=linear
    return np.ascontiguousarray(rgba)


def sdr_rendition(pixels, settings):
    """Continuous, monotonic shoulder in linear light; never wraps bright values."""
    from .pixel_jobs import transform
    return transform(pixels,lambda tile:_sdr_rendition(tile,settings)) if pixels.dtype==np.float32 else _sdr_rendition(pixels,settings)


def _sdr_rendition(pixels, settings):
    from .engine import to_linear,to_srgb
    a=to_linear(pixels)*2**float(np.clip(settings.get('sdr_exposure',0),-5,5))
    peak=np.maximum(a.max(axis=-1,keepdims=True),0)
    # Compression starts between 0.1 and 1.0 SDR white. A zero value clips.
    knee=1-.9*float(np.clip(settings.get('sdr_compression',75),0,100))/100
    span=max(1-knee,1e-6)
    delta=np.maximum(peak-knee,0)
    shoulder=knee+span*delta/(delta+span)
    mapped=np.where(peak>knee,shoulder,peak)
    a*=np.divide(mapped,peak,out=np.ones_like(peak),where=peak>1e-8)
    return np.clip(to_srgb(a),0,1)


def histogram(pixels):
    from .engine import to_linear
    step=max(1,pixels.shape[0]//300)
    a=pixels[::step,::step]
    # Equal-width SDR and HDR halves, with one stop per quarter of HDR.
    coordinates=np.where(a<=1,np.clip(a,0,1)*.5,
                         .5+np.log2(np.maximum(to_linear(a),1))/8)
    bins=np.minimum((np.clip(coordinates,0,1)*128).astype(np.int32),127)
    return [np.bincount(bins[...,c].ravel(),minlength=128) for c in range(3)]


def visualize(pixels):
    from .engine import to_linear
    stops=np.log2(np.maximum(to_linear(pixels).max(axis=-1),1))
    colors=np.array([[.25,.55,1],[.2,1,.45],[1,.9,.1],[1,.25,.15]],np.float32)
    indices=np.clip(np.ceil(stops).astype(int)-1,0,3)
    gray=np.clip(pixels.mean(axis=-1),0,1)*.35
    return np.where((stops>1e-5)[...,None],colors[indices],gray[...,None])
