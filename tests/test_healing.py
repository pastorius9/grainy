from copy import deepcopy
import cv2
import numpy as np
import pytest
from luma.engine import defaults,develop
from luma.processing import apply_retouch,brush_mask,blur
from luma.healing import automatic_offset


@pytest.mark.parametrize('points',[[[.5,.5]],[[.35,.5],[.6,.5]],[[.015,.5],[.12,.5]]])
def test_auto_heal_fills_dark_marks_and_preserves_unpainted_pixels(points):
    source=np.full((180,240,3),[.35,.5,.6],np.float32)
    damage=brush_mask(source.shape,points,.012,0)>0
    source[damage]=0;original=source.copy()
    operation=dict(type='heal',version=2,points=points,source=None,radius=.05,feather=30)
    output=apply_retouch(source,[operation])
    np.testing.assert_allclose(output[damage],np.broadcast_to([.35,.5,.6],output[damage].shape),atol=2e-5)
    coverage=brush_mask(source.shape,points,.05,30)
    np.testing.assert_array_equal(output[coverage==0],source[coverage==0])
    np.testing.assert_array_equal(source,original)


def test_donor_does_not_intersect_freehand_target():
    image=np.full((240,320,3),.5,np.float32)
    mask=brush_mask(image.shape,[[.4,.3],[.5,.7],[.6,.35]],.035,50)
    offset=automatic_offset(image,mask);assert offset is not None
    dx,dy=map(round,offset);y,x=np.where(mask>0)
    assert np.all((x+dx>=0)&(x+dx<320)&(y+dy>=0)&(y+dy<240))
    assert not np.any(mask[y+dy,x+dx]>0)


def test_manual_heal_transfers_texture_and_matches_boundary_color():
    y,x=np.mgrid[:180,:240];image=np.full((180,240,3),[.3,.45,.6],np.float32)
    image[:, :100]+=.12+np.sin(x[:,:100,None]*1.7)*.02
    image[86:95,146:155]=0
    op=dict(type='heal',version=2,points=[[.625,.5]],source=[.25,.5],radius=.065,feather=30)
    output=apply_retouch(image,[op])
    assert np.abs(output[86:95,146:155].mean(axis=(0,1))-[.3,.45,.6]).max()<.01
    assert output[87:94,147:154,0].std()>.008


def test_legacy_heal_pixels_unchanged_and_new_heal_survives_geometry_cache():
    rng=np.random.default_rng(42);image=rng.uniform(.2,.6,(90,120,3)).astype(np.float32)
    old=dict(type='heal',points=[[.6,.5]],source=[.3,.4],radius=.06,feather=60)
    mask=brush_mask(image.shape,old['points'],.06,60);y,x=np.where(mask>0);pad=max(5,int(.06*90*2))
    x0,x1=max(0,x.min()-pad),min(120,x.max()+pad+1);y0,y1=max(0,y.min()-pad),min(90,y.max()+pad+1)
    part=image[y0:y1,x0:x1];yy,xx=np.mgrid[y0:y1,x0:x1].astype(np.float32)
    delta=np.array(old['source'])-np.array(old['points'][0])
    fixed=cv2.remap(image,xx+float(delta[0])*120,yy+float(delta[1])*90,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101)
    fixed+=blur(part,max(1,pad/3))-blur(fixed,max(1,pad/3))
    expected=image.copy();weight=mask[y0:y1,x0:x1,None]
    expected[y0:y1,x0:x1]=part*(1-weight)+fixed*weight
    np.testing.assert_array_equal(apply_retouch(image,[old]),np.maximum(expected,0))
    new={**old,'version':2,'source':None};s=defaults();s.update(retouch=[new],rotation=1,crop=[.1,.2,.9,.8])
    from luma.render_cache import DevelopmentCache
    cache=DevelopmentCache()
    np.testing.assert_array_equal(develop(image,s,cache=cache),develop(apply_retouch(image,[new]),{**s,'retouch':[]}))
    t=deepcopy(s);t['retouch'][0]['points']=[[.4,.4]]
    assert not np.array_equal(develop(image,s,cache=cache),develop(image,t,cache=cache))
