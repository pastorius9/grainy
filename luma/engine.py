"""Linear-light editing pipeline. Source buffers are immutable by convention."""
from __future__ import annotations

import io
import os
from pathlib import Path
from copy import deepcopy

import numpy as np
from collections import OrderedDict
from threading import Lock
from PIL import Image, ImageCms, ImageOps, ImageFilter
from .processing import (EXTRA_DEFAULTS,white_balance,color_tools,detail_tools,mix_hsl,
                         optical_geometry,apply_local,apply_retouch,sharpen,blur)

RAW_EXTENSIONS = {'.arw', '.cr2', '.cr3', '.nef', '.nrw', '.raf', '.rw2', '.orf', '.pef', '.dng', '.srw', '.3fr', '.iiq', '.kdc', '.mos'}
# Video support was removed in 0.5.74 (no FFmpeg in the app). The set stays so batch tools keep skipping
# video entries that an older version put in a catalog; videos can no longer be imported or opened.
VIDEO_EXTENSIONS={'.mp4','.mov','.avi','.mkv','.m4v'}
IMAGE_EXTENSIONS = RAW_EXTENSIONS | {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp'}
DEFAULTS = dict(exposure=0., contrast=0., highlights=0., shadows=0., whites=0., blacks=0.,
                temperature=0., tint=0., saturation=0., vibrance=0., clarity=0., sharpen=0.,
                vignette=0., grain=0.,
                # Post-crop vignette shape and grain character; these
                # defaults reproduce the earlier amount-only vignette and pixel grain exactly.
                vignette_midpoint=50., vignette_roundness=0., vignette_feather=50., vignette_highlights=0.,
                grain_size=25., grain_roughness=50., rotation=0, straighten=0., flip=False, crop=None,
                curve=[[0., 0.], [.25, .25], [.5, .5], [.75, .75], [1., 1.]],
                hsl=[[0., 0., 0.] for _ in range(8)], monochrome=False,
                video_color=None, video_time=0., hdr=False, hdr_limit=4.,
                hdr_brightness=0., sdr_exposure=0., sdr_compression=75.,
                photo_filter='warming85', photo_filter_enabled=False,
                photo_filter_density=25., photo_filter_luminosity=True,
                # 2 (0.5.56): exposure, white balance and lens vignette lift roll highlights off smoothly
                # instead of clipping. Stored settings without the key keep version 1 when that changes them.
                # 3 (0.5.80): Highlights and Shadows keep local contrast and colour (tone_response). Stored
                # edits that use those sliders keep their version, and their look, until it is updated.
                # 0.5.85 redesigned the response of process 3 (see tone_response) without a new number:
                # photos edited with those sliders in 0.5.80-0.5.84 changed a little.
                tone_version=3,
                # 2 (0.5.60): sharpening adds luminance detail only, so it no longer
                # amplifies leftover colour noise. 3 (0.5.61): wavelet luminance noise reduction.
                # Stored edits without the key keep the version that renders them unchanged.
                detail_version=3)
DEFAULTS.update(deepcopy(EXTRA_DEFAULTS))
SHOULDER_KNEE = .5   # linear light; values below are never changed by the highlight roll-off
ROLLOFF_WHITE = 4.   # at most two stops of highlights are rolled off; stronger gains still clip (and warn)

PRESETS = {
    '원본의 색': {},
    'Soft daylight': dict(exposure=.15, contrast=-12, highlights=-22, shadows=18, temperature=4, saturation=-6),
    'Amber film': dict(temperature=16, tint=4, contrast=12, highlights=-28, shadows=12, saturation=-14, grain=12, vignette=12),
    'Silver / B&W': dict(monochrome=True, contrast=24, highlights=-15, shadows=10, grain=16),
    'Crisp landscape': dict(contrast=15, vibrance=22, highlights=-24, shadows=22, clarity=22, sharpen=25),
    'Quiet blue': dict(temperature=-13, saturation=-16, contrast=-6, shadows=20, blacks=8),
}


def defaults():
    return deepcopy(DEFAULTS)


# The HDR editing mode is switched off (0.5.73): its ordinary (SDR) export dulled whites and there is no
# shareable HDR output format yet. Photos saved with HDR on develop as ordinary photos. The code and
# its tests stay so the mode can return; tests and the self-test switch it on.
HDR_FEATURE = False


def normalized(settings):
    result = defaults()
    result.update({k: deepcopy(v) for k, v in settings.items() if k in DEFAULTS})
    if not HDR_FEATURE:
        result['hdr'] = False
    if 'tone_version' not in settings:
        # Edits saved before 0.5.56: the roll-off only changes pixels when the linear gain exceeds 1,
        # so those photos keep the original clipping and every other photo already looks the same.
        result['tone_version'] = 1 if tone_white(result) > 1 and not result['hdr'] else 2
    if result['tone_version'] == 2 and not result['shadows'] and not result['highlights']:
        result['tone_version'] = 3          # identical pixels; the sliders then start with the current process
    if 'detail_version' not in settings:
        result['detail_version'] = 1 if result['sharpen'] else 2 if result['noise_luma'] else 3
    return result


def tone_white(s):
    """Largest linear gain the tone stage applies before encoding: exposure, white balance, vignette lift."""
    lift = max(1., 1 + 2 * float(s['lens_vignette']) / 100)
    return float(2. ** s['exposure']) * float(np.max(white_balance(s))) * lift


def rolloff_white(s):
    """The linear value mapped to white by the roll-off: the tone gain, capped at ROLLOFF_WHITE."""
    return min(tone_white(s), ROLLOFF_WHITE)


def highlight_rolloff(a, white, knee=SHOULDER_KNEE):
    """Compress linear values above knee so that `white` (the brightest input after gain) maps to 1.

    Rational shoulder k + (1-k)*A*u/(1+(A-1)*u), u=(x-k)/(white-k), A=(white-k)/(1-k): slope 1 at the
    knee (no visible bend), monotonic, and no plateau below white. Values below the knee are unchanged;
    values above white continue past 1 and are clipped later.
    """
    if white <= 1:
        return a
    slope = np.float32((white - knee) / (1 - knee))
    u = (a - np.float32(knee)) / np.float32(white - knee)
    rolled = np.float32(knee) + np.float32(1 - knee) * (slope * u / (1 + (slope - 1) * u))
    return np.where(a > knee, rolled, a).astype(np.float32)


def to_linear(rgb):
    a = np.asarray(rgb, dtype=np.float32)
    from .pixel_jobs import transform
    return transform(a, lambda a: np.where(a <= .04045, a / 12.92,
        (np.maximum(a + .055,0) / 1.055) ** 2.4).astype(np.float32))


def to_srgb(rgb):
    from .pixel_jobs import transform
    return transform(np.asarray(rgb), _to_srgb)


def _to_srgb(rgb):
    a = np.maximum(rgb, 0)
    return np.where(a <= .0031308, a * 12.92, 1.055 * np.power(a, 1 / 2.4) - .055).astype(np.float32)


def srgb_profile():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()


def load_image(path, max_size=None,working_space='sRGB',raw_options=None):
    path = Path(path)
    metadata = {'filename': path.name, 'format': path.suffix[1:].upper(), 'size_mb': round(path.stat().st_size / 1024**2, 2)}
    from .preview_store import signature
    metadata['file_fingerprint']=signature(path)
    metadata['working_space']=working_space
    wide=working_space=='ProPhoto'
    from .colorio import convert,profile
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        raise ValueError('동영상 파일은 지원하지 않습니다.')
    metadata.update(read_metadata(path))
    if path.suffix.lower() in RAW_EXTENSIONS:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            full_width,full_height = raw.sizes.width,raw.sizes.height
            if raw.sizes.flip in (5,6):
                full_width,full_height = full_height,full_width
            # Half-size Bayer development is enough for previews; exports always develop at full size.
            from .rawcolor import enabled,decode
            native=enabled(raw_options or {})
            if native:rgb=decode(raw,metadata,max_size,path)
            else:
                half = max_size is not None and max(raw.sizes.width, raw.sizes.height) > max_size * 2
                a = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=16,
                                    gamma=(1, 1), output_color=rawpy.ColorSpace.ProPhoto if wide else rawpy.ColorSpace.sRGB,
                                    half_size=half)
                from .rasterio import _normalise
                rgb = _normalise(a, 16)   # a.astype(np.float32)/65535 on the row workers
                if not half:
                    from .rawcolor import reduce_false_color
                    rgb = reduce_false_color(rgb)
                metadata['raw_native']=False
            metadata.update(width=full_width, height=full_height, decoder='LibRaw',
                source_orientation={3:3,5:8,6:6}.get(raw.sizes.flip,1))
        if max_size and max(rgb.shape[:2]) > max_size:
            rgb = resize_float(rgb, max_size)
        return rgb, metadata
    from .rasterio import read_bitmap
    rgb,info=read_bitmap(path,working_space,max_size)
    metadata.update(info)
    return rgb,metadata


