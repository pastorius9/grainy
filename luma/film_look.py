"""Film looks: named film stocks simulated from the curves their makers publish.

Each stock is a JSON file in assets/film/stocks, papers and print films in assets/film/papers, made by
tools/film/build.py from the datasheet curves read by tools/film/digitize.py: each emulsion layer's spectral
sensitivity, its characteristic curve (density against log exposure) and, where the simulation needs them, the
dyes' spectral densities. Nothing is fitted to another program's rendering.

On scene-linear pixels (after exposure and white balance), a look works as the film would:
1. Exposure. A pixel's reflectance is a smooth spectrum, linear in its linear sRGB value (the basis in
   colorimetry.json); under the light the film is balanced for, each layer's exposure is that spectrum weighted
   by the layer's sensitivity. That is a 3x3 matrix of the pixel; a mid grey (0.18) exposes every layer at the
   stock's reference exposure (log_h_ref), so a grey stays grey whatever the light.
2. Each layer's characteristic curve gives its density.
3. A colour negative is printed: each paper layer is exposed through the negative's density of the matching
   dye (its printing density; the negative's orange mask exists to make this nearly independent per layer),
   with printer lights that print a mid grey neutral at a print mid grey, and the paper's curves give the
   print's densities. Slides are viewed as they are. A black-and-white negative is printed on a
   black-and-white paper.
4. The dyes' spectral densities give the print's (or slide's) spectrum under the viewing light, then its colour,
   adapted so that the paper (or clear film) is white.
Steps 1-3 are per layer (one-dimensional tables over log exposure); step 4 is a three-dimensional table over the
three densities, interpolated in -log10(XYZ), where it is smooth. Tables are built the first time a stock is
used (a few milliseconds) and kept.
"""
import json
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

FOLDER = Path(__file__).resolve().parents[1]/'assets'/'film'
GREY = .18                       # scene reflectance exposed at the stock's reference exposure
PRINT_GREY = .18                 # the print's luminance of that grey, relative to the paper's white
STATUS_A = (610., 546., 436.)    # peak wavelengths of Status A red, green, blue densitometry (ISO 5-3)
SPAN = (-6., 4.)                 # log10 exposure of the per-layer tables, around the reference exposure
SAMPLES = 2048
NODES = 33                       # per axis of the density table
TINY = 1e-7
LN10 = float(np.log(10))
LIGHTS = {'daylight': 5503., 'tungsten': 3200.}
SRGB_XYZ = np.array([[.4124564, .3575761, .1804375], [.2126729, .7151522, .0721750], [.0193339, .1191920, .9503041]])
XYZ_SRGB = np.linalg.inv(SRGB_XYZ)
BRADFORD = np.array([[.8951, .2664, -.1614], [-.7502, 1.7135, .0367], [.0389, -.0685, 1.0296]])
D65 = SRGB_XYZ.sum(axis=1)
_lock = threading.Lock()


@lru_cache(None)
def colorimetry():
    data = json.loads((FOLDER/'colorimetry.json').read_text('utf-8'))
    return {'wavelengths': np.asarray(data['wavelengths'], float), 'cmf': np.asarray(data['cmf'], float),
            'daylight': np.asarray(data['daylight'], float), 'basis': np.asarray(data['basis'], float)}


def wavelengths():
    return colorimetry()['wavelengths']


def daylight(kelvin):
    """CIE daylight of a correlated colour temperature (CIE 015)."""
    t = float(kelvin)
    if t <= 7000:
        x = -4.6070e9/t**3 + 2.9678e6/t**2 + .09911e3/t + .244063
    else:
        x = -2.0064e9/t**3 + 1.9018e6/t**2 + .24748e3/t + .237040
    y = -3*x*x + 2.870*x - .275
    m = .0241 + .2562*x - .7341*y
    s = colorimetry()['daylight']
    return s[:, 0] + (-1.3515 - 1.7703*x + 5.9114*y)/m*s[:, 1] + (.0300 - 31.4424*x + 30.0717*y)/m*s[:, 2]


