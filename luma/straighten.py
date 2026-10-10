"""Crop limits for a straightened photo: the crop always stays inside the turned photo.

The photo turns about the centre of a canvas of its own size (engine.geometry), so its corners leave
the canvas and empty corners appear. Crops are [x0, y0, x1, y1] in fractions of that canvas; width and
height are the canvas size in pixels. MARGIN keeps the crop off the photo's resampled edge.
"""
import math

MARGIN = 2.         # pixels of the photo's edge left out when it is turned
FULL = [0., 0., 1., 1.]


def _extent(angle, width, height):
    """Half-size of the turned photo that a crop may use, and the cosine and sine of the angle."""
    a = math.radians(float(angle))
    margin = MARGIN if abs(float(angle)) > 1e-9 else 0.
    return max(1e-6, width/2-margin), max(1e-6, height/2-margin), math.cos(a), math.sin(a)


def padding(angle, width, height):
    """Pixels to add (left and right, top and bottom) so that the whole turned photo stays visible."""
    a = math.radians(float(angle)); cos, sin = abs(math.cos(a)), abs(math.sin(a))
    return max(0, math.ceil((width*cos+height*sin-width)/2)), max(0, math.ceil((width*sin+height*cos-height)/2))


def valid(crop, angle, width, height, tolerance=1e-6):
    """True when every corner of the crop lies on the turned photo and on the canvas."""
    x0, y0, x1, y1 = crop
    if x0 < -tolerance or y0 < -tolerance or x1 > 1+tolerance or y1 > 1+tolerance or x1 <= x0 or y1 <= y0:
        return False
    half_width, half_height, cos, sin = _extent(angle, width, height)
    slack = tolerance*max(width, height)
    for u in (x0, x1):
        for v in (y0, y1):
            x, y = (u-.5)*width, (v-.5)*height
            # The canvas point turned back into the photo's own frame (positive angles turn the photo clockwise).
            if abs(x*cos+y*sin) > half_width+slack or abs(-x*sin+y*cos) > half_height+slack:
                return False
    return True


def ratio(crop, width, height):
    """Width over height of a crop in pixels."""
    return (crop[2]-crop[0])*width/max(1e-9, (crop[3]-crop[1])*height)


def largest(angle, width, height, aspect=None):
    """The largest centred crop of the given aspect (width over height in pixels; the canvas's by default)."""
    aspect = float(aspect) if aspect else width/height
    half_width, half_height, cos, sin = _extent(angle, width, height)
    cos, sin = abs(cos), abs(sin)
    y = min(half_width/(aspect*cos+sin), half_height/(aspect*sin+cos), height/2, width/2/aspect)
    x = aspect*y
    return [.5-x/width, .5-y/height, .5+x/width, .5+y/height]


def _scaled(crop, factor):
    cx, cy = (crop[0]+crop[2])/2, (crop[1]+crop[3])/2
    w, h = (crop[2]-crop[0])*factor/2, (crop[3]-crop[1])*factor/2
    return [cx-w, cy-h, cx+w, cy+h]


def shrink(crop, angle, width, height):
    """The crop made smaller about its own centre until it fits; a centre off the photo gives the largest crop."""
    crop = [float(v) for v in crop]
    if valid(crop, angle, width, height):
        return crop
    if not valid(_scaled(crop, 1e-3), angle, width, height):
        return largest(angle, width, height, ratio(crop, width, height))
    low, high = 1e-3, 1.
    for _ in range(40):
        middle = (low+high)/2
        if valid(_scaled(crop, middle), angle, width, height):low = middle
        else:high = middle
    return _scaled(crop, low)


def limit(candidate, previous, angle, width, height):
    """As far from the previous crop towards the candidate as stays on the photo (for dragging)."""
    candidate = [float(v) for v in candidate]
    if valid(candidate, angle, width, height) or not valid(previous, angle, width, height):
        return candidate
    low, high = 0., 1.
    for _ in range(30):
        middle = (low+high)/2
        if valid([p+(c-p)*middle for p, c in zip(previous, candidate)], angle, width, height):low = middle
        else:high = middle
    return [p+(c-p)*low for p, c in zip(previous, candidate)]


def close(a, b, tolerance=2e-3):
    return all(abs(x-y) <= tolerance for x, y in zip(a, b))


def refit(crop, previous_angle, angle, width, height, aspect=None):
    """The crop after the angle changed from previous_angle; None means the whole canvas.

    No crop, or the automatic crop of the previous angle, follows the new angle at full size (in the
    chosen aspect, if any). A crop the user placed keeps its centre and shape and only shrinks as needed.
    """
    current = [float(v) for v in crop] if crop else list(FULL)
    automatic = close(current, FULL) or close(current, largest(previous_angle, width, height, ratio(current, width, height)))
    if automatic:
        result = largest(angle, width, height, aspect or ratio(current, width, height))
    else:
        result = shrink(current, angle, width, height)
    return None if close(result, FULL, 1e-9) else result
