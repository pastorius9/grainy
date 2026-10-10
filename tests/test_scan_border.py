import numpy as np
import pytest
from PIL import Image
from luma.scan_border import detect
from test_studio_ui import app, wait
from luma.app import MainWindow


def scene(h=400, w=900, seed=3):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:h, :w].astype(np.float32)
    return np.clip(.45 + .25*np.sin(x/37)*np.cos(y/23) + rng.normal(0, .05, (h, w)), 0, 1).astype(np.float32)


def bordered(left=40, right=30, top=0, bottom=0):
    a = scene()
    h, w = a.shape
    for sl in (np.s_[:, :left], np.s_[:, w-right:], np.s_[:top, :], np.s_[h-bottom:, :]):
        a[sl] = .02 + np.random.default_rng(1).random(a[sl].shape) * .03
    a[5, 10] = .8   # dust on the film edge
    # A soft three-pixel film edge.
    for i, value in enumerate((.06, .15, .3)):
        if left:a[:, left+i] = np.minimum(a[:, left+i], value)
    return a


def test_film_edges_are_cropped_just_inside_the_soft_edge():
    crop, kinds = detect(bordered())
    assert kinds == {'left': 'dark', 'right': 'dark'}
    x0, y0, x1, y1 = crop
    assert 43/900 <= x0 <= 70/900 and 1-60/900 <= x1 <= 1-30/900 and y0 == 0 and y1 == 1
    crop, kinds = detect(np.repeat(bordered(0, 0, 25, 25)[..., None], 3, axis=2))
    assert set(kinds) == {'top', 'bottom'} and crop[0] == 0 and crop[2] == 1


@pytest.mark.parametrize('name', ['clean', 'dark photo', 'bright sky', 'wide dark area', 'dark inside'])
def test_pictures_without_a_film_edge_keep_their_frame(name):
    a = scene()
    if name == 'dark photo':a = a*.12
    if name == 'bright sky':a[:120] = .97
    if name == 'wide dark area':a[:, :200] = .02   # a window frame, 22% of the width
    if name == 'dark inside':a[:, :40] = .02;a[:, 40:120] = .06
    assert detect(a) == (None, {})


def test_auto_border_crop_button_sets_an_undoable_crop(app, tmp_path):
    rgb = np.repeat(bordered()[..., None], 3, axis=2)
    path = tmp_path/'scan.png';Image.fromarray(np.uint8(rgb*255)).save(path)
    w = MainWindow(tmp_path/'data');ident = w.catalog.add(path);w.refresh_lists();w.activate(ident);w.show()
    try:
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        assert w.settings['crop'] is None
        w.auto_border_crop();wait(lambda: w.border_crop_button.isEnabled())
        crop = w.settings['crop']
        assert crop is not None and 40/900 < crop[0] < 80/900 and crop[2] < 1
        assert w.catalog.photo(ident)['settings']['crop'] == crop
        w.undo();assert w.settings['crop'] is None
    finally:
        wait(lambda: not w.render_running);w.close()


def test_batch_border_crop_changes_only_photos_with_a_film_edge(app, tmp_path):
    scan = tmp_path/'scan.png';Image.fromarray(np.uint8(np.repeat(bordered()[..., None], 3, axis=2)*255)).save(scan)
    clean = tmp_path/'clean.png';Image.fromarray(np.uint8(np.repeat(scene()[..., None], 3, axis=2)*255)).save(clean)
    w = MainWindow(tmp_path/'data');a = w.catalog.add(scan);b = w.catalog.add(clean)
    w.refresh_lists();w.activate(a);w.show()
    try:
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        w.filmstrip.restore_selection([a, b], a)
        w.workflow.border_crop();wait(lambda: w.workflow.cancel is None)
        assert w.catalog.photo(a)['settings']['crop'] is not None
        assert w.catalog.photo(b)['settings']['crop'] is None
        assert w.catalog.photo(a)['settings']['crop'] == w.settings['crop']   # the open photo follows
    finally:
        wait(lambda: not w.render_running);w.close()


def test_batch_auto_tone_matches_single_photo_auto_tone(app, tmp_path):
    from luma.engine import auto_tone_settings, load_image
    paths = []
    for i, (low, high) in enumerate(((.2, .6), (.35, .8))):
        rgb = np.repeat((low + (high-low)*scene(seed=i))[..., None], 3, axis=2)
        paths.append(tmp_path/f'p{i}.png');Image.fromarray(np.uint8(rgb*255)).save(paths[-1])
    w = MainWindow(tmp_path/'data');ids = [w.catalog.add(p) for p in paths]
    w.refresh_lists();w.activate(ids[0]);w.show()
    try:
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        w.filmstrip.restore_selection(ids, ids[0])
        w.workflow.auto_tone()
        for ident, path in zip(ids, paths):
            settings = w.catalog.photo(ident)['settings']
            source, _ = load_image(path, 720)
            expected = auto_tone_settings(source, w.catalog.photo(ident)['settings'] | {'exposure': 0})[0]
            assert settings['exposure'] == pytest.approx(expected['exposure'], abs=1e-6)
            assert settings['blacks'] == pytest.approx(expected['blacks'], abs=1e-4)
        w.undo();assert w.settings['exposure'] == 0
    finally:
        wait(lambda: not w.render_running);w.close()