def blackbody(kelvin):
    w = wavelengths()*1e-9
    radiance = 1/(w**5*(np.exp(1.4388e-2/(w*kelvin)) - 1))
    return radiance/np.interp(560e-9, w, radiance)


def light(name):
    """Spectrum of a light: 'daylight' (D55), 'tungsten' (3200 K), 'D50', 'D55', 'D65' or '<kelvin>K'."""
    if name in LIGHTS:
        kelvin = LIGHTS[name]
        return daylight(kelvin) if kelvin >= 4000 else blackbody(kelvin)
    if name.startswith('D') and name[1:].isdigit():
        return daylight(int(name[1:])*100*1.4388/1.4380)
    if name.endswith('K'):
        kelvin = float(name[:-1])
        return daylight(kelvin) if kelvin >= 4000 else blackbody(kelvin)
    raise ValueError(f'unknown light {name!r}')


def sampled(points, fill=0.):
    """A curve given as [[nm, value], ...] on the wavelength grid; `fill` outside its range (None: its end values)."""
    p = np.asarray(points, float)
    p = p[np.argsort(p[:, 0])]
    if fill is None:
        return np.interp(wavelengths(), p[:, 0], p[:, 1])
    return np.interp(wavelengths(), p[:, 0], p[:, 1], left=fill, right=fill)


def curve(points):
    p = np.asarray(points, float)
    p = p[np.argsort(p[:, 0])]
    return p[:, 0], p[:, 1]


def adaptation(white):
    """3x3 (Bradford) taking colours seen under `white` (XYZ) to D65, with that white at Y = 1."""
    source = BRADFORD @ (np.asarray(white, float)/white[1])
    target = BRADFORD @ D65
    return np.linalg.inv(BRADFORD) @ np.diag(target/source) @ BRADFORD


class Dyes:
    """A film's or paper's dye set: densities by wavelength, unmixed from Status A densities."""

    def __init__(self, dyes, minimum, view):
        # Beyond a plotted dye curve's ends its last value continues: plots stop where the curve runs along the
        # axis or the frame, and a gap with no dye at all would let light through every dense area.
        self.spectra = np.stack([sampled(dyes[k], None) for k in 'cmy'], axis=1)            # n x 3
        self.base = sampled(dyes['min'], np.nan) if 'min' in dyes else None
        peaks = np.array([[np.interp(w, wavelengths(), self.spectra[:, j]) for j in range(3)] for w in STATUS_A])
        self.spectra = self.spectra/np.diag(peaks)                     # each dye has density 1 at its own peak band
        self.unmix = np.linalg.inv(peaks/np.diag(peaks))
        if self.base is None or np.isnan(self.base).all():
            # Only the minimum densities at the three bands are known: a smooth base through them.
            self.base = np.interp(wavelengths(), STATUS_A[::-1], np.asarray(minimum, float)[::-1])
        else:
            self.base = np.where(np.isnan(self.base), np.nanmin(self.base), self.base)
        self.base_bands = np.array([np.interp(w, wavelengths(), self.base) for w in STATUS_A])
        self.view = light(view)
        cmf = colorimetry()['cmf']
        self.weights = cmf*self.view[:, None]
        white = (10**-self.base) @ self.weights
        self.adapt = adaptation(white)/white[1]
        # Papers and slides are made so that equal Status A densities above the base look neutral (Kodak plots
        # slide dyes "normalised to a visual neutral"). Reading densities at the three peak wavelengths only
        # approximates the broad Status A bands, more so in dense areas, so each dye's strength is set, level by
        # level, to keep that: equal densities d above the base give a neutral of visual density d.
        self.levels = np.linspace(.05, 3.5, 24)
        self.strengths = np.ones((len(self.levels), 3))
        strength = np.ones(3)
        target = np.log10(D65/D65[1])
        for i, level in enumerate(self.levels):
            # In densities (log10 of the colour), where dense areas are as well conditioned as light ones.
            def error(s):
                return -np.log10(np.maximum(self._colour(self.base_bands + level, s), 1e-30)) - (level - target)
            for _ in range(40):
                e = error(strength)
                if np.abs(e).max() < 1e-7:
                    break
                jacobian = np.stack([(error(strength*(1 + h)) - e)/1e-6 for h in np.eye(3)*1e-6], axis=1)  # per log strength
                step = np.clip(np.linalg.lstsq(jacobian, -e, rcond=None)[0], -.5, .5)
                strength = np.clip(strength*np.exp(step), .05, 20)
            self.strengths[i] = strength

    def _colour(self, densities, strength):
        amounts = np.maximum((np.asarray(densities, float) - self.base_bands) @ self.unmix.T, 0)*strength
        spectrum = amounts @ self.spectra.T + self.base
        return (10**-spectrum) @ self.weights @ self.adapt.T

    def colour(self, densities):
        """XYZ (D65, white = 1) of total Status A densities (..., 3)."""
        densities = np.asarray(densities, float)
        level = np.mean(densities - self.base_bands, axis=-1)
        strength = np.stack([np.interp(level, self.levels, self.strengths[:, j]) for j in range(3)], axis=-1)
        return self._colour(densities, strength)


