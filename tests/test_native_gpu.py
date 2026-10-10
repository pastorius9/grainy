import numpy as np
import pytest
from luma import native_gpu
from luma.engine import defaults, normalized, develop, _develop_tone_pixels

pytestmark = pytest.mark.skipif(not native_gpu.available(), reason='GPU module not built')

CASES = {
    'default': {},
    'exposure_wb': dict(exposure=.7, temperature=25, tint=-12),
    'kelvin': dict(kelvin_enabled=True, kelvin=4300, wb_reference=6500),
    'full': dict(exposure=-.4, temperature=-15, tint=8, shadows=40, highlights=-60, blacks=-30, whites=25, contrast=35,
                 curve=[[0, 0], [.25, .18], [.5, .52], [.8, .9], [1, 1]], lens_vignette=-30),
    'lift': dict(blacks=45, whites=-40, contrast=-20, shadows=-25, highlights=30, lens_vignette=40),
    'flat_curve_segment': dict(curve=[[0, .1], [.3, .3], [.3, .6], [1, .9]]),
}


def frame(h=240, w=360, seed=1):
    y, x = np.mgrid[:h, :w].astype(np.float32)
    noise = np.random.default_rng(seed).normal(0, .05, (h, w, 3))
    return np.clip(np.stack([.05+.9*x/w, .05+.9*y/h, .5+.4*np.sin(x/37)*np.cos(y/29)], -1)+noise, 0, 1.3).astype(np.float32)


@pytest.fixture(scope='module', params=['hardware', 'warp'])
def _device_mode(request):
    # One device per mode for the module; tests may still set GRAINY_GPU=0 for their CPU reference.
    import os
    previous = os.environ.get('GRAINY_GPU')
    if request.param == 'warp':
        os.environ['GRAINY_GPU'] = 'warp'
    else:
        os.environ.pop('GRAINY_GPU', None)
    native_gpu.reset()
    yield request.param
    native_gpu.reset()
    if previous is None:
        os.environ.pop('GRAINY_GPU', None)
    else:
        os.environ['GRAINY_GPU'] = previous


@pytest.fixture
def device(_device_mode):
    if native_gpu.status().startswith('unavailable'):
        pytest.skip(native_gpu.status())
    return _device_mode


@pytest.mark.parametrize('name', CASES)
def test_gpu_tone_matches_cpu_reference(device, name):
    a = frame()
    s = normalized({**defaults(), **CASES[name]})
    cpu = _develop_tone_pixels(a.copy(), s)
    gpu = native_gpu.tone(a, s)
    assert gpu is not None and gpu.dtype == np.float32 and gpu.shape == a.shape
    # Float32 transcendental functions differ by a few ULP; 16-bit output may move by one code at most.
    assert np.abs(cpu-gpu).max() < 2e-6
    assert np.abs(np.uint16(cpu*65535+.5).astype(int)-np.uint16(gpu*65535+.5).astype(int)).max() <= 1
    assert np.array_equal(a, frame())  # input is never modified


def test_odd_sizes_and_buffer_reuse(device):
    s = normalized({**defaults(), **CASES['full']})
    for h, w in ((1, 1), (17, 5), (240, 360), (3, 1000), (240, 360)):
        a = frame(h, w, seed=h)
        assert np.abs(_develop_tone_pixels(a.copy(), s)-native_gpu.tone(a, s)).max() < 2e-6


def test_fallbacks_return_none(monkeypatch):
    a = frame(32, 32)
    s = normalized(defaults())
    assert native_gpu.tone(a[..., :1], s) is None
    assert native_gpu.tone(a, {**s, 'curve': [[0, 0], [.6, .5], [.4, .7], [1, 1]]}) is None
    native_gpu.set_enabled(False)
    try:
        assert not native_gpu.enabled() and native_gpu.tone(a, s) is None and native_gpu.status() == 'off'
    finally:
        native_gpu.set_enabled(True)
    monkeypatch.setenv('GRAINY_GPU', '0')
    assert native_gpu.tone(a, s) is None


