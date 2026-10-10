"""Automatic colour-cast correction written as RGB channel curves.

Shadows, midtones and highlights are examined separately, so a cast that changes with brightness
(typical of film scans) is removed, not only a uniform one. In each band the near-neutral pixels
form a cluster; its offset from grey is the cast there, and each channel curve moves that level
back to grey. Strongly coloured pixels (stage lights, a red wall) lie outside the cluster and do
not count, and a band without enough near-neutral pixels is left alone. Brightness is kept. The
result is ordinary `rgb_curves`, editable in the curve panel. It is a statistical estimate and
cannot know the scene's intent; `amount` (0-1) scales every shift."""
from copy import deepcopy
import numpy as np

BANDS = ((.02, .25), (.25, .7), (.7, .98))   # luminance ranges: shadows, midtones, highlights
RADII = (.06, .12, .2)         # cluster sizes tried in turn: the smallest with enough pixels wins
SUPPORT = .25                  # share of a band's pixels the neutral cluster must hold
MIN_BAND = .02                 # share of the picture a band must cover to be judged
WIDE_BAND = .15                # ...and to accept a wider cluster: a few coloured lights are not a cast
LIMIT = .2                     # largest correction of any channel (display-encoded 0-1)
GAP = .04                      # anchors closer than this are merged into the earlier one
WEIGHTS = np.array([.2126, .7152, .0722], np.float32)


def neutral_offset(chroma, radii=RADII):
    """Offset of the near-neutral cluster from grey (channel minus luminance), or None."""
    for radius in radii:
        centre = np.zeros(3, np.float32); near = None
        for _ in range(5):
            near = np.abs(chroma-centre).max(axis=1) < radius
            if near.mean() < SUPPORT:near = None; break
            centre = chroma[near].mean(axis=0)
        if near is not None:return centre, near
    return None, None


def channel_points(image, amount=1.):
    """(three point lists, report) for display-encoded RGB (H x W x 3, 0-1) as it enters the channel
    curves, or (None, report) when no band has a measurable cast."""
    a = np.clip(np.asarray(image, np.float32).reshape(-1, 3), 0, 1)
    a = a[np.isfinite(a).all(axis=1) & (a.max(axis=1) < .995)]         # clipped pixels have no colour left
    if len(a) < 256:return None, {'status': 'flat'}
    luminance = a@WEIGHTS; chroma = a-luminance[:, None]
    amount = float(np.clip(amount, 0, 1)); anchors = []; casts = []
    for low, high in BANDS:
        inside = (luminance >= low) & (luminance < high)
        if inside.mean() < MIN_BAND:casts.append(None); continue
        offset, near = neutral_offset(chroma[inside], RADII if inside.mean() >= WIDE_BAND else RADII[:1])
        if offset is None:casts.append(None); continue
        level = float(luminance[inside][near].mean())
        offset = np.clip(offset, -LIMIT, LIMIT)
        casts.append([round(float(v), 4) for v in offset])
        if not anchors or level-anchors[-1][0] >= GAP:anchors.append((level, offset))
    if not anchors or max(float(np.abs(o).max()) for _, o in anchors) < .004:
        return None, {'status': 'neutral' if anchors else 'flat', 'casts': casts}
    curves = []
    for c in range(3):
        points = [[0., 0.]]
        for level, offset in anchors:
            x = float(np.clip(level+offset[c], GAP/2, 1-GAP/2)); y = x+(level-x)*amount
            if x-points[-1][0] >= GAP/2:points.append([round(x, 4), round(float(np.clip(max(y, points[-1][1]), 0, 1)), 4)])
        points.append([1., 1.])
        curves.append(points)
    return curves, {'status': 'applied', 'amount': amount, 'casts': casts}


def auto_color_settings(source, settings, amount=1.):
    """(settings with automatic rgb_curves, report). Existing channel curves are replaced; everything
    else, including the crop used for the analysis, stays as it is."""
    from .engine import normalized, geometry, resize_float, _develop_tone_pixels, _develop_color_mix
    from .optics import lens_shading
    from .rawcolor import develop_camera
    s = normalized(settings)
    if s['monochrome']:return s, {'status': 'monochrome'}
    image = geometry(lens_shading(develop_camera(resize_float(source, 720), s), s), s)
    image = _develop_tone_pixels(np.array(image, np.float32, copy=True), s)
    curves, report = channel_points(_develop_color_mix(np.clip(image, 0, 1), s), amount)
    if curves is None:return s, report
    s = deepcopy(s); s['rgb_curves'] = curves
    return s, report