def read_metadata(path):
    import exifread
    result={}
    if Path(path).suffix.lower()=='.png':
        with Image.open(path) as image:
            exif=image.getexif()
            values={**exif,**exif.get_ifd(34665)}
            for key,name in [(271,'make'),(272,'camera'),(315,'creator'),(33432,'copyright'),
                    (33434,'shutter'),(33437,'aperture'),(34855,'iso'),(37386,'focal_length'),
                    (36867,'date'),(42036,'lens'),(37382,'subject_distance'),(42033,'serial')]:
                if key in values:
                    value=values[key]
                    if name in ('iso','aperture','focal_length','subject_distance'):
                        try:value=float(value)
                        except (ValueError,TypeError):value=str(value)
                    else:value=str(value)
                    result[name]=value
            gps=exif.get_ifd(34853)
            for name,key,ref in [('latitude',2,1),('longitude',4,3)]:
                if key in gps:
                    try:
                        d,m,s=map(float,gps[key]);value=d+m/60+s/3600
                        if gps.get(ref) in ('S','W',b'S',b'W'):value=-value
                        result[name]=value
                    except (ValueError,TypeError,ZeroDivisionError):pass
        return result
    with Path(path).open('rb') as file:
        tags=exifread.process_file(file,details=False,strict=False)
    if not tags and Path(path).suffix.lower() in ('.cr3','.raf'):
        tags=_container_tags(path)
    for key,name in [('Image Make','make'),('Image Model','camera'),('EXIF ExposureTime','shutter'),
            ('EXIF FNumber','aperture'),('EXIF ISOSpeedRatings','iso'),('EXIF FocalLength','focal_length'),
            ('EXIF DateTimeOriginal','date'),('EXIF LensModel','lens'),('EXIF SubjectDistance','subject_distance'),('Image Artist','creator'),('Image Copyright','copyright'),('EXIF BodySerialNumber','serial')]:
        if key in tags:
            value=tags[key]
            if name in ('iso','aperture','focal_length','subject_distance'):
                try:value=float(value.values[0])
                except (ValueError,TypeError,IndexError):value=str(value)
            else:value=str(value)
            result[name]=value
    if 'iso' not in result and Path(path).suffix.lower() in ('.rw2','.rwl') and 'Image Tag 0x0017' in tags:
        # Panasonic/Leica raws keep ISO in their own IFD0 tag 0x0017 instead of EXIF ISOSpeedRatings.
        try:result['iso']=float(tags['Image Tag 0x0017'].values[0])
        except (ValueError,TypeError,IndexError):pass
    for name,tag,ref in [('latitude','GPS GPSLatitude','GPS GPSLatitudeRef'),('longitude','GPS GPSLongitude','GPS GPSLongitudeRef')]:
        if tag in tags:
            try:
                values=[float(v) for v in tags[tag].values]
                coordinate=values[0]+values[1]/60+values[2]/3600
                if str(tags.get(ref,'')) in ('S','W'):coordinate=-coordinate
                result[name]=coordinate
            except (ValueError,TypeError,IndexError):pass
    return result


def _container_tags(path):
    """exifread tags for raw containers exifread cannot parse: Canon CR3 (TIFF blocks in CMT1 camera,
    CMT2 EXIF and CMT4 GPS boxes) and Fujifilm RAF (EXIF of the embedded JPEG)."""
    import exifread, io, struct
    path = Path(path)
    try:
        with path.open('rb') as file:
            if path.suffix.lower() == '.raf':
                header = file.read(92)
                if not header.startswith(b'FUJIFILMCCD-RAW'):
                    return {}
                offset, length = struct.unpack('>II', header[84:92])
                if not 0 < length <= 64 * 1024**2:
                    return {}
                file.seek(offset)
                return exifread.process_file(io.BytesIO(file.read(length)), details=False, strict=False)
            head = file.read(4 * 1024**2)
    except (OSError, struct.error, ValueError):
        return {}
    tags = {}
    for box, prefix in ((b'CMT1', 'Image'), (b'CMT2', 'EXIF'), (b'CMT4', 'GPS')):
        at = head.find(box)
        if at < 4:
            continue
        size = struct.unpack('>I', head[at - 4:at])[0]
        block = head[at + 4:at - 4 + size]
        if not block.startswith((b'II*\x00', b'MM\x00*')):
            continue
        for key, value in exifread.process_file(io.BytesIO(block), details=False, strict=False).items():
            # Each block is a stand-alone TIFF, so exifread files every tag under 'Image'.
            tags[prefix + key[5:] if key.startswith('Image ') else key] = value
    return tags