class Stock:
    def __init__(self, data, paper=None):
        self.data = data
        self.kind = data['kind']
        self.channels = 1 if self.kind == 'bw-negative' else 3
        lines = ('v',) if self.channels == 1 else ('r', 'g', 'b')
        self.ref = float(data['log_h_ref'])
        self.matrix = self.exposure_matrix(data, lines)
        x = np.linspace(self.ref + SPAN[0], self.ref + SPAN[1], SAMPLES)
        self.grid, self.dx = x.astype(np.float32), float(x[1] - x[0])
        density = np.stack([np.interp(x, *curve(data['curves'][k])) for k in lines])
        if self.kind == 'colour-negative':
            density = self.printing_density(density, x)
        if self.kind == 'colour-slide':
            self.dyes = Dyes(data['dyes'], density.min(axis=1), data.get('view', 'D50'))
            self.tables = density.astype(np.float32)
        elif self.kind in ('colour-negative', 'bw-negative'):
            if paper is None:
                raise ValueError(f"{data['id']}: a negative needs a paper")
            self.paper = paper
            self.tables = self.printed(density, paper).astype(np.float32)
        else:
            raise ValueError(f"{data['id']}: cannot simulate a {self.kind}")
        self.slopes = np.diff(self.tables, axis=1, append=self.tables[:, -1:])
        if self.channels == 3:
            self.build_cube()
        if self.kind == 'colour-slide':
            self.place_grey()

    @staticmethod
    def exposure_matrix(data, lines):
        c = colorimetry()
        sensitivity = data.get('sensitivity') or {}
        if all(k in sensitivity for k in lines):
            s = np.stack([10**sampled(sensitivity[k], -np.inf) for k in lines], axis=1)
        else:
            s = c['cmf'][:, 1:2].repeat(len(lines), axis=1)          # no sensitivity published: the eye's
        weight = s*light(data.get('balance', 'daylight'))[:, None]
        m = weight.T @ c['basis']
        m = m/m.sum(axis=1, keepdims=True)
        return m

    def printing_density(self, density, x):
        """Status M densities as the paper sees them. A negative's published curves are Status M; its blue layer
        reads 10-15 % steeper and its red a few per cent flatter than its green on every Kodak sheet, while
        prints of a grey scale stay grey: the paper's sensitivities see the dyes differently from the
        densitometer. Each layer's contrast is matched to the green layer's around the reference exposure, its
        toe and shoulder kept in shape."""
        near = (x >= self.ref - 1) & (x <= self.ref + 1)
        slopes = np.array([np.polyfit(x[near], d[near], 1)[0] for d in density])
        at_ref = np.array([np.interp(self.ref, x, d) for d in density])
        scale = slopes[1]/np.maximum(slopes, 1e-6)
        return at_ref[:, None] + (density - at_ref[:, None])*scale[:, None]

    def printed(self, density, paper):
        """Per-layer print densities over the log exposure grid, with printer lights for a neutral mid grey."""
        x = self.grid.astype(float)
        grey = np.array([np.interp(self.ref, x, d) for d in density])
        paper_curves = [curve(paper['curves'][k]) for k in (('v',) if self.channels == 1 else 'rgb')]
        # A print's black is neutral: the paper's layers stop at the lowest of their maximum densities (the
        # published shoulders part only above density 2, where the plots are least certain).
        top = min(float(y.max()) for _, y in paper_curves)
        paper_curves = [(px, np.minimum(py, top)) for px, py in paper_curves]

        def print_density(lights, d):
            return np.stack([np.interp(lights[j] - d[j], *paper_curves[j]) for j in range(len(paper_curves))])

        # Printer lights: start where each paper layer prints the grey at a density near a print mid grey,
        # then correct all three together until the grey prints neutral at PRINT_GREY.
        target_density = -np.log10(PRINT_GREY)
        lights = np.array([np.interp(target_density + y.min(), y, x) for x, y in paper_curves]) + grey
        if self.channels == 1:
            self.paper_white = paper_curves[0][1].min()

            def error(l):
                p = print_density(l, grey[:, None])[:, 0]
                return np.array([10**-(p[0] - paper_curves[0][1].min()) - PRINT_GREY])
        else:
            dyes = paper['dyes']
            self.paper_dyes = Dyes(dyes, [c[1].min() for c in paper_curves], paper.get('view', 'D50'))

            def error(l):
                p = print_density(l, grey[:, None])[:, 0]
                return self.paper_dyes.colour(p) - PRINT_GREY*D65/D65[1]
        for _ in range(40):
            e = error(lights)
            if np.abs(e).max() < 1e-6:
                break
            jacobian = np.stack([(error(lights + h) - e)/1e-4 for h in np.eye(len(lights))*1e-4], axis=1)
            lights = lights - np.linalg.lstsq(jacobian, e, rcond=None)[0]
        self.lights = lights
        return print_density(lights, density)

    def place_grey(self):
        """Expose a slide so that a mid grey shows as a mid grey, as a scan of a well-exposed slide does: the
        reference exposure moves to where the slide's neutral scale passes PRINT_GREY. (A negative's printer
        lights do the same for prints.)"""
        y = self.dyes.colour(self.tables.T.astype(float))[:, 1]
        order = np.argsort(y)
        self.ref = float(np.interp(PRINT_GREY, y[order], self.grid[order]))

    def build_cube(self):
        dyes = self.dyes if self.kind == 'colour-slide' else self.paper_dyes
        self.lo = self.tables.min(axis=1)
        self.hi = np.maximum(self.tables.max(axis=1), self.lo + 1e-3)
        axes = [np.linspace(self.lo[j], self.hi[j], NODES) for j in range(3)]
        grid = np.stack(np.meshgrid(*axes, indexing='ij'), axis=-1)
        xyz = dyes.colour(grid.reshape(-1, 3)).reshape(NODES, NODES, NODES, 3)
        self.cube = (-np.log10(np.maximum(xyz, TINY))).astype(np.float32)
        self.step = (self.hi - self.lo)/(NODES - 1)
        self.flat = self.cube.reshape(-1, 3)
        self.lo32 = self.lo.astype(np.float32)
        self.inverse_step = (1/self.step).astype(np.float32)

    def densities(self, rgb):
        """Per-layer output densities (..., channels) of linear sRGB scene colours."""
        x = np.log10(np.maximum(rgb @ self.matrix.T.astype(np.float32), np.float32(TINY)))
        # Table index on the regular log exposure grid (np.interp's search is several times slower).
        u = np.clip((x + np.float32(self.ref - np.log10(GREY) - self.grid[0]))*np.float32(1/self.dx), 0, SAMPLES - 1.001)
        i = u.astype(np.int32)
        u -= i
        out = np.empty(x.shape, np.float32)
        for j in range(self.channels):
            index = i[..., j]
            out[..., j] = self.tables[j][index] + self.slopes[j][index]*u[..., j]
        return out

    def xyz(self, rgb):
        """XYZ (D65, white = 1) of linear sRGB scene colours."""
        d = self.densities(np.asarray(rgb, np.float32))
        if self.channels == 1:
            y = np.exp(np.float32(-LN10)*(d[..., 0] - np.float32(self.paper_white)))
            return y[..., None]*(D65/D65[1]).astype(np.float32)
        shape = d.shape[:-1]
        f = np.clip((d.reshape(-1, 3) - self.lo32)*self.inverse_step, 0, NODES - 1.001).astype(np.float32)
        i = f.astype(np.int32)
        t = f - i
        base = (i[:, 0]*NODES + i[:, 1])*NODES + i[:, 2]
        c = self.flat
        t0, t1, t2 = t[:, 0:1], t[:, 1:2], t[:, 2:3]
        s0, s1, s2 = 1 - t0, 1 - t1, 1 - t2
        n2, n1 = NODES*NODES, NODES
        v = ((c[base]*s0 + c[base + n2]*t0)*s2 + (c[base + 1]*s0 + c[base + n2 + 1]*t0)*t2)*s1
        v += ((c[base + n1]*s0 + c[base + n2 + n1]*t0)*s2 + (c[base + n1 + 1]*s0 + c[base + n2 + n1 + 1]*t0)*t2)*t1
        return np.exp(np.float32(-LN10)*v).reshape(*shape, 3)


