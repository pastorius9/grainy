import time
from PIL import Image
from test_studio_ui import app, window, wait


def settled(w):wait(lambda:w.source is not None and not w.render_running and not w.jobs)


def test_neighbours_are_decoded_before_they_are_opened(window, tmp_path):
    w = window;ids = []
    for n in range(4):
        path = tmp_path/f'next-{n}.jpg';Image.new('RGB', (1600, 1000), (30*n, 80, 140)).save(path, quality=90)
        ids.append(w.catalog.add(path))
    w.refresh_lists();wait(lambda: not w.library_loading)
    w.activate(ids[1]);settled(w)
    cache = w.source_cache
    # Both neighbours of ids[1] end up in the cache without being opened.
    wait(lambda: len(cache.entries) >= 3 and not cache.inflight)
    misses = cache.misses
    w.activate(ids[2]);settled(w)
    wait(lambda: len(cache.entries) >= 4 and not cache.inflight)
    # Opening ids[2] decoded nothing new; the only new decode is its unseen neighbour ids[3].
    assert cache.misses == misses+1 and w.current_id == ids[2]
    # Rapid moves drop stale requests instead of queueing every photo.
    before = cache.misses
    for ident in ids:w.activate(ident)
    settled(w);wait(lambda: not cache.inflight)
    assert cache.misses-before <= 2


def test_closing_stops_prefetch(app, tmp_path):
    from luma.app import MainWindow
    w = MainWindow(tmp_path/'data')
    path = tmp_path/'a.jpg';Image.new('RGB', (800, 600), (1, 2, 3)).save(path)
    ident = w.catalog.add(path);w.refresh_lists();wait(lambda: not w.library_loading)
    w.activate(ident);w.show();settled(w)
    w.close()
    assert w.prefetch_pool._shutdown
