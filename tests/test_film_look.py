import json
import shutil

import numpy as np
import pytest

from luma import engine, film_look

NM = np.arange(380, 781, 5)


def gauss(centre, width):
    return [[float(x), float(np.exp(-.5*((x - centre)/width)**2))] for x in NM]


def log_sensitivity(centre, width):
    return [[float(x), float(2.5 - .5*((x - centre)/width)**2)] for x in NM if abs(x - centre) < 2.2*width]


SENSITIVITY = {'r': log_sensitivity(650, 30), 'g': log_sensitivity(545, 30), 'b': log_sensitivity(450, 30)}
NEGATIVE = {'id': 'test-negative', 'name': 'Test Negative 400', 'maker': 'Test', 'kind': 'colour-negative', 'iso': 400,
            'log_h_ref': -1.44, 'balance': 'daylight', 'print': 'test-paper', 'sensitivity': SENSITIVITY,
            'curves': {k: [[float(x), float(base + .6*np.log10(1 + 10**((x + 2.7)/.6)))] for x in np.arange(-4.5, 1.5, .05)]
                       for k, base in (('r', .2), ('g', .65), ('b', .85))},
            'grain': {'grain': 17., 'grain_size': 32., 'grain_roughness': 50.}}
PAPER = {'id': 'test-paper', 'name': 'Test Paper', 'kind': 'colour-paper', 'view': 'D50',
         'curves': {k: [[float(x), float(.1 + 2.3/(1 + np.exp(-(x - 1)*4.5)))] for x in np.arange(-1, 3, .02)] for k in 'rgb'},
         'dyes': {'c': gauss(660, 45), 'm': gauss(545, 35), 'y': gauss(445, 30)}}
SLIDE = {'id': 'test-slide', 'name': 'Test Slide 100', 'maker': 'Test', 'kind': 'colour-slide', 'iso': 100, 'log_h_ref': -1.2,
         'balance': 'daylight', 'view': 'D50', 'sensitivity': SENSITIVITY,
         'curves': {k: [[float(x), float(.15 + 3.4/(1 + np.exp((x + 1.2)*3.2)))] for x in np.arange(-4, 1.5, .05)] for k in 'rgb'},
         'dyes': {'c': gauss(650, 50), 'm': gauss(550, 38), 'y': gauss(450, 32)}}
BW_PAPER = {'id': 'test-bw-paper', 'name': 'Test BW Paper', 'kind': 'bw-paper',
            'curves': {'v': [[float(x), float(.05 + 2.1/(1 + np.exp(-(x - 1)*4)))] for x in np.arange(-1, 3, .02)]}}
BW = {'id': 'test-bw', 'name': 'Test BW 400', 'maker': 'Test', 'kind': 'bw-negative', 'iso': 400, 'log_h_ref': -1.44,
      'print': 'test-bw-paper', 'curves': {'v': NEGATIVE['curves']['g']}}


@pytest.fixture
def films(tmp_path, monkeypatch):
    """film_look reading a folder of made-up, datasheet-shaped stocks."""
    folder = tmp_path/'film'
    (folder/'stocks').mkdir(parents=True)
    (folder/'papers').mkdir()
    shutil.copy(film_look.FOLDER/'colorimetry.json', folder/'colorimetry.json')
    for data in (NEGATIVE, SLIDE, BW):
        (folder/'stocks'/f"{data['id']}.json").write_text(json.dumps(data), 'utf-8')
    for data in (PAPER, BW_PAPER):
        (folder/'papers'/f"{data['id']}.json").write_text(json.dumps(data), 'utf-8')
    index = [{'id': d['id'], 'name': d['name'], 'maker': 'Test', 'kind': d['kind'], 'group': d['kind'], 'kind_title': '컬러 네거티브',
              'iso': d['iso'], 'print_name': None, 'grain': d.get('grain')} for d in (NEGATIVE, SLIDE, BW)]
    (folder/'index.json').write_text(json.dumps(index), 'utf-8')
    monkeypatch.setattr(film_look, 'FOLDER', folder)
    film_look.catalog.cache_clear();film_look._stocks.clear()
    yield folder
    film_look.catalog.cache_clear();film_look._stocks.clear()


def to_rgb(xyz):
    return xyz @ film_look.XYZ_SRGB.T


def test_a_mid_grey_prints_as_a_neutral_print_grey(films):
    for name in ('test-negative', 'test-bw'):
        rgb = to_rgb(film_look.stock(name).xyz(np.full((1, 3), .18, np.float32)))[0]
        assert np.allclose(rgb, film_look.PRINT_GREY, atol=2e-3), (name, rgb)


