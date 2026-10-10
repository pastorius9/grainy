"""Find the unexposed film edge or scanner border around a scanned photo.

A border side is a thin run of straight lines from the image edge inward that are almost
entirely very dark (unexposed film edge, a black film holder) and clearly brighter just inside.
Dark photographs, dark areas wider than MAX_SIDE (a window frame, a night scene) and bright
areas (skies, light leaks) keep their edges; checked by eye on scans of the user's library.
The result is a crop in the same normalised (x0, y0, x1, y1) form as settings['crop'].
"""
import numpy as np

DARK = .10                       # sRGB luma threshold for border pixels
COVERAGE = .9                    # share of a line that must be border-coloured
MAX_SIDE = .10                   # film edges are thin; a wider dark run is picture content
CONTRAST = .08                   # the picture inside must differ from the border by this much
EDGE_SPAN = .02                  # soft transition (film edge blur) removed after the border
SAFETY = .002                    # extra inset past the transition


def _luma(image, longest=1000):
    step = max(1, int(np.ceil(max(image.shape[:2]) / longest)))
    small = np.asarray(image[::step, ::step], np.float32)
    if small.ndim == 3:
        small = small[..., :3] @ np.array([.2126, .7152, .0722], np.float32)
    return small


def _side(lines):
    """Border depth in lines for one side; lines[i] is the i-th line from the edge inward."""
    limit = int(len(lines) * MAX_SIDE)
    dark = lambda v: v < DARK
    depth = 0
    while depth <= limit and dark(lines[depth]).mean() >= COVERAGE:
        depth += 1
    if depth < 2 or depth > limit:
        return 0, None
    # The picture just inside must be clearly brighter, or this is a dark photograph.
    band = lines[depth:depth + max(3, len(lines) // 30)]
    if not len(band) or float(band.mean()) - float(lines[:depth].mean()) < CONTRAST:
        return 0, None
    # Skip the blurred film edge: lines still partly border-coloured.
    end = min(len(lines), depth + max(1, int(len(lines) * EDGE_SPAN)))
    while depth < end and dark(lines[depth]).mean() > .02:
        depth += 1
    return depth + int(np.ceil(len(lines) * SAFETY)), 'dark'


def detect(image):
    """(crop, kinds) with crop normalised to the given frame, or (None, {}) when no border is found."""
    luma = _luma(image)
    h, w = luma.shape
    if h < 20 or w < 20:
        return None, {}
    sides = {'left': _side(luma.T), 'right': _side(luma.T[::-1]), 'top': _side(luma), 'bottom': _side(luma[::-1])}
    if not any(depth for depth, _ in sides.values()):
        return None, {}
    x0, x1 = sides['left'][0] / w, 1 - sides['right'][0] / w
    y0, y1 = sides['top'][0] / h, 1 - sides['bottom'][0] / h
    if x1 - x0 < .5 or y1 - y0 < .5:
        return None, {}
    return (round(x0, 5), round(y0, 5), round(x1, 5), round(y1, 5)), {k: v[1] for k, v in sides.items() if v[0]}


def crop_for(source, settings, longest=1200):
    """detect() on the photo's uncropped geometry frame at preview size; the crop for settings['crop'] or None."""
    from .engine import geometry, resize_float, to_srgb
    from .optics import lens_shading
    from .rawcolor import develop_camera
    frame = geometry(lens_shading(develop_camera(resize_float(source, longest), settings), settings), settings, False)
    return detect(to_srgb(frame))[0]
