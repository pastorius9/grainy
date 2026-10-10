from threading import Event
from copy import deepcopy
import numpy as np
import pytest
from luma.dust import Options,detect,Cancelled,repair,spot_bounds
from luma.dust_soft import candidates,merge
from luma.engine import to_linear,to_srgb
from test_dust_soft import soft_fixture,matches


def test_optional_grain_suppression_reduces_dust_free_candidates_and_finds_injections():
    clean,dirty,truth=soft_fixture();before=dirty.copy()
    ordinary=detect(dirty,Options(detailed=True),limit=5000)
    result=detect(dirty,Options(detailed=True,soft=True,suppress_grain=True),limit=5000)
    assert result['spots'][:len(ordinary['spots'])]==ordinary['spots']
    assert matches(result,truth)==4
    assert all(s['review'] for s in result['spots'][len(ordinary['spots']):])
    unfiltered=detect(clean,Options(detailed=True,soft=True),limit=5000)
    filtered=detect(clean,Options(detailed=True,soft=True,suppress_grain=True),limit=5000)
    assert unfiltered['total']>filtered['total']==0
    np.testing.assert_array_equal(dirty,before)
    # The flag has no effect outside the optional soft-candidate stage.
    assert detect(dirty,Options(suppress_grain=True))['spots']==detect(dirty)['spots']
    assert detect(dirty,Options(detailed=True,suppress_grain=True))['spots']==ordinary['spots']


@pytest.mark.parametrize('kind',['dark','bright'])
def test_strict_size_polarity_and_limit(kind):
    _,dirty,truth=soft_fixture();opts=Options(detailed=True,soft=True,suppress_grain=True,polarity=kind,minimum=6,maximum=20)
    all_found=detect(dirty,opts,limit=5000);limited=detect(dirty,opts,limit=1)
    assert matches(all_found,[t for t in truth if t[2]==kind])==2
    assert limited['total']==all_found['total'] and len(limited['spots'])==1 and limited['truncated']
    assert all(s['polarity']==kind and 6<=s['diameter']<=20 for s in all_found['spots'])


def test_strict_tiles_and_cancel():
    _,dirty,_=soft_fixture();h,w=dirty.shape;opts=Options(detailed=True,soft=True,suppress_grain=True)
    whole=merge([],candidates(dirty,opts,tile_size=1024),w,h,opts.maximum)
    tiled=merge([],candidates(dirty,opts,tile_size=128),w,h,opts.maximum)
    assert [{k:s[k] for k in ('x','y','rx','ry','polarity')} for s in whole]==[{k:s[k] for k in ('x','y','rx','ry','polarity')} for s in tiled]
    cancel=Event()
    def stop(done,total):cancel.set()
    with pytest.raises(Cancelled):candidates(dirty,opts,cancel=cancel,progress=stop,tile_size=128)
    with pytest.raises(ValueError):detect(dirty,Options(suppress_grain='yes'))


def test_strict_repair_improves_core_and_preserves_every_pixel_outside_selection():
    clean,dirty,truth=soft_fixture();h,w=dirty.shape;yy,xx=np.mgrid[:h,:w]
    result=detect(dirty,Options(detailed=True,soft=True,suppress_grain=True),limit=5000)
    selected=[min((s for s in result['spots'] if s['polarity']==kind),key=lambda s:(s['x']*(w-1)-x)**2+(s['y']*(h-1)-y)**2) for x,y,kind in truth]
    original=np.repeat(to_linear(dirty)[...,None],3,-1);source=original.copy();fixed=repair(source,selected)
    mask=np.zeros((h,w),bool)
    for s in selected:
        x,y,rx,ry=spot_bounds(s,source.shape);mask|=((xx-x)/rx)**2+((yy-y)/ry)**2<=1
    np.testing.assert_array_equal(source,original);np.testing.assert_array_equal(fixed[~mask],source[~mask])
    for x,y,_ in truth:
        core=(xx-x)**2+(yy-y)**2<=16
        assert np.mean(np.abs(to_srgb(fixed)[...,0][core]-clean[core]))<np.mean(np.abs(dirty[core]-clean[core]))
