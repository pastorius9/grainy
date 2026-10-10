from copy import deepcopy
from threading import Event
import cv2
import numpy as np
import pytest
from luma.dust import Options,detect,repair,Cancelled
from luma.dust_shapes import ellipses,paint,manual,MAX_PARTS
from luma.dust_scratches import candidates


def fixture(seed=96030,grain=.012):
    h,w=600,900;y,x=np.mgrid[:h,:w]
    clean=(.38+.18*x/w+.12*y/h+np.random.default_rng(seed).normal(0,grain,(h,w))).astype(np.float32)
    dirty=clean.copy();truth=[]
    for points,width,kind in [([(65,85),(350,85)],4,'dark'),
        ([(470,70),(540,105),(595,185),(725,230)],3,'bright'),
        ([(65,250),(95,345),(190,405),(330,310)],6,'dark'),
        ([(515,355),(740,510)],9,'bright')]:
        mask=np.zeros((h,w),np.uint8);cv2.polylines(mask,[np.array(points,np.int32)],False,255,width)
        alpha=cv2.GaussianBlur(mask.astype(np.float32)/255,(0,0),.65)
        dirty+=alpha*(.28 if kind=='bright' else -.28)
        truth.append(dict(mask=mask,core=alpha>.7,polarity=kind))
    return clean,dirty,truth


def footprint(spot,shape):
    mask=np.zeros(shape[:2],np.uint8);paint(mask,ellipses(spot,shape));return mask>0


def test_detect_and_repair_curved_filaments_without_filling_the_enclosing_oval():
    clean,dirty,truth=fixture();before=dirty.copy()
    found=candidates(dirty,Options(scratches=True));assert len(found)==4
    for target in truth:
        matched=[s for s in found if s['polarity']==target['polarity']]
        best=max(matched,key=lambda s:np.count_nonzero(footprint(s,dirty.shape)&target['core']))
        mask=footprint(best,dirty.shape)
        assert np.count_nonzero(mask&target['core'])/target['core'].sum()>=.98
        assert np.count_nonzero(mask&(target['mask']>0))/np.count_nonzero(mask|(target['mask']>0))>.4
        assert best['review'] and best['detail_kind']=='scratch' and 2<len(best['parts'])<=MAX_PARTS
    selected=[s for s in found if s['polarity']=='dark']
    image=np.repeat(dirty[...,None],3,-1);repaired=repair(image,selected)
    union=np.any([footprint(s,dirty.shape) for s in selected],axis=0)
    np.testing.assert_array_equal(repaired[~union],image[~union])
    core=np.any([t['core'] for t in truth if t['polarity']=='dark'],axis=0)
    assert np.abs(repaired[...,0]-clean)[core].mean()<np.abs(dirty-clean)[core].mean()*.12
    # This point lies within the curved defect's bounding oval but outside it.
    np.testing.assert_array_equal(repaired[300,170],image[300,170])
    np.testing.assert_array_equal(dirty,before)


def test_optional_stage_preserves_prior_candidates_and_makes_progress_cancellable():
    clean,dirty,_=fixture();base=detect(dirty,Options(detailed=True,soft=True),limit=5000)
    progress=[];result=detect(dirty,Options(detailed=True,soft=True,scratches=True),limit=5000,progress=lambda a,b:progress.append((a,b)))
    assert result['spots'][:len(base['spots'])]==base['spots']
    assert len(result['spots'])>len(base['spots']) and progress[-1][0]==progress[-1][1]
    limited=detect(dirty,Options(scratches=True),limit=2)
    assert limited['truncated'] and len(limited['spots'])==2 and all(s.get('detail_kind')=='scratch' for s in limited['spots'])
    assert all(a<=b for a,b in progress) and all(a[0]<=b[0] for a,b in zip(progress,progress[1:]))
    cancel=Event()
    with pytest.raises(Cancelled):candidates(dirty,Options(scratches=True),cancel=cancel,progress=lambda *_:cancel.set(),tile_size=128)
    with pytest.raises(ValueError):Options(scratch_width=1).validate()


def test_clean_gradient_grain_and_step_edge_do_not_become_long_defects():
    clean,_,_=fixture(grain=.025)
    clean[260:,500:]+=.22
    assert candidates(clean,Options(scratches=True))==[]


