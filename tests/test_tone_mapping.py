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


LOOK_SETTINGS = [dict(zip(tone_mapping.KEYS, look)) for look in tone_mapping.LOOKS.values()] +     [dict.fromkeys(tone_mapping.KEYS, 100.), dict.fromkeys(tone_mapping.KEYS, -100.)]


def test_the_look_settings_at_zero_are_the_original_rendering():
    img = (np.random.default_rng(0).random((32, 32, 3))**3*20).astype(np.float32)
    assert np.array_equal(tone_mapping.render(img), tone_mapping.render(img, dict.fromkeys(tone_mapping.KEYS, 0.)))
    assert tone_mapping.params({}).s == tone_mapping.S0 and tone_mapping.params({}).p == tone_mapping.CONTRAST
    assert tone_mapping.look_of({}) == '표준' and tone_mapping.look_of({'drt_toe': 5.}) == ''


def test_every_look_keeps_mid_grey_and_rises_to_white():
    x = np.geomspace(1e-5, 1e5, 2000).astype(np.float32)
    for settings in LOOK_SETTINGS:
        prm = tone_mapping.params(settings)
        y = tone_mapping.curve(x, prm)
        assert abs(float(tone_mapping.curve(np.float32(.18), prm)) - .18) < 1e-5, settings
        assert np.all(np.diff(y) > -1e-6) and y.max() <= 1 and y[-1] > .99, settings


def test_toe_shoulder_and_white_start_move_their_own_end_of_the_curve():
    base = tone_mapping.params({})
    curve = lambda value, **settings: float(tone_mapping.curve(np.float32(value), tone_mapping.params(settings)))
    assert curve(.01, drt_toe=100.) < curve(.01) < curve(.01, drt_toe=-100.)          # deeper / lifted blacks
    assert curve(0., drt_toe=-100.) > .02 and curve(0., drt_toe=100.) == 0
    assert curve(.18*2**6, drt_shoulder=100.) == 1 > curve(.18*2**6)                   # white reached at +6 stops
    assert curve(.18*2**4, drt_shoulder=-100.) < curve(.18*2**4)                       # a longer shoulder
    assert abs(curve(.18*2**-3, drt_shoulder=-100.) - curve(.18*2**-3)) < 1e-6         # shadows untouched by it
    assert curve(.18*8, drt_contrast=100.) > curve(.18*8) and curve(.18/8, drt_contrast=100.) < curve(.18/8)
    assert tone_mapping.params({'drt_white': -100.}).white < base.white < tone_mapping.params({'drt_white': 100.}).white
    black = tone_mapping.render(np.zeros((1, 1, 3), np.float32), {'drt_toe': -100.})[0, 0]
    assert np.ptp(black) < 1e-6 and black[0] > .02                                     # a lifted black is neutral grey


def test_the_look_settings_change_only_the_drt():
    frame = np.random.default_rng(3).random((20, 30, 3)).astype(np.float32)*3
    punchy = dict(zip(tone_mapping.KEYS, tone_mapping.LOOKS['대비 강하게']))
    assert np.array_equal(engine.develop(frame, punchy), engine.develop(frame, {}))     # classic: ignored
    assert not np.allclose(engine.develop(frame, {'tone_mapping': 'drt', **punchy}), engine.develop(frame, {'tone_mapping': 'drt'}), atol=1e-3)
    assert set(tone_mapping.KEYS) <= set(engine.TONE_KEYS) and all(engine.defaults()[k] == 0 for k in tone_mapping.KEYS)


def test_look_menu_and_sliders_in_the_basic_panel(window):
    w = window
    assert not w.drt_panel.isVisibleTo(w)
    w.tone_mapping.setCurrentIndex(w.tone_mapping.findData('drt')); wait(lambda: w.settings['tone_mapping'] == 'drt')
    assert w.drt_panel.isVisibleTo(w) and w.drt_look.currentData() == '표준'
    history = len(w.catalog.histories(w.current_id))
    w.drt_look.setCurrentIndex(w.drt_look.findData('필름처럼'))
    look = dict(zip(tone_mapping.KEYS, tone_mapping.LOOKS['필름처럼']))
    assert all(w.settings[k] == v for k, v in look.items()) and len(w.catalog.histories(w.current_id)) == history + 1
    assert all(w.adjustments[k].value.value() == v for k, v in look.items())
    w.undo(); wait(lambda: w.settings['drt_toe'] == 0)
    assert w.drt_look.currentData() == '표준'
    w.adjustments['drt_toe'].slider.setValue(40); w.adjustments['drt_toe'].committed.emit()
    assert w.settings['drt_toe'] == 40 and w.drt_look.currentData() == ''                # custom
    w.drt_advanced.click(); assert w.drt_sliders.isHidden() is not w.drt_advanced.isChecked()
    w.tone_mapping.setCurrentIndex(w.tone_mapping.findData('classic')); wait(lambda: w.settings['tone_mapping'] == 'classic')
    assert not w.drt_panel.isVisibleTo(w)
