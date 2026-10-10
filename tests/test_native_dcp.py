from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pytest
from luma import native_dcp,dcp


@pytest.fixture(autouse=True)
def require_native():
    assert native_dcp.available(),'Run tools/build_native_dcp.py before Windows validation'


def sample(shape,dtype,seed=91):
    rng=np.random.default_rng(seed)
    table=rng.uniform(-360,100,(*shape,3)).astype(dtype)
    coords=[rng.uniform(0,d if i==1 else d-1,(113,271)).astype(np.float32)
            for i,d in enumerate(shape)]
    for i,d in enumerate(shape):
        coords[i][0,0]=0;coords[i][0,1]=d if i==1 else d-1
        coords[i][0,2]=np.nextafter(np.float32(coords[i][0,1]),np.float32(0))
    return coords,table


@pytest.mark.parametrize('shape',[(1,1,2),(1,90,32),(2,72,48),(17,32,64),(256,1,2),(1,360,256)])
@pytest.mark.parametrize('dtype',[np.float32,np.float64])
def test_interpolation_exact_at_edges_and_random_values(shape,dtype):
    coords,table=sample(shape,dtype)
    expected=dcp._table_values_numpy(coords,table)
    actual=native_dcp.interpolate(coords,table)
    np.testing.assert_array_equal(actual,expected)
    assert actual.dtype==expected.dtype and actual.flags.owndata


@pytest.mark.parametrize('dtype',[np.float32,np.float64])
def test_strided_readonly_input_is_unchanged(dtype):
    coords,table=sample((5,12,16),dtype)
    coords=[c[::-2,::3] for c in coords];table=table[:,::-1,::-1,:]
    original=[c.copy() for c in [*coords,table]]
    for c in [*coords,table]:c.setflags(write=False)
    np.testing.assert_array_equal(native_dcp.interpolate(coords,table),dcp._table_values_numpy(coords,table))
    for c,old in zip([*coords,table],original):np.testing.assert_array_equal(c,old)


@pytest.mark.parametrize('dtype',[np.float32,np.float64])
@pytest.mark.parametrize('encoding',[0,1])
def test_complete_hsv_transform_exact_with_out_of_gamut_values(monkeypatch,dtype,encoding):
    rng=np.random.default_rng(550)
    rgb=rng.uniform(-.2,1.8,(139,287,3)).astype(np.float32)
    rgb[0,:8]=[[0,0,0],[1,1,1],[1,0,0],[0,1,0],[0,0,1],[1,1,0],[1,0,1],[0,1,1]]
    table=rng.uniform(.5,1.5,(5,72,8,3)).astype(dtype);table[...,0]=rng.uniform(-30,30,table.shape[:-1])
    before=rgb.copy();native=dcp.table_map(rgb,table,encoding)
    monkeypatch.setattr(native_dcp,'_lib',None)
    reference=dcp.table_map(rgb,table,encoding)
    np.testing.assert_array_equal(native,reference)
    np.testing.assert_array_equal(rgb,before)


def test_concurrent_calls_own_buffers_and_coefficients():
    def work(i):
        coords,table=sample((3+i%4,12,16),np.float64 if i%2 else np.float32,seed=i)
        expected=dcp._table_values_numpy(coords,table)
        for _ in range(3):np.testing.assert_array_equal(native_dcp.interpolate(coords,table),expected)
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(work,range(24)))


@pytest.mark.parametrize('kind',['nan','infinity','negative','high_v','high_h','high_s'])
def test_bad_coordinates_are_rejected_before_returning_pixels(kind):
    coords,table=sample((3,12,16),np.float32)
    values={'nan':(0,np.nan),'infinity':(1,np.inf),'negative':(2,-1),
            'high_v':(0,3),'high_h':(1,13),'high_s':(2,16)}
    channel,value=values[kind];coords[channel][-1,-1]=value
    with pytest.raises(ValueError):native_dcp.interpolate(coords,table)


def test_bad_shapes_and_dimensions():
    coords,table=sample((3,12,16),np.float32)
    for invalid in (table[...,0],table[:,:,:1],np.broadcast_to(np.float32(1),(257,12,16,3)),
                    np.broadcast_to(np.float32(1),(256,360,256,3))):
        with pytest.raises(ValueError):native_dcp.interpolate(coords,invalid)
    with pytest.raises(ValueError):native_dcp.interpolate(coords[:2],table)
    with pytest.raises(ValueError):native_dcp.interpolate([*coords[:2],coords[2][:2]],table)


def test_portable_fallback_and_empty_buffers(monkeypatch):
    coords,table=sample((1,12,16),np.float32)
    assert native_dcp.interpolate([c.astype(np.float64) for c in coords],table) is None
    assert native_dcp.interpolate(coords,table.astype(np.int32)) is None
    empty=native_dcp.interpolate([c[:0] for c in coords],table)
    assert empty.shape==(0,271,3)
    monkeypatch.setattr(native_dcp,'_lib',None)
    assert native_dcp.interpolate(coords,table) is None


def test_absent_or_unloadable_library_falls_back(monkeypatch):
    monkeypatch.setattr(native_dcp.ct,'CDLL',lambda *args:(_ for _ in ()).throw(OSError('unavailable')))
    assert native_dcp._load() is None
