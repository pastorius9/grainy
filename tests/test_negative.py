"""Film negative conversion: the scan becomes a positive before every other stage."""
import numpy as np
import pytest
from PIL import Image
from test_studio_ui import app, window, wait
from luma import negative, native_gpu
from luma.engine import defaults, normalized, develop, to_srgb, auto_tone_settings, export_image
from luma.render_cache import DevelopmentCache

BASE, PEAK = (.62, .38, .21), (.031, .012, .006)          # an orange film base and the densest part, linear


def idle(w):
    wait(lambda: not w.jobs and not w.render_running and not w.preview_timer.isActive() and not w.refine_timer.isActive())


def scene(height=120, width=180):
    """Scene light: black, a ramp over 6.5 stops, white, with a coloured patch."""
    stops = np.clip(np.linspace(-8, 1.5, width, dtype=np.float32), -6.5, 0)
    light = np.repeat(np.float32(2.)**stops[None, :, None], 3, axis=2).repeat(height, axis=0)
    light[height//4:height//2, int(width*.45):int(width*.6)] *= np.float32([.9, .5, .25])      # in the middle of the ramp
    return light


def scanned(light, span=6.5):
    """What a scanner sees of the negative that recorded `light` over `span` stops."""
    base, peak = np.float32(BASE), np.float32(PEAK)
    density = np.log(base/peak)*(np.log2(np.maximum(light, 1e-9))/span+1)
    return (base*np.exp(-density)).astype(np.float32)


def settings(**more):
    return normalized({**defaults(), 'negative': True, 'negative_base': list(BASE), 'negative_peak': list(PEAK), **more})


def test_off_leaves_the_frame_alone_and_defaults_are_off():
    frame = scanned(scene())
    assert normalized({})['negative'] is False and normalized({})['negative_base'] is None
    assert negative.apply(frame, normalized({})) is frame
    assert negative.apply(frame, {**normalized({}), 'negative': True}) is frame          # not measured yet: nothing to do


def test_the_positive_is_the_scene_that_was_photographed():
    light = scene(); frame = scanned(light)
    positive = negative.apply(frame, settings())
    assert positive.dtype == np.float32 and positive is not frame
    np.testing.assert_allclose(positive, light, rtol=2e-3)                               # the orange base is gone as well
    brighter = negative.apply(frame, settings(negative_density=1.))
    np.testing.assert_allclose(brighter, light*2, rtol=2e-3)                             # density is exposure, in stops
    harder = negative.apply(frame, settings(negative_range=8.))
    assert harder[60, 5, 1] < positive[60, 5, 1] and harder[60, -1, 1] == pytest.approx(positive[60, -1, 1], rel=2e-3)
    alpha = np.dstack([frame, np.full(frame.shape[:2], .5, np.float32)])
    assert np.array_equal(negative.apply(alpha, settings())[..., 3], alpha[..., 3])


def test_spots_denser_than_the_picture_stop_at_its_white():
    light = scene(); frame = scanned(light); frame[10:14, 10:14] = 0; frame[20:24, 20:24] = np.float32(PEAK)*np.float32(.2)      # dust, the holder
    positive = negative.apply(frame, settings())
    assert positive.max() == pytest.approx(1., rel=1e-4) and positive[11, 11, 0] == positive.max()
    assert negative.apply(frame, settings(negative_density=1.)).max() == pytest.approx(2., rel=1e-4)
    tone, report = auto_tone_settings(frame, settings())
    clean, _ = auto_tone_settings(scanned(light), settings())
    assert report['status'] == 'applied' and tone['exposure'] == pytest.approx(clean['exposure'], abs=.02)      # the specks change nothing


def test_the_scan_is_measured_inside_the_crop():
    light = scene(300, 450); frame = np.ones((400, 650, 3), np.float32)                     # a light table around the film
    frame[50:350, 100:550] = scanned(light)
    whole = negative.converted(frame, normalized({}))
    assert whole['negative_base'] == pytest.approx([1., 1., 1.], rel=1e-3)                  # the bare light, not the film
    crop = [100/650, 50/400, 550/650, 350/400]
    cropped = negative.converted(frame, normalized({'crop': crop}))
    np.testing.assert_allclose(cropped['negative_base'], BASE, rtol=.05); np.testing.assert_allclose(cropped['negative_peak'], PEAK, rtol=.25)
    assert cropped['crop'] == crop and cropped['negative'] is True
    # The measured density puts the picture's median at middle grey (within the allowed range).
    inside = frame[50:350, 100:550][18:-18, 27:-27]
    median = float(np.median(negative.apply(inside, cropped) @ np.float32([.2126, .7152, .0722])))
    assert median == pytest.approx(negative.MIDDLE, rel=.06) and negative.DENSITY_RANGE[0] <= cropped['negative_density'] <= negative.DENSITY_RANGE[1]
    assert negative.converted(np.full((60, 60, 3), .3, np.float32), normalized({}))['negative_density'] in negative.DENSITY_RANGE      # nothing to place: a limit
    # A camera RAW file's import brightening is taken back; an exposure the user chose is kept.
    from luma.raw_defaults import BUILTIN_RAW
    assert negative.converted(frame, normalized({**BUILTIN_RAW, 'crop': crop}))['exposure'] == 0. and negative.converted(frame, normalized({**BUILTIN_RAW, 'crop': crop}))['blacks'] == 0.
    assert negative.converted(frame, normalized({'exposure': .4, 'crop': crop}))['exposure'] == .4


def test_a_gamma_encoded_scan_gives_the_same_positive():
    light = scene(); frame = scanned(light); encoded = frame**np.float32(1/2.2)
    s = settings(negative_base=[v**(1/2.2) for v in BASE], negative_peak=[v**(1/2.2) for v in PEAK])
    np.testing.assert_allclose(negative.apply(encoded, s), light, rtol=5e-3)


def test_analysis_finds_the_film_base_and_ignores_the_edge_of_the_scan():
    light = scene(300, 450); frame = scanned(light)
    frame[:6] = frame[-6:] = 1.; frame[:, :8] = frame[:, -8:] = 1.                       # scanner light around the film
    base, peak = negative.analyse(frame)
    np.testing.assert_allclose(base, BASE, rtol=.05); np.testing.assert_allclose(peak, PEAK, rtol=.25)
    measured = negative.converted(frame, normalized({}))
    assert measured['negative'] is True and measured['negative_base'] == base
    np.testing.assert_allclose(negative.base_from(np.tile(np.float32(BASE), (9, 9, 1))), BASE, rtol=1e-6)
    flat = negative.analyse(np.full((50, 50, 3), .3, np.float32))                         # no density range at all
    assert all(p < b for b, p in zip(*flat)) and np.isfinite(negative.apply(np.full((4, 4, 3), .3, np.float32), settings(negative_base=flat[0], negative_peak=flat[1]))).all()
    assert np.isfinite(negative.apply(np.zeros((4, 4, 3), np.float32), settings())).all()


def test_a_photographed_negative_is_read_in_the_cameras_own_colours():
    from luma import rawcolor
    from luma.source_cache import DecodedSourceCache
    assert not rawcolor.enabled(normalized({})) and rawcolor.enabled(normalized({'negative': True}))        # loaders decode it natively
    assert DecodedSourceCache.decode_options(normalized({})) != DecodedSourceCache.decode_options(normalized({'negative': True}))
    cm = np.array([[.88, -.24, -.07], [-.48, 1.26, .25], [-.06, .15, .75]])
    info = dict(camera='Test Camera', format='NEF', xyz_to_camera=cm.tolist(), neutral=[.45, 1, .7], daylight_neutral=[.5, 1, .85])
    light = scene(); source = rawcolor.CameraSource(scanned(light), info)
    plain = rawcolor.develop_camera(source, normalized({}))
    assert np.abs(plain-np.asarray(source)).max() > .01                                     # an ordinary RAW gets the camera's colour transform
    untouched = rawcolor.develop_camera(source, normalized({'negative': True}))
    assert np.array_equal(untouched, np.asarray(source))                                    # a negative does not
    s = negative.converted(source, normalized({}))
    np.testing.assert_allclose(s['negative_base'], BASE, rtol=.05)
    was = native_gpu.enabled()
    try:
        native_gpu.set_enabled(False)
        cache = DevelopmentCache(64*1024*1024)
        out = develop(source, s, cache=cache)
        np.testing.assert_allclose(out, develop(scanned(light), s), atol=1e-6)              # the same as the bare scan values
        assert np.abs(develop(source, {**s, 'negative': False}, cache=cache)-develop(source, {**s, 'negative': False})).max() < 1e-6
        assert np.abs(develop(source, s, cache=cache)-out).max() < 1e-6                     # the cache tells the two apart
    finally:
        native_gpu.set_enabled(was)


def test_develop_starts_from_the_positive_with_and_without_cache_and_gpu():
    light = scene(); frame = scanned(light)
    was = native_gpu.enabled()
    try:
        native_gpu.set_enabled(False)
        expected = develop(light, normalized(defaults())); converted = develop(frame, settings())
        assert np.abs(converted-expected).max() < 4e-3
        edited = settings(exposure=.5, shadows=30, contrast=10)
        reference = develop(frame, edited)
        assert np.abs(reference-develop(light, normalized({**defaults(), 'exposure': .5, 'shadows': 30, 'contrast': 10}))).max() < 6e-3
        cache = DevelopmentCache(64*1024*1024)
        assert np.abs(develop(frame, edited, cache=cache)-reference).max() < 1e-6
        assert np.abs(develop(frame, {**edited, 'negative_density': .5}, cache=cache)-develop(frame, {**edited, 'negative_density': .5})).max() < 1e-6
        assert np.abs(develop(frame, {**edited, 'negative': False}, cache=cache)-develop(frame, {**edited, 'negative': False})).max() < 1e-6
        if native_gpu.available():
            native_gpu.set_enabled(True)
            assert np.abs(develop(frame, edited, cache=DevelopmentCache(64*1024*1024))-reference).max() < 2e-3
    finally:
        native_gpu.set_enabled(was)


def test_auto_tone_and_export_use_the_positive(tmp_path):
    light = scene(200, 300); frame = scanned(light)
    tone, report = auto_tone_settings(frame, settings())
    plain, _ = auto_tone_settings(light, normalized(defaults()))
    assert report['status'] == 'applied' and tone['exposure'] == pytest.approx(plain['exposure'], abs=.05)
    import tifffile
    source = tmp_path/'negative.tif'; tifffile.imwrite(source, np.uint16(np.clip(to_srgb(frame), 0, 1)*65535+.5), photometric='rgb')
    from luma.engine import load_image
    loaded = load_image(source)[0]
    s = negative.converted(loaded, normalized(defaults()))
    out = tmp_path/'positive.tif'; export_image(source, out, s, 'TIFF 16-bit')
    result = tifffile.imread(out).astype(np.float32)/65535
    assert result[100, 20].mean() < result[100, -20].mean() - .3                           # dark to bright as in the scene
    assert abs(float(result[150, 150, 0])-float(result[150, 150, 2])) < .03              # neutral: no orange cast left


def test_button_inverts_measures_once_and_undoes(window, tmp_path):
    w = window; frame = scanned(scene(200, 300))
    path = tmp_path/'scan.png'; Image.fromarray(np.uint8(np.clip(to_srgb(frame), 0, 1)*255+.5)).save(path)
    ident = w.catalog.add(path); w.activate(ident); wait(lambda: w.source is not None); idle(w)
    assert not w.negative_button.isChecked() and w.negative_panel.isHidden()
    before = w.view.on_screen.copy(); history = len(w.catalog.histories(ident))
    w.negative_button.click(); idle(w)
    s = w.settings
    assert s['negative'] and s['negative_base'] and s['negative_peak'] and not w.negative_panel.isHidden()
    assert w.catalog.photo(ident)['settings']['negative'] and len(w.catalog.histories(ident)) == history+1
    shown = w.view.on_screen
    assert shown[100, 10].mean() < shown[100, -10].mean() and before[100, 10].mean() > before[100, -10].mean()
    np.testing.assert_allclose(shown, develop(w.source, s), atol=2e-3)
    measured = (s['negative_base'], s['negative_peak']); first = s['negative_density']
    w.adjustments['negative_density'].changed.emit('negative_density', first+.8); w.finish_interaction(); idle(w)
    assert w.settings['negative_density'] == first+.8 and w.view.on_screen.mean() > shown.mean()
    w.negative_button.click(); idle(w)                                                    # off: the scan again
    assert not w.settings['negative'] and w.negative_panel.isHidden()
    np.testing.assert_allclose(w.view.on_screen, before, atol=2e-3)
    w.negative_button.click(); idle(w)                                                    # on again: the first measurement is kept
    assert (w.settings['negative_base'], w.settings['negative_peak']) == measured and w.settings['negative_density'] == first+.8
    w.settings['negative_base'] = [v*.5 for v in measured[0]]; w.measure_negative_again(); idle(w)
    assert w.settings['negative_base'] == measured[0] and w.settings['negative_density'] == first      # measured again, density too
    for _ in range(3):w.undo(); idle(w)                                                  # the re-measure, the changed base, switching on
    assert not w.settings['negative'] and not w.negative_button.isChecked() and w.negative_panel.isHidden()


def test_film_base_can_be_picked_on_the_photo(window, tmp_path):
    w = window; frame = scanned(scene(200, 300)); frame[:, :12] = np.float32(BASE)*np.float32(1.3)     # clear film at the left edge, outside the analysed area
    path = tmp_path/'strip.png'; Image.fromarray(np.uint8(np.clip(to_srgb(frame), 0, 1)*255+.5)).save(path)
    ident = w.catalog.add(path); w.activate(ident); wait(lambda: w.source is not None); idle(w)
    w.negative_base_button.click(); assert not w.negative_base_button.isChecked()         # nothing to pick before the conversion
    w.negative_button.click(); idle(w)
    automatic = list(w.settings['negative_base'])
    w.negative_base_button.click(); assert w.view.sample_mode and w.negative_base_button.isChecked()
    w.sample_wb(.02, .5); idle(w)
    assert not w.view.sample_mode and not w.negative_base_button.isChecked()
    picked = w.settings['negative_base']
    assert picked != automatic and picked[0] == pytest.approx(float(w.source[100, 6, 0]), rel=.02)
    w.negative_base_button.click(); w.sample_wb(.95, .5); idle(w)                          # the densest corner is refused
    assert w.settings['negative_base'] == picked


def test_film_base_can_be_picked_with_a_rectangle(window, tmp_path):
    w = window; frame = scanned(scene(200, 300)); frame[:, :12] = np.float32(BASE)*np.float32(1.3)
    path = tmp_path/'strip.png'; Image.fromarray(np.uint8(np.clip(to_srgb(frame), 0, 1)*255+.5)).save(path)
    ident = w.catalog.add(path); w.activate(ident); wait(lambda: w.source is not None); idle(w)
    w.negative_button.click(); idle(w)
    automatic = list(w.settings['negative_base'])
    w.negative_base_button.click(); assert w.view.sample_mode
    w.sample_wb_area(0., .3, .035, .7); idle(w)                                              # dragged over the clear film at the edge
    assert not w.view.sample_mode and not w.negative_base_button.isChecked()
    picked = w.settings['negative_base']
    assert picked != automatic and picked[0] == pytest.approx(float(w.source[100, 6, 0]), rel=.02)
    w.negative_base_button.click(); w.sample_wb_area(.9, .3, 1., .7); idle(w)                 # the densest corner is refused, as for a click
    assert w.settings['negative_base'] == picked


def test_selected_photos_are_inverted_each_on_its_own(window, tmp_path):
    w = window; ids = []
    for index, gain in enumerate((1., .5)):
        frame = scanned(scene(120, 180))*np.float32(gain)
        path = tmp_path/f'roll-{index}.png'; Image.fromarray(np.uint8(np.clip(to_srgb(frame), 0, 1)*255+.5)).save(path)
        ids.append(w.catalog.add(path))
    w.refresh_lists(); w.activate(ids[0]); wait(lambda: w.source is not None); idle(w)
    w.select_ids(ids) if hasattr(w, 'select_ids') else None
    records = [w.catalog.photo(i) for i in ids]
    values = [negative.converted(np.asarray(Image.open(r['path']).convert('RGB'), np.float32)/255, normalized(r['settings'])) for r in records]
    assert all(v['negative'] for v in values) and values[0]['negative_base'] != values[1]['negative_base']
    assert hasattr(w.workflow, 'invert_negatives')


def uneven():
    """A frame of the same roll without black or white of its own: grey between a bright orange and a dark blue."""
    light = np.full((120, 180, 3), .18, np.float32)
    light[:, :60] = np.float32([.6, .25, .05]); light[:, 120:] = np.float32([.01, .02, .05])
    return light


def test_photos_of_one_roll_share_one_measurement():
    full, part = scanned(scene()), scanned(uneven()); plain = normalized(defaults())
    alone = negative.converted(part, plain)                                               # its orange is taken for white, its blue for clear film
    grey = negative.apply(part, alone)[60, 90]
    assert max(grey)/min(grey) > 1.15
    samples = [negative.sample(full, plain), negative.sample(part, plain)]
    first, second = negative.converted_together(samples, [plain, plain])
    assert first['negative_base'] == second['negative_base'] and first['negative_peak'] == second['negative_peak']
    assert first['negative_base'] == pytest.approx(negative.converted(full, plain)['negative_base'], rel=.02)      # the roll's film base
    assert second['negative'] and first['negative_density'] != second['negative_density']                         # brightness photo by photo
    grey = negative.apply(part, second)[60, 90]
    assert max(grey)/min(grey) < 1.01                                                     # grey is grey in every frame of the roll
    assert grey[1] == pytest.approx(.18*2**second['negative_density'], rel=.03)


def test_a_photo_adds_a_bounded_sample_from_inside_its_crop():
    frame = np.ones((400, 650, 3), np.float32); frame[50:350, 100:550] = scanned(scene(300, 450))      # a light table around the film
    whole = negative.sample(frame, normalized({}))
    assert whole.shape[1] == 3 and len(whole) <= negative.SAMPLE and whole.max() == 1.
    cropped = negative.sample(frame, normalized({'crop': [100/650, 50/400, 550/650, 350/400]}))
    assert len(cropped) > 1000 and cropped.max() < .7                                     # the bare light is outside
    large = negative.sample(np.full((900, 1300, 3), .2, np.float32), normalized({}))
    assert negative.SAMPLE/4 < len(large) <= negative.SAMPLE


def test_menu_converts_the_selected_roll_alike(window, tmp_path, monkeypatch):
    w = window; ids = []
    for index, light in enumerate((scene(), uneven())):
        path = tmp_path/f'roll-{index}.png'; Image.fromarray(np.uint8(np.clip(to_srgb(scanned(light)), 0, 1)*255+.5)).save(path)
        ids.append(w.catalog.add(path))
    w.refresh_lists(); w.activate(ids[1]); wait(lambda: w.source is not None); idle(w)
    w.workflow.invert_roll()                                                              # one photo: nothing to share
    assert not w.catalog.photo(ids[1])['settings']['negative']
    monkeypatch.setattr(w, 'selected_ids', lambda: ids)
    history = [len(w.catalog.histories(i)) for i in ids]
    w.workflow.invert_roll(); idle(w)
    first, second = [w.catalog.photo(i)['settings'] for i in ids]
    assert first['negative'] and second['negative'] and first['negative_base'] == second['negative_base'] and first['negative_peak'] == second['negative_peak']
    assert [len(w.catalog.histories(i)) for i in ids] == [n+1 for n in history] and w.catalog.histories(ids[1])[0]['label'] == '네거티브 반전 (한 롤)'
    assert w.settings['negative'] and w.negative_button.isChecked()                       # the open photo follows
    shown = w.view.on_screen[60, 90, :3]
    assert max(shown)-min(shown) < .02


def test_copying_edits_to_other_photos_carries_the_conversion(window, tmp_path, monkeypatch):
    from luma.manager import EDIT_GROUPS
    assert set(EDIT_GROUPS['네거티브 반전']) == set(negative.KEYS)                       # its own entry among the items to copy
    w = window; ids = []
    for index, light in enumerate((scene(), uneven())):
        path = tmp_path/f'strip-{index}.png'; Image.fromarray(np.uint8(np.clip(to_srgb(scanned(light)), 0, 1)*255+.5)).save(path)
        ids.append(w.catalog.add(path))
    w.refresh_lists(); w.activate(ids[0]); wait(lambda: w.source is not None); idle(w)
    w.negative_button.click(); idle(w)
    w.manager.copy_keys = {key for keys in EDIT_GROUPS.values() for key in keys}             # as after choosing the items to copy
    monkeypatch.setattr(w, 'selected_ids', lambda: ids)
    w.manager.sync_selected()
    first, second = [w.catalog.photo(i)['settings'] for i in ids]
    assert second['negative'] and all(second[key] == first[key] for key in negative.KEYS)