def resize_float(a, longest):
    h, w = a.shape[:2]
    if max(w, h) <= longest:
        return a
    factor = longest / max(w, h)
    size = (max(1, round(w * factor)), max(1, round(h * factor)))
    # PIL releases the GIL while resampling; channels resize in parallel with identical results.
    resize=lambda c:np.asarray(Image.fromarray(a[..., c]).resize(size, Image.Resampling.BILINEAR))
    result=np.stack(list(_channel_pool().map(resize,range(a.shape[-1]))), axis=-1)
    from .rawcolor import CameraSource
    return CameraSource(result,a.raw_info) if isinstance(a,CameraSource) else result


def resize_export(a, longest, hdr=False):
    """Export downscaling: Lanczos-3 per channel on the encoded output values.

    Sharper at 2-6 px detail and with less moire above Nyquist than resize_float (bilinear); see
    CHANGELOG 0.5.56. Negative lobes are clipped to the valid range (0..1, or >=0 for HDR).
    """
    h, w = a.shape[:2]
    if max(w, h) <= longest:
        return a
    factor = longest / max(w, h)
    size = (max(1, round(w * factor)), max(1, round(h * factor)))
    resize=lambda c:np.asarray(Image.fromarray(np.ascontiguousarray(a[..., c],dtype=np.float32)).resize(size, Image.Resampling.LANCZOS))
    result = np.stack(list(_channel_pool().map(resize, range(a.shape[-1]))), axis=-1)
    return np.maximum(result, 0) if hdr else np.clip(result, 0, 1)


_rotate_pool = None


def _channel_pool():
    global _rotate_pool
    if _rotate_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _rotate_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='LumaChannels')
    return _rotate_pool


def geometry(a, settings, use_crop=True, copy=True):
    """Oriented, corrected and cropped frame; always a new array unless copy=False.

    use_crop='all' is the crop tool's view: uncropped, on a canvas enlarged (straighten.padding) so
    that the corners of a straightened photo stay visible. Perspective alignment keeps its own canvas.

    copy=False returns a read-only view of `a` when no geometry applies (develop's stages never write
    their input), saving a full-size copy.
    """
    source=a
    a=optical_geometry(a,settings)
    turns=-int(settings.get('rotation', 0)) % 4
    if turns:
        a = np.rot90(a, turns).copy()
    elif copy and a is source:
        a = a.copy()
    if settings.get('flip'):
        a = a[:, ::-1].copy()
    angle = float(settings.get('straighten', 0))
    if angle and use_crop == 'all' and not settings.get('upright'):
        from .straighten import padding
        pad_x, pad_y = padding(angle, a.shape[1], a.shape[0])
        if pad_x or pad_y:
            a = np.pad(a, ((pad_y, pad_y), (pad_x, pad_x))+((0, 0),)*(a.ndim-2))
    if angle:
        # PIL releases the GIL while rotating, so the channels rotate in parallel with identical results.
        from . import native_gpu
        rotated = native_gpu.rotate_any(a, -angle) if a.ndim == 3 and a.shape[-1] in (1, 3) else None
        if rotated is None:
            rotate = lambda c: np.asarray(Image.fromarray(a[..., c]).rotate(-angle, Image.Resampling.BICUBIC, expand=False))
            rotated = np.stack(list(_channel_pool().map(rotate, range(a.shape[-1]))), axis=-1)
        a = rotated
    if settings.get('upright'):
        from .upright import apply
        a=apply(a,settings)
    crop = settings.get('crop') if use_crop and use_crop != 'all' else None
    if crop:
        x0, y0, x1, y1 = crop
        h, w = a.shape[:2]
        l, t = min(w-1, max(0, round(x0*w))), min(h-1, max(0, round(y0*h)))
        r, b = max(l+1, min(w, round(x1*w))), max(t+1, min(h, round(y1*h)))
        a = a[t:b, l:r].copy()
    if a is source:
        a = a.view();a.setflags(write=False)
    return a


def rgb_to_hsv(a):
    hi, lo = a.max(axis=-1), a.min(axis=-1)
    d = hi-lo
    safe = np.maximum(d, 1e-7)
    hue = np.zeros_like(hi)
    for c, base in [(2, 4), (1, 2), (0, 0)]:
        h = ((a[..., (c+1)%3] - a[..., (c+2)%3]) / safe + base) / 6
        hue = np.where(hi == a[..., c], h, hue)
    return hue % 1, np.where(hi > 0, d / np.maximum(hi, 1e-7), 0), hi


def hsv_to_rgb(h, s, v):
    i = np.floor(h*6).astype(int) % 6
    f = h*6-np.floor(h*6)
    p, q, t = v*(1-s), v*(1-f*s), v*(1-(1-f)*s)
    combos = [(v,t,p),(q,v,p),(p,v,t),(p,q,v),(t,p,v),(v,p,q)]
    out = np.zeros(h.shape+(3,), np.float32)
    for n, channels in enumerate(combos):
        for c in range(3):
            out[...,c] = np.where(i == n, channels[c], out[...,c])
    return out


def _writable(a):
    return a if a.flags.writeable else a.copy()


def _develop_tone(a, s):
    from . import native_gpu
    result = native_gpu.tone(a, s)
    if result is not None:
        return result
    a = _writable(a)
    local = tone_local(a, s)
    if not s['lens_vignette']:
        from .pixel_jobs import rows, transform
        if local is not None and a.ndim == 3:
            return rows(a.shape[0], a.shape[1], lambda first, stop: _develop_tone_pixels(a[first:stop], s, local, first), channels=a.shape[2])
        return transform(a, lambda tile: _develop_tone_pixels(tile, s))
    return _develop_tone_pixels(a, s, local)


def _tone_encoded(a, s):
    """Exposure, white balance, lens vignette and highlight roll-off on linear pixels (in place), then sRGB encoding."""
    a *= 2. ** s['exposure']
    a *= white_balance(s)
    if s['lens_vignette']:
        y,x=np.ogrid[-1:1:complex(a.shape[0]),-1:1:complex(a.shape[1])]
        a*=np.maximum(.1,1+(x*x+y*y)*s['lens_vignette']/100)[...,None]
    if s.get('tone_version',1) >= 2 and not s['hdr']:
        a = highlight_rolloff(a, rolloff_white(s))
    return to_srgb(a)


def tone_base(a, s):
    """Whole-frame analysis of linear frame `a` for the process 3 Highlights/Shadows. It depends on the
    frame and on exposure, white balance, lens vignette and roll-off, not on the two sliders."""
    import cv2
    from . import tone_response
    height, width = a.shape[:2]
    scale = tone_response.MAP_SIZE / max(height, width)
    small = cv2.resize(a, (max(1, round(width*scale)), max(1, round(height*scale))), interpolation=cv2.INTER_AREA) if scale < 1 else a
    return tone_response.analyse(_tone_encoded(np.array(small[..., :3], np.float32, copy=True), s), (height, width))