def test_full_develop_same_as_cpu_within_one_8bit_step(monkeypatch):
    a = frame(300, 450)
    s = {**defaults(), **CASES['full'], 'saturation': 12, 'clarity': 20, 'sharpen': 30}
    gpu = develop(a, s)
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu = develop(a, s)
    difference = np.abs(np.uint8(gpu*255+.5).astype(int)-np.uint8(cpu*255+.5).astype(int))
    assert difference.max() <= 1 and (difference > 0).mean() < 1e-3


def test_preferences_persist_gpu_choice(tmp_path):
    from luma.preferences import Preferences
    prefs = Preferences(tmp_path/'settings.json')
    assert prefs.use_gpu
    prefs.save_language('en');prefs.save_use_gpu(False)
    again = Preferences(tmp_path/'settings.json')
    assert not again.use_gpu and again.language == 'en'


LOCAL_CURVE = [[0, 0], [.3, .2], [.7, .8], [1, 1]]
LOCAL_CASES = {
    'exposure': dict(exposure=.6),
    'basic': dict(exposure=-.3, contrast=30, temperature=20, tint=-15, saturation=-25, shadows=40, highlights=-35),
    'whites_blacks': dict(whites=30, blacks=-20, saturation=40),
    'curves': dict(local_curves=[LOCAL_CURVE, [[0, .1], [1, .9]], [[0, 0], [1, 1]], [[0, 0], [.5, .6], [1, 1]]]),
    'hue': dict(hue=40, exposure=.2),
    'hsl': dict(hsl=[[20, -30, 10], [0, 0, 0], [0, 50, 0], [-15, 0, 20], [0, 0, 0], [30, 20, -10], [0, 0, 0], [5, 5, 5]]),
    'everything': dict(exposure=.4, contrast=-20, temperature=-10, tint=12, saturation=15, shadows=-20, highlights=25,
                       whites=-10, blacks=15, local_curves=[LOCAL_CURVE], hue=-25, hsl=[[10, 10, 10]]*8),
}


@pytest.mark.parametrize('name', LOCAL_CASES)
def test_gpu_local_region_matches_cpu(device, name, monkeypatch):
    from luma.processing import _local_region, component_mask
    image = frame(200, 300, seed=4)
    mask = component_mask((200, 300), {'type': 'radial', 'points': [[.2, .2], [.8, .9]], 'feather': 60})
    edit = LOCAL_CASES[name]
    gpu = _local_region(image, mask, edit)
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu = _local_region(image, mask, edit)
    # HSL goes through OpenCV's SIMD hue conversion; allow a few more ULP there.
    assert np.abs(cpu-gpu).max() < 1e-5
    assert np.abs(np.uint16(cpu*65535+.5).astype(int)-np.uint16(gpu*65535+.5).astype(int)).max() <= 1
    outside = mask == 0
    assert np.array_equal(gpu[outside], image[outside])


def test_gpu_local_leaves_spatial_and_point_colour_edits_to_cpu():
    image = frame(64, 64);weights = np.ones((64, 64), np.float32)
    for edit in ({'clarity': 20}, {'sharpen': 30}, {'noise_luma': 10}, {'point_colors': [{}]}, {'hdr': True},
                 {'local_curves': [[[0, 0], [.6, .5], [.4, .7], [1, 1]]]}, {'hsl': [[1, 0, 0]]}):
        assert native_gpu.local(image, weights, edit) is None
    assert native_gpu.local(image, weights[:10], {'exposure': 1}) is None


MIXER = [[20, -30, 10], [0, 0, 0], [0, 50, 0], [-15, 0, 20], [0, 0, 0], [30, 20, -10], [0, 0, 0], [5, 5, 5]]
COLOR_CASES = {
    'saturation': dict(saturation=35),
    'vibrance': dict(vibrance=40, saturation=-10),
    'hsl_mode': dict(mixer_mode='hsl', hsl=MIXER),
    'hsv_mode': dict(mixer_mode='hsv', hsl=MIXER),
    'rgb_curves': dict(rgb_curves=[[[0, 0], [.4, .5], [1, 1]], [[0, 0], [1, 1]], [[0, .1], [.6, .5], [1, .95]]]),
    'parametric': dict(parametric=[30, -20, 15, -40]),
    'calibration': dict(calibration=[[20, -30], [0, 40], [-15, 10]]),
    'grading': dict(grading=[[220, 40, -10], [35, 20, 5], [50, 30, 10]], grading_balance=-20),
    'matrix': dict(camera_matrix=[[1.1, -.05, -.05], [-.02, 1.04, -.02], [0, -.1, 1.1]]),
    'everything': dict(saturation=15, vibrance=20, mixer_mode='hsv', hsl=MIXER, rgb_curves=[[[0, 0], [.4, .5], [1, 1]]]*3,
                       parametric=[10, -10, 10, -10], calibration=[[10, 10], [0, -20], [5, 0]],
                       grading=[[200, 30, 0], [0, 0, 0], [40, 25, -5]], camera_matrix=[[1.05, 0, 0], [0, 1, 0], [0, 0, .95]]),
}


