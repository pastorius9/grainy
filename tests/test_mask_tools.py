import platform
from copy import deepcopy
import numpy as np
import pytest
from luma.processing import layer_mask,component_mask,_local_region
from luma.engine import develop,defaults
from luma.render_cache import DevelopmentCache


def brush(**values):
    return dict(type='brush',points=[[.5,.5]],radius=.2,feather=50,paint_version=2,flow=20,density=60,**values)


def mask(components):
    image=np.full((101,101,3),.5,np.float32)
    return layer_mask(image,image.shape,dict(components=components),lambda a:a)


def test_repeated_flow_reaches_density_without_event_rate_bias():
    stroke=brush();one=mask([stroke]);two=mask([stroke]*2);many=mask([stroke]*100)
    assert one[50,50]==pytest.approx(.12)
    assert two[50,50]==pytest.approx(.216)
    assert many.max()==pytest.approx(.6,abs=1e-6)
    duplicate=deepcopy(stroke);duplicate['points']*=100
    np.testing.assert_array_equal(mask([duplicate]),one)
    assert one[50,71]==0 and one[0,0]==0


def test_erase_intersect_disabled_and_density_never_lowers_existing_paint():
    full={**brush(),'flow':100,'density':100}
    assert mask([{**full,'operation':'subtract'}]).max()==0
    assert mask([full,{**brush(),'operation':'subtract'}])[50,50]==pytest.approx(.88)
    assert mask([full,{**brush(),'operation':'intersect'}])[50,50]==pytest.approx(.12)
    assert mask([full,brush()])[50,50]==1
    np.testing.assert_array_equal(mask([full,{**brush(),'enabled':False}]),mask([full]))


def test_legacy_strokes_retain_max_union_and_subtraction():
    old={k:v for k,v in brush().items() if k not in ('paint_version','flow','density')}
    second={**old,'points':[[.6,.5]]};a=component_mask((101,101),old);b=component_mask((101,101),second)
    np.testing.assert_array_equal(mask([old,second]),np.maximum(a,b))
    np.testing.assert_array_equal(mask([old,{**second,'operation':'subtract'}]),a*(1-b))
    # Disabling the positive base must not turn the following eraser into paint.
    assert mask([{**old,'enabled':False},{**second,'operation':'subtract'}]).max()==0
    assert mask([{**old,'enabled':False},{**second,'operation':'intersect'}]).max()==0


@pytest.mark.parametrize('key,value',[('whites',55),('blacks',-45),('texture',65),('dehaze',35),('sharpen',65),('noise_luma',80),('noise_color',75)])
def test_partial_controls_are_spatially_limited_and_match_full_filter(key,value):
    image=np.random.default_rng(515).uniform(.15,.75,(160,200,3)).astype(np.float32);original=image.copy()
    coverage=np.zeros(image.shape[:2],np.float32);coverage[60:100,75:125]=.6
    edit={key:value,'clarity':30}
    actual=_local_region(image,coverage,edit)
    full=_local_region(image,np.ones(image.shape[:2],np.float32),edit)
    expected=image*(1-coverage[...,None])+full*coverage[...,None]
    # chroma_guided divides box-sum differences, so float rounding that depends on the region's size
    # (OpenCV's running box sums) grows to a few 1e-7.
    # Apple Silicon rounds the luminance filter's sums differently from x86 as well: 4.9e-7 measured there.
    arm=platform.machine().lower() in ('arm64','aarch64')
    np.testing.assert_allclose(actual,expected,atol=3e-6 if key=='noise_color' else 6e-7 if arm and key=='noise_luma' else 2e-7)
    np.testing.assert_array_equal(actual[:50],image[:50]);np.testing.assert_array_equal(image,original)
    assert np.max(abs(actual-image))>.001
    assert np.max(abs(actual-_local_region(image,coverage,{'clarity':30})))>.001


@pytest.mark.parametrize('edit', [dict(noise_luma=70, detail_version=3), dict(sharpen=80, detail_version=2),
                                  dict(noise_luma=40, sharpen=50, noise_color=30, detail_version=3)])
def test_partial_wavelet_noise_reduction_and_luminance_sharpening_match_the_full_filter(edit):
    from luma.processing import _local_region, rgb_to_lab
    rng = np.random.default_rng(71)
    image = np.clip(rng.uniform(.2, .7, (1, 1, 3)) + rng.normal(0, .03, (200, 260, 3)), 0, 1).astype(np.float32)
    coverage = np.zeros(image.shape[:2], np.float32);coverage[80:130, 100:170] = .7
    actual = _local_region(image, coverage, edit)
    full = _local_region(image, np.ones(image.shape[:2], np.float32), edit)
    np.testing.assert_allclose(actual, image*(1-coverage[..., None])+full*coverage[..., None], atol=3e-6)
    np.testing.assert_array_equal(actual[:10], image[:10])
    old = _local_region(image, coverage, {**edit, 'detail_version': 1})
    assert np.max(abs(actual - old)) > .001                       # the photo's process changes the local method
    if edit.get('sharpen') and not edit.get('noise_luma'):       # luminance sharpening adds no colour
        ab = lambda a: rgb_to_lab(a)[80:130, 100:170, 1:]
        assert np.abs(ab(actual) - ab(image)).mean() < np.abs(ab(old) - ab(image)).mean() / 2


def test_develop_passes_the_photo_process_to_mask_edits():
    source = np.random.default_rng(12).random((120, 160, 3), dtype=np.float32)
    settings = {**defaults(), 'masks': [dict(components=[brush()], adjustments={'noise_luma': 60, 'sharpen': 60})]}
    new, old = develop(source, {**settings, 'detail_version': 3}), develop(source, {**settings, 'detail_version': 1})
    assert np.max(abs(new - old)) > .001


def test_new_masks_follow_geometry_and_cache_invalidation():
    source=np.random.default_rng(41).random((180,240,3),dtype=np.float32);original=source.copy()
    settings=defaults();settings.update(rotation=1,crop=[.1,.2,.9,.8],masks=[dict(components=[brush(),brush()],adjustments={'texture':55,'whites':-20})])
    cache=DevelopmentCache()
    for flow in (20,80,35):
        settings['masks'][0]['components'][0]['flow']=flow
        np.testing.assert_array_equal(develop(source,settings,cache=cache),develop(source,settings))
    np.testing.assert_array_equal(source,original)
