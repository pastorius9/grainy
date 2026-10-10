from copy import deepcopy
import numpy as np
import pytest

from luma.engine import defaults, develop, to_linear, to_srgb
from luma.processing import _local_region, blur
from luma.render_cache import DevelopmentCache


def fixture_settings():
    s=defaults()
    s.update(exposure=.3,shadows=12,noise_color=15,texture=8,
             distortion=-5,straighten=1.5,working_space='sRGB')
    s['masks']=[
        {'name':'Brush','components':[{'type':'brush','points':[[.15,.2],[.7,.6]],'radius':.06}],
         'adjustments':{'exposure':.2,'clarity':18}},
        {'components':[{'type':'luma','range':[.35,.7],'softness':.1}],
         'adjustments':{'saturation':-20}}]
    return s


def full_frame_local(image,mask,edit):
    # Deliberately evaluate the complete frame, the pre-optimization reference.
    a=to_srgb(to_linear(image)*(2**edit.get('exposure',0)))
    a=(a-.5)*(1+edit.get('contrast',0)/125)+.5
    a+=np.array([edit.get('temperature',0),-edit.get('tint',0)*.5,-edit.get('temperature',0)],np.float32)/400
    gray=a@np.array([.2126,.7152,.0722],np.float32)
    a=gray[...,None]+(a-gray[...,None])*(1+edit.get('saturation',0)/100)
    a+=edit.get('shadows',0)/300*(1-np.clip(gray,0,1))[...,None]**2
    a+=edit.get('highlights',0)/300*np.clip(gray,0,1)[...,None]**2
    if edit.get('clarity'):a+=(a-blur(a,2))*edit['clarity']/100
    weight=mask[...,None]
    return image*(1-weight)+np.clip(a,0,1)*weight


@pytest.mark.parametrize('bounds',[(40,80,30,110),(0,30,0,40),(0,140,0,160)])
@pytest.mark.parametrize('clarity',[0,45,-30])
def test_region_matches_full_frame_including_blur_halo(bounds,clarity):
    image=np.random.default_rng(12).uniform(-.08,1.08,(140,160,3)).astype(np.float32)
    before=image.copy()
    y0,y1,x0,x1=bounds
    mask=np.zeros((140,160),np.float32)
    mask[y0:y1,x0:x1]=np.random.default_rng(8).random((y1-y0,x1-x0),dtype=np.float32)
    edit=dict(exposure=.4,contrast=18,saturation=-12,temperature=5,tint=-7,
              shadows=15,highlights=-18,clarity=clarity)
    assert np.allclose(_local_region(image,mask,edit),full_frame_local(image,mask,edit),atol=2e-7)
    assert np.array_equal(before,image)


def test_cached_stages_follow_changes_and_source_identity():
    source=np.random.default_rng(99).random((85,130,3),dtype=np.float32)
    original=source.copy();source.setflags(write=False)
    cache=DevelopmentCache(4*1024*1024)
    s=fixture_settings()
    changes=[('exposure',1.),('tint',-9),('whites',-20),('curve',[[0,.03],[.5,.55],[1,.97]]),
             ('parametric',[10,0,-5,0]),('rgb_curves',[[[0,0],[.5,.6],[1,1]]]*3),
             ('hsl',[[15,20,-10]]+[[0,0,0]]*7),('mixer_mode','hsl'),
             ('grading',[[30,10,0],[120,10,0],[200,10,0]]),
             ('noise_luma',25),('defringe',12),('sharpen',20),('grain',8),
             ('crop',[.1,.15,.9,.8]),('flip',True),('perspective_v',7),('ca_red',10),
             ('retouch',[{'type':'clone','points':[[.4,.4]],'source':[.2,.2],'radius':.06}])]
    for key,value in changes:
        s[key]=value
        assert np.allclose(develop(source,s,cache=cache),develop(source,s),atol=1e-6),key
        assert cache.bytes<=cache.max_bytes
    other=np.full(source.shape,.15,np.float32)
    assert np.allclose(develop(other,s,cache=cache),develop(other,s),atol=1e-6)
    assert cache.source is other and np.array_equal(source,original)


def test_preceding_local_edit_invalidates_following_range_mask():
    source=np.random.default_rng(7).random((80,120,3),dtype=np.float32)
    cache=DevelopmentCache();s=fixture_settings()
    first={};second={}
    develop(source,s,cache=cache,on_mask=lambda i,m:first.setdefault(i,m.copy()))
    hits=cache.hits
    s['masks'][0]['adjustments']['exposure']=1.4
    actual=develop(source,s,cache=cache,on_mask=lambda i,m:second.setdefault(i,m.copy()))
    assert np.allclose(actual,develop(source,s),atol=1e-6)
    assert cache.hits>hits and np.array_equal(first[0],second[0])
    assert not np.allclose(first[1],second[1])
    s['masks'][0]['enabled']=False
    masks={}
    actual=develop(source,s,cache=cache,on_mask=lambda i,m:masks.setdefault(i,m.copy()))
    assert 0 in masks and np.allclose(actual,develop(source,s),atol=1e-6)