def _read(kind, name):
    return json.loads((FOLDER/kind/f'{name}.json').read_text('utf-8'))


@lru_cache(None)
def catalog():
    """[{id, name, maker, kind, cine, iso, ...}] of the shipped stocks, in display order."""
    path = FOLDER/'index.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else []


KINDS = (('colour-negative', '컬러 네거티브'), ('colour-slide', '컬러 슬라이드'), ('bw-negative', '흑백'),
         ('cine', '영화용 필름'))


def entry(name):
    """The catalog entry of stock `name`, or None."""
    if not name:
        return None
    return next((e for e in catalog() if e['id'] == name), None)


def known(name):
    return entry(name) is not None


def _key(text):
    return ''.join(c for c in str(text).lower() if c.isalnum())


def find(text):
    """(stock id, None) for a film named as people write it ('Kodak Portra 400', 'portra400', an id), '' for
    none, or (None, [up to five close names])."""
    wanted = _key(text)
    if not wanted or wanted in ('none', 'off', 'nofilm'):
        return '', None
    entries = catalog()
    for e in entries:
        if wanted in (_key(e['id']), _key(e['name'])):
            return e['id'], None
    ending = [e for e in entries if _key(e['name']).endswith(wanted)]          # 'portra 400' -> Kodak Portra 400
    if len(ending) == 1:
        return ending[0]['id'], None
    partial = [e for e in entries if wanted in _key(e['name'])]
    if len(partial) == 1:
        return partial[0]['id'], None
    import difflib
    names = {_key(e['name']): e['name'] for e in entries}
    close = [names[k] for k in difflib.get_close_matches(wanted, list(names), n=5, cutoff=.4)]
    return None, [e['name'] for e in partial[:5]] or close


