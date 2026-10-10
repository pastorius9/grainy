from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
import numpy as np
import pytest
from luma.engine import defaults
from luma.source_cache import DecodedSourceCache


def test_decode_dependencies_and_independent_metadata(tmp_path):
    path=tmp_path/'a.nef';path.write_bytes(b'first');cache=DecodedSourceCache();s=defaults();calls=[]
    def read():calls.append(True);return np.zeros((5,7,3),np.float32),{'nested':{'name':'original'}}
    a,meta=cache.load(path,s,1800,read);assert not a.flags.writeable
    meta['nested']['name']='changed';s.update(exposure=1,raw_kelvin=6300,raw_tint=5)
    b,meta=cache.load(path,s,1800,read);assert b is a and meta['nested']['name']=='original' and len(calls)==1
    for changes in (dict(raw_mode='as_shot'),dict(working_space='ProPhoto'),dict(video_time=1),dict(video_color={'matrix':'bt709'})):
        s.update(changes);cache.load(path,s,1800,read)
    assert len(calls)==5;cache.load(path,s,None,read);assert len(calls)==6


def test_lru_byte_entry_bounds_and_oversized_bypass(tmp_path):
    paths=[tmp_path/f'{i}.tif' for i in range(5)]
    for p in paths:p.write_bytes(b'a')
    cache=DecodedSourceCache(max_bytes=240,max_entries=2);calls=[]
    def read():calls.append(True);return np.ones((2,5,3),np.float32),{}
    for p in paths[:2]:cache.load(p,defaults(),1800,read)
    cache.load(paths[0],defaults(),1800,read);cache.load(paths[2],defaults(),1800,read)
    assert len(cache.entries)==2 and cache.bytes==240
    cache.load(paths[1],defaults(),1800,read);assert len(calls)==4
    cache.load(paths[3],defaults(),None,lambda:(np.ones((10,10,3),np.float32),{}))
    assert cache.bytes<=240 and len(cache.entries)==2
    cache.clear();assert cache.bytes==0 and not cache.entries


def test_file_change_offline_reconnect_and_force_clear(tmp_path):
    path=tmp_path/'a.raw';offline=tmp_path/'a.npz';path.write_bytes(b'1');offline.write_bytes(b'o');cache=DecodedSourceCache();calls=[]
    def read():
        content=(path if path.exists() else offline).read_bytes();calls.append(content)
        return np.full((2,3,3),content[0],np.float32),{}
    a,_=cache.load(path,defaults(),1800,read,offline=offline);path.write_bytes(b'22')
    b,_=cache.load(path,defaults(),1800,read,offline=offline);assert not np.array_equal(a,b)
    path.unlink();c,_=cache.load(path,defaults(),1800,read,offline=offline);assert c[0,0,0]==ord('o')
    path.write_bytes(b'3');d,_=cache.load(path,defaults(),1800,read,offline=offline);assert d[0,0,0]==ord('3')
    cache.clear();cache.load(path,defaults(),1800,read,offline=offline);assert len(calls)==5


def test_inflight_requests_share_one_decode_and_errors_do_not_poison_cache(tmp_path):
    path=tmp_path/'image';path.write_bytes(b'a');cache=DecodedSourceCache();entered=Event();release=Event();calls=[]
    def read():calls.append(True);entered.set();assert release.wait(5);return np.ones((2,3,3),np.float32),{}
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(cache.load,path,defaults(),1800,read);assert entered.wait(5)
        b=pool.submit(cache.load,path,defaults(),1800,read)
        # Both requests remain live until the shared decoder is released.
        from time import monotonic,sleep
        deadline=monotonic()+5
        while not cache.shared and monotonic()<deadline:sleep(.005)
        assert cache.shared==1;release.set();assert a.result()[0] is b.result()[0]
    assert len(calls)==1
    cache.clear()
    def fail():raise OSError('fixture failure')
    with pytest.raises(OSError):cache.load(path,defaults(),1800,fail)
    assert not cache.inflight and not cache.entries
    cache.load(path,defaults(),1800,read);assert len(calls)==2


def test_change_during_decode_retries_and_clear_during_decode_cannot_repopulate(tmp_path):
    path=tmp_path/'a';path.write_bytes(b'old');cache=DecodedSourceCache();calls=[]
    def read():
        calls.append(True)
        if len(calls)==1:path.write_bytes(b'new file')
        return np.full((2,3,3),len(calls),np.float32),{}
    pixels,_=cache.load(path,defaults(),1800,read);assert len(calls)==2 and pixels[0,0,0]==2
    cache.clear()
    def clear_read():cache.clear();return np.ones((2,3,3),np.float32),{}
    cache.load(path,defaults(),1800,clear_read);assert not cache.entries and cache.bytes==0
    def changing():path.write_bytes(path.read_bytes()+b'x');return np.ones((2,3,3),np.float32),{}
    with pytest.raises(OSError,match='변경'):cache.load(path,defaults(),1800,changing)


def test_camera_source_calibration_type_and_pixels_preserved(tmp_path):
    from luma.rawcolor import CameraSource
    path=tmp_path/'a';path.write_bytes(b'raw');info={'neutral':[.5,1,.8]};pixels=CameraSource(np.ones((3,5,3),np.float32),info)
    cache=DecodedSourceCache();s=defaults();s['raw_mode']='as_shot'
    result,_=cache.load(path,s,1800,lambda:(pixels,{'raw_info':deepcopy(info)}));cached,_=cache.load(path,s,1800,lambda:pytest.fail('Decoded twice'))
    assert isinstance(cached,CameraSource) and cached.raw_info==info;np.testing.assert_array_equal(result,pixels)


@pytest.mark.parametrize('size,offset',[(136,67),(400000,0),(400000,200000),(400000,399999)])
def test_same_size_same_timestamp_rewrite_invalidates_content(tmp_path,size,offset):
    import os
    path=tmp_path/'same.bin';path.write_bytes(b'a'*size);stamp=path.stat();cache=DecodedSourceCache();calls=[]
    def read():
        calls.append(True)
        with path.open('rb') as stream:stream.seek(offset);value=stream.read(1)[0]
        return np.full((2,3,3),value,np.float32),{}
    first,_=cache.load(path,defaults(),None,read)
    with path.open('r+b') as stream:stream.seek(offset);stream.write(b'b')
    os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert path.stat().st_size==stamp.st_size and path.stat().st_mtime_ns==stamp.st_mtime_ns
    changed,_=cache.load(path,defaults(),None,read)
    assert len(calls)==2 and first[0,0,0]==ord('a') and changed[0,0,0]==ord('b')