def test_compact_jagged_shapes_are_rejected_but_looped_hair_keeps_its_hole():
    gray=np.full((260,460),.6,np.float32)
    for index in range(4):
        center=np.array([50+index*105,50]);angles=np.linspace(0,2*np.pi,24,endpoint=False)
        radii=np.where(np.arange(24)%2,12,19)
        polygon=(center+np.stack((np.cos(angles),np.sin(angles)),axis=-1)*radii[:,None]).round().astype(np.int32)
        cv2.fillPoly(gray,[polygon],.18)
    assert candidates(gray,Options(scratches=True,scratch_width=12,reduce_patterns=True))==[]
    assert candidates(gray,Options(scratches=True,scratch_width=12))==[]
    cv2.circle(gray,(230,175),45,.18,3)
    found=candidates(gray,Options(scratches=True,reduce_patterns=True));assert len(found)==1
    mask=footprint(found[0],gray.shape);assert not mask[175,230]
    repaired=repair(np.repeat(gray[...,None],3,-1),found)
    assert repaired[175,230,0]==gray[175,230]


def test_tile_seams_polarity_and_width():
    _,dirty,_=fixture()
    options=Options(scratches=True,polarity='dark')
    whole=candidates(dirty,options,tile_size=1024);tiled=candidates(dirty,options,tile_size=128)
    assert whole==tiled and len(tiled)==2
    # Integer OpenCV stroke thickness includes rounded endpoints and can be
    # wider than the requested number. Use literal three/twelve-pixel strips.
    strips=np.full((200,350),.5,np.float32);strips[70:73,40:270]=.85;strips[120:132,40:270]=.85
    narrow=candidates(strips,Options(scratches=True,polarity='bright',scratch_width=4))
    assert len(narrow)==1 and narrow[0]['diameter']<=5


def test_manual_stroke_width_adjustment_resize_and_order_independence():
    shape=(180,260);spot=manual([[.15,.2],[.3,.55],[.7,.65]],shape,8)
    image=np.random.default_rng(302).uniform(.1,.8,(*shape,3)).astype(np.float32);saved=deepcopy(spot)
    small=footprint({**spot,'scale':50},shape);large=footprint({**spot,'scale':180},shape)
    assert np.all(~small|large) and large.sum()>small.sum()*2
    assert [a[:2] for a in ellipses({**spot,'scale':50},shape)]==[a[:2] for a in ellipses(spot,shape)]
    dot=dict(x=.3,y=.55,rx=.03,ry=.035,polarity='manual')
    np.testing.assert_array_equal(repair(image,[spot,dot]),repair(image,[dot,spot]))
    for target in ((2,3),(90,130),(180,260)):
        assert np.isfinite(repair(np.ones((*target,3),np.float32),[spot])).all()
    assert spot==saved


def test_filament_export_sidecar_and_original_preservation(tmp_path):
    import hashlib,tifffile
    from PIL import Image
    from luma.catalog import Catalog
    from luma.engine import load_image,export_image,develop,defaults
    from luma.library import write_sidecar,read_sidecar
    _,dirty,_=fixture();source=tmp_path/'source.png'
    Image.fromarray(np.uint8(np.clip(np.repeat(dirty[...,None],3,-1),0,1)*255)).save(source)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    spot=candidates(dirty,Options(scratches=True))[0];spot['scale']=125
    settings=defaults();settings.update(retouch=[dict(type='dust',version=3,spots=[spot])],rotation=1,crop=[.1,.15,.9,.85])
    catalog=Catalog(tmp_path/'catalog')
    try:
        ident=catalog.add(source);catalog.edit(ident,settings)
        sidecar=tmp_path/'source.xmp';write_sidecar(catalog.photo(ident),sidecar)
        restored=read_sidecar(sidecar)['settings'];assert restored['retouch']==settings['retouch']
        output=tmp_path/'out.tif';export_image(source,output,restored,format='TIFF 16-bit')
        expected=np.uint16(np.clip(develop(load_image(source)[0],restored),0,1)*65535+.5)
        np.testing.assert_array_equal(tifffile.imread(output),expected)
    finally:catalog.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest


@pytest.mark.parametrize('parts',[[],[dict(x=.5,y=.5,rx=.1,ry=float('nan'))],[dict(x=1.1,y=.5,rx=.1,ry=.1)],
    [dict(x=.5,y=.5,rx=.1,ry=.1,parts=[])],[dict(x=.5,y=.5,rx=.1,ry=.1)]*(MAX_PARTS+1)])
def test_malformed_footprints_are_rejected(parts):
    from luma.dust_store import DustReviewStore
    spot=dict(x=.5,y=.5,rx=.1,ry=.1,polarity='dark',parts=parts)
    with pytest.raises(ValueError):repair(np.zeros((20,30,3),np.float32),[spot])
    with pytest.raises(ValueError):DustReviewStore.validate_spots([spot])
