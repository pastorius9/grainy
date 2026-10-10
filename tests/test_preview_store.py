"""Observable cache correctness across edits, races, offline use and restore."""
from contextlib import closing
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import hashlib
import json
import os
import sqlite3
import numpy as np
import pytest
from PIL import Image
from luma.catalog import Catalog
from luma import engine,preview_store as store
from luma.library import restore_backup


@pytest.fixture
def photo(tmp_path):
    y,x=np.mgrid[:80,:120]
    rgb=np.stack([.12+x/300,.15+y/220,np.full_like(x,.35,dtype=float)],-1)
    path=tmp_path/'source.png';Image.fromarray(np.uint8(rgb*255)).save(path)
    cat=Catalog(tmp_path/'library');ident=cat.add(path)
    yield cat,ident,path
    cat.close()


def cache(photo):
    cat,ident,_=photo
    assert store.rebuild(cat.directory,ident)=='갱신 완료'
    assert not store.inspect(cat.directory,ident)['rebuild']
    return store.files(cat.directory,ident)


def test_legacy_cache_rebuilt_with_edits_without_changing_original(photo):
    cat,ident,path=photo;original=path.read_bytes()
    Image.new('RGB',(100,70),'red').save(store.files(cat.directory,ident)[0])
    settings=engine.defaults();settings.update(exposure=.7,temperature=15,crop=[.1,.1,.9,.9])
    cat.edit(ident,settings)
    legacy=store.inspect(cat.directory,ident)
    assert legacy['rebuild'] and legacy['image'] is not None and legacy['status']=='갱신 중'
    jpeg,_=cache(photo)
    source,_=engine.load_image(path,1800,raw_options=settings)
    expected=engine.resize_float(engine.develop(source,settings),320)
    assert np.mean(np.abs(np.asarray(Image.open(jpeg))/255-expected))<.01
    assert path.read_bytes()==original


def test_tokens_track_edits_paths_copies_and_deletes_not_classification(photo,tmp_path):
    cat,ident,path=photo;snap=lambda:store.snapshot(cat.directory,ident)
    initial=snap();cat.update(ident,rating=4,keywords='풍경',label='초록')
    assert snap()['token']==initial['token']
    with closing(sqlite3.connect(cat.directory/'catalog.sqlite')) as db,db:
        db.execute('UPDATE photos SET edits=? WHERE id=?',(json.dumps({'exposure':1}),ident))
    edited=snap();assert edited['token']!=initial['token']
    with closing(sqlite3.connect(cat.directory/'catalog.sqlite')) as db,db:
        db.execute('UPDATE photos SET path=? WHERE id=?',(str(tmp_path/'moved.png'),ident))
    assert snap()['token']!=edited['token']
    other=cat.virtual_copy(ident)
    assert store.snapshot(cat.directory,other)['token']!=snap()['token']
    cat.remove_photos([other]);assert store.snapshot(cat.directory,other) is None
    assert path.is_file()