def test_tone_rises_with_exposure_and_stays_within_the_print(films):
    ramp = np.repeat(np.geomspace(1e-4, 40, 200)[:, None], 3, axis=1).astype(np.float32)
    for name in ('test-negative', 'test-slide', 'test-bw'):
        y = film_look.stock(name).xyz(ramp)[:, 1]
        assert np.all(np.diff(y) >= -1e-6) and y[0] < .03 and .5 < y[-1] <= 1.0001, name


def test_the_table_reproduces_the_spectral_computation(films):
    """The density table is only a cache: its colours match the direct spectral computation."""
    film = film_look.stock('test-negative')
    colours = np.random.default_rng(3).random((500, 3)).astype(np.float32)*2
    densities = film.densities(colours)
    exact = film.paper_dyes.colour(densities.astype(float))
    lab = lambda xyz: np.cbrt(np.maximum(xyz, 1e-6))          # perceptual-ish spacing
    assert np.abs(lab(film.xyz(colours)) - lab(exact)).max() < .01


def test_develop_strength_zero_is_no_film_and_half_is_between(films):
    frame = np.random.default_rng(1).random((24, 32, 3)).astype(np.float32)*.8
    plain = engine.develop(frame, {})
    full = engine.develop(frame, {'film_look': 'test-negative'})
    half = engine.develop(frame, {'film_look': 'test-negative', 'film_strength': 50})
    assert np.array_equal(engine.develop(frame, {'film_look': 'test-negative', 'film_strength': 0}), plain)
    assert np.array_equal(engine.develop(frame, {'film_look': 'no-such-film'}), plain)
    assert not np.allclose(full, plain, atol=1e-3)
    assert np.allclose(half, (plain + full)/2, atol=2e-3)


def test_prophoto_working_space_renders_the_same_grey(films):
    grey = np.full((4, 4, 3), .18, np.float32)
    srgb = film_look.render(grey, 'test-negative', 'sRGB')
    prophoto = film_look.render(grey, 'test-negative', 'ProPhoto')
    assert np.allclose(srgb, film_look.PRINT_GREY, atol=2e-3) and np.allclose(prophoto, film_look.PRINT_GREY, atol=2e-3)


def test_film_looks_run_on_the_cpu_and_key_the_tone_stage(films):
    from luma import native_gpu
    s = engine.normalized({'film_look': 'test-negative'})
    assert native_gpu._tone_params((8, 8, 3), s) is None
    assert {'film_look', 'film_strength'} <= set(engine.TONE_KEYS)


def test_grain_comes_from_the_catalog(films):
    assert film_look.grain_settings('test-negative') == {'grain': 17., 'grain_size': 32., 'grain_roughness': 50.}
    assert film_look.grain_settings('') is None and film_look.grain_settings('no-such-film') is None


# The shipped stocks (assets/film): every one in the list builds, keeps a grey scale grey (negatives, printed) or
# brightens with exposure (slides), and has a document behind it.
SHIPPED = [e['id'] for e in film_look.catalog()]


@pytest.mark.parametrize('name', SHIPPED)
def test_shipped_stock(name):
    film = film_look.stock(name)
    data = film.data
    assert data['name'] and data['source'].get('sha256') or data.get('derived_from')
    ramp = np.repeat(np.geomspace(.005, 4, 40)[:, None], 3, axis=1).astype(np.float32)
    xyz = film.xyz(ramp)
    assert np.all(np.isfinite(xyz)) and np.all(np.diff(xyz[:, 1]) >= -2e-3)
    grey = to_rgb(film.xyz(np.full((1, 3), .18, np.float32)))[0]
    assert np.allclose(grey.mean(), film_look.PRINT_GREY, atol=.02)
    if data['kind'] != 'colour-slide':
        assert np.ptp(grey)/grey.mean() < .02                # printer lights make a mid grey neutral


def test_ai_programs_choose_a_film_by_name_and_it_brings_its_grain():
    from luma import photo_service
    from luma.photo_service import ServiceError
    name = film_look.catalog()[0]
    changes, _ = photo_service.parse_changes({'film_look': name['name'].lower()})
    assert changes == {'film_look': name['id']}
    with pytest.raises(ServiceError, match='close'):
        photo_service.parse_changes({'film_look': name['name'][:-1] + 'zz'})
    with pytest.raises(ServiceError):
        photo_service.parse_changes(None, {'film_look': 'x'})
    settings = photo_service.edited(engine.defaults(), changes, {'film_look': 'set'}, {}, 1)
    assert settings['film_look'] == name['id'] and settings['grain'] == (name['grain'] or {}).get('grain', 0)
    off = photo_service.edited(settings, {'film_look': ''}, {'film_look': 'set'}, {}, 1)
    assert off['film_look'] == '' and off['grain'] == 0
    own = photo_service.edited(engine.defaults(), {'film_look': name['id'], 'grain': 5.}, {'film_look': 'set', 'grain': 'set'}, {}, 1)
    assert own['grain'] == 5.                                  # a grain given with the film stays