@pytest.mark.parametrize('name', COLOR_CASES)
def test_gpu_color_stage_matches_cpu(device, name, monkeypatch):
    from luma.engine import _develop_color
    image = np.clip(frame(200, 300, seed=6), 0, 1)
    s = normalized({**defaults(), **COLOR_CASES[name]})
    gpu = _develop_color(image.copy(), s)
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu = _develop_color(image.copy(), s)
    assert np.abs(cpu-gpu).max() < 1e-5
    assert np.abs(np.uint16(cpu*65535+.5).astype(int)-np.uint16(gpu*65535+.5).astype(int)).max() <= 1


def test_gpu_color_leaves_filters_hdr_and_invalid_points_to_cpu():
    image = frame(32, 32);s = normalized(defaults())
    assert native_gpu.color(image, {**s, 'point_colors': [{'version': 2, 'hue': 10, 'range': 0}]}) is None
    assert native_gpu.color(image, {**s, 'photo_filter_enabled': True}) is None
    assert native_gpu.color(image, {**s, 'hdr': True}) is None
    assert native_gpu.color(image, {**s, 'hsl': [[1, 0, 0]]}) is None


DETAIL_CASES = {
    'texture': dict(texture=40), 'clarity': dict(clarity=35), 'sharpen': dict(sharpen=60), 'sharpen_max': dict(sharpen=150),
    'sharpen_detail_mask': dict(sharpen=80, sharpen_radius=1.4, sharpen_detail=30, sharpen_mask=40),
    'sharpen_rgb_v1': dict(sharpen=80, sharpen_detail=30, sharpen_mask=40, detail_version=1),
    'mono': dict(monochrome=True, texture=-20), 'vignette': dict(vignette=-45),
    'vignette_shape': dict(vignette=60, vignette_midpoint=30, vignette_roundness=-60, vignette_feather=20, vignette_highlights=70),
    'vignette_round': dict(vignette=45, vignette_midpoint=70, vignette_roundness=80, vignette_feather=90), 'grain': dict(grain=30, clarity=10),
    'everything': dict(texture=25, clarity=-30, sharpen=50, sharpen_detail=60, sharpen_mask=20, vignette=30, monochrome=True),
    'noise_reduction': dict(noise_luma=25, noise_color=30, texture=20, clarity=15, sharpen=40),
    'noise_luma_only': dict(noise_luma=60),
    'noise_luma_bilateral_v2': dict(noise_luma=60, noise_color=20, detail_version=2),
    'noise_luma_wavelet_max': dict(noise_luma=100, texture=30),
    'noise_colour_only': dict(noise_color=100),
    'dehaze': dict(dehaze=30, clarity=20),
    'dehaze_negative': dict(dehaze=-40),
    'defringe': dict(defringe=70),
    'noise_dehaze_defringe_all': dict(noise_luma=20, noise_color=25, dehaze=25, defringe=40, texture=20, clarity=10,
                                      sharpen=30, sharpen_mask=20, monochrome=True, vignette=-20),
}


@pytest.mark.parametrize('scale', [1., .42])
@pytest.mark.parametrize('name', DETAIL_CASES)
def test_gpu_detail_stage_matches_cpu(device, name, scale, monkeypatch):
    from luma.engine import _develop_detail
    image = np.clip(frame(150, 220, seed=8), 0, 1)
    image[40:70, 60:120] = [.7, .2, .8]   # a purple patch with hard edges for defringe
    s = normalized({**defaults(), **DETAIL_CASES[name]})
    gpu = _develop_detail(image.copy(), s, scale)
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu = _develop_detail(image.copy(), s, scale)
    # Bilateral weights come from a lookup table; float rounding moves a few pixels slightly more.
    assert np.abs(cpu-gpu).max() < (5e-5 if 'noise' in name else 2e-6)
    assert np.abs(np.uint16(np.clip(cpu, 0, 1)*65535+.5).astype(int)-np.uint16(np.clip(gpu, 0, 1)*65535+.5).astype(int)).max() <= 1


