"""Film negative to positive, as the first step of development.

A scanned negative is turned into scene light before anything else, so every other tool (exposure,
curves, Auto Colour, masks, dust removal) then works on a normal positive.

Each channel is read as density above the clear film: d = log(base/value). The clear film (`base`)
takes the orange mask of colour negative film out, per channel. The densest part of the picture
(`peak`) is the brightest thing in the scene. Density between the two is laid out over `range` stops
of light, the way a negative records exposure: light = 2^(range*(d/d_peak - 1) + density). Dividing by
the peak density makes the result the same whether the scan stored linear or gamma-encoded values.

`base` and `peak` are measured once (analyse) and stored with the edit, so previews, crops and exports
of one photo all use the same conversion.
"""
import numpy as np

KEYS = ('negative', 'negative_base', 'negative_peak', 'negative_range', 'negative_density')
DEFAULTS = dict(negative=False, negative_base=None, negative_peak=None, negative_range=6.5, negative_density=0.)
FLOOR = 1e-5          # scan values below this are treated as this (no light through the film)
BORDER = .06          # share of each side left out of the analysis: film rebate, holder, scanner light
BASE_PERCENT = 99.7   # clear film: the brightest part of the frame
PEAK_PERCENT = .3     # densest part of the frame


def analyse(source):
    """(base, peak): scan values of the clear film and of the densest part, per channel, as lists.
    `source` is the linear frame as loaded (any size; a reduced copy is enough)."""
    a = np.asarray(source, np.float32)[..., :3]
    height, width = a.shape[:2]
    top, left = int(height*BORDER), int(width*BORDER)
    inner = a[top:height-top or None, left:width-left or None].reshape(-1, 3)
    base = np.percentile(inner, BASE_PERCENT, axis=0); peak = np.percentile(inner, PEAK_PERCENT, axis=0)
    base = np.maximum(base, FLOOR*10); peak = np.clip(peak, FLOOR, base*.9)      # always some density range
    return [float(v) for v in base], [float(v) for v in peak]


def converted(source, settings):
    """Settings with the conversion switched on, measured from `source` (the frame as loaded, any size)."""
    from .rawcolor import develop_camera
    base, peak = analyse(develop_camera(source, settings))
    return {**settings, 'negative': True, 'negative_base': base, 'negative_peak': peak}


def base_from(sample):
    """Clear-film value from pixels the user pointed at (the unexposed edge of the film)."""
    values = np.asarray(sample, np.float32)[..., :3].reshape(-1, 3)
    return [float(v) for v in np.maximum(np.median(values, axis=0), FLOOR*10)]


def apply(a, settings):
    """The positive (scene light, linear) of linear frame `a`; `a` itself when the conversion is off."""
    if not settings.get('negative') or settings.get('negative_base') is None or settings.get('negative_peak') is None:
        return a
    a = np.asarray(a, np.float32)
    base = np.asarray(settings['negative_base'], np.float32); peak = np.asarray(settings['negative_peak'], np.float32)
    span = np.log(base/np.minimum(np.maximum(peak, FLOOR), base*np.float32(.9)))
    scale = np.float32(settings.get('negative_range', DEFAULTS['negative_range'])*np.log(2))/span
    offset = np.float32((settings.get('negative_density', 0.)-settings.get('negative_range', DEFAULTS['negative_range']))*np.log(2))
    out = np.empty_like(a)
    # light = exp(range*ln2*(d/span - 1) + density*ln2), d = log(base/value)
    np.maximum(a[..., :3], np.float32(FLOOR), out=out[..., :3])
    np.log(out[..., :3], out=out[..., :3])
    out[..., :3] = (np.log(base)-out[..., :3])*scale+offset
    np.exp(out[..., :3], out=out[..., :3])
    if a.shape[-1] > 3:out[..., 3:] = a[..., 3:]
    return out