def tone_local(a, s, base=None):
    """What tone_response.apply needs for linear frame `a`; None when process 3 Highlights/Shadows are not used."""
    if a.ndim != 3 or s.get('tone_version',1) < 3 or s['hdr'] or not (s['shadows'] or s['highlights']):
        return None
    from . import tone_response
    return tone_response.with_curve(base or tone_base(a, s), s['highlights'], s['shadows'])


def _develop_tone_pixels(a, s, local=None, first=0):
    """Tone stage on linear rows first.. of a frame. `local` is tone_local() of the whole frame; without
    it `a` is taken to be the whole frame."""
    if local is None:
        local = tone_local(a, s)            # before the pixels change in place
    a = _tone_encoded(a, s)
    if local is not None:
        from . import tone_response
        a = tone_response.apply(a, local, first)
    elif s['shadows'] or s['highlights']:
        luma = a @ np.array([.2126, .7152, .0722], np.float32)
        low = np.clip(1-luma, 0, 1)**3
        high = np.clip(luma, 0, 1)**3
        a += (s['shadows']/180 * low * np.clip(luma*4, 0, 1) + s['highlights']/180 * high * np.clip((1-luma)*4, 0, 1))[..., None]
    # Endpoint controls, rather than a weak shadow/highlight offset. At -100,
    # Blacks maps the bottom 40% to black; -200 reaches a black point of 0.8.
    # Positive Blacks lifts the floor while preserving white. Whites spans
    # 0.25x–4x over the UI range, with neutral settings exactly unchanged.
    black = float(np.clip(s['blacks'], -200, 200)) / 250
    if black < 0:
        a = np.maximum(a + black, 0) / (1 + black)
    elif black > 0:
        a = a * (1 - black) + black
    if s['whites']:
        a *= 2. ** (float(np.clip(s['whites'], -200, 200)) / 100)
    if s['contrast']:
        a = (a-.5) * (1+s['contrast']/125) + .5
    curve = np.array(s['curve'], np.float32)
    if not np.allclose(curve[:,0], curve[:,1]):
        if s['hdr']:
            from .hdr import curve_extended
            a=curve_extended(a,curve)
        else:a = np.interp(np.clip(a, 0, 1), curve[:,0], curve[:,1]).astype(np.float32)
    a = np.maximum(a,0) if s['hdr'] else np.clip(a, 0, 1)
    return a


def _develop_color(a, s, on_point_input=None):
    if s.get('hdr'):
        from .hdr import color_operation
        return color_operation(a,lambda bounded:_develop_color(bounded,{**s,'hdr':False},on_point_input))
    if on_point_input is None:
        from . import native_gpu
        result = native_gpu.color(a, s)
        if result is not None:
            return result
    a=color_tools(_develop_color_mix(_writable(a),s),s,on_point_input)
    if s['photo_filter_enabled']:
        from .photo_filter import apply
        a=apply(a,s)
    return a


def _develop_color_mix(a, s):
    """Saturation, vibrance and the colour mixer: everything before the channel curves."""
    if s['saturation'] or s['vibrance']:
        gray = a @ np.array([.2126, .7152, .0722], np.float32)
        factor = 1+s['saturation']/100
        if s['vibrance']:
            sat = a.max(axis=-1)-a.min(axis=-1)
            factor = (factor+s['vibrance']/100*(1-sat))[...,None]
        a = gray[...,None] + (a-gray[...,None])*factor
    if s['mixer_mode']=='hsl' and any(any(x for x in group) for group in s['hsl']):
        a=mix_hsl(a,s['hsl'])
    elif any(any(x for x in group) for group in s['hsl']):
        h, sat, val = rgb_to_hsv(np.clip(a, 0, 1))
        dh, ds, dv = np.zeros_like(h), np.zeros_like(h), np.zeros_like(h)
        for center, (hh, ss, ll) in zip([0, 1/12, 1/6, 1/3, .5, 2/3, .75, 5/6], s['hsl']):
            if not (hh or ss or ll):continue
            distance = np.abs((h-center+.5) % 1-.5)
            weight = np.maximum(0, 1-distance/(1/8)) ** 2
            if hh:dh += weight * hh / 600
            if ss:ds += weight * ss / 100
            if ll:dv += weight * ll / 200
        a = hsv_to_rgb((h+dh)%1, np.clip(sat*(1+ds),0,1), np.clip(val+dv,0,1))
    return a


def _develop_detail(a, s, pixel_scale=1.):
    from . import native_gpu
    tools_done = False
    if not s.get('hdr') and native_gpu.enabled():
        if not native_gpu.detail_tools_on_gpu(s, pixel_scale):
            a = detail_tools(a, s, pixel_scale)
            tools_done = True
        result = native_gpu.detail(a, s, pixel_scale, tools=not tools_done)
        if result is not None:
            return _add_grain(result, s, owned=True, pixel_scale=pixel_scale)
    a = _writable(a)
    if s.get('hdr'):
        from .hdr import color_operation
        a=color_operation(a,lambda bounded:detail_tools(bounded,s,pixel_scale))
    elif not tools_done:a=detail_tools(a,s,pixel_scale)
    if s['monochrome']:
        a = np.repeat((a @ np.array([.2126,.7152,.0722],np.float32))[...,None], 3, axis=-1)
    if s['clarity'] or s['sharpen']:
        if s['clarity']:
            a += (a-blur(a,max(pixel_scale,min(a.shape[:2])/80)))*s['clarity']/140
        if s['sharpen']:
            a=sharpen(a,s['sharpen'],s['sharpen_radius'],s['sharpen_detail'],s['sharpen_mask'],pixel_scale,
                      luminance=s.get('detail_version',1)>=2)
    if s['vignette']:
        a = post_crop_vignette(a, s)
    return _add_grain(a, s, pixel_scale=pixel_scale)


def vignette_shape(s, width, height):
    """(roundness blend, p-norm, midpoint scale squared, feather exponent, highlight protection, x/y aspect)
    shared with the GPU. Defaults give (0, 2, 1, 1, 0, ...): the earlier elliptical x*x+y*y vignette."""
    roundness = float(s.get('vignette_roundness', 0))
    longest = max(width, height, 1)
    return (max(0., roundness)/100, 2+max(0., -roundness)/100*6, 4**((50-float(s.get('vignette_midpoint', 50)))/50),
            4**((50-float(s.get('vignette_feather', 50)))/50), float(s.get('vignette_highlights', 0))/100,
            width/longest, height/longest)