@pytest.mark.parametrize('shape', [(1, 1), (5, 7), (3, 40), (40, 2)])
def test_gpu_blur_reflects_like_opencv_when_kernel_exceeds_image(device, shape, monkeypatch):
    from luma.engine import _develop_detail
    image = np.random.default_rng(1).random(shape+(3,), dtype=np.float32)
    s = normalized({**defaults(), 'texture': 50, 'clarity': 40, 'sharpen': 70, 'sharpen_radius': 3, 'sharpen_mask': 30})
    gpu = _develop_detail(image.copy(), s, 1.)
    monkeypatch.setenv('GRAINY_GPU', '0')
    assert np.abs(_develop_detail(image.copy(), s, 1.)-gpu).max() < 2e-6


def test_exact_lab_roundtrip_is_far_tighter_than_opencv_float_lab():
    import cv2
    from luma.processing import rgb_to_lab, lab_to_rgb
    rgb = np.random.default_rng(0).random((200, 300, 3), dtype=np.float32)
    assert np.abs(lab_to_rgb(rgb_to_lab(rgb))-rgb).max() < 3e-5
    opencv = cv2.cvtColor(cv2.cvtColor(rgb, cv2.COLOR_RGB2Lab), cv2.COLOR_Lab2RGB)
    assert np.abs(opencv-rgb).max() > 1e-3   # OpenCV 5 float Lab is fixed-point; documents why it is not used
    grey = np.repeat(np.linspace(0, 1, 11, dtype=np.float32)[None, :, None], 3, -1)
    assert np.abs(rgb_to_lab(grey)[..., 1:]).max() < 1e-4


def test_noise_reduction_on_flat_image_keeps_colour(device):
    from luma.engine import _develop_detail
    flat = np.full((40, 50, 3), [.4, .3, .2], np.float32)
    s = normalized({**defaults(), 'noise_luma': 40, 'noise_color': 40})
    assert np.abs(_develop_detail(flat.copy(), s, 1.)-flat).max() < 3e-5


GEOMETRY_CASES = {
    'distortion': dict(distortion=-6),
    'perspective': dict(perspective_v=12, perspective_h=-8, transform_scale=95),
    'everything': dict(distortion=8, distortion_k2=-3, distortion_k3=1, perspective_h=5, perspective_v=-4, aspect_scale=10,
                       shift_x=3, shift_y=-2, transform_scale=90, ca_red=30, ca_blue=-20),
    'straighten': dict(straighten=2.5),
    'straighten_negative': dict(straighten=-13.7),
    'combined': dict(distortion=-4, straighten=5, rotation=1, flip=True, crop=(.1, .05, .9, .95)),
}


@pytest.mark.parametrize('shape', [(120, 180), (37, 53), (64, 64)])
@pytest.mark.parametrize('name', GEOMETRY_CASES)
def test_gpu_geometry_matches_cpu_on_noise(device, name, shape, monkeypatch):
    # Random noise is the worst case: any sample-position error shows up as a large value error.
    from luma.engine import geometry
    image = np.random.default_rng(sum(shape)).random(shape+(3,), dtype=np.float32)
    s = {**defaults(), **GEOMETRY_CASES[name]}
    gpu = geometry(image, s)
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu = geometry(image, s)
    assert gpu.shape == cpu.shape and np.abs(gpu-cpu).max() < 1e-6


def test_gpu_geometry_skips_masks_fast_paths_and_missing_doubles():
    image = np.random.default_rng(0).random((40, 60, 3), dtype=np.float32)
    assert native_gpu.rotate(image, 0) is None and native_gpu.rotate(image, 180) is None
    assert native_gpu.rotate(np.zeros((30, 30, 3), np.float32), 90) is None
    assert native_gpu.optical(image[..., :1], defaults()) is None


