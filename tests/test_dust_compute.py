"""Numerical compatibility of the detector's optimized statistical path."""
import math
import cv2
import numpy as np
import pytest
from luma.dust import _component_indices,_median,_quantiles


@pytest.mark.parametrize('minimum,maximum',[(2,3),(3,40),(80,160)])
def test_component_prefilter_matches_scalar_bounds_and_label_order(minimum,maximum):
    rng=np.random.default_rng(98231)
    image=np.uint8(rng.random((533,717))>.86)
    for center,size in [((150,140),(4,14)),((280,290),(80,160)),((590,350),(20,20))]:
        cv2.rectangle(image,center,(center[0]+size[0],center[1]+size[1]),1,-1)
    _,_,stats,_=cv2.connectedComponentsWithStats(image,8)
    before=stats.copy()
    for left,top,right,bottom in [(0,0,717,533),(127,256,512,384),(301,113,302,114)]:
        expected=[]
        for index,row in enumerate(stats[1:],1):
            x,y,w,h,area=map(int,row)
            if math.sqrt(4*area/math.pi)<minimum or max(w,h)>maximum or min(w,h)==0:continue
            if max(w,h)/min(w,h)>3.5 or area/(w*h)<.3:continue
            if left<=x+(w-1)/2+17<right and top<=y+(h-1)/2+23<bottom:expected.append(index)
        assert _component_indices(stats,minimum,maximum,17,23,left,top,right,bottom).tolist()==expected
    np.testing.assert_array_equal(stats,before)
    assert _component_indices(stats[:1],minimum,maximum,0,0,0,0,717,533).size==0


def test_statistics_remain_exact_for_readonly_strides_and_cache_eviction():
    rng=np.random.default_rng(93759)
    source=rng.uniform(-.5,1.5,1300).astype(np.float32);before=source.copy()
    source.setflags(write=False)
    for size in range(1,620):
        values=source[:size*2:2][::-1]
        assert _median(values)==float(np.median(values))
        fractions=(.1,.9) if size%2 else (.15,.6,.85)
        expected=[np.percentile(values,q*100) for q in fractions]
        np.testing.assert_array_equal(_quantiles(values,*fractions),expected)
    # Revisit a size after more than the cache capacity of distinct plans.
    np.testing.assert_array_equal(_quantiles(source[:2],.1,.9),[np.percentile(source[:2],q) for q in (10,90)])
    np.testing.assert_array_equal(source,before)
