"""Highlights and Shadows of tone process 3.

The sliders work on a smooth "base" of the photo's brightness and leave the difference between each pixel
and that base (local contrast, texture) as it is; pixels are scaled rather than offset, so colours keep
their saturation. The earlier process added one offset per pixel, which flattened and greyed the photo.

The base comes from a guided filter (He, Sun and Tang, 2010) on a reduced copy (at most MAP_SIZE on the
long edge), stored as two coefficient maps: base = a*luma + b at any resolution, so previews, exports
and tiles agree.

The response (0.5.85) is our own construction. It follows from the conditions below, and its constants
were chosen on our own test scenes and photos by the measures named with each constant; it is not fitted
to the output of any other program.
- Middle grey (PIVOT: 18% reflectance, sRGB-encoded) separates the two sliders: Shadows moves only the
  base below it and Highlights only the base above it.
- Black, white and the pivot stay where they are and the curve passes the pivot with slope 1, so the
  sliders do not work against each other and the base is never pushed out of range.
- Shadows +100 is t + LIFT*t*(1-t)^2 on t = base/pivot, the lowest-order polynomial with those ends.
- A negative amount is the exact inverse of the positive one: +n and then -n gives back the base.
- Highlights is the same curve mirrored about the pivot (t measured from white), so -100 separates the
  brightest tones as much as Shadows +100 separates the darkest.
- No pixel moves more than REACH of its distance to black or white, so detail that is darker or
  brighter than its surroundings is not pushed into clipping.
"""
import cv2
import numpy as np

LIFT = 2.           # the largest lift that keeps a third of the base's contrast everywhere (slope >= 1-LIFT/3)
REACH = .5          # also a constant in the tone shaders (native/gpu_compute.cpp, gpu_compute_metal.mm)
PIVOT = .4614       # 18% grey, sRGB-encoded
MAP_SIZE = 512      # long edge of the coefficient maps
# RADIUS and EPS: with both sliders at their ends, a hard edge between a near-black and a bright field
# shows a halo of about 0.02 (encoded); the settings tried with a smaller halo kept less local contrast
# on 18 photos (here 0.87 of the fine detail with Shadows +100, 0.82 with Highlights -100).
RADIUS = .02        # guided-filter radius, as a fraction of the map's long edge
EPS = .02           # edges with less brightness variance than this are smoothed into the base
# Below about FLOOR the change turns from scaling into an offset, so shadow noise is not multiplied.
# Of 0.05, 0.15 and 0.3 this kept colours closest to their saturation on the same photos (chroma to
# lightness 0.89 with Shadows +100, 1.02 with Highlights -100, 1.22 with Shadows -100).
FLOOR = .05
WEIGHTS = np.array([.2126, .7152, .0722], np.float32)
X = np.linspace(0, 1, 257)
_T = np.linspace(0, 1, 2049)


def _side(t, amount):
    """One side of the curve on t = 0 (black or white) .. 1 (pivot); amount -1..1, positive moves away from 0."""
    lift = lambda t, amount: t+amount*LIFT*t*(1-t)**2
    return lift(t, amount) if amount >= 0 else np.interp(t, lift(_T, -amount), _T)


def curve(highlights, shadows, pivot=PIVOT):
    """Brightness change as a function of the base brightness (X), float32."""
    shadows, highlights = (float(np.clip(value, -100, 100))/100 for value in (shadows, highlights))
    below = pivot*_side(np.minimum(X/pivot, 1), shadows)
    above = 1-(1-pivot)*_side(np.minimum((1-X)/(1-pivot), 1), -highlights)
    return (np.where(X < pivot, below, above)-X).astype(np.float32)


def _box(a, r):
    return cv2.boxFilter(a, -1, (2*r+1, 2*r+1), borderType=cv2.BORDER_REFLECT_101)


def analyse(encoded, shape):
    """Coefficient maps from a reduced sRGB-encoded copy of the frame; shape is the frame's
    (height, width). They do not depend on the slider values."""
    luma = np.clip(encoded[..., :3] @ WEIGHTS, 0, 1)
    r = max(1, round(RADIUS*max(luma.shape)))
    mean = _box(luma, r); variance = _box(luma*luma, r)-mean*mean
    a = variance/(variance+np.float32(EPS)); b = mean-a*mean
    return dict(a=_box(a, r), b=_box(b, r), shape=tuple(shape))


def with_curve(base, highlights, shadows):
    """analyse()'s result plus the change curve for the slider values: what apply() takes."""
    return {**base, 'curve': curve(highlights, shadows)}


def _band(coefficients, first, count, shape):
    """Rows first..first+count of a map enlarged bilinearly to shape; the same values for any tiling."""
    height, width = shape; rows, columns = coefficients.shape
    if (rows, columns) == (height, width):
        return coefficients[first:first+count]
    y = np.clip((np.arange(first, first+count, dtype=np.float32)+.5)*(rows/height)-.5, 0, rows-1)
    upper = np.floor(y).astype(np.intp); lower = np.minimum(upper+1, rows-1); t = (y-upper)[:, None]
    band = coefficients[upper]*(1-t)+coefficients[lower]*t
    return band if columns == width else cv2.resize(band, (width, count), interpolation=cv2.INTER_LINEAR)


def apply(a, local, first=0):
    """Highlights/Shadows on sRGB-encoded rows first..first+len(a) of the frame that `local` describes."""
    luma = a @ WEIGHTS
    base = _band(local['a'], first, len(a), local['shape'])*luma+_band(local['b'], first, len(a), local['shape'])
    position = np.clip(base, 0, 1)*np.float32(len(X)-1)
    index = np.minimum(position.astype(np.intp), len(X)-2); t = position-index
    change = local['curve'][index]*(1-t)+local['curve'][index+1]*t
    change = np.clip(change, -np.maximum(luma, 0)*np.float32(REACH), np.maximum(1-luma, 0)*np.float32(REACH))
    target = np.maximum(luma+change, 0)[..., None]; luma = luma[..., None]
    # Luma becomes exactly `target`: bright pixels are scaled (colour ratios kept); towards black the
    # change turns into the same offset on every channel, so shadow noise is not multiplied.
    floor = np.float32(FLOOR)
    return (a*target+floor*(a+target-luma))/(luma+floor)
