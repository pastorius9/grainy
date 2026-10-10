from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import numpy as np
import pytest
from luma import pixel_jobs
from luma.engine import to_linear, to_srgb, develop, defaults


def serial(monkeypatch, function):
    with monkeypatch.context() as patch:
        patch.setattr(pixel_jobs, 'WORKERS', 1)
        return function()


@pytest.mark.parametrize('dtype',[np.float32,np.float64])
def test_transfer_values_exact_against_independent_formula(dtype):
    a=np.random.default_rng(4).uniform(-.12,4,(193,701,3)).astype(dtype)
    a[0,:4]=np.array([0,.0031308,.04045,1])[:,None]
    a=a[:,::-1]  # Read-only and negative-stride inputs are legal.
    a.setflags(write=False)
    f=a.astype(np.float32)
    expected=np.where(f<=.04045,f/12.92,(np.maximum(f+.055,0)/1.055)**2.4).astype(np.float32)
    np.testing.assert_array_equal(to_linear(a),expected)
    positive=np.maximum(a,0)
    expected=np.where(positive<=.0031308,positive*12.92,
        1.055*np.power(positive,1/2.4)-.055).astype(np.float32)
    np.testing.assert_array_equal(to_srgb(a),expected)


@pytest.mark.parametrize('lens_vignette',[0,27])
def test_entire_photo_development_remains_exact(monkeypatch,lens_vignette):
    a=np.random.default_rng(93).random((193,701,3),dtype=np.float32)
    a.setflags(write=False)
    s=defaults();s.update(exposure=.6,temperature=13,tint=-4,highlights=-29,
        shadows=30,blacks=-21,whites=18,contrast=11,lens_vignette=lens_vignette,
        curve=[[0,.03],[.25,.31],[.7,.8],[1,.97]],vibrance=12,sharpen=15,grain=8)
    expected=serial(monkeypatch,lambda:develop(a,s))
    np.testing.assert_array_equal(develop(a,s),expected)


def test_nested_parallel_requests_finish_with_bounded_tile_size():
    a=np.ones((377,701,3),np.float32)
    sizes=[];lock=Lock()
    def nested(tile):
        with lock:sizes.append(tile.shape[0]*tile.shape[1])
        return pixel_jobs.transform(tile,lambda inner:inner*2)
    with ThreadPoolExecutor(8) as callers:
        jobs=[callers.submit(pixel_jobs.transform,a,nested) for _ in range(8)]
        for future in jobs:np.testing.assert_array_equal(future.result(timeout=10),a*2)
    assert max(sizes)<=pixel_jobs.TILE_PIXELS


def test_worker_error_policy_and_recovery():
    a=np.zeros((193,701,3),np.float32)
    with np.errstate(divide='raise'):
        with pytest.raises(FloatingPointError):pixel_jobs.transform(a,lambda x:1/x)
    # Completed sibling tasks cannot corrupt a subsequent result.
    np.testing.assert_array_equal(pixel_jobs.transform(a,lambda x:x+2),a+2)


@pytest.mark.parametrize('shape',[(),(3,),(9,3),(0,6,3),(7,0,3)])
def test_scalar_empty_and_small_shapes(shape):
    a=np.zeros(shape,np.float32)
    np.testing.assert_array_equal(to_linear(a),a)
    np.testing.assert_array_equal(to_srgb(a),a)
