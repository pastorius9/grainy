import numpy as np
import pytest
from PIL import Image
from test_studio_ui import app, wait
from luma.engine import defaults, normalized, develop, highlight_rolloff, tone_white, resize_export, export_image


def test_rolloff_keeps_highlight_separation_and_is_smooth():
    x = np.linspace(0, 2, 2001, dtype=np.float32)
    y = highlight_rolloff(x, 2.)
    assert np.array_equal(y[x <= .5], x[x <= .5])                        # below the knee: unchanged
    assert np.all(np.diff(y) > 0) and y[-1] == pytest.approx(1, abs=1e-6) and y[-2] < 1
    slope = np.diff(y) / np.diff(x)
    assert abs(slope[499] - slope[501]) < .01                           # no bend at the knee
    assert highlight_rolloff(x, 1.) is x                                # nothing to compress


def test_exposure_increase_keeps_bright_detail_instead_of_clipping():
    ramp = np.repeat(np.linspace(.6, 1, 400, dtype=np.float32)[None, :, None], 3, axis=2).repeat(20, axis=0)
    new = develop(ramp, {**defaults(), 'exposure': 1.})
    old = develop(ramp, {**defaults(), 'exposure': 1., 'tone_version': 1})
    assert (old[0] >= 1).mean() > .6                    # the old process clips the top of the ramp
    assert new[0, :-1, 1].max() < 1 and np.all(np.diff(new[0, :, 1]) > 0)   # only input 1.0 reaches white


def test_saved_edits_keep_their_look_and_new_edits_use_the_rolloff():
    assert normalized({})['tone_version'] == 3
    assert normalized({'exposure': -.4})['tone_version'] == 3                     # identical either way
    assert normalized({'exposure': .4})['tone_version'] == 1                      # brightened before 0.5.56
    assert normalized({'temperature': 30})['tone_version'] == 1
    assert normalized({'exposure': .4, 'tone_version': 2})['tone_version'] == 3     # roll-off photo, sliders unused: current
    image = np.random.default_rng(1).random((60, 80, 3), dtype=np.float32)
    for settings in ({'exposure': -.4}, {}, {'contrast': 20, 'blacks': -30}):
        assert np.array_equal(develop(image, normalized(settings)), develop(image, {**normalized(settings), 'tone_version': 1}))
    assert tone_white(normalized({'exposure': 1.})) == pytest.approx(2)


def test_extreme_exposure_still_clips_and_warns():
    from luma.engine import rolloff_white
    assert rolloff_white(normalized({'exposure': 5.})) == 4
    ramp = np.repeat(np.linspace(.2, .7, 300, dtype=np.float32)[None, :, None], 3, axis=2)
    out = develop(ramp, {**defaults(), 'exposure': 5.})
    assert (out >= 1).mean() > .5 and np.all(np.diff(out[0, :, 1]) >= 0)


def test_export_resize_is_lanczos_clipped_and_leaves_small_images(tmp_path):
    rng = np.random.default_rng(2)
    a = rng.random((300, 450, 3), dtype=np.float32)
    small = resize_export(a, 150)
    expected = np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(a[..., c])).resize((150, 100), Image.Resampling.LANCZOS))
                         for c in range(3)], -1)
    assert small.shape == (100, 150, 3) and np.array_equal(small, np.clip(expected, 0, 1))
    assert resize_export(a, 500) is a
    hdr = resize_export(a * 4, 150, hdr=True)
    assert hdr.max() > 1 and hdr.min() >= 0
    path = tmp_path/'in.png';Image.fromarray(np.uint8(a*255)).save(path)
    export_image(path, tmp_path/'out.jpg', defaults(), 'JPEG', longest=200)
    assert Image.open(tmp_path/'out.jpg').size == (200, 133)


def test_update_process_menu_switches_only_legacy_photos(app, tmp_path):
    from luma.app import MainWindow
    paths = []
    for i in range(2):
        paths.append(tmp_path/f'p{i}.png');Image.fromarray(np.uint8(np.random.default_rng(i).random((40, 60, 3))*255)).save(paths[-1])
    w = MainWindow(tmp_path/'data');ids = [w.catalog.add(p) for p in paths]
    try:
        w.catalog.set_settings(ids[0], {**defaults(), 'exposure': .5, 'tone_version': 1})
        w.refresh_lists();w.activate(ids[1]);w.show()
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        w.filmstrip.restore_selection(ids, ids[1])
        w.workflow.update_process()
        assert [w.catalog.photo(i)['settings']['tone_version'] for i in ids] == [3, 3]
        assert [w.catalog.photo(i)['settings']['detail_version'] for i in ids] == [3, 3]
        assert w.catalog.photo(ids[0])['settings']['exposure'] == .5
    finally:
        wait(lambda: not w.render_running);w.close()


def test_sharpen_slider_reaches_150(app, tmp_path):
    from luma.app import MainWindow
    path = tmp_path/'p.png';Image.fromarray(np.full((20, 30, 3), 128, np.uint8)).save(path)
    w = MainWindow(tmp_path/'data');ident = w.catalog.add(path);w.refresh_lists();w.activate(ident);w.show()
    try:
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        assert w.adjustments['sharpen'].slider.maximum() == 150
    finally:
        wait(lambda: not w.render_running);w.close()


def test_luminance_sharpening_keeps_colour_noise_and_old_edits_keep_rgb_sharpening():
    from luma.processing import rgb_to_lab
    rng = np.random.default_rng(4)
    y, x = np.mgrid[:120, :160].astype(np.float32)
    grey = .45 + .2 * np.sin(x / 3) * np.cos(y / 4)
    image = np.repeat(grey[..., None], 3, axis=2)
    image[..., 0] += rng.normal(0, .01, grey.shape);image[..., 2] -= rng.normal(0, .01, grey.shape)   # colour noise
    image = np.clip(image, 0, 1).astype(np.float32)
    s = {**defaults(), 'sharpen': 100}
    new, old, flat = (develop(image, v) for v in (s, {**s, 'detail_version': 1}, defaults()))
    chroma = lambda a: float(np.hypot(*(rgb_to_lab(a)[..., 1:] - rgb_to_lab(flat)[..., 1:]).transpose(2, 0, 1)).mean())
    lum = lambda a: float(np.abs(a @ np.array([.2126, .7152, .0722], np.float32) - flat @ np.array([.2126, .7152, .0722], np.float32)).mean())
    assert chroma(new) < chroma(old) / 2                          # colour noise not sharpened
    assert lum(new) == pytest.approx(lum(old), rel=.15)          # about the same luminance detail (noise passes the threshold less)
    assert normalized({})['detail_version'] == 3 and normalized({'exposure': .3})['detail_version'] == 3
    assert normalized({'sharpen': 40})['detail_version'] == 1        # sharpened before 0.5.60
    assert normalized({'noise_luma': 30})['detail_version'] == 2     # bilateral luminance NR before 0.5.61
    assert normalized({'sharpen': 40, 'detail_version': 2})['detail_version'] == 2