POINT = lambda **k: {'version': 2, 'rgb': [.8, .3, .2], **k}
POINT_CASES = {
    'v2_hue': [POINT(hue=40)],
    'v2_ranges': [POINT(saturation=-50, lightness=30, range=.2, saturation_range=.4, lightness_range=.3)],
    'v2_all_ranges': [POINT(hue=-20, range=.5, saturation_range=1, lightness_range=1)],
    'v2_grey_reference': [{'version': 2, 'rgb': [.5, .5, .51], 'hue': 30}],
    'v1': [{'rgb': [.2, .6, .3], 'hue': 25, 'saturation': 20, 'lightness': -10, 'range': .15}],
    'several': [POINT(hue=40), {'version': 2, 'rgb': [.2, .4, .9], 'saturation': 60}, {'rgb': [.9, .8, .1], 'lightness': 20}, POINT()],
}


@pytest.mark.parametrize('name', POINT_CASES)
def test_gpu_point_colours_match_cpu(device, name, monkeypatch):
    from luma.engine import _develop_color
    image = np.clip(frame(150, 220, seed=11), 0, 1)
    s = normalized({**defaults(), 'point_colors': POINT_CASES[name], 'saturation': -10, 'grading': [[200, 20, 0], [0, 0, 0], [0, 0, 0]]})
    gpu = native_gpu.color(image, s)
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu = _develop_color(image.copy(), s)
    assert gpu is not None and np.abs(cpu-gpu).max() < 1e-5
    assert np.abs(np.uint16(cpu*65535+.5).astype(int)-np.uint16(gpu*65535+.5).astype(int)).max() <= 1


@pytest.mark.parametrize('name', POINT_CASES)
def test_gpu_point_colours_out_of_gamut_boundary_flips_are_rare(device, name, monkeypatch):
    # v2 keeps a colour only where w>0. For pixels already outside [0,1] (here after +10 saturation)
    # a weight at the exact range edge can be 0 on one side and ~1e-8 on the other, because OpenCV's
    # SIMD hue differs from the exact formula by ~1e-7; the roundtrip then clips that pixel.
    from luma.engine import _develop_color
    image = np.clip(frame(150, 220, seed=11), 0, 1)
    s = normalized({**defaults(), 'point_colors': POINT_CASES[name], 'saturation': 10, 'grading': [[200, 20, 0], [0, 0, 0], [0, 0, 0]]})
    gpu = native_gpu.color(image, s)
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu = _develop_color(image.copy(), s)
    differing = np.abs(cpu-gpu).max(axis=-1) > 1e-5
    assert differing.mean() < .01


RESIDENT_EDITS = [
    ('exposure', .4), ('exposure', -.2), ('saturation', 25), ('sharpen', 45), ('noise_luma', 20), ('exposure', .4),
    ('straighten', 2.), ('hsl', [[10, 20, 0]]+[[0, 0, 0]]*7), ('grain', 12), ('dehaze', 15),
]


def _resident_settings():
    return {**defaults(), 'contrast': 15, 'clarity': 20, 'texture': 10, 'defringe': 20, 'vignette': -15,
            'point_colors': [POINT(hue=30)], 'masks': [{'enabled': True, 'components': [{'type': 'radial', 'points': [[.2, .2], [.8, .8]]}],
                                                        'adjustments': {'exposure': .3, 'saturation': 10}}]}


