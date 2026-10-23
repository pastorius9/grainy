import numpy as np

from luma import engine, tone_mapping
from test_studio_ui import app, window, wait


def test_curve_keeps_mid_grey_and_reaches_white_only_at_infinity():
    x = np.geomspace(1e-4, 1e4, 400).astype(np.float32)
    y = tone_mapping.curve(x)
    assert abs(float(tone_mapping.curve(np.float32(.18))) - .18) < 1e-6
    assert np.all(np.diff(y) > 0) and y[-1] < 1 and y[-1] > .99


def test_colours_keep_their_hue_until_near_white_and_no_channel_passes_white():
    rgb = np.array([[[.5, .2, .05], [.05, .3, .6], [40, 16, 4], [400, 400, 400]]], np.float32)
    out = tone_mapping.render(rgb)[0]
    assert np.allclose(out[0]/out[0].max(), rgb[0, 0]/rgb[0, 0].max(), atol=1e-5)      # mid tones: same ratios
    assert np.allclose(out[1]/out[1].max(), rgb[0, 1]/rgb[0, 1].max(), atol=1e-5)
    assert out[2, 0] > out[2, 1] > out[2, 2] and out[2, 2] > .4                         # bright orange: lighter, still orange
    assert out.max() <= 1 and np.ptp(out[3]) < 1e-6


def test_develop_uses_it_only_when_chosen_and_film_looks_win():
    frame = np.random.default_rng(2).random((20, 30, 3)).astype(np.float32)*3
    classic = engine.develop(frame, {})
    drt = engine.develop(frame, {'tone_mapping': 'drt'})
    assert np.array_equal(classic, engine.develop(frame, {'tone_mapping': 'classic'}))
    assert not np.allclose(classic, drt, atol=1e-3)
    grey = np.full((4, 4, 3), .18, np.float32)
    assert np.allclose(engine.develop(grey, {'tone_mapping': 'drt'}), engine.develop(grey, {}), atol=2/255)
    from luma import film_look, native_gpu
    name = film_look.catalog()[0]['id']
    assert np.array_equal(engine.develop(frame, {'film_look': name, 'tone_mapping': 'drt'}), engine.develop(frame, {'film_look': name}))
    assert native_gpu._tone_params((8, 8, 3), engine.normalized({'tone_mapping': 'drt'})) is None
    assert 'tone_mapping' in engine.TONE_KEYS


def test_basic_panel_switch_is_one_undo_step(window):
    w = window
    history = len(w.catalog.histories(w.current_id))
    w.tone_mapping.setCurrentIndex(w.tone_mapping.findData('drt'))
    wait(lambda: w.settings['tone_mapping'] == 'drt')
    assert len(w.catalog.histories(w.current_id)) == history + 1
    w.undo()
    wait(lambda: w.settings['tone_mapping'] == 'classic')
    assert w.tone_mapping.currentData() == 'classic'
