"""Smooth highlights (DRT): a display rendering for scene-linear pixels, chosen instead of the classic roll-off.

The classic rendering keeps every value up to half of white and rolls off the rest, so bright skies and lights
reach white within two stops and colours there clip channel by channel. This one, built on the principles that
modern display transforms share (and written here, not taken from any of them):
1. One tone curve, (x / (x + s0)) ** p, applied to the largest channel and the other two scaled by the same
   factor, so a colour keeps its hue and saturation while it darkens or brightens and no channel passes white.
   It meets white only at infinity; a mid grey (0.18) stays 0.18, so exposures match the classic rendering.
2. Path to white: near white, colours move towards neutral, as bright lights do on film and to the eye, instead
   of staying saturated up to a hard edge.
Out-of-gamut (negative) values are clamped first. Settings after it (Highlights, Shadows, Whites, Blacks,
Contrast, the curve) work on its result as on the classic one.
"""
import numpy as np

MID = .18
CONTRAST = 1.35          # p: the curve's slope in deep shadows; mid-tone contrast is close to the classic rendering's
S0 = MID/MID**(1/CONTRAST) - MID
WHITE_START = .8         # display level where colours start moving towards white (earlier washes bright oranges to salmon)


def curve(x):
    x = np.maximum(x, 0)
    return (x/(x + np.float32(S0)))**np.float32(CONTRAST)


def _pixels(tile):
    rgb = np.maximum(tile[..., :3], 0)
    peak = rgb.max(axis=-1)
    level = curve(peak)
    out = rgb*(level/np.maximum(peak, np.float32(1e-9)))[..., None]
    toward = np.clip((level - np.float32(WHITE_START))/np.float32(1 - WHITE_START), 0, 1)**2
    out += (level[..., None] - out)*toward[..., None]
    return out.astype(np.float32)


def render(a):
    """Display-linear pixels (0..1) of scene-linear working-space pixels `a`."""
    from .pixel_jobs import transform
    return transform(np.asarray(a, np.float32), _pixels)


def active(settings):
    return settings.get('tone_mapping') == 'drt' and not settings.get('hdr')
