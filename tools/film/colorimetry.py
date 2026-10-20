"""Write assets/film/colorimetry.json: the CIE data the film simulation needs, and the spectra Grainy uses for
an RGB colour, on 380-780 nm in 5 nm steps.

- cmf: CIE 1931 2-degree colour-matching functions (CIE dataset DOI 10.25039/CIE.DS.xvudnb9b, CC BY-SA 4.0).
- daylight: the components S0, S1, S2 of daylight (CIE dataset DOI 10.25039/CIE.DS.w7zunnny, CC BY-SA 4.0).
- basis: three smooth reflectance spectra whose sum is 1 at every wavelength and whose colours under D65 are the
  linear sRGB primaries, so that r*basis_r + g*basis_g + b*basis_b is a plausible reflectance for linear sRGB
  (r, g, b) in 0..1 (the method of Mallett and Yuksel, "Spectral primary decomposition for rendering with sRGB
  reflectance", EGSR 2019; computed here from the CIE data, not copied).

The CIE files are read from <GRAINY_FILM_SHEETS>/cie (downloaded from files.cie.co.at/Publications-datasets/).
The output file is a derivative of the CIE datasets and carries their licence (CC BY-SA 4.0); see
assets/film/README.md.
"""
import json
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from digitize import FOLDER

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'assets'/'film'/'colorimetry.json'
WAVELENGTHS = np.arange(380, 781, 5)
SRGB_XYZ = np.array([[.4124564, .3575761, .1804375], [.2126729, .7151522, .0721750], [.0193339, .1191920, .9503041]])


def table(name):
    rows = [line.split(',') for line in (FOLDER/'cie'/name).read_text('utf-8').splitlines() if line.strip()]
    data = np.array([[float(v) if v not in ('', 'NaN') else 0. for v in row] for row in rows])
    return np.stack([np.interp(WAVELENGTHS, data[:, 0], data[:, i]) for i in range(1, data.shape[1])], axis=1)


def daylight_d65(components):
    x = .31271; y = .32902        # D65 chromaticity, CIE 015
    m = .0241 + .2562*x - .7341*y
    m1 = (-1.3515 - 1.7703*x + 5.9114*y)/m
    m2 = (.0300 - 31.4424*x + 30.0717*y)/m
    return components[:, 0] + m1*components[:, 1] + m2*components[:, 2]


def basis(cmf, light):
    """Smoothest B (n x 3) with 0 <= B <= 1, rows summing to 1, and the colour of B[:, c] under `light` equal to
    sRGB primary c (white scaled to Y = 1)."""
    n = len(WAVELENGTHS)
    weights = cmf*light[:, None]
    weights /= weights[:, 1].sum()                       # a perfect white reflector has Y = 1
    target = SRGB_XYZ                                     # columns: XYZ of the primaries; they sum to D65 white
    scale = (np.ones(n) @ weights)/SRGB_XYZ.sum(axis=1)   # same white for both (D65 from CIE vs the sRGB matrix)
    target = SRGB_XYZ*scale[:, None]

    def unpack(v):
        return v.reshape(n, 3)

    def cost(v):
        b = unpack(v)
        d = np.diff(b, axis=0)
        return float((d*d).sum())

    def gradient(v):
        b = unpack(v)
        d = np.diff(b, axis=0)
        g = np.zeros_like(b)
        g[:-1] -= 2*d
        g[1:] += 2*d
        return g.ravel()
    # Blue follows from red, green and the sums of 1 (the primaries add up to white), so it is not a constraint.
    constraints = [{'type': 'eq', 'fun': lambda v: (weights.T @ unpack(v) - target)[:, :2].ravel(),
                    'jac': lambda v: np.kron(weights.T, np.eye(3)).reshape(3, 3, -1)[:, :2].reshape(6, -1)},
                   {'type': 'eq', 'fun': lambda v: unpack(v).sum(axis=1) - 1, 'jac': lambda v: np.kron(np.eye(n), np.ones((1, 3)))}]
    start = np.full(n*3, 1/3)
    result = minimize(cost, start, jac=gradient, bounds=[(0, 1)]*(n*3), constraints=constraints, method='SLSQP',
                      options={'maxiter': 2000, 'ftol': 1e-12})
    if not result.success:
        raise RuntimeError(result.message)
    b = unpack(result.x)
    error = np.abs(weights.T @ b - target).max()
    if error > 1e-6:
        raise RuntimeError(f'basis misses the primaries by {error}')
    return b


def main():
    cmf = table('CIE_xyz_1931_2deg.csv')
    components = table('CIE_illum_Dxx_comp.csv')
    b = basis(cmf, daylight_d65(components))
    data = {'wavelengths': [int(w) for w in WAVELENGTHS],
            'cmf': np.round(cmf, 8).tolist(), 'daylight': np.round(components, 4).tolist(), 'basis': np.round(b, 7).tolist(),
            'source': {'cmf': 'CIE 1931 colour-matching functions, 2 degree observer. CIE (2019), DOI 10.25039/CIE.DS.xvudnb9b, CC BY-SA 4.0',
                       'daylight': 'Components of the relative spectral distribution of daylight. CIE (2022), DOI 10.25039/CIE.DS.w7zunnny, CC BY-SA 4.0',
                       'basis': 'Computed by tools/film/colorimetry.py from the two datasets above, after Mallett and Yuksel (EGSR 2019)'}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(',', ':')), 'utf-8')
    print(OUT, OUT.stat().st_size, 'bytes; basis min', b.min().round(4), 'max', b.max().round(4))


if __name__ == '__main__':
    main()
