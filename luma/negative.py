"""Film negative to positive, as the first step of development.

A scanned negative is turned into scene light before anything else, so every other tool (exposure,
curves, Auto Colour, masks, dust removal) then works on a normal positive.

Each channel is read as density above the clear film: d = log(base/value). The clear film (`base`)
takes the orange mask of colour negative film out, per channel. The densest part of the picture
(`peak`) is the brightest thing in the scene. Density between the two is laid out over `range` stops
of light, the way a negative records exposure: light = 2^(range*(d/d_peak - 1) + density). Dividing by
the peak density makes the result the same whether the scan stored linear or gamma-encoded values.

A photographed negative (a camera RAW file) is read in the camera's own colours while the conversion is
on: rawcolor.enabled() makes every loader decode it that way and develop_camera leaves it alone.

`base` and `peak` are measured once (analyse) and stored with the edit, so previews, crops and exports
of one photo all use the same conversion. They are measured inside the photo's crop: when the scan shows
more than the film (a light table, the holder), crop to the frame first and measure again.

Photos of one roll can be measured together (converted_together): one `base` and one `peak` for all of
them, read from all their pixels at once, so every frame gets the same colours and a frame whose own
brightest or darkest part is coloured no longer bends its conversion. Brightness stays per photo. This
needs scans made with the same settings: with a different exposure per frame the film base differs too.
"""
import numpy as np

KEYS = ('negative', 'negative_base', 'negative_peak', 'negative_range', 'negative_density')
DEFAULTS = dict(negative=False, negative_base=None, negative_peak=None, negative_range=6.5, negative_density=0.)
FLOOR = 1e-5          # scan values below this are treated as this (no light through the film)
BORDER = .06          # share of each side left out of the analysis: film rebate, holder, scanner light
BASE_PERCENT = 99.7   # clear film: the brightest part of the frame
PEAK_PERCENT = .3     # densest part of the frame
MIDDLE = .18          # the measured density setting puts the picture's median brightness here (middle grey)
DENSITY_RANGE = (-1., 2.5)
SAMPLE = 40000        # pixels kept of each photo when several are measured together


def inside(frame):
    """The frame without its border (three channels)."""
    a = np.asarray(frame, np.float32)[..., :3]
    height, width = a.shape[:2]
    top, left = int(height*BORDER), int(width*BORDER)
    return a[top:height-top or None, left:width-left or None]


def analyse(source):
    """(base, peak): scan values of the clear film and of the densest part, per channel, as lists.
    `source` is the linear frame as loaded (any size; a reduced copy is enough)."""
    return ends(inside(source).reshape(-1, 3))


def ends(inner):
    """analyse for rows of pixels that are already inside the picture."""
    # Both ends are read from the same pixels in every channel: the clearest and the densest parts by
    # overall density. Channels are not ranked one by one, and pixels where a channel has no reading
    # (clipped to black, as the blue of a camera-scanned colour negative can be) are left out.
    usable = (inner > FLOOR*20).all(axis=1)
    if usable.sum() < 100:usable = np.ones(len(inner), bool)
    logs = np.log(np.maximum(inner[usable], FLOOR)); overall = logs.mean(axis=1)
    clear, dense = np.percentile(overall, [BASE_PERCENT, PEAK_PERCENT])
    base = np.exp(np.median(logs[overall >= clear], axis=0)); peak = np.exp(np.median(logs[overall <= dense], axis=0))
    base = np.maximum(base, FLOOR*10); peak = np.clip(peak, FLOOR, base*.9)      # always some density range
    return [float(v) for v in base], [float(v) for v in peak]


def measured(source, settings):
    """The part of `source` (the frame as loaded, any size) a measurement is made from: inside the crop of
    `settings`, without the border."""
    from .engine import geometry, normalized
    from .optics import lens_shading
    from .rawcolor import develop_camera
    plain = {**normalized(settings), 'negative': True}        # a camera RAW source stays in the camera's colours
    return inside(geometry(lens_shading(develop_camera(source, plain), plain), plain))


def sample(source, settings):
    """measured() as at most SAMPLE rows of pixels spread evenly over the picture: what one photo adds to
    a measurement shared by several."""
    part = measured(source, settings); step = max(1, int(np.ceil(np.sqrt(part.shape[0]*part.shape[1]/SAMPLE))))
    return np.ascontiguousarray(part[::step, ::step]).reshape(-1, 3)


def converted(source, settings):
    """Settings with the conversion switched on, measured from `source` (the frame as loaded, any size)
    inside the crop of `settings`."""
    pixels = measured(source, settings).reshape(-1, 3)
    return placed(settings, pixels, *ends(pixels))


def converted_together(samples, settings):
    """converted for the photos of one roll: `samples` (sample() of each photo) are measured as one, so all
    photos share the clear film and the densest part; brightness is still placed photo by photo."""
    base, peak = ends(np.concatenate(samples))
    return [placed(one, pixels, base, peak) for pixels, one in zip(samples, settings)]


def placed(settings, pixels, base, peak):
    """`settings` converted with `base` and `peak`, with a first brightness from the photo's own `pixels`."""
    result = {**settings, 'negative': True, 'negative_base': list(base), 'negative_peak': list(peak), 'negative_density': 0.}
    # A first brightness: the median of the picture at middle grey. The densest part alone (often a small
    # highlight) would leave most pictures dark.
    light = apply(pixels, result)[..., :3] @ np.float32([.2126, .7152, .0722])
    result['negative_density'] = round(float(np.clip(np.log2(MIDDLE/max(float(np.median(light)), 1e-4)), *DENSITY_RANGE))*20)/20
    # The import defaults of a camera RAW file brighten a dark RAW; the positive is already placed.
    from .raw_defaults import BUILTIN_RAW, BUILTIN_LINEAR_RAW
    if any(all(settings.get(key) == value for key, value in builtin.items() if key in ('exposure', 'blacks')) for builtin in (BUILTIN_RAW, BUILTIN_LINEAR_RAW)):
        result['exposure'] = 0.; result['blacks'] = 0.
    return result


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
    # Spots denser than the densest part of the picture (dust, the edge of the holder) are not scene light:
    # they stop at that part's brightness, so a few specks do not become the picture's white point.
    np.minimum(out[..., :3], np.float32(settings.get('negative_density', 0.)*np.log(2)), out=out[..., :3])
    np.exp(out[..., :3], out=out[..., :3])
    if a.shape[-1] > 3:out[..., 3:] = a[..., 3:]
    return out
