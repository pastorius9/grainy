import numpy as np
import pytest
from PIL import Image
from test_studio_ui import app, wait
from luma import tone_response, pixel_jobs
from luma.engine import defaults, normalized, develop, resize_float, _develop_tone, _develop_tone_pixels, to_srgb

LUMA = np.array([.2126, .7152, .0722], np.float32)


def scene(height=360, width=540, seed=3):
    """Dark textured field next to a bright textured field, with a coloured patch in each (linear light)."""
    rng = np.random.default_rng(seed)
    image = np.empty((height, width, 3), np.float32)
    image[:, :width//2] = .06; image[:, width//2:] = .6
    image += rng.normal(0, 1, (height, width, 1)).astype(np.float32)*np.where(np.arange(width) < width//2, .01, .05)[None, :, None]
    image[60:140, 60:160] = (.15, .04, .03)                  # dark red
    image[60:140, width//2+60:width//2+160] = (.3, .55, .8)   # bright blue
    return np.clip(image, 0, 1)


def test_versions_keep_saved_looks_and_new_edits_use_the_current_process():
    assert defaults()['tone_version'] == 3 and normalized({})['tone_version'] == 3
    assert normalized({'shadows': 20})['tone_version'] == 2                        # saved before 0.5.80 with the slider used
    assert normalized({'shadows': 20, 'tone_version': 2})['tone_version'] == 2
    assert normalized({'tone_version': 2, 'contrast': 10})['tone_version'] == 3    # same pixels, so the sliders start current
    assert normalized({'highlights': -30, 'exposure': .4})['tone_version'] == 1    # older still: stays as it was
    image = scene()
    for settings in ({}, {'contrast': 15, 'blacks': -20}, {'exposure': .5}):
        assert np.array_equal(develop(image, {**defaults(), **settings, 'tone_version': 2}), develop(image, {**defaults(), **settings}))


def test_every_response_curve_is_monotonic_and_neutral_at_zero():
    assert abs(tone_response.curve(0, 0, .4)).max() < 1e-7
    for pivot in (.2, tone_response.PIVOT, .5, .8):
        for highlights in range(-100, 101, 25):
            for shadows in range(-100, 101, 25):
                out = tone_response.X+tone_response.curve(highlights, shadows, pivot)
                assert np.all(np.diff(out) >= -1e-7) and out.min() >= 0 and out.max() <= 1
    lift = tone_response.curve(0, 60, .5); cut = tone_response.curve(-60, 0, .5)
    assert lift[:128].max() > .03 and abs(lift[200:]).max() < .02               # shadows: the dark half
    assert cut[128:].min() < -.03 and abs(cut[:100]).max() < .02                # highlights: the bright half
    assert np.array_equal(tone_response.curve(0, 150, .5), tone_response.curve(0, 100, .5))    # clamped to the slider range


def test_shadows_keep_local_contrast_and_colour_where_the_old_process_flattened():
    image = scene(); width = image.shape[1]
    base = develop(image, defaults())
    new = develop(image, {**defaults(), 'shadows': 80})
    old = develop(image, {**defaults(), 'shadows': 80, 'tone_version': 2})
    dark = np.s_[180:340, 20:width//2-20]; patch = np.s_[70:130, 70:150]
    brightness = lambda a, region: float((a[region] @ LUMA).mean())
    texture = lambda a, region: float((a[region] @ LUMA).std())
    assert brightness(new, dark) > brightness(base, dark)+.02                   # lifted
    assert texture(new, dark) == pytest.approx(texture(base, dark), rel=.05)    # texture is kept as it is
    assert texture(old, dark) < .85*texture(base, dark)                         # the old process flattened it
    saturation = lambda a: float(((a[patch].max(-1)-a[patch].min(-1))/a[patch].max(-1)).mean())
    assert saturation(new) > .9*saturation(base) and saturation(old) < .8*saturation(base)
    bright = np.s_[180:340, width//2+20:-20]
    assert np.abs(new[bright]-base[bright]).max() < .03                         # the bright side is left alone
    lowered = develop(image, {**defaults(), 'highlights': -80})
    assert brightness(lowered, bright) < brightness(base, bright)-.02 and np.abs(lowered[dark]-base[dark]).max() < .03


def test_the_sliders_meet_at_middle_grey_whatever_the_photo():
    ramp = np.repeat(np.linspace(0, 1, 512, dtype=np.float32)[None, :, None]**2.2, 3, axis=2).repeat(64, axis=0)
    plain = develop(ramp, defaults()) @ LUMA
    lifted = develop(ramp, {**defaults(), 'shadows': 60}) @ LUMA; cut = develop(ramp, {**defaults(), 'highlights': -60}) @ LUMA
    dark, bright = plain < tone_response.PIVOT-.02, plain > tone_response.PIVOT+.02
    assert (lifted-plain)[dark].max() > .05 and np.abs(lifted-plain)[bright].max() < .005
    assert (cut-plain)[bright].min() < -.05 and np.abs(cut-plain)[dark].max() < .005
    assert tone_response.PIVOT == pytest.approx(float(to_srgb(np.float32(.18))), abs=1e-3)
    # The same curve for a dark and for a bright photo: only the base under each pixel decides.
    shade = develop(np.float32(.25)*ramp, {**defaults(), 'shadows': 60})-develop(np.float32(.25)*ramp, defaults())
    assert float(shade.mean()) > .02


def test_the_response_is_our_own_construction():
    X = tone_response.X
    for pivot in (.2, .37, .5, .8):
        up = X+tone_response.curve(0, 100, pivot); down = X+tone_response.curve(0, -100, pivot)
        # Black, the pivot and everything above it stay; at least a third of the base's contrast is kept.
        assert up[0] == 0 and np.abs(up[X >= pivot]-X[X >= pivot]).max() < 1e-6
        assert np.diff(up).min() >= np.diff(X)[0]/3-1e-6 and np.diff(up).max() <= np.diff(X)[0]*3+1e-6
        # A negative amount is the inverse of the positive one.
        assert np.abs(np.interp(up, X, down)-X).max() < 2e-3
        # Highlights is Shadows mirrored about the pivot.
        mirrored = 1-(X+tone_response.curve(-100, 0, 1-pivot))[::-1]
        assert np.abs(mirrored-up).max() < 1e-6
    # The strongest lift is where the cubic has its maximum, a third of the way from black to the pivot.
    lift = tone_response.curve(0, 100, .6)
    assert X[lift.argmax()] == pytest.approx(.2, abs=.005) and lift.max() == pytest.approx(.6*2*4/27, abs=1e-4)
    assert np.allclose(tone_response.curve(0, 50, .6), lift/2, atol=1e-6)        # linear in the amount


def test_no_pixel_moves_more_than_half_way_to_black_or_white():
    rng = np.random.default_rng(8)
    encoded = np.clip(np.where(np.arange(240)[None, :, None] < 120, .06, .93)+rng.normal(0, .05, (160, 240, 1)), 0, 1).astype(np.float32).repeat(3, 2)
    luma = encoded @ LUMA
    for highlights, shadows in ((100, -100), (-100, 100), (100, 100), (-100, -100)):
        local = tone_response.with_curve(tone_response.analyse(encoded, encoded.shape[:2]), highlights, shadows)
        out = tone_response.apply(encoded.copy(), local) @ LUMA
        assert np.all(out >= luma/2-1e-6) and np.all(1-out >= (1-luma)/2-1e-6)
        assert ((out <= 0) | (out >= 1)).sum() <= ((luma <= 0) | (luma >= 1)).sum()    # nothing newly clipped


def test_tiles_previews_and_exports_agree(monkeypatch):
    image = scene(900, 1400)
    s = normalized({**defaults(), 'shadows': 70, 'highlights': -60})
    tiled = _develop_tone(image, s)
    whole = _develop_tone_pixels(image.copy(), s)
    assert np.abs(tiled-whole).max() < 2e-6
    monkeypatch.setattr(pixel_jobs, 'WORKERS', 1)
    assert np.abs(_develop_tone(image, s)-whole).max() < 2e-6
    # The same analysis at preview size: a reduced source gives the reduced result.
    smooth = resize_float(resize_float(scene(900, 1400, 5), 300), 1400)
    full = develop(smooth, s); preview = develop(resize_float(smooth, 700), s)
    assert np.abs(preview-resize_float(full, 700)).mean() < .004
    tiny = scene(40, 60)                                                        # smaller than the analysis map
    assert np.isfinite(develop(tiny, s)).all()
    rgba = np.dstack([image[:64, :64], np.ones((64, 64, 1), np.float32)])
    assert np.isfinite(_develop_tone_pixels(rgba[..., :3].copy(), s)).all()


def test_gpu_matches_cpu_and_keeps_the_analysis_while_the_sliders_move():
    from luma import native_gpu
    from luma.render_cache import DevelopmentCache
    if not native_gpu.enabled():pytest.skip('no GPU device')
    image = scene(300, 452)
    for edit in ({'shadows': 60, 'highlights': -40}, {'shadows': -100, 'highlights': 100, 'exposure': .7, 'contrast': 20, 'curve': [[0, 0], [.5, .6], [1, 1]]},
                 {'shadows': 100, 'lens_vignette': 40, 'temperature': 20}):
        s = normalized({**defaults(), **edit})
        gpu = native_gpu.tone(image, s)
        assert gpu is not None and np.abs(gpu-_develop_tone_pixels(image.copy(), s)).max() < 2e-5
    s = normalized({**defaults(), 'shadows': 30})
    cache = DevelopmentCache(64*1024*1024)
    first = develop(image, s, cache=cache)
    kept = getattr(cache, '_tone_base', None)
    assert kept is not None and np.abs(first-develop(image, s)).max() < 2e-5
    develop(image, {**s, 'shadows': 55}, cache=cache)
    assert cache._tone_base[1] is kept[1]                                       # sliders: analysis reused
    develop(image, {**s, 'exposure': .5}, cache=cache)
    assert cache._tone_base[1] is not kept[1]                                   # exposure changes it


def test_moving_the_sliders_takes_a_rolloff_photo_to_the_current_process(app, tmp_path):
    from luma.app import MainWindow
    paths = []
    for i in range(2):
        paths.append(tmp_path/f'p{i}.png');Image.fromarray(np.uint8(np.random.default_rng(i).random((40, 60, 3))*255)).save(paths[-1])
    w = MainWindow(tmp_path/'data');ids = [w.catalog.add(p) for p in paths]
    try:
        w.catalog.set_settings(ids[0], {**defaults(), 'shadows': 20., 'tone_version': 2})
        w.catalog.set_settings(ids[1], {**defaults(), 'exposure': .5, 'shadows': 20., 'tone_version': 1})
        w.refresh_lists();w.activate(ids[0]);w.show()
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        assert w.settings['tone_version'] == 2                                  # opened: unchanged look
        w.set_setting('contrast', 5.);assert w.settings['tone_version'] == 2
        w.set_setting('shadows', 25.);assert w.settings['tone_version'] == 3
        wait(lambda: not w.render_running)
        w.activate(ids[1]);wait(lambda: w.current_id == ids[1] and w.source is not None and not w.render_running)
        w.set_setting('highlights', -10.);assert w.settings['tone_version'] == 1   # the clip-era process is only changed by the menu command
        assert all(v['tone_version'] == 3 for v in w.all_presets.values() if 'tone_version' in v and (v.get('shadows') or v.get('highlights')))
    finally:
        wait(lambda: not w.render_running);w.close()
