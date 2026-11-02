"""Smooth highlights (DRT): a display rendering for scene-linear pixels, chosen instead of the classic roll-off.

The classic rendering keeps every value up to half of white and rolls off the rest, so bright skies and lights
reach white within two stops and colours there clip channel by channel. This one, built on the principles that
modern display transforms share (and written here, not taken from any of them):
1. One tone scale, m * (x / (x + s)) ** p, applied to the largest channel and the other two scaled by the same
   factor, so a colour keeps its hue and saturation while it darkens or brightens and no channel passes white.
   With m = 1 it meets white only at infinity; a mid grey (0.18) stays 0.18, so exposures match the classic
   rendering.
2. A toe after it: positive values deepen the shadows (y*y / (y + t), scaled so white stays white), negative ones
   lift the blacks (a flare). s is solved so that mid grey still lands on 0.18 after the toe.
3. Path to white: near white, colours move towards neutral, as bright lights do on film and to the eye, instead
   of staying saturated up to a hard edge.
Out-of-gamut (negative) values are clamped first. Settings after it (Highlights, Shadows, Whites, Blacks,
Contrast, the curve) work on its result as on the classic one.

The look is set with four settings from -100 to 100 (0 everywhere is the original rendering): drt_contrast (p),
drt_toe, drt_shoulder (m above 1: the brightest highlights reach white at a finite exposure; below 0 the shoulder
is longer than the original, by compressing the highlights above mid grey further) and drt_white (where colours
start moving towards white). LOOKS are named sets of them.
"""
from collections import namedtuple

import numpy as np

MID = .18
CONTRAST = 1.35          # p: the curve's slope in deep shadows; mid-tone contrast is close to the classic rendering's
S0 = MID/MID**(1/CONTRAST) - MID
WHITE_START = .8         # display level where colours start moving towards white (earlier washes bright oranges to salmon)
KEYS = ('drt_contrast', 'drt_toe', 'drt_shoulder', 'drt_white')
DEFAULTS = dict.fromkeys(KEYS, 0.)
# Named looks for the Basic panel: values of KEYS. '표준' is the original rendering.
LOOKS = {'표준': (0., 0., 0., 0.), '부드럽게': (-35., -25., -40., 0.), '대비 강하게': (40., 30., 30., 0.),
         '필름처럼': (15., 35., -20., -45.)}

Params = namedtuple('Params', 'p s m toe white shoulder')


def _value(settings, key):
    try:
        return float(np.clip(float((settings or {}).get(key, 0.) or 0.), -100, 100))/100
    except (TypeError, ValueError):
        return 0.


def _untoe(y, toe):
    """The tone-scale level that the toe turns into display level y."""
    if toe > 0:
        return (y + np.sqrt(y*y + 4*(1 + toe)*y*toe))/(2*(1 + toe))
    if toe < 0:
        return (y + toe)/(1 + toe)          # toe = -flare
    return y


def params(settings=None):
    contrast, toe, shoulder, white = (_value(settings, key) for key in KEYS)
    p = CONTRAST*2**(.5*contrast)                              # 0.95 .. 1.91
    toe = .08*toe if toe > 0 else .03*toe                      # deepen up to t = 0.08, or lift the blacks by up to 0.03
    m = 1 + .25*shoulder if shoulder > 0 else 1.
    grey = _untoe(MID, toe)
    s = MID/(grey/m)**(1/p) - MID
    return Params(p, s, m, toe, float(np.clip(WHITE_START + .15*white, .6, .95)), min(shoulder, 0.))


def _tone(x, prm):
    """The tone scale and a deepening toe, without the flare of a lifted black."""
    x = np.maximum(x, 0)
    if prm.shoulder < 0:
        # A longer shoulder: above mid grey the scene level is compressed in log2 by up to 30 %, so the highlights
        # take more stops to approach white; at and below grey nothing changes.
        stops = np.log2(np.maximum(x, np.float32(1e-12))/np.float32(MID))
        x = np.where(stops > 0, np.float32(MID)*np.exp2(stops*np.float32(1 + .3*prm.shoulder)), x)
    y = np.float32(prm.m)*(x/(x + np.float32(prm.s)))**np.float32(prm.p)
    if prm.m > 1:
        y = np.minimum(y, 1)
    if prm.toe > 0:
        t = np.float32(prm.toe)
        y = (1 + t)*y*y/(y + t)
    return y.astype(np.float32)


def _flare(y, prm):
    f = np.float32(max(-prm.toe, 0.))
    return (f + (1 - f)*y).astype(np.float32) if f else y


def curve(x, prm=None):
    """Display level of scene level x (the largest channel)."""
    prm = prm or params()
    return _flare(_tone(x, prm), prm)


def _pixels(tile, prm):
    rgb = np.maximum(tile[..., :3], 0)
    peak = rgb.max(axis=-1)
    level = _tone(peak, prm)
    out = rgb*(level/np.maximum(peak, np.float32(1e-9)))[..., None]
    out, level = _flare(out, prm), _flare(level, prm)          # a lifted black lifts every channel, as flare does
    start = np.float32(prm.white)
    toward = np.clip((level - start)/np.float32(1 - start), 0, 1)**2
    out += (level[..., None] - out)*toward[..., None]
    return out.astype(np.float32)


def render(a, settings=None):
    """Display-linear pixels (0..1) of scene-linear working-space pixels `a`."""
    from .pixel_jobs import transform
    prm = params(settings)
    return transform(np.asarray(a, np.float32), lambda tile: _pixels(tile, prm))


def look_of(settings):
    """The name of the look the settings match, or '' for a custom one."""
    values = tuple(round(float((settings or {}).get(key, 0.) or 0.), 3) for key in KEYS)
    return next((name for name, look in LOOKS.items() if values == tuple(float(v) for v in look)), '')


def active(settings):
    return settings.get('tone_mapping') == 'drt' and not settings.get('hdr')
