from pathlib import Path
from threading import Event
from PIL import Image
import numpy as np
from PySide6.QtCore import QTimer
from test_studio_ui import app,window,wait


def add(w,name,color):
    path=w.catalog.directory.parent/name;Image.new('RGB',(180,120),color).save(path);return w.catalog.add(path)


def test_switch_back_uses_same_pixels_and_detects_file_change(window,monkeypatch):
    import luma.app as module
    w=window;first=w.current_id;original=w.view.on_screen.copy();second=add(w,'second.png',(40,160,70));calls=[];real=module.load_image
    def read(*a,**k):calls.append(str(a[0]));return real(*a,**k)
    monkeypatch.setattr(module,'load_image',read)
    for ident in (second,first,second,first):
        w.activate(ident);wait(lambda:w.source is not None and not w.render_running and not w.jobs)
    assert len(calls)==1 and w.source_cache.hits>=3
    np.testing.assert_array_equal(w.view.on_screen,original)
    path=Path(w.catalog.photo(first)['path']);Image.new('RGB',(181,120),(200,30,30)).save(path)
    w.activate(second);wait(lambda:w.source is not None and not w.render_running)
    w.activate(first);wait(lambda:w.source is not None and not w.render_running)
    assert w.original_size==(181,120) and w.view.on_screen[30,40,0]>.7 and len(calls)==2


def test_obsolete_queued_reads_skip_and_latest_photo_renders_while_other_jobs_block(window,monkeypatch):
    import luma.app as module
    w=window;ids=[add(w,f'{i}.png',(40+i*30,70,90)) for i in range(5)]
    w.source_pool.setMaxThreadCount(1);entered=Event();release=Event();other_release=Event();calls=[];real=module.load_image;ticks=[]
    def read(*a,**k):
        calls.append(str(a[0]))
        if len(calls)==1:entered.set();assert release.wait(8)
        return real(*a,**k)
    monkeypatch.setattr(module,'load_image',read)
    timer=QTimer();timer.setInterval(10);timer.timeout.connect(lambda:ticks.append(1));timer.start()
    w.spawn(lambda:other_release.wait(8),lambda _:None);w.spawn(lambda:other_release.wait(8),lambda _:None)
    try:
        w.activate(ids[0]);wait(entered.is_set)
        for ident in ids[1:]:w.activate(ident)
        wait(lambda:len(ticks)>=4);release.set()
        wait(lambda:w.source is not None and not w.render_running)
        assert w.current_id==ids[-1] and len(calls)==2
        assert w.view.on_screen[40,50,0]>.6 and not other_release.is_set()
    finally:release.set();other_release.set();timer.stop();wait(lambda:not w.jobs and not w.render_running)


def test_full_resolution_revisit_reuses_buffer_but_applies_latest_edits(window,monkeypatch):
    import luma.app as module
    from luma.engine import develop
    w=window;first=w.current_id;second=add(w,'full-second.png',(40,160,70));calls=[];real=module.load_image
    def read(*a,**k):calls.append((str(a[0]),a[1] if len(a)>1 else None));return real(*a,**k)
    monkeypatch.setattr(module,'load_image',read)
    w.view.fit_mode=False;w.load_full_resolution()
    def settled():return w.full_source is not None and not w.jobs and not w.render_running and w.presented_version==w.render_version and w.presented_quality=='quality'
    wait(settled);original=w.full_source
    w.set_setting('exposure',.65);w.commit();w.render();wait(settled)
    for ident in (second,first,second,first):w.activate(ident);wait(settled)
    assert len(calls)==3 and w.full_source is original and not original.flags.writeable
    np.testing.assert_array_equal(w.view.on_screen,develop(original,w.settings,original_size=w.original_size))
    assert w.settings['exposure']==.65
    w.activate(first,force=True);wait(settled)
    assert len(calls)==5 and w.full_source is not original
