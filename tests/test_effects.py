import numpy as np
import pytest
from luma.engine import defaults, develop, post_crop_vignette, grain_pattern, settings_grain
from luma.render_cache import DevelopmentCache
from luma.adobe_develop import convert


def grey(h=120, w=200, value=.6):
    return np.full((h, w, 3), value, np.float32)


def darkening(s, h=120, w=200, value=.6):
    return 1 - post_crop_vignette(grey(h, w, value), {**defaults(), **s})[..., 1]/value


def test_default_vignette_shape_is_the_earlier_amount_only_vignette():
    rng = np.random.default_rng(2)
    a = rng.random((90, 130, 3), dtype=np.float32)
    y, x = np.ogrid[-1:1:90j, -1:1:130j]
    old = a*(1-(x*x+y*y).clip(0, 2)*40/220)[..., None]
    new = post_crop_vignette(a.copy(), {**defaults(), 'vignette': 40})
    assert new.dtype == np.float32 and np.array_equal(new, old.astype(np.float32))


def test_vignette_midpoint_roundness_feather_and_highlights():
    base = darkening({'vignette': 60})
    ring = (60, 150)                                                     # between centre and corner
    assert darkening({'vignette': 60, 'vignette_midpoint': 20})[ring] > base[ring] > darkening({'vignette': 60, 'vignette_midpoint': 80})[ring]
    assert darkening({'vignette': 60, 'vignette_feather': 0})[ring] < base[ring]   # harder: concentrated at the edge
    assert darkening({'vignette': 60, 'vignette_feather': 0})[0, 0] == pytest.approx(base[0, 0], abs=1e-6)
    circle = darkening({'vignette': 60, 'vignette_roundness': 100})
    assert circle[60, 100+50] == pytest.approx(circle[60+50, 100], abs=.01)   # equal pixel distance, equal darkening
    ellipse = darkening({'vignette': 60})
    assert ellipse[60, 150] < ellipse[110, 100]                          # frame-shaped by default
    square = darkening({'vignette': 60, 'vignette_roundness': -100})
    diagonal = (int(60-.7*59.5), int(100+.7*99.5))                         # x = y = .7
    assert square[60, 190] == pytest.approx(base[60, 190], abs=1e-4)          # same on the axes
    assert square[diagonal] < base[diagonal] - .02                            # squarer: keeps more of the frame
    bright = darkening({'vignette': 60, 'vignette_highlights': 100}, value=.95)
    assert bright[0, 0] < darkening({'vignette': 60}, value=.95)[0, 0]/3
    dark = darkening({'vignette': 60, 'vignette_highlights': 100}, value=.3)
    assert dark[0, 0] == pytest.approx(darkening({'vignette': 60}, value=.3)[0, 0])      # only highlights protected


def test_grain_size_and_roughness():
    shape = (256, 256)
    corr = lambda f: float(np.corrcoef(f[:, :-1].ravel(), f[:, 1:].ravel())[0, 1])
    local_std = lambda f: np.array([f[i:i+32, j:j+32].std() for i in range(0, 256, 32) for j in range(0, 256, 32)])
    plain = grain_pattern(shape)
    assert np.array_equal(plain, np.random.default_rng(2026).standard_normal(shape))   # the earlier field
    big = grain_pattern(shape, size=100)
    assert corr(big) > .8 and abs(corr(plain)) < .05 and big.std() == pytest.approx(1.9, abs=.1)   # larger grains stay visible
    assert corr(grain_pattern(shape, size=100, pixel_scale=.25)) < corr(big)          # previews scale the size
    rough, even = grain_pattern(shape, roughness=100), grain_pattern(shape, roughness=0)
    assert local_std(rough).std() > 2*local_std(plain).std()                        # clumpy
    assert corr(even) > corr(plain) + .2
    s = {**defaults(), 'grain': 40, 'grain_size': 70, 'grain_roughness': 80}
    assert np.array_equal(settings_grain(shape, s), (40/1800*grain_pattern(shape, 70, 80)).astype(np.float32))


def test_effects_match_between_previews_exports_and_the_cpu():
    rng = np.random.default_rng(9)
    source = rng.random((150, 220, 3), dtype=np.float32)
    s = {**defaults(), 'vignette': 55, 'vignette_midpoint': 35, 'vignette_roundness': -40, 'vignette_feather': 20,
         'vignette_highlights': 60, 'grain': 30, 'grain_size': 60, 'grain_roughness': 75}
    export = develop(source, s)
    preview = develop(source, s, cache=DevelopmentCache())
    assert np.abs(preview - export).max() < 2e-5


def test_lightroom_effects_settings_are_imported():
    result = convert({'GrainAmount': 20, 'GrainSize': 60, 'GrainFrequency': 70, 'PostCropVignetteAmount': -35,
                      'PostCropVignetteMidpoint': 40, 'PostCropVignetteRoundness': -20, 'PostCropVignetteFeather': 70,
                      'PostCropVignetteHighlightContrast': 30})
    s = result['settings']
    assert (s['grain'], s['grain_size'], s['grain_roughness']) == (20, 60, 70)
    assert (s['vignette'], s['vignette_midpoint'], s['vignette_roundness'], s['vignette_feather'], s['vignette_highlights']) == (35, 40, -20, 70, 30)
    lighten = convert({'PostCropVignetteAmount': 25})
    assert lighten['settings']['vignette'] == 0 and 'PostCropVignetteAmount' in lighten['omitted']