def test_resident_preview_matches_uncached_develop(device, monkeypatch):
    # Cached previews keep tone and colour results on the device; every edit must still equal the uncached
    # chain exactly, including edits that start from a kept stage, a new source and a competing cache.
    from luma.render_cache import DevelopmentCache
    calls = []
    original = native_gpu._lib.grainy_gpu_resident
    monkeypatch.setattr(native_gpu._lib, 'grainy_gpu_resident', lambda handle, data, slot, first, *rest: (
        calls.append((data is not None, slot, first)), original(handle, data, slot, first, *rest))[1])
    source = frame(150, 210, seed=4);source.setflags(write=False)
    other = frame(150, 210, seed=5);other.setflags(write=False)
    preview, live, third = (DevelopmentCache(64*1024*1024) for _ in range(3))
    s = _resident_settings()
    for key, value in RESIDENT_EDITS:
        s = {**s, key: value}
        assert np.array_equal(develop(source, s, cache=preview), develop(source, s)), key
    uploads = [upload for upload, slot, first in calls]
    firsts = [first for upload, slot, first in calls]
    # Uploads only for the first frame and the geometry change; colour and detail edits skip earlier stages.
    assert uploads == [True, False, False, False, False, False, True, False, False, False]
    assert firsts == [0, 0, 1, 2, 2, 0, 0, 1, 2, 2]
    calls.clear()
    # Two caches keep separate slot groups; a third takes the least recently used one.
    for cache, image in ((live, other), (preview, source), (third, other), (preview, source), (live, other)):
        s = {**s, 'exposure': s['exposure']+.1}
        assert np.array_equal(develop(image, s, cache=cache), develop(image, s))
    assert [upload for upload, slot, first in calls] == [True, False, True, False, True]
    # A new source object with identical settings never reuses the old device frames.
    calls.clear()
    assert np.array_equal(develop(other, s, cache=preview), develop(other, s)) and calls[0][0]
    # A preview scale change reruns only the detail stage from the kept colour result.
    calls.clear()
    for size in ((840, 600), (420, 300)):
        assert np.array_equal(develop(other, s, cache=preview, original_size=size), develop(other, s, original_size=size))
    assert [(upload, first) for upload, slot, first in calls] == [(False, 2), (False, 2)]


def test_resident_preview_falls_back_to_stage_functions(device):
    from luma.render_cache import DevelopmentCache
    source = frame(90, 120, seed=6);source.setflags(write=False)
    cache = DevelopmentCache(64*1024*1024)
    for extra in ({'photo_filter_enabled': True, 'photo_filter': 'warm'}, {'exposure': .3}, {'hdr': True}):
        s = {**_resident_settings(), **extra}
        assert np.allclose(develop(source, s, cache=cache), develop(source, s), atol=1e-6), extra


def _layer(component, **edit):
    return {'enabled': True, 'components': [component], 'adjustments': edit}


RADIAL = {'type': 'radial', 'points': [[.2, .2], [.8, .8]]}
LINEAR = {'type': 'linear', 'points': [[.1, .1], [.9, .7]]}


def test_resident_chain_applies_shape_masks_grain_and_clip_on_the_device(device, monkeypatch):
    from luma.render_cache import DevelopmentCache
    calls = []
    original = native_gpu._lib.grainy_gpu_resident
    monkeypatch.setattr(native_gpu._lib, 'grainy_gpu_resident', lambda handle, data, slot, first, keep, output, *rest: (
        calls.append((data is not None, first, rest[-1])), original(handle, data, slot, first, keep, output, *rest))[1])
    source = frame(140, 200, seed=8);source.setflags(write=False)
    cache = DevelopmentCache(64*1024*1024)
    s = {**defaults(), 'exposure': .3, 'grain': 20, 'clarity': 15,
         'masks': [_layer(RADIAL, exposure=.6, saturation=20, hsl=[[10, 20, 0]]+[[0, 0, 0]]*7),
                   _layer(LINEAR, contrast=30, local_curves=[[[0, 0], [.5, .6], [1, 1]]]),
                   {**_layer(RADIAL, exposure=-1), 'enabled': False}]}
    seen = {}
    edits = [{}, {'exposure': .5}, {'masks': [{**s['masks'][0], 'adjustments': {'exposure': .9}}]+s['masks'][1:]}, {'grain': 35}]
    for change in edits:
        s = {**s, **change}
        result = develop(source, s, cache=cache, on_mask=lambda i, m: seen.__setitem__(i, m))
        assert np.array_equal(result, develop(source, s)), change
        assert result.flags.writeable and result.min() >= 0 and result.max() <= 1
        assert sorted(seen) == [0, 1, 2]
    # Mask edit: only the local passes (first stage 3); grain: detail again. Every call clipped on the device.
    assert [(upload, first) for upload, first, clip in calls] == [(True, 0), (False, 0), (False, 3), (False, 2)]
    assert all(clip for upload, first, clip in calls)
    # The same masks as apply_local passes to on_mask.
    monkeypatch.setenv('GRAINY_GPU', '0')
    cpu_masks = {}
    develop(source, s, cache=DevelopmentCache(64*1024*1024), on_mask=lambda i, m: cpu_masks.__setitem__(i, m))
    assert all(np.array_equal(seen[i], cpu_masks[i]) for i in cpu_masks)


