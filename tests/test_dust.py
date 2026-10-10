from copy import deepcopy
from threading import Event
import numpy as np
import cv2
import pytest
from luma.dust import Options,detect,repair,Cancelled,spot_bounds
from luma.engine import defaults,develop,to_linear,geometry
from luma.processing import apply_retouch
from luma.render_cache import DevelopmentCache


def fixture(size=(480,640),grain=0.):
    h,w=size;y,x=np.mgrid[:h,:w]
    clean=(.38+.18*x/w+.1*y/h).astype(np.float32)
    if grain:clean+=np.random.default_rng(41).normal(0,grain,clean.shape).astype(np.float32)
    dusty=clean.copy();truth=[]
    for px,py,r,kind in [(.2,.25,4,'dark'),(.7,.3,7,'bright'),(.45,.65,10,'dark'),(.8,.8,3,'bright')]:
        cx,cy=round(px*w),round(py*h);mask=(x-cx)**2+(y-cy)**2<=r*r
        dusty[mask]+=(-.25 if kind=='dark' else .25);truth.append((cx,cy,r,kind))
    return clean,dusty,truth


def matched(spots,truth,shape):
    h,w=shape
    return [any(abs(s['x']*(w-1)-x)<3 and abs(s['y']*(h-1)-y)<3 and s['polarity']==kind for s in spots) for x,y,r,kind in truth]


@pytest.mark.parametrize('grain',[0.,.012,.025])
def test_bright_dark_dust_detection_with_film_grain(grain):
    clean,dirty,truth=fixture(grain=grain);before=dirty.copy()
    result=detect(dirty)
    assert all(matched(result['spots'],truth,dirty.shape))
    assert result['total']<=len(truth)+2
    np.testing.assert_array_equal(dirty,before)


@pytest.mark.parametrize('kind',['dark','bright'])
def test_polarity_and_sizes(kind):
    _,dirty,truth=fixture()
    spots=detect(dirty,Options(polarity=kind))['spots']
    assert len(spots)==2 and {s['polarity'] for s in spots}=={kind}
    spots=detect(dirty,Options(minimum=10,maximum=30))['spots']
    assert len(spots)==2


def test_no_candidates_on_clean_gradient_edges_or_grain():
    clean,_,_=fixture(grain=.016)
    clean[150:,:220]+=.2;clean[:150,400:]-=.18
    cv2.line(clean,(300,40),(300,400),.1,2)
    assert detect(clean)['spots']==[]
    for value in (0.,.4,1.):assert detect(np.full((80,100),value,np.float32))['spots']==[]


def test_soft_sensor_spot_and_sensitivity():
    y,x=np.mgrid[:200,:260];clean=np.full(x.shape,.6,np.float32)
    dirty=clean-.035*np.exp(-((x-130)**2+(y-100)**2)/(2*5**2)).astype(np.float32)
    assert len(detect(dirty,Options(sensitivity=80))['spots'])==1
    assert detect(dirty,Options(sensitivity=5))['spots']==[]


def test_tile_seams_do_not_duplicate_or_lose_candidates():
    _,dirty,truth=fixture((512,640))
    reference=detect(dirty,tile_size=1024)['spots']
    tiled=detect(dirty,tile_size=128)['spots']
    assert len(tiled)==len(reference)==4
    for a,b in zip(sorted(tiled,key=lambda s:s['x']),sorted(reference,key=lambda s:s['x'])):
        for key in ('x','y','rx','ry'):assert a[key]==pytest.approx(b[key],abs=1e-7)


def test_candidates_are_bounded_and_cancellation_is_cooperative():
    _,dirty,_=fixture();result=detect(dirty,limit=2)
    assert result['total']==4 and result['truncated'] and len(result['spots'])==2
    cancel=Event();progress=[]
    def update(done,total):progress.append(done);cancel.set()
    with pytest.raises(Cancelled):detect(dirty,cancel=cancel,progress=update,tile_size=128)
    assert progress==[1]
    for options in (Options(minimum=20,maximum=5),Options(sensitivity=0),Options(polarity='unknown')):
        with pytest.raises(ValueError):detect(dirty,options)


def test_selected_repair_reduces_defect_and_preserves_other_pixels_exactly():
    clean,dirty,truth=fixture();image=np.repeat(to_linear(dirty)[...,None],3,-1);before=image.copy()
    spots=detect(dirty)['spots'];chosen=[next(s for s in spots if s['polarity']=='dark')]
    actual=repair(image,chosen);x,y,rx,ry=spot_bounds(chosen[0],image.shape)
    yy,xx=np.mgrid[:dirty.shape[0],:dirty.shape[1]];region=((xx-x)/rx)**2+((yy-y)/ry)**2<=1
    np.testing.assert_array_equal(actual[~region],before[~region]);np.testing.assert_array_equal(image,before)
    target=to_linear(clean)[region]
    assert np.abs(actual[region,0]-target).mean()<np.abs(before[region,0]-target).mean()*.08
    np.testing.assert_array_equal(repair(image,[]),image)