def grain_settings(name):
    """Effects › Grain values for a stock (from its datasheet granularity), or None."""
    e = entry(name)
    return dict(e['grain']) if e and e.get('grain') else None


_stocks = {}


def stock(name):
    with _lock:
        built = _stocks.get(name)
        if built is None:
            data = _read('stocks', name)
            paper = _read('papers', data['print']) if data.get('print') else None
            built = _stocks[name] = Stock(data, paper)
        return built


# Working spaces: linear sRGB, or linear ProPhoto (D50; colorimetric values adapted with Bradford).
def _prophoto():
    from .dcp import PROPHOTO_XYZ
    d50 = PROPHOTO_XYZ.sum(axis=1)
    to_d65 = np.linalg.inv(BRADFORD) @ np.diag((BRADFORD @ D65)/(BRADFORD @ d50)) @ BRADFORD
    return to_d65 @ PROPHOTO_XYZ


def _matrices(working_space):
    if working_space == 'ProPhoto':
        to_xyz = _prophoto()
        return XYZ_SRGB @ to_xyz, np.linalg.inv(to_xyz)
    return np.eye(3), XYZ_SRGB


def render(a, name, working_space='sRGB'):
    """Display-linear working-space pixels (0..1) of scene-linear working-space pixels `a` through stock `name`."""
    from .pixel_jobs import transform
    film = stock(name)
    into, out = _matrices(working_space)
    into = into.astype(np.float32)
    out = out.astype(np.float32)

    a = np.asarray(a, np.float32)
    glow = halation(a, film.data.get('halation'), into)

    def pixels(tile, first=0):
        rgb = tile[..., :3] @ into.T
        return (film.xyz(rgb).astype(np.float32) @ out.T).astype(np.float32)
    if glow is None:
        return transform(a, pixels)
    from .pixel_jobs import rows
    return rows(a.shape[0], a.shape[1], lambda first, stop: pixels(a[first:stop] + glow[first:stop] @ np.linalg.inv(into).T.astype(np.float32)))