def post_crop_vignette(a, s):
    """Darken towards the frame edges. Roundness above 0 turns the frame-shaped ellipse into a circle,
    below 0 into a rounded rectangle; midpoint below 50 starts nearer the centre; feather below 50
    makes the edge harder; highlights keeps bright areas."""
    round_t, p, scale, exponent, protect, ax, ay = vignette_shape(s, a.shape[1], a.shape[0])
    y, x = np.ogrid[-1:1:complex(a.shape[0]), -1:1:complex(a.shape[1])]
    if round_t:
        x = x*np.float32(1+round_t*(ax-1)); y = y*np.float32(1+round_t*(ay-1))
    d2 = x*x+y*y if p == 2 else (np.abs(x)**p+np.abs(y)**p)**(2/p)
    if scale != 1: d2 = d2*scale
    d2 = d2.clip(0, 2)
    if exponent != 1: d2 = 2*(d2/2)**exponent
    darken = d2*s['vignette']/220
    if protect:
        lum = np.clip((a@np.array([.2126, .7152, .0722], np.float32)-.5)/.5, 0, 1)
        darken = darken*(1-protect*lum*lum)
    a *= (1-darken)[..., None]       # in place like before: float32 result, identical at the defaults
    return a


_GRAIN_FIELDS = OrderedDict()
_GRAIN_LOCK = Lock()


def _grain_field(shape):
    """Standard-normal field of the fixed grain seed; normal(0, s) equals s*field exactly."""
    with _GRAIN_LOCK:
        field = _GRAIN_FIELDS.pop(shape, None)
        if field is None:
            field = np.random.default_rng(2026).standard_normal(shape)
            field.setflags(write=False)
            while len(_GRAIN_FIELDS) >= 3:
                _GRAIN_FIELDS.popitem(last=False)
        _GRAIN_FIELDS[shape] = field
        return field


_GRAIN_NOISE = OrderedDict()


def _normalized_blur(field, sigma):
    import cv2
    f = cv2.GaussianBlur(np.asarray(field, np.float32), (0, 0), sigma, borderType=cv2.BORDER_REFLECT_101)
    return f/max(float(f.std()), 1e-6)


def grain_pattern(shape, size=25., roughness=50., pixel_scale=1.):
    """Unit-deviation grain of the fixed seed. Size above 25 blurs the pixel noise into larger grains
    (sigma up to 4.5 original pixels at 100, scaled like the other spatial filters); roughness above 50
    modulates it with a coarse field (clumpy, uneven), below 50 blends in a slightly smoothed copy
    (even). Larger grains get up to 1.9x the deviation (size 100), since soft large grains of equal
    deviation looked much weaker than pixel grain. size 25 / roughness 50 is the field itself."""
    field = _grain_field(shape)
    sigma = max(0., (size-25)/25)*1.5*pixel_scale
    f = _normalized_blur(field, sigma) if sigma >= .3 else field
    if size > 25: f = f*np.float32(1+(size-25)/75*.9)
    if roughness > 50:
        depth = (roughness-50)/50*.6
        coarse = _normalized_blur(field[::-1, ::-1], max(2., 3*sigma+2))
        f = f*np.maximum(0, 1+depth*coarse)
    elif roughness < 50:
        k = (50-roughness)/50
        f = f*(1-k)+_normalized_blur(f, .7)*k
    return f


def _grain_noise(shape, grain, size=25., roughness=50., pixel_scale=1.):
    """float32 grain for (shape, amount, character), kept for the last two combinations (slider edits reuse it)."""
    plain = size == 25 and roughness == 50
    key = (shape, float(grain)) if plain else (shape, float(grain), float(size), float(roughness), float(pixel_scale))
    with _GRAIN_LOCK:
        noise = _GRAIN_NOISE.pop(key, None)
    if noise is None:
        pattern = _grain_field(shape) if plain else grain_pattern(shape, size, roughness, pixel_scale)
        noise = (0 + grain/1800 * pattern).astype(np.float32)
        noise.setflags(write=False)
    with _GRAIN_LOCK:
        _GRAIN_NOISE[key] = noise
        while len(_GRAIN_NOISE) > 2:
            _GRAIN_NOISE.popitem(last=False)
    return noise


def grain_key(shape, s, pixel_scale=1.):
    """Identity of the grain field for the resident GPU slot."""
    return (shape, float(s['grain']), float(s.get('grain_size', 25)), float(s.get('grain_roughness', 50)), float(pixel_scale))


def settings_grain(shape, s, pixel_scale=1.):
    return _grain_noise(shape, s['grain'], s.get('grain_size', 25.), s.get('grain_roughness', 50.), pixel_scale)


def _add_grain(a, s, owned=False, pixel_scale=1.):
    """owned=True adds in place (a fresh GPU result), saving a full-size allocation."""
    if s['grain']:
        # Stable seed keeps repeated previews/exports deterministic at a given resolution.
        # Row tiles on the pixel workers; one broadcast add over 12 MP otherwise takes ~0.1 s.
        from .pixel_jobs import rows, each
        noise = settings_grain(a.shape[:2], s, pixel_scale)
        if owned and a.dtype == np.float32 and a.flags.writeable:
            each(a.shape[0], a.shape[1], lambda first, stop: np.add(a[first:stop], noise[first:stop, :, None], out=a[first:stop]))
            return a
        return rows(a.shape[0], a.shape[1], lambda first, stop: a[first:stop] + noise[first:stop, :, None], channels=a.shape[2])
    return a


GEOMETRY_KEYS=('retouch','rotation','straighten','flip','crop','distortion',
               'distortion_k2','distortion_k3','perspective_h','perspective_v',
               'aspect_scale','shift_x','shift_y','transform_scale','guides','ca_red','ca_blue',
               'lensfun','lensfun_enabled','lensfun_distortion','lensfun_tca','lensfun_vignette','lensfun_scale',
               'upright','upright_crop')
TONE_KEYS=('exposure','temperature','tint','kelvin','wb_reference','kelvin_enabled',
           'lens_vignette','shadows','highlights','blacks','whites','contrast','curve','hdr','tone_version')
COLOR_KEYS=('saturation','vibrance','mixer_mode','hsl','rgb_curves','parametric',
            'calibration','point_colors','grading','grading_balance','camera_matrix',
            'photo_filter','photo_filter_enabled','photo_filter_density','photo_filter_luminosity','monochrome')
DETAIL_KEYS=('noise_luma','noise_color','dehaze','texture','defringe','monochrome',
             'clarity','sharpen','sharpen_radius','sharpen_detail','sharpen_mask','vignette','grain','detail_version',
             'vignette_midpoint','vignette_roundness','vignette_feather','vignette_highlights','grain_size','grain_roughness')