def test_overlapping_repairs_are_order_independent_and_work_on_tiny_previews():
    clean,dirty,_=fixture();image=np.repeat(dirty[...,None],3,-1)
    a=detect(dirty)['spots'][0];b={**a,'x':a['x']+.003}
    np.testing.assert_array_equal(repair(image,[a,b]),repair(image,[b,a]))
    assert np.isfinite(repair(image[:2,:3],[a])).all()
    with pytest.raises(ValueError):repair(image,[{**a,'rx':float('nan')}])


def test_retouch_geometry_cache_legacy_and_source_immutability():
    _,dirty,_=fixture((240,320));source=np.repeat(to_linear(dirty)[...,None],3,-1);before=source.copy()
    spots=detect(dirty)['spots'];operation=dict(type='dust',version=1,spots=spots)
    settings=defaults();settings.update(retouch=[operation],rotation=1,flip=True,crop=[.1,.2,.9,.8])
    expected=develop(repair(source,spots),{**settings,'retouch':[]})
    cache=DevelopmentCache();np.testing.assert_array_equal(develop(source,settings,cache=cache),expected)
    settings['retouch'][0]['spots']=spots[:1]
    np.testing.assert_array_equal(develop(source,settings,cache=cache),develop(source,settings))
    np.testing.assert_array_equal(source,before)
    legacy=[dict(type='inpaint',points=[[.2,.25]],radius=.035,feather=20)]
    np.testing.assert_array_equal(apply_retouch(source,legacy),apply_retouch(source,legacy+[{**operation,'enabled':False}]))