@pytest.mark.parametrize('change',['edit','path','source','delete'])
def test_stale_worker_cannot_replace_current_thumbnail(photo,tmp_path,change):
    cat,ident,path=photo;jpeg,_=cache(photo);state=store.snapshot(cat.directory,ident)
    before=jpeg.read_bytes();started=Event();release=Event()
    def old_worker():
        started.set();assert release.wait(5)
        return store.publish(cat.directory,state,Image.new('RGB',(120,80),'red'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(old_worker);assert started.wait(5)
        try:
            if change=='edit':cat.edit(ident,{'exposure':1})
            elif change=='path':
                with cat.db:cat.db.execute('UPDATE photos SET path=? WHERE id=?',(str(tmp_path/'other.png'),ident))
            elif change=='source':Image.new('RGB',(123,81),'blue').save(path)
            else:cat.remove_photos([ident])
        finally:release.set()
        assert not future.result(5)
    assert jpeg.read_bytes()==before and not list(jpeg.parent.glob('*.tmp'))


@pytest.mark.parametrize('damage',['jpeg','digest','stamp','missing_stamp'])
def test_damaged_cache_recovers(photo,damage):
    cat,ident,_=photo;jpeg,stamp=cache(photo)
    if damage=='jpeg':jpeg.write_bytes(b'broken')
    elif damage=='digest':Image.new('RGB',(120,80),'purple').save(jpeg)
    elif damage=='stamp':stamp.write_text('[]')
    else:stamp.unlink()
    assert store.inspect(cat.directory,ident)['rebuild']
    cache(photo)


def test_interrupted_publish_is_detected_and_recoverable(photo,monkeypatch):
    cat,ident,_=photo;jpeg,stamp=cache(photo);native=store.os.replace
    def fail_stamp(source,target):
        if target==stamp:raise OSError('simulated full disk')
        return native(source,target)
    with monkeypatch.context() as patch:
        patch.setattr(store.os,'replace',fail_stamp)
        with pytest.raises(OSError,match='full disk'):
            store.publish(cat.directory,store.snapshot(cat.directory,ident),Image.new('RGB',(120,80),'red'))
    assert not list(jpeg.parent.glob('*.tmp'))
    assert store.inspect(cat.directory,ident)['rebuild'];cache(photo)


def test_cancel_after_decode_preserves_cache(photo,monkeypatch):
    cat,ident,_=photo;jpeg,_=cache(photo);before=jpeg.read_bytes();cancel=Event();native=engine.load_image
    cat.edit(ident,{'exposure':1})
    def decode(*args,**kwargs):
        result=native(*args,**kwargs);cancel.set();return result
    monkeypatch.setattr(engine,'load_image',decode)
    assert store.rebuild(cat.directory,ident,cancel)=='중단'
    assert jpeg.read_bytes()==before and store.inspect(cat.directory,ident)['rebuild']


def test_offline_current_cache_retained_and_changed_edit_rebuilt(photo):
    cat,ident,path=photo;cache(photo)
    pixels,info=engine.load_image(path,1800)
    previews=cat.directory/'previews';previews.mkdir()
    np.savez_compressed(previews/f'{ident}.npz',pixels=pixels,info=json.dumps(info))
    path.rename(path.with_suffix('.unplugged'))
    result=store.inspect(cat.directory,ident)
    assert not result['rebuild'] and result['status']=='오프라인'
    cat.edit(ident,{'exposure':.8})
    assert store.inspect(cat.directory,ident)['rebuild'];jpeg,_=cache(photo)
    expected=engine.develop(pixels,cat.photo(ident)['settings'])
    assert np.mean(np.abs(np.asarray(Image.open(jpeg))/255-expected))<.01
    assert store.inspect(cat.directory,ident)['status']=='오프라인'
    np.savez_compressed(previews/f'{ident}.npz',pixels=pixels*.5,info=json.dumps(info))
    assert store.inspect(cat.directory,ident)['rebuild']


def test_missing_source_without_offline_keeps_old_preview_and_reports_state(photo):
    cat,ident,path=photo;cache(photo);path.rename(path.with_suffix('.unplugged'))
    cat.edit(ident,{'exposure':1});result=store.inspect(cat.directory,ident)
    assert result['image'] is not None and not result['rebuild']
    assert result['status']=='원본 없음 · 이전 미리보기'
    assert store.rebuild(cat.directory,ident,force=True)=='원본 없음'


def test_backup_restore_invalidates_future_cache_and_migrates_legacy(photo,tmp_path):
    cat,ident,_=photo;cache(photo);backup=cat.backup(tmp_path/'backup.sqlite')
    cat.edit(ident,{'exposure':1});cache(photo)
    restore_backup(cat,backup)
    assert cat.photo(ident)['settings']['exposure']==0
    assert store.inspect(cat.directory,ident)['rebuild'];cache(photo)
    # Older backups contain no derived preview table or triggers.
    with closing(sqlite3.connect(backup)) as db,db:
        for name in ['preview_added','preview_changed','preview_removed']:db.execute('DROP TRIGGER '+name)
        db.execute('DROP TABLE preview_state')
    before=cat.photo(ident)
    restore_backup(cat,backup)
    assert cat.photo(ident)==before
    assert store.snapshot(cat.directory,ident)['token'];cache(photo)


def test_render_writer_rejects_uncommitted_settings_and_changed_file(photo):
    cat,ident,path=photo;pixels,info=engine.load_image(path);settings=engine.defaults()
    assert not store.store_render(cat.directory,ident,{**settings,'exposure':1},pixels,info['file_fingerprint'])
    assert store.store_render(cat.directory,ident,settings,engine.develop(pixels,settings),info['file_fingerprint'])
    old=store.files(cat.directory,ident)[0].read_bytes()
    os.utime(path,ns=(path.stat().st_atime_ns,path.stat().st_mtime_ns+1000000))
    assert not store.store_render(cat.directory,ident,settings,pixels,info['file_fingerprint'])
    assert store.files(cat.directory,ident)[0].read_bytes()==old


def test_camera_source_offline_rebuild_keeps_sensor_pipeline(photo):
    from luma.rawcolor import CameraSource
    cat,ident,path=photo;settings=engine.defaults();settings.update(raw_mode='as_shot')
    # Use the established synthetic camera metadata fixture.
    info={'raw_native':True,'raw_info':dict(camera='Test Camera',format='NEF',
        xyz_to_camera=np.eye(3).tolist(),neutral=[.45,1,.7],daylight_neutral=[.5,1,.85]),'working_space':'sRGB'}
    pixels=np.full((30,40,3),.2,dtype=np.float32)
    previews=cat.directory/'previews';previews.mkdir()
    np.savez_compressed(previews/f'{ident}.npz',pixels=pixels,info=json.dumps(info))
    path.rename(path.with_suffix('.unplugged'));cat.set_settings(ident,settings)
    source,_=store.load_offline(cat.directory,ident,settings)
    assert isinstance(source,CameraSource)
    assert store.rebuild(cat.directory,ident)=='갱신 완료'
