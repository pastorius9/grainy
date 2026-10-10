"""Exact independent codec comparison and native handle/concurrency lifetime."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import gc
import weakref
import imagecodecs
import numpy as np
import pytest
from luma import colorio,native_color as native

pytestmark=pytest.mark.skipif(not native.available(),reason='Bundled Windows color library required')


def reference(array,source,destination):
    return imagecodecs.cms_transform(np.ascontiguousarray(array),source,destination,
        colorspace='rgb',outcolorspace='rgb',outdtype='float32',intent=1)


@pytest.mark.parametrize('source,destination',[
    ('Luma Wide','sRGB'),('Luma Wide','Display P3'),('sRGB','Adobe RGB'),
    ('Display P3','Luma Wide'),('Linear ProPhoto','Linear sRGB'),
    ('Luma Wide','ProPhoto RGB'),('sRGB','sRGB')])
def test_float_precision_matches_existing_engine(source,destination):
    rng=np.random.default_rng(173)
    array=rng.uniform(-.1,1.3,(385,513,3)).astype(np.float32)
    # Boundary and extended values are retained; this is not an 8/16-bit LUT.
    array[0,:6]=[[-.1,0,1],[1.2,1.1,1],[0,0,0],[1,1,1],[.04045]*3,[1e-7]*3]
    source=colorio.profile(source);destination=colorio.profile(destination)
    before=array.copy();expected=reference(array,source,destination)
    np.testing.assert_array_equal(native.convert(array,source,destination),expected)
    np.testing.assert_array_equal(array,before)


def test_noncontiguous_readonly_and_empty_buffers():
    array=np.random.default_rng(25).random((50,81,3),dtype=np.float32)[::-2,::3]
    array.setflags(write=False)
    source=colorio.profile('sRGB');destination=colorio.profile('Display P3')
    np.testing.assert_array_equal(native.convert(array,source,destination),reference(array,source,destination))
    assert native.convert(np.empty((0,4,3),np.float32),source,destination).shape==(0,4,3)


def test_lru_bounds_profile_bytes_and_changes(monkeypatch):
    native.clear_cache();monkeypatch.setattr(native,'MAX_TRANSFORMS',2)
    source=colorio.profile('sRGB');targets=[colorio.profile(n) for n in ('Display P3','Adobe RGB','ProPhoto RGB')]
    array=np.full((2,3,3),.37,np.float32)
    for target in targets:
        np.testing.assert_array_equal(native.convert(array,source,target),reference(array,source,target))
    info=native.cache_info();assert info['transforms']==2 and info['misses']==3
    native.convert(array,source,targets[-1]);assert native.cache_info()['hits']==1
    native.clear_cache();monkeypatch.setattr(native,'MAX_PROFILE_BYTES',len(source)+len(targets[-1]))
    for target in targets:native.convert(array,source,target)
    assert native.cache_info()['profile_bytes']<=native.MAX_PROFILE_BYTES
    assert native.cache_info()['transforms']==1


def test_concurrent_reuse_eviction_and_profile_switch(monkeypatch):
    native.clear_cache();monkeypatch.setattr(native,'MAX_TRANSFORMS',2)
    array=np.random.default_rng(321).random((391,563,3),dtype=np.float32)
    sources=[colorio.profile(n) for n in ('Luma Wide','sRGB','ProPhoto RGB')]
    targets=[colorio.profile(n) for n in ('Display P3','Adobe RGB','sRGB')]
    expected={(i,j):reference(array,a,b) for i,a in enumerate(sources) for j,b in enumerate(targets)}
    def run(k):
        i,j=k%3,(k//3)%3
        return np.array_equal(native.convert(array,sources[i],targets[j]),expected[i,j])
    with ThreadPoolExecutor(max_workers=6) as pool:assert all(pool.map(run,range(36)))
    assert native.cache_info()['transforms']<=2


def test_evicted_transform_lives_until_running_jobs_finish(monkeypatch):
    native.clear_cache();source=colorio.profile('sRGB');target=colorio.profile('Display P3')
    transform=native._transform(source,target);ref=weakref.ref(transform);del transform
    entered=Event();resume=Event();original=native.Transform.chunk
    def slow(self,packet):
        entered.set();assert resume.wait(5);original(self,packet)
    monkeypatch.setattr(native.Transform,'chunk',slow)
    array=np.full((450,450,3),.33,np.float32)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(native.convert,array,source,target)
        try:
            assert entered.wait(5);native.clear_cache();gc.collect();assert ref() is not None
        finally:resume.set()
        np.testing.assert_array_equal(pending.result(),reference(array,source,target))
    gc.collect();assert ref() is None


def test_bad_profiles_and_input_rejected_then_recovery():
    source=colorio.profile('sRGB');array=np.ones((3,4,3),np.float32)
    for target in (b'',b'x'*512,imagecodecs.cms_profile('gray',gamma=2.2)):
        with pytest.raises(ValueError):native.convert(array,source,target)
    for invalid in (np.ones((4,4),np.float32),np.ones((4,4,4),np.float32),array.astype(np.float64)):
        with pytest.raises(ValueError):native.convert(invalid,source,source)
    with pytest.raises(TypeError):native.convert(array,'sRGB',source)
    np.testing.assert_array_equal(native.convert(array,source,source),array)


def test_colorio_fallback_keeps_other_precision_and_missing_runtime(monkeypatch):
    source=colorio.profile('sRGB');target=colorio.profile('Adobe RGB')
    array=np.random.default_rng(25).random((20,31,3),dtype=np.float32)
    monkeypatch.setattr(native,'available',lambda:False)
    np.testing.assert_array_equal(colorio.convert(array,source,target),reference(array,source,target))
    for dtype in ('uint8','uint16','float64'):
        expected=imagecodecs.cms_transform(array,source,target,colorspace='rgb',outcolorspace='rgb',outdtype=dtype,intent=1)
        np.testing.assert_array_equal(colorio.convert(array,source,target,dtype),expected)


@pytest.mark.parametrize('working_space',['sRGB','ProPhoto'])
def test_embedded_custom_icc_16bit_input_and_export_match_fallback(tmp_path,monkeypatch,working_space):
    import hashlib
    import tifffile
    from luma import engine
    pixels=np.random.default_rng(962).integers(0,65536,(389,517,3),dtype=np.uint16)
    embedded=imagecodecs.cms_profile('rgb',whitepoint=(.3127,.329),
        primaries=(.708,.292,.170,.797,.131,.046),gamma=1.7)
    source=tmp_path/'custom-profile.tif'
    tifffile.imwrite(source,pixels,photometric='rgb',metadata=None,extratags=[(34675,'B',len(embedded),embedded,False)])
    digest=hashlib.sha256(source.read_bytes()).digest()
    native.clear_cache();actual,_=engine.load_image(source,working_space=working_space)
    assert native.cache_info()['misses']==1
    destination=colorio.profile('Linear ProPhoto' if working_space=='ProPhoto' else 'Linear sRGB')
    expected=reference(pixels.astype(np.float32)/65535,embedded,destination)
    np.testing.assert_array_equal(actual,expected)
    settings=engine.defaults();settings.update(working_space=working_space,exposure=.37)
    engine.export_image(source,tmp_path/'native.tif',settings,'TIFF 16-bit')
    monkeypatch.setattr(native,'available',lambda:False)
    fallback,_=engine.load_image(source,working_space=working_space)
    np.testing.assert_array_equal(actual,fallback)
    engine.export_image(source,tmp_path/'fallback.tif',settings,'TIFF 16-bit')
    np.testing.assert_array_equal(tifffile.imread(tmp_path/'native.tif'),tifffile.imread(tmp_path/'fallback.tif'))
    assert hashlib.sha256(source.read_bytes()).digest()==digest


def test_large_profile_uses_existing_engine_instead_of_restricting_input(monkeypatch):
    source=colorio.profile('sRGB');target=colorio.profile('Display P3')
    # Lower only the optimization's limit to exercise this without a huge ICC.
    monkeypatch.setattr(native,'MAX_PROFILE_SIZE',128)
    def fail(*args):raise AssertionError('Oversize profile entered bounded native cache')
    monkeypatch.setattr(native,'convert',fail)
    array=np.full((3,4,3),.33,np.float32)
    np.testing.assert_array_equal(colorio.convert(array,source,target),reference(array,source,target))