def test_budget_never_evicts_prefix_into_repeated_cache_misses():
    source=np.full((50,70,3),.3,np.float32);s=fixture_settings()
    cache=DevelopmentCache(source.nbytes*2)
    develop(source,s,cache=cache)
    previous=cache.hits
    for value in [.4,.6,.8]:
        s['masks'][0]['adjustments']['exposure']=value
        assert np.allclose(develop(source,s,cache=cache),develop(source,s),atol=1e-6)
        assert cache.bytes<=cache.max_bytes
    assert cache.hits-previous>=6


def test_negative_encoded_values_do_not_trigger_invalid_power():
    with np.errstate(invalid='raise'):
        assert np.isfinite(to_linear(np.array([-.2,-.03,.5],np.float32))).all()


def test_full_resolution_budget_grows_with_source_but_respects_ceiling():
    from luma.render_cache import physical_memory
    assert physical_memory() is None or physical_memory() > 0
    cache = DevelopmentCache(1000, per_source=8, ceiling=50_000)
    small = np.zeros((10, 10, 3), np.float32)       # 1,200 bytes -> 9,600
    large = np.zeros((40, 40, 3), np.float32)       # 19,200 bytes -> capped at 50,000
    cache.bind(small);assert cache.max_bytes == 9_600
    cache.bind(large);assert cache.max_bytes == 50_000
    fixed = DevelopmentCache(1000);fixed.bind(large);assert fixed.max_bytes == 1000


def test_large_source_keeps_stage_cache_so_only_changed_stage_recomputes(monkeypatch):
    monkeypatch.setenv('GRAINY_GPU', '0')   # CPU stage cache; the GPU chain is covered in test_native_gpu
    source = np.random.default_rng(3).random((300, 400, 3), dtype=np.float32)
    edits = defaults();edits['masks'] = [{'enabled': True, 'components': [{'type': 'radial', 'points': [[.2, .2], [.8, .8]]}],
                                          'adjustments': {'exposure': .3}}]
    fixed = DevelopmentCache(source.nbytes)            # too small: develop() disables it entirely
    adaptive = DevelopmentCache(source.nbytes, per_source=8, ceiling=source.nbytes*8)
    for cache in (fixed, adaptive):
        develop(source, edits, cache=cache)
        misses = cache.misses
        changed = deepcopy(edits);changed['masks'][0]['adjustments']['exposure'] = .5
        result = develop(source, changed, cache=cache)
        assert np.array_equal(result, develop(source, changed))
    assert fixed.hits == 0
    assert adaptive.hits >= 4 and adaptive.misses - misses <= 1


def _masked(component, exposure):
    edits = defaults();edits.update(exposure=exposure, distortion=-6)
    edits['masks'] = [{'enabled': True, 'components': [component], 'adjustments': {'exposure': .4, 'saturation': 20}}]
    return edits


@pytest.mark.parametrize('component', [
    {'type': 'radial', 'points': [[.2, .2], [.8, .8]], 'feather': 50},
    {'type': 'linear', 'points': [[.1, .1], [.9, .7]]},
    {'type': 'luma', 'range': [.3, .7], 'softness': .1},
    {'type': 'color', 'rgb': [.6, .4, .3], 'tolerance': .3},
])
def test_global_edit_reuses_shape_masks_but_recomputes_range_masks(component):
    source = np.random.default_rng(9).random((120, 180, 3), dtype=np.float32)
    cache = DevelopmentCache(64*1024*1024)
    develop(source, _masked(component, 0), cache=cache)
    calls = []
    import luma.processing as processing
    original = processing._layer_component
    processing._layer_component = lambda *a, **k: (calls.append(1), original(*a, **k))[1]
    try:
        for exposure in (.3, -.2):
            cached = develop(source, _masked(component, exposure), cache=cache)
            assert np.array_equal(cached, develop(source, _masked(component, exposure)))
    finally:
        processing._layer_component = original
    image_based = component['type'] in ('luma', 'color')
    # Uncached reference renders always compute the component; cached ones only when the image matters.
    assert len(calls) == (4 if image_based else 2)


def test_lookup_store_discard_and_source_version():
    cache = DevelopmentCache(1024)
    source = np.zeros((4, 4, 3), np.float32)
    cache.bind(source);version = cache.version
    assert cache.lookup('tone', 1) is None
    value = cache.store('tone', 1, np.ones(8, np.float32))
    assert cache.lookup('tone', 1) is value and not value.flags.writeable and cache.bytes == 32
    assert cache.lookup('tone', 2) is None and cache.bytes == 0     # stale entry dropped
    cache.store('color', 1, np.ones(8, np.float32));cache.discard('color', 'missing')
    assert cache.bytes == 0 and not cache.entries
    cache.bind(source);assert cache.version == version
    cache.bind(source.copy());assert cache.version == version+1


def test_clip_runs_on_row_workers_without_touching_callers_array():
    from luma.engine import _clip01
    a = np.random.default_rng(1).normal(.5, 1, (700, 500, 3)).astype(np.float32);a.setflags(write=False)
    kept = a.copy()
    clipped = _clip01(a)
    assert np.array_equal(clipped, np.clip(kept, 0, 1)) and np.array_equal(a, kept)
    owned = kept.copy()
    assert _clip01(owned) is owned and np.array_equal(owned, clipped)
    assert _clip01(np.float64([[-1, 2]])).dtype == np.float32