def develop(source, settings, use_crop=True,output_space='sRGB', *, cache=None,on_mask=None,original_size=None,on_point=None,on_layer_input=None):
    """Develop a frame with the same math for preview and export.

    An optional worker-owned cache reuses unchanged stages without approximating
    their pixels. on_mask receives (layer_index, coverage) before each local edit.
    output_space=None returns developed working pixels for independent display,
    histogram and thumbnail transforms without repeating the editing pipeline.
    original_size is the oriented, uncropped (width, height) of a reduced source.
    Spatial filter radii then follow original pixels; full-size exports retain
    their existing math. Filtering a reduced source remains an approximation.
    """
    from .render_cache import settings_key
    if on_point is not None or on_layer_input is not None:cache=None
    s = normalized(settings)
    pixel_scale=1.
    if original_size is not None:
        size=np.asarray(original_size,dtype=float)
        if size.shape!=(2,) or not np.isfinite(size).all() or np.any(size<=0):
            raise ValueError('Original image size must contain positive width and height')
        pixel_scale=min(1.,max(source.shape[:2])/float(max(size)))
    if cache is not None:
        cache.bind(source)
        # Avoid extra full-resolution copies when a useful prefix cannot fit.
        if source.nbytes * 2 > cache.max_bytes:
            cache = None
    from .rawcolor import CameraSource,develop_camera,RAW_KEYS
    raw_key=settings_key(s,RAW_KEYS) if cache and isinstance(source,CameraSource) else None
    linear=(cache.evaluate('raw',raw_key,lambda:develop_camera(source,s)) if cache else develop_camera(source,s)) if isinstance(source,CameraSource) else source
    key = (raw_key,use_crop, settings_key(s, GEOMETRY_KEYS)) if cache else None
    geometry_key = key
    from .optics import lens_shading
    make_geometry = lambda: geometry(lens_shading(apply_retouch(linear,s['retouch']),s),s,use_crop,copy=False)
    a = cache.evaluate('geometry', key, make_geometry) if cache else make_geometry()
    color_function=(lambda a,s:_develop_color(a,s,lambda i,value:on_point(None,i,value,None,(0,0),value.shape[:2]))) if on_point is not None else _develop_color
    stages=[('tone',TONE_KEYS,_develop_tone),
            ('color',COLOR_KEYS,color_function),
            ('detail',DETAIL_KEYS,lambda a,s:_develop_detail(a,s,pixel_scale))]
    color_source=None
    if s['masks'] and any(c.get('auto_mask') for layer in s['masks'] for c in layer.get('components',[])):
        color_source=cache.evaluate('mask-source',raw_key,lambda:to_srgb(linear)) if cache else to_srgb(linear)
    finished=False
    if cache and on_point is None:
        # Previews: stage results stay on the GPU. With shape masks only (or none), grain, local edits and the
        # clip run there too and only the developed frame comes back; otherwise the detail result does.
        from . import native_gpu
        if native_gpu.enabled() and native_gpu.detail_tools_on_gpu(s,pixel_scale):
            tone_key=(key,settings_key(s,TONE_KEYS));color_key=(tone_key,settings_key(s,COLOR_KEYS))
            detail_key=((color_key,pixel_scale),settings_key(s,DETAIL_KEYS))
            version=cache.version
            keys=((version,key),(version,tone_key),(version,color_key),(version,detail_key))
            grain=(settings_grain(a.shape[:2],s,pixel_scale),grain_key(a.shape[:2],s,pixel_scale)) if s['grain'] else None
            tail=None
            if on_layer_input is None and not s['hdr']:
                from .processing import resident_layers
                tail=resident_layers(a,source.shape,s['masks'],lambda mask:geometry(mask,s,use_crop),cache,geometry_key,
                                     color_source,version,with_disabled=on_mask is not None)
            if tail is not None:
                result=native_gpu.develop_resident(cache,a,keys,s,pixel_scale,grain,tail[0])
                if result is not None:
                    cache.discard('tone','color')   # stale host copies; the device holds the current ones
                    if on_mask is not None:
                        for index,mask in tail[1]:on_mask(index,mask)
                    a,stages,finished=result,[],True
            if not finished:
                detail=cache.lookup('detail',detail_key)
                if detail is None:
                    result=native_gpu.develop_resident(cache,a,keys,s,pixel_scale,grain)
                    if result is not None:
                        cache.discard('tone','color')
                        detail=cache.store('detail',detail_key,result)
                if detail is not None:
                    a,key,stages=detail,detail_key,[]
    if not cache and on_point is None:
        # Exports and uncached renders: one GPU round trip for tone, colour and detail.
        from . import native_gpu
        chained = native_gpu.develop(a, s, pixel_scale)
        if chained is not None:
            a = _add_grain(chained, s, owned=True, pixel_scale=pixel_scale)
            stages = []
    for stage, keys, function in stages:
        if cache:
            if stage=='detail':key=(key,pixel_scale)
            key = (key,settings_key(s,keys))
            # Cached arrays are read-only; GPU stages never write their input and CPU fallbacks copy it.
            a = cache.evaluate(stage,key,lambda a=a,f=function:f(a,s))
        else:
            a = function(a,s)
    if s['masks'] and not finished:
        layers=[{**layer,'adjustments':{**layer.get('adjustments',{}),'hdr':True}} for layer in s['masks']] if s['hdr'] else s['masks']
        a=apply_local(a,source.shape,layers,lambda mask:geometry(mask,s,use_crop),
                      cache=cache,cache_key=key,on_mask=on_mask,source_image=color_source,pixel_scale=pixel_scale,on_point=on_point,on_layer_input=on_layer_input,
                      geometry_key=geometry_key,detail_version=s.get('detail_version',1))
    if s['hdr']:
        from .hdr import limit,sdr_rendition
        a=limit(a,s)
        if output_space is None:return a
        a=sdr_rendition(a,s)
    else:
        if not finished:a=_clip01(a)   # the GPU preview chain already clipped
        if output_space=='sRGB' and s['working_space']!='ProPhoto':return a   # output_rgb would only clip again
    return a if output_space is None else output_rgb(a,s['working_space'],output_space,owned=True)


def _clip01(a, owned=True):
    """np.clip(a,0,1) as float32; in place when the caller owns a writable float32 array."""
    if a.dtype!=np.float32 or a.ndim!=3:
        return np.clip(a,0,1).astype(np.float32)
    # Row tiles on the pixel workers: a single-threaded clip of 12 MP takes ~40 ms.
    from .pixel_jobs import each
    result=a if owned and a.flags.writeable else np.empty_like(a)
    each(a.shape[0],a.shape[1],lambda first,stop:np.clip(a[first:stop],0,1,out=result[first:stop]))
    return result


def output_rgb(a,working_space='sRGB',destination='sRGB',owned=False):
    """Convert the developed working pixels directly to an output profile.

    owned=True lets the final clip reuse `a` (develop's own result); callers' arrays are never modified.
    """
    if working_space=='ProPhoto' or destination!='sRGB':
        from .colorio import convert,profile
        a=convert(a,profile('Luma Wide' if working_space=='ProPhoto' else 'sRGB'),
            destination if isinstance(destination,bytes) else profile(destination))
        owned=True
    return _clip01(a,owned)


_DITHER_TILE=None