def test_resident_chain_leaves_range_masks_and_cpu_local_edits_to_apply_local(device, monkeypatch):
    from luma.render_cache import DevelopmentCache
    clips = []
    original = native_gpu._lib.grainy_gpu_resident
    monkeypatch.setattr(native_gpu._lib, 'grainy_gpu_resident', lambda *args: (clips.append(args[-1]), original(*args))[1])
    source = frame(120, 160, seed=9);source.setflags(write=False)
    for masks in ([_layer({'type': 'luma', 'range': [.3, .7], 'softness': .1}, exposure=.4)],
                  [_layer(RADIAL, clarity=40)], [_layer(RADIAL, exposure=.2)]*12):
        clips.clear()
        s = {**defaults(), 'exposure': .2, 'masks': masks}
        assert np.array_equal(develop(source, s, cache=DevelopmentCache(64*1024*1024)), develop(source, s))
        assert clips == [0]   # detail result only; masks and the clip ran in apply_local


HDR_TONE_CASES = {
    'plain': dict(exposure=1.5),
    'curve': dict(exposure=1.2, curve=[[0, .03], [.25, .265], [.5, .5], [.75, .735], [1, .97]], blacks=-9, whites=7),
    'steep_end': dict(exposure=2, curve=[[0, 0], [.6, .5], [.9, .95], [1, 1.2]], contrast=30, shadows=20, highlights=-30),
    'flat_end': dict(exposure=1, curve=[[0, 0], [.5, .6], [.8, .9], [.8, .95]], lens_vignette=-20),
}


@pytest.mark.parametrize('name', HDR_TONE_CASES)
def test_gpu_hdr_tone_matches_cpu_reference(device, name):
    a = frame() * 3   # well above SDR white
    s = normalized({**defaults(), **HDR_TONE_CASES[name], 'hdr': True})
    cpu = _develop_tone_pixels(a.copy(), s)
    gpu = native_gpu.tone(a, s)
    assert gpu is not None and cpu.max() > 1.2
    # Relative, since values extend above 1; 16-bit steps of the SDR range for the rest.
    assert np.abs(cpu-gpu).max() / max(1., float(cpu.max())) < 4e-6
    assert gpu.min() >= 0


def test_hdr_helpers_on_row_workers_match_serial_math():
    from luma import hdr
    rng = np.random.default_rng(12)
    a = (rng.random((700, 900, 3), dtype=np.float32) * 4) - .2
    s = {'hdr_brightness': 35, 'hdr_limit': 3, 'sdr_exposure': .5, 'sdr_compression': 60}
    assert np.array_equal(hdr.limit(a, s), hdr._limit(a, s))
    assert np.array_equal(hdr.sdr_rendition(a, s), hdr._sdr_rendition(a, s))
    operation = lambda b: np.clip(b * 1.1 - .05, 0, 1).astype(np.float32)
    gain = np.maximum(1., a.max(axis=-1, keepdims=True))
    expected = operation(a / gain) * gain
    result = hdr.color_operation(a, operation)
    assert result.dtype == expected.dtype and np.array_equal(result, expected)


def test_large_scratch_buffers_are_kept_between_calls_and_freed_when_idle(device, monkeypatch):
    import time
    rng = np.random.default_rng(9)
    frame = rng.random((1400, 2100, 3), dtype=np.float32)*.6          # above the size whose buffers used to be freed per call
    s = normalized({**defaults(), 'noise_color': 25., 'sharpen': 60.})
    first = native_gpu.develop(frame, s)
    assert first is not None
    assert np.array_equal(native_gpu.develop(frame, s), first)         # reused buffers: the same pixels
    before = native_gpu.trims
    native_gpu.trim()
    assert native_gpu.trims == before+1
    assert np.array_equal(native_gpu.develop(frame, s), first)         # and after they were freed
    monkeypatch.setattr(native_gpu, 'TRIM_DELAY', .2)
    count = native_gpu.trims
    native_gpu.develop(frame, s)
    deadline = time.monotonic()+15                                     # an earlier timer may still run with the old delay
    while native_gpu.trims == count and time.monotonic() < deadline:time.sleep(.05)
    assert native_gpu.trims > count
    assert np.array_equal(native_gpu.develop(frame, s), first)