def halation(a, spec, into):
    """Light scattered back from the film base into the emulsion around bright areas, or None.

    Films without an anti-halation layer (CineStill: Kodak Vision3 with its rem-jet backing removed) spread light
    from highlights into the red-sensitive layer, which lies deepest, and a little into green: a red-orange
    glow. spec: {"radius": fraction of the image diagonal, "threshold": scene-linear level where the glow begins,
    "strength": [r, g, b] share of the light above it}. These are not datasheet values. The result is in linear
    sRGB, sized to the frame, so a preview and a full-size export glow alike."""
    if not spec or a.ndim != 3:
        return None
    import cv2
    rgb = a[..., :3] @ into.T
    luminance = rgb @ np.array([.2126, .7152, .0722], np.float32)
    bright = np.maximum(luminance - np.float32(spec.get('threshold', 1.)), 0)
    sigma = float(spec.get('radius', .004))*float(np.hypot(*a.shape[:2]))
    if sigma < .5 or not bright.any():
        return None
    spread = cv2.GaussianBlur(bright, (0, 0), sigma)
    return spread[..., None]*np.asarray(spec.get('strength', (.35, .06, 0)), np.float32)


def with_film(settings, name, explicit=()):
    """settings with stock `name` ('' = none) as choosing it in the Film panel makes them: the stock's grain
    goes into Effects › Grain, and a previous stock's grain leaves with it unless it was changed (keys in
    `explicit` are left as they are)."""
    result = dict(settings)
    before = grain_settings(result.get('film_look'))
    if before and all(abs(float(result.get(k, 0)) - v) < 1e-6 for k, v in before.items()):
        from .engine import DEFAULTS
        for k in before:
            if k not in explicit:
                result[k] = DEFAULTS[k]
    for k, v in (grain_settings(name) or {}).items():
        if k not in explicit:
            result[k] = v
    result['film_look'] = name
    return result


def active(settings):
    """Whether a develop with these settings uses a film look (HDR editing has none)."""
    name = settings.get('film_look')
    return bool(name) and not settings.get('hdr') and float(settings.get('film_strength', 100)) > 0 and known(name)
