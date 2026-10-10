import numpy as np
from PIL import Image
from test_studio_ui import app, wait
from luma.auto_color import channel_points, auto_color_settings, WEIGHTS
from luma.engine import defaults, develop
from luma.processing import IDENTITY


def scene(seed=3):
    """Mostly grey surfaces of every brightness with a quarter of saturated colours."""
    rng = np.random.default_rng(seed)
    grey = np.repeat(rng.uniform(.04, .96, (120, 160, 1)), 3, axis=2)
    grey += rng.normal(0, .01, grey.shape)
    grey[:30] = rng.uniform(.05, .95, (30, 160, 3))
    return np.clip(grey, 0, 1).astype(np.float32)


def cast(image):
    """Green shadows turning magenta in the highlights (a film-scan crossover)."""
    out = image.copy(); level = image@WEIGHTS
    out[..., 1] += .08*(1-level)-.07*level
    out[..., 2] -= .05
    return np.clip(out, 0, 1)


def apply(image, curves):
    out = image.copy()
    for c, points in enumerate(curves):
        p = np.asarray(points, np.float32); out[..., c] = np.interp(image[..., c], p[:, 0], p[:, 1])
    return out


def greyness(image):
    rows = image[30:].reshape(-1, 3)                               # the grey part of scene()
    return float(np.abs(rows-(rows@WEIGHTS)[:, None]).mean())


def test_crossover_cast_is_removed_and_brightness_kept():
    tinted = cast(scene())
    curves, report = channel_points(tinted)
    fixed = apply(tinted, curves)
    assert report['status'] == 'applied' and all(c is not None for c in report['casts'])
    assert greyness(tinted) > .025 and greyness(fixed) < .012
    assert abs(float((fixed@WEIGHTS).mean()-(tinted@WEIGHTS).mean())) < .01
    for points in curves:                                           # valid for the CPU and GPU curve stages
        p = np.asarray(points)
        assert p[0].tolist() == [0, 0] and p[-1].tolist() == [1, 1] and np.all(np.diff(p[:, 0]) > 0) and np.all(np.diff(p[:, 1]) >= 0)


def test_amount_scales_the_correction_and_zero_changes_nothing():
    tinted = cast(scene())
    full = greyness(apply(tinted, channel_points(tinted, 1.)[0]))
    half = greyness(apply(tinted, channel_points(tinted, .5)[0]))
    assert full < half < greyness(tinted)
    assert np.allclose(apply(tinted, channel_points(tinted, 0.)[0]), tinted, atol=1e-4)


def test_neutral_pictures_and_coloured_lights_are_left_alone():
    assert channel_points(scene())[1]['status'] == 'neutral'
    stage = np.full((120, 160, 3), .03, np.float32)                # a dark hall...
    stage[50:60, 40:120] = (.15, .45, .95)                         # ...with blue stage lighting
    curves, report = channel_points(stage)
    assert curves is None and report['casts'][1] is None
    assert channel_points(np.ones((40, 40, 3), np.float32))[1]['status'] == 'flat'      # clipped white: nothing to judge


def test_settings_keep_everything_but_the_channel_curves():
    source = cast(scene())**2.2                                     # develop() takes linear pixels
    base = defaults(); base.update(exposure=.2, contrast=10.)
    adjusted, report = auto_color_settings(source, base)
    assert report['status'] == 'applied'
    assert {k for k in adjusted if adjusted[k] != base[k]} == {'rgb_curves'}
    assert greyness(develop(source, adjusted)) < greyness(develop(source, base))*.5
    mono, report = auto_color_settings(source, {**base, 'monochrome': True})
    assert report['status'] == 'monochrome' and mono['rgb_curves'] == [IDENTITY]*3


def test_button_slider_undo_and_batch(app, tmp_path):
    from luma.app import MainWindow
    files = []
    for n in range(2):
        path = tmp_path/f'{n}.png'; Image.fromarray(np.uint8(cast(scene(n))*255)).save(path); files.append(path)
    w = MainWindow(tmp_path/'data')
    try:
        ids = [w.catalog.add(p) for p in files]; w.refresh_lists(); w.activate(ids[0]); w.show()
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        assert w.auto_color_row.isHidden()
        w.auto_color_button.click()
        full = w.settings['rgb_curves']
        assert full != [IDENTITY]*3 and not w.auto_color_row.isHidden()
        assert w.catalog.histories(ids[0])[0]['label'] == '자동 색'
        w.auto_color_slider.setValue(50); w.auto_color_slider.sliderReleased.emit()
        half = w.settings['rgb_curves']
        shift = lambda curves: max(abs(y-x) for points in curves for x, y in points)
        assert 0 < shift(half) < shift(full) and w.catalog.preference('auto_color_amount') == 50
        w.undo(); assert w.settings['rgb_curves'] == full
        wait(lambda: not w.render_running)
        w.activate(ids[1]); wait(lambda: w.source is not None and not w.render_running)
        assert w.auto_color_row.isHidden() and w.settings['rgb_curves'] == [IDENTITY]*3
        w.workflow.auto_color()                                     # menu command: selected photos at the saved strength
        assert shift(w.catalog.photo(ids[1])['settings']['rgb_curves']) > 0
    finally:
        wait(lambda: not w.render_running); w.close()