def _dither_tile():
    """Fixed 256x256 triangular (TPDF) noise in (-1, 1) quantisation steps; exports stay reproducible."""
    global _DITHER_TILE
    if _DITHER_TILE is None:
        rng=np.random.default_rng(1905)
        tile=rng.random((256,256),dtype=np.float32)-rng.random((256,256),dtype=np.float32)
        tile.setflags(write=False);_DITHER_TILE=tile
    return _DITHER_TILE


def _quantize8_dithered(result):
    """uint8 of result*255 with the dither tile added before rounding, the same noise in every channel
    (no colour speckle; neutral stays neutral). Exact 0 and 1 stay pure black and white."""
    from .pixel_jobs import rows
    tile=_dither_tile();w=result.shape[1];columns=np.arange(w)%256
    def part(first,stop):
        values=result[first:stop]
        noise=tile[np.ix_(np.arange(first,stop)%256,columns)][...,None]
        noise=np.where((values<=0)|(values>=1),np.float32(0),noise)
        scaled=values*np.float32(255);scaled+=np.float32(.5);scaled+=noise
        return np.clip(scaled,0,255.5).astype(np.uint8)
    return rows(result.shape[0],w,part,channels=result.shape[2],dtype=np.uint8)


def _neutral_gray(result,tolerance=1e-4):
    """The green channel as an (h, w, 1) frame when every pixel is neutral within tolerance, else None.

    Monochrome renders are exactly equal; gray sources through an ICC gray profile differ by ~1e-5.
    """
    if result.ndim!=3 or result.shape[2]!=3:return None
    from .pixel_jobs import each
    spreads=[]
    def tile(first,stop):
        part=result[first:stop];green=part[...,1]
        spreads.append(float(max(np.abs(part[...,0]-green).max(initial=0),np.abs(part[...,2]-green).max(initial=0))))
    each(result.shape[0],result.shape[1],tile)
    if not spreads or max(spreads)>tolerance or not np.isfinite(max(spreads)):return None
    return np.ascontiguousarray(result[...,1:2])


def _quantize(result, scale, dtype):
    """dtype(result*scale+.5) without two full-size temporaries when `result` may be overwritten."""
    if result.dtype==np.float32 and result.flags.writeable:
        if result.ndim==3:
            # Row tiles on the pixel workers (same per-element math).
            from .pixel_jobs import rows
            def tile(first,stop):
                part=result[first:stop];np.multiply(part,scale,out=part);part+=.5
                return part.astype(dtype)
            return rows(result.shape[0],result.shape[1],tile,channels=result.shape[2],dtype=dtype)
        np.multiply(result,scale,out=result);result+=.5
        return result.astype(dtype)
    return dtype(result*scale+.5)


def thumbnail(path, size=240,with_info=False):
    a,info = load_image(path, size)
    image=Image.fromarray(np.uint8(np.clip(to_srgb(a), 0, 1)*255))
    return (image,info) if with_info else image


AUTO_SLIDER_TARGETS={'whites':(99.9,.99),'blacks':(.1,.01)}


def auto_slider_value(source, settings, key):
    """Shift+double-click on a Basic slider: that slider's automatic value, the others kept.

    whites/blacks: the value that brings the brightest (99.9th percentile of each pixel's highest
    channel) / darkest (0.1th percentile of the lowest channel) tones just inside clipping, searched
    on a small render. exposure: auto tone's exposure. None when the photo has no tonal range."""
    s=normalized(settings)
    if key=='exposure':
        adjusted,report=auto_tone_settings(source,s)
        return None if report['status']!='applied' else round(float(adjusted['exposure']),2)
    if key not in AUTO_SLIDER_TARGETS:return None
    percentile,target=AUTO_SLIDER_TARGETS[key]
    small=resize_float(source,480)
    def measure(value):
        a=develop(small,{**s,key:value,'masks':[],'grain':0.,'vignette':0.})
        channel=a.max(axis=-1) if key=='whites' else a.min(axis=-1)
        return float(np.percentile(channel,percentile))
    current=develop(small,{**s,'masks':[],'grain':0.,'vignette':0.})@np.array([.2126,.7152,.0722],np.float32)
    if float(np.percentile(current,99.9)-np.percentile(current,.1))<1e-3:return None     # no tonal range
    low,high=-200.,200.
    if key=='whites':
        # Brighter with higher values; the brightest tones below target at +200 cannot reach it.
        if measure(high)<target or measure(low)>target:return None
    elif measure(low)>target or measure(high)<target:return None
    for _ in range(14):
        middle=(low+high)/2
        if measure(middle)<target:low=middle
        else:high=middle
    return float(round((low+high)/2))


def auto_tone_settings(source, settings):
    """Map the 0.1–99.9 percentile luminance interval to 3–97% brightness.

    Apply shared RGB controls, never independent channel equalization. Geometry,
    white balance, color, and effects stay intact. Curve compensation handles
    narrow tonal ranges that cannot be stretched by the endpoint sliders alone.
    Reserve output headroom in the curve so preview and export agree, including
    when the endpoint sliders reach their limits. Analysis percentiles stay put.
    """
    s=normalized(settings)
    from .optics import lens_shading
    from .rawcolor import develop_camera
    image=geometry(lens_shading(develop_camera(resize_float(source,720),s),s),s)
    image*=white_balance(s)
    weights=np.array([.2126,.7152,.0722],np.float32)
    low_linear,high_linear=np.percentile(image@weights,[.1,99.9])
    if high_linear-low_linear<1e-6:
        return s,{'status':'flat','message':'밝기 차이가 거의 없어 자동톤을 적용하지 않았습니다.'}
    original_low,original_high=np.percentile(to_srgb(image)@weights,[.1,99.9])
    exposure=float(np.clip(np.log2(.9/max(float(high_linear),1e-8)),-5,5))
    # Auto tone always produces the current process; measure through its highlight roll-off.
    s['tone_version']=3
    exposed=image*(2**exposure)
    if not s['hdr']:exposed=highlight_rolloff(exposed,rolloff_white({**s,'exposure':exposure}))
    low,high=np.percentile(to_srgb(exposed)@weights,[.1,99.9])
    black_point=float(np.clip(low,0,.8))
    white_gain=(1-black_point)/max(float(high)-black_point,1e-6)
    whites=float(np.clip(100*np.log2(white_gain),-200,200))
    s.update(exposure=exposure,blacks=-250*black_point,whites=whites,
             contrast=0.,highlights=0.,shadows=0.,curve=deepcopy(DEFAULTS['curve']))
    mapped_low,mapped_high=np.clip((np.array([low,high])-black_point)/(1-black_point)*(2**(whites/100)),0,1)
    if mapped_high-mapped_low>1e-6 and (mapped_low>1e-6 or mapped_high<1-1e-6):
        points={0.:0.,float(mapped_low):0.,float((mapped_low+mapped_high)/2):.5,float(mapped_high):1.,1.:1.}
        s['curve']=[[x,y] for x,y in sorted(points.items())]
    margin=.03
    s['curve']=[[x,margin+(1-2*margin)*y] for x,y in s['curve']]
    return s,{'status':'applied','low_percentile':.1,'high_percentile':99.9,
              'input_black':round(float(original_low)*255,1),'input_white':round(float(original_high)*255,1),
              'output_black':margin*255,'output_white':(1-margin)*255,'margin_percent':margin*100,
              'exposure':exposure,'black_point':black_point,'white_gain':2**(whites/100)}


