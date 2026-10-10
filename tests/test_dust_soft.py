from threading import Event
import numpy as np
import pytest

from luma.dust import Options,detect,Cancelled,repair,spot_bounds
from luma.engine import to_linear,to_srgb
from luma.dust_soft import candidates,merge


def soft_fixture(shape=(384,512)):
    h,w=shape;y,x=np.mgrid[:h,:w]
    clean=(.5+np.random.default_rng(20261001).normal(0,.055,shape)).astype(np.float32)
    dirty=clean.copy();truth=[]
    for px,py,kind in [(.25,.25,'dark'),(.5,.33,'bright'),(.75,.66,'dark'),(.4,.75,'bright')]:
        cx,cy=round(px*w),round(py*h);radius=6
        dirty+=(.14 if kind=='bright' else -.14)*np.exp(-((x-cx)**2+(y-cy)**2)/(2*(radius*.55)**2)).astype(np.float32)
        truth.append((cx,cy,kind))
    return clean,dirty,truth


def matches(result,truth):
    h,w=result['height'],result['width']
    return sum(any(s['polarity']==kind and abs(s['x']*(w-1)-x)<=3 and abs(s['y']*(h-1)-y)<=3 for s in result['spots']) for x,y,kind in truth)


def test_soft_dust_in_heavy_grain_is_added_for_review_without_changing_existing_results():
    clean,dirty,truth=soft_fixture();original=dirty.copy()
    before=detect(dirty,Options(detailed=True),limit=5000)
    after=detect(dirty,Options(detailed=True,soft=True),limit=5000)
    assert matches(before,truth)<=1 and matches(after,truth)==4
    assert after['spots'][:len(before['spots'])]==before['spots']
    new=after['spots'][len(before['spots']):]
    assert new and all(s['review'] and s['detail_kind']=='soft' for s in new)
    assert detect(clean,Options(detailed=True,soft=True),limit=5000)['total']<=4
    np.testing.assert_array_equal(dirty,original)
    assert detect(dirty,Options(soft=True))['spots']==detect(dirty)['spots']


@pytest.mark.parametrize('kind',['dark','bright'])
def test_polarity_size_and_candidate_limits(kind):
    _,dirty,truth=soft_fixture()
    result=detect(dirty,Options(detailed=True,soft=True,polarity=kind),limit=1)
    assert len(result['spots'])==1 and result['truncated'] and result['total']>=2
    assert result['spots'][0]['polarity']==kind
    bounded=detect(dirty,Options(detailed=True,soft=True,minimum=6,maximum=20,polarity=kind),limit=5000)
    assert all(6<=s['diameter']<=20 for s in bounded['spots'] if s.get('detail_kind')=='soft')


def test_soft_tile_seams_preserve_positions_and_sizes():
    _,dirty,truth=soft_fixture();h,w=dirty.shape;options=Options(detailed=True,soft=True)
    whole=merge([],candidates(dirty,options,tile_size=1024),w,h,options.maximum)
    tiled=merge([],candidates(dirty,options,tile_size=128),w,h,options.maximum)
    assert len(whole)==len(tiled)
    for a,b in zip(sorted(whole,key=lambda s:(s['y'],s['x'])),sorted(tiled,key=lambda s:(s['y'],s['x']))):
        for key in ('x','y','rx','ry'):assert a[key]==b[key]
    assert matches(dict(spots=tiled,height=h,width=w),truth)==4


def test_new_merge_preserves_every_existing_candidate_including_close_centres():
    a=dict(x=.4,y=.5,rx=.004,ry=.004,polarity='dark',score=2)
    b={**a,'x':.401}
    extra={**a,'review':True,'detail_kind':'soft','score':10}
    assert merge([a,b],[extra],1000,1000,40)==[a,b]


def test_selected_soft_repair_reduces_known_dust_without_changing_other_pixels():
    clean,dirty,truth=soft_fixture();h,w=dirty.shape;yy,xx=np.mgrid[:h,:w]
    found=detect(dirty,Options(detailed=True,soft=True),limit=5000)
    selected=[min((s for s in found['spots'] if s['polarity']==kind),
        key=lambda s:(s['x']*(w-1)-x)**2+(s['y']*(h-1)-y)**2) for x,y,kind in truth]
    source=np.repeat(to_linear(dirty)[...,None],3,-1);original=source.copy()
    fixed=repair(source,selected);mask=np.zeros((h,w),bool)
    for s in selected:
        x,y,rx,ry=spot_bounds(s,source.shape);mask|=((xx-x)/rx)**2+((yy-y)/ry)**2<=1
    np.testing.assert_array_equal(source,original)
    np.testing.assert_array_equal(fixed[~mask],original[~mask])
    restored=to_srgb(fixed)[...,0]
    for x,y,kind in truth:
        core=(xx-x)**2+(yy-y)**2<=36
        assert np.mean(np.abs(restored[core]-clean[core]))<np.mean(np.abs(dirty[core]-clean[core]))*.8


def test_cancel_in_extra_phase_and_invalid_option():
    gray=np.full((200,300),.5,np.float32);cancel=Event();updates=[]
    def progress(done,total):
        updates.append((done,total))
        if done>total//2:cancel.set()
    with pytest.raises(Cancelled):detect(gray,Options(detailed=True,soft=True),cancel=cancel,progress=progress,tile_size=128)
    assert updates==[(i,12) for i in range(1,8)]
    with pytest.raises(ValueError):detect(gray,Options(soft='yes'))
