import time
from threading import Event
import numpy as np
from PIL import Image
import pytest
from luma import preview_store as store,engine
from test_studio_ui import app,window,wait


def settled(w):
    return not w.render_running and not w.preview_queue.running and not w.preview_queue.visible and not w.photo_model.pending_thumbs and not w.thumbnail_pool.activeThreadCount()


def test_edit_undo_and_periodic_file_change_update_visible_thumbnail(window):
    w=window;ident=w.current_id;directory=w.catalog.directory
    wait(lambda:not w.jobs and not store.inspect(directory,ident)['rebuild'])
    original=store.files(directory,ident)[0].read_bytes()
    w.set_setting('exposure',1);w.commit();w.render()
    wait(lambda:not w.jobs and not store.inspect(directory,ident)['rebuild'])
    assert store.files(directory,ident)[0].read_bytes()!=original
    w.undo()
    wait(lambda:not w.jobs and not store.inspect(directory,ident)['rebuild'])
    assert store.files(directory,ident)[0].read_bytes()==original
    # No catalog edit: the next visible refresh detects an external source change.
    Image.new('RGB',(180,120),(40,70,150)).save(w.catalog.photo(ident)['path'])
    w.photo_model.thumb_checked[ident]=0;w.photo_model.thumbnail(ident)
    wait(lambda:not store.inspect(directory,ident)['rebuild'])
    assert store.files(directory,ident)[0].read_bytes()!=original
    pixel=np.asarray(store.inspect(directory,ident)['image'])[40,50]
    np.testing.assert_allclose(pixel,[40,70,150],atol=2)


def test_cancel_and_resume_batch_preserve_completed_work_without_full_catalog_reads(window,tmp_path,monkeypatch):
    w=window;wait(lambda:settled(w));ids=[]
    # Keep these off screen, so automatic visible work cannot mask batch cancellation.
    for n in range(3):
        path=tmp_path/f'batch-{n}.png';Image.new('RGB',(90,60),(80+n*20,100,140)).save(path)
        ids.append(w.catalog.add(path))
    native=store.rebuild;started=Event();release=Event()
    def paused(directory,ident,cancel=None,**kwargs):
        if ident==ids[1]:started.set();assert release.wait(10)
        return native(directory,ident,cancel,**kwargs)
    monkeypatch.setattr(store,'rebuild',paused)
    monkeypatch.setattr(w.catalog,'photos',lambda:pytest.fail('batch eagerly read the catalog'))
    w.preview_queue.start_batch(ids)
    try:
        wait(started.is_set)
        assert not store.inspect(w.catalog.directory,ids[0])['rebuild']
        w.preview_cancel_button.click();release.set();wait(lambda:not w.preview_queue.running)
        assert store.inspect(w.catalog.directory,ids[1])['rebuild']
        assert store.inspect(w.catalog.directory,ids[2])['rebuild']
        assert not w.preview_cancel_button.isVisible()
        monkeypatch.setattr(store,'rebuild',native);w.preview_queue.start_batch(ids)
        wait(lambda:not w.preview_queue.running and not w.preview_queue.batch and not w.preview_queue.total)
        assert all(not store.inspect(w.catalog.directory,i)['rebuild'] for i in ids)
        assert w.preview_queue.completed==3 and w.preview_queue.failures==0
    finally:release.set()


def test_visible_corrupt_source_reports_error_without_retry_storm(window,tmp_path,monkeypatch):
    w=window;wait(lambda:settled(w));path=tmp_path/'corrupt.png';path.write_bytes(b'broken PNG')
    ident=w.catalog.add(path);calls=[];native=store.rebuild
    def tracked(*args,**kwargs):calls.append(args[1]);return native(*args,**kwargs)
    monkeypatch.setattr(store,'rebuild',tracked)
    w.photo_model.thumbnail(ident)
    wait(lambda:ident in w.photo_model.rebuild_errors and not w.preview_queue.running)
    w.photo_model.thumbnail(ident);wait(lambda:ident not in w.photo_model.pending_thumbs)
    assert w.photo_model.thumb_status[ident].startswith('오류') and calls==[ident]
    assert not w.preview_queue.visible


def test_reuses_decoded_buffer_only_when_provenance_matches(window,monkeypatch):
    w=window;wait(lambda:settled(w));ident=w.current_id;calls=[];native=engine.load_image
    def load(*args,**kwargs):calls.append(args[0]);return native(*args,**kwargs)
    monkeypatch.setattr(engine,'load_image',load)
    w.preview_queue.start_batch([ident]);wait(lambda:not w.preview_queue.running and not w.preview_queue.total)
    assert calls==[]
    # A changed file must be decoded even though an older buffer is in the editor.
    Image.new('RGB',(180,120),'blue').save(w.catalog.photo(ident)['path'])
    w.preview_queue.start_batch([ident]);wait(lambda:not w.preview_queue.running and not w.preview_queue.total)
    assert len(calls)==1