def jxl_distance(quality):
    """libjxl's quality-to-distance mapping (cjxl -q): 100 = 0.1, 90 = 1.0, 30 = 6.4."""
    quality = float(quality)
    return .1 + (100-quality)*.09 if quality >= 30 else 6.4 + (30-quality)**2*2.5/900


def _jxl_container(codestream, exif=None, xmp=None):
    """A JPEG XL file: the bare codestream, or the ISO BMFF container carrying Exif/XMP boxes."""
    if not exif and not xmp:return codestream
    import struct
    box = lambda kind, payload: struct.pack('>I', 8+len(payload))+kind+payload
    out = bytes.fromhex('0000000c4a584c200d0a870a')+box(b'ftyp', b'jxl \0\0\0\0jxl ')
    if exif:
        tiff = exif[6:] if exif.startswith(b'Exif\0\0') else exif
        out += box(b'Exif', b'\0\0\0\0'+tiff)                       # offset of the TIFF header: 0
    if xmp:out += box(b'xml ', xmp)
    return out+box(b'jxlc', codestream)


def export_image(source_path, output_path, settings, format='JPEG', quality=95, longest=None,
                 color_space='sRGB',keep_metadata=False,user_metadata=None,keywords='',grayscale=False,dither=False):
    """grayscale=True writes neutral (black-and-white) sRGB results as one-channel gray files with a gray
    profile of the sRGB tone curve (JPEG, PNG, 8/16-bit TIFF). dither=True adds fixed triangular noise
    before 8-bit rounding to break up banding in smooth gradients.
    AVIF: 8-bit with the colour profile and metadata. JPEG XL: 16-bit sRGB (the encoder takes no ICC
    profile), quality as libjxl's distance, Exif/XMP in the JXL container."""
    source_path, output_path = Path(source_path), Path(output_path)
    from .folder_sync import note_output
    note_output(output_path)
    if format == 'JPEG XL' and color_space != 'sRGB':
        raise ValueError('JPEG XL은 sRGB로만 저장합니다. 출력 색공간을 sRGB로 바꿔 주세요.')
    if source_path.resolve() == output_path.resolve():
        raise ValueError('원본 파일에는 덮어쓸 수 없습니다.')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    settings=normalized(settings)
    source, _ = load_image(source_path,working_space=settings['working_space'],raw_options=settings)
    hdr_export=format=='TIFF HDR 32-bit'
    if hdr_export:
        if not settings['hdr']:raise ValueError('HDR TIFF를 저장하려면 사진의 HDR 모드를 켜 주세요.')
        result=to_linear(develop(source,settings,output_space=None))
    else:result = develop(source, settings,output_space=color_space)
    if longest:
        result = resize_export(result, int(longest), hdr_export)
    from .colorio import profile,convert,export_exif,jpeg_iptc,iptc_bytes,xmp_metadata,append_tiff_exif
    icc=profile(('Linear ProPhoto' if settings['working_space']=='ProPhoto' else 'Linear sRGB') if hdr_export else color_space)
    gray=_neutral_gray(result) if grayscale and color_space=='sRGB' and format in ('JPEG','PNG','TIFF 8-bit','TIFF 16-bit') else None
    if gray is not None:icc=profile('Gray sRGB')
    frame=result if gray is None else gray
    eight=lambda a:_quantize8_dithered(a) if dither else _quantize(a,255,np.uint8)
    plane=lambda a:a[...,0] if gray is not None else a
    exif=export_exif(source_path,user_metadata,(result.shape[1],result.shape[0]),'HDR linear' if hdr_export else color_space) if keep_metadata else None
    # Exclusive create prevents accidental overwrites, including concurrent export jobs.
    with output_path.open('x+b') as handle:
        try:
            if format in ('TIFF 16-bit','TIFF 8-bit','TIFF HDR 32-bit'):
                import tifffile
                tags=[(34675,'B',len(icc),icc,False)]
                if keep_metadata:
                    packet=xmp_metadata(user_metadata or {},keywords,exif)
                    tags.append((700,'B',len(packet),packet,False))
                    iim=iptc_bytes(user_metadata or {},keywords);tags.append((33723,'B',len(iim),iim,False))
                pixels=result.astype(np.float32) if hdr_export else plane(_quantize(frame,65535,np.uint16) if format=='TIFF 16-bit' else eight(frame))
                tifffile.imwrite(handle,pixels,photometric='minisblack' if gray is not None else 'rgb',metadata=None,extratags=tags)
                if exif:append_tiff_exif(handle,exif)
            elif format=='JPEG XL':
                import imagecodecs
                packet=xmp_metadata(user_metadata or {},keywords,exif) if keep_metadata else None
                pixels=_quantize(frame,65535,np.uint16)
                codestream=imagecodecs.jpegxl_encode(plane(pixels),distance=jxl_distance(quality),effort=7)
                handle.write(_jxl_container(codestream,exif,packet))
            elif format=='PNG 16-bit':
                from .rasterio import write_png16
                packet=xmp_metadata(user_metadata or {},keywords,exif) if keep_metadata else None
                write_png16(handle,_quantize(result,65535,np.uint16),icc,exif,packet)
            else:
                im = Image.fromarray(plane(eight(frame)))
                options = {'icc_profile': icc}
                if exif:options['exif']=exif
                if keep_metadata and format=='PNG':
                    from PIL.PngImagePlugin import PngInfo
                    info=PngInfo();info.add_itxt('XML:com.adobe.xmp',xmp_metadata(user_metadata or {},keywords,exif).decode('utf-8'))
                    options['pnginfo']=info
                if format == 'JPEG':
                    options.update(quality=int(quality), subsampling=0)
                if format == 'AVIF':
                    options.update(quality=int(quality), subsampling='4:4:4', speed=6)
                    if keep_metadata:options['xmp']=xmp_metadata(user_metadata or {},keywords,exif)
                    if im.mode=='L':im=im.convert('RGB')
                im.save(handle, format=format, **options)
        except Exception:
            handle.close()
            output_path.unlink(missing_ok=True)
            raise
    if keep_metadata and format=='JPEG':
        try:jpeg_iptc(output_path,user_metadata or {},keywords)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
    return result.shape[1], result.shape[0]