def test_tiff16_export_persistence_and_original_hash(tmp_path):
    import hashlib,json,tifffile
    from PIL import Image
    from luma.engine import export_image,load_image
    _,dirty,_=fixture((160,240));path=tmp_path/'dusty.png'
    Image.fromarray(np.uint8(np.repeat(dirty[...,None],3,-1)*255)).save(path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest();source,_=load_image(path)
    spots=detect(dirty)['spots'];spots[0]['scale']=65
    spots.append(dict(x=.31,y=.44,rx=.025,ry=.03,manual=True,polarity='manual',scale=135))
    settings=defaults();settings.update(retouch=[dict(type='dust',version=2,spots=spots)],rotation=1,crop=[.1,.15,.9,.85])
    settings=json.loads(json.dumps(settings));output=tmp_path/'clean.tif'
    export_image(path,output,settings,format='TIFF 16-bit')
    expected=np.uint16(np.clip(develop(source,settings),0,1)*65535+.5)
    np.testing.assert_array_equal(tifffile.imread(output),expected)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest


def edge_fixture():
    y,x=np.mgrid[:220,:300];clean=np.full((220,300),.88,np.float32);clean[y<95]=.2
    dirty=clean.copy();dirty[(x-150)**2+(y-100)**2<=9]-=.24
    dirty[(x-65)**2+(y-155)**2<=25]-=.28
    return clean,dirty


def test_detailed_candidates_recover_edge_dust_without_promoting_it():
    clean,dirty=edge_fixture();ordinary=detect(dirty);detailed=detect(dirty,Options(detailed=True))
    edge=[s for s in detailed['spots'] if abs(s['x']*299-150)<3 and abs(s['y']*219-100)<3]
    assert len(edge)==1 and edge[0]['review']
    assert not any(abs(s['x']*299-150)<3 and abs(s['y']*219-100)<3 for s in ordinary['spots'])
    assert [s for s in detailed['spots'] if not s['review']]==ordinary['spots']
    assert detect(clean,Options(detailed=True))['spots']==[]


def test_detailed_candidate_limit_counts_deduplicated_spots():
    _,dirty,_=fixture();result=detect(dirty,Options(detailed=True),limit=2,tile_size=128)
    assert result['total']==4 and len(result['spots'])==2 and result['truncated']
    assert not any(s['review'] for s in result['spots'])


def test_detailed_rejects_repeating_clean_texture():
    y,x=np.mgrid[:260,:380].astype(np.float32)
    clean=.28+.3*x/380+.16*y/260+.06*np.sin(x*.48)*np.sin(y*.31)
    clean+=np.random.default_rng(812).normal(0,.006,clean.shape).astype(np.float32)
    assert detect(clean,Options(detailed=True))['spots']==[]


@pytest.mark.parametrize('size',[1,2,3,17,64,501])
def test_fast_statistics_match_numpy_without_mutating_inputs(size):
    from luma.dust import _median,_quantiles
    rng=np.random.default_rng(570+size)
    for values in (rng.random(size,dtype=np.float32),rng.uniform(-1,1,size).astype(np.float32),np.ones(size,np.float32)):
        before=values.copy();q=[10,15,60,85,90]
        assert _median(values)==float(np.median(values))
        np.testing.assert_array_equal(_quantiles(values,*[x/100 for x in q]),[np.percentile(values,x) for x in q])
        np.testing.assert_array_equal(values,before)


def test_repeated_shape_needs_matching_intensity_not_only_correlation():
    from luma.dust import _repeated_detail
    pattern=np.full((120,120),.6,np.float32)
    for y in range(15,115,16):
        for x in range(15,115,16):cv2.circle(pattern,(x,y),2,.35,-1)
    clean=cv2.GaussianBlur(pattern,(0,0),.65)
    assert _repeated_detail(clean,45,45,5,5)
    dirty=pattern.copy();cv2.circle(dirty,(47,47),2,.15,-1)
    dirty=cv2.GaussianBlur(dirty,(0,0),.65)
    assert not _repeated_detail(dirty,45,45,5,5)


def test_spot_emphasis_is_symmetric_bounded_and_does_not_modify_source():
    from luma.dust import visualize
    _,gray,_=fixture();before=gray.copy()
    a=visualize(gray,30);b=visualize(gray,90)
    assert a.dtype==np.float32 and a.shape==gray.shape and np.all((a>=0)&(a<=1))
    assert np.all(b>=a) and np.max(b-a)>.2
    # Float32 blur rounding is amplified by the display-only contrast gain.
    np.testing.assert_allclose(visualize(1-gray,30),a,atol=4e-6)
    np.testing.assert_array_equal(gray,before)
    assert not visualize(np.full((20,30),.5,np.float32)).any()


def test_repair_resize_preserves_legacy_and_unselected_pixels():
    image=np.random.default_rng(992).uniform(.05,.8,(101,151,3)).astype(np.float32)
    spot=dict(x=.5,y=.5,rx=.08,ry=.09)
    np.testing.assert_array_equal(repair(image,[spot]),repair(image,[{**spot,'scale':100}]))
    reduced=repair(image,[{**spot,'scale':50}]);y,x=np.mgrid[:101,:151]
    mask=((x-75)/(.08*151*.5))**2+((y-50)/(.09*101*.5))**2<=1
    np.testing.assert_array_equal(reduced[~mask],image[~mask]);assert np.any(reduced[mask]!=image[mask])
    for invalid in (0,300,float('nan')):
        with pytest.raises(ValueError):repair(image,[{**spot,'scale':invalid}])


@pytest.mark.parametrize('method', ['smooth', 'texture'])
def test_parallel_repair_waves_equal_the_sequential_order(monkeypatch, method):
    # Crowded, overlapping and isolated spots: waves on the pixel workers must reproduce the serial result.
    from luma import pixel_jobs
    rng = np.random.default_rng(21)
    image = np.clip(rng.normal(.5, .15, (420, 640, 3)), 0, 1).astype(np.float32)
    spots = [{'x': float(x), 'y': float(y), 'rx': float(r), 'ry': float(r*rng.uniform(.6, 1.4)), 'polarity': 'dark'}
             for x, y, r in zip(rng.uniform(0, 1, 260), rng.uniform(0, 1, 260), rng.uniform(.002, .02, 260))]
    parallel = repair(image, spots, method=method)
    monkeypatch.setattr(pixel_jobs, 'WORKERS', 1)
    assert np.array_equal(parallel, repair(image, spots, method=method))


def test_heal_bounds_search_matches_full_frame_mask():
    from luma.processing import brush_mask
    for shape, points, radius in (((300, 500), [[.1, .2], [.4, .25], [.45, .6]], .03), ((200, 200), [[0, 0]], .05),
                                  ((120, 900), [[.99, .5], [1.2, .5]], .02)):
        mask, bounds = brush_mask(shape, points, radius, 60, with_bounds=True)
        ys, xs = np.where(mask > 0)
        inner = np.where(mask[bounds[0]:bounds[1], bounds[2]:bounds[3]] > 0)
        assert np.array_equal(ys, inner[0]+bounds[0]) and np.array_equal(xs, inner[1]+bounds[2])
        assert np.array_equal(mask, brush_mask(shape, points, radius, 60))
