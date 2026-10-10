from copy import deepcopy
import cv2
import numpy as np
import pytest
from luma.dust import repair,Cancelled
from luma.dust_shapes import manual,ellipses,paint


def sample(seed=31591,grain=.035,shape=(240,360)):
    rng=np.random.default_rng(seed);h,w=shape;y,x=np.mgrid[:h,:w]
    luminance=rng.normal(0,grain,shape)
    clean=np.stack([.35+.12*x/w+luminance,.44+.1*y/h+luminance,.53+.08*x/w+luminance],-1).astype(np.float32)
    spots=[manual([[.15,.22],[.3,.65],[.75,.48]],shape,14)]
    mask=np.zeros(shape,np.uint8);paint(mask,ellipses(spots[0],shape))
    dirty=clean.copy();dirty[mask>0]*=.1
    core=cv2.erode(mask,np.ones((5,5),np.uint8))>0
    return clean,dirty,spots,mask,core


@pytest.mark.parametrize('grain',[.012,.035,.07])
def test_texture_retains_grain_and_rgb_correlation_without_altering_unselected_pixels(grain):
    clean,dirty,spots,mask,core=sample(grain=grain);before=dirty.copy();saved=deepcopy(spots)
    smooth=repair(dirty,spots);fixed=repair(dirty,spots,method='texture')
    residual=lambda a:(a-cv2.GaussianBlur(a,(0,0),1.5))[core]
    reference=residual(clean);new=residual(fixed);old=residual(smooth)
    ratio=float(new.std()/reference.std())
    assert .75<ratio<1.25 and old.std()<reference.std()*.25
    assert np.corrcoef(new.T).min()>.98
    assert np.abs(cv2.GaussianBlur(fixed,(0,0),4)-cv2.GaussianBlur(clean,(0,0),4))[core].mean()<.018
    np.testing.assert_array_equal(fixed[mask==0],dirty[mask==0]);np.testing.assert_array_equal(dirty,before)
    np.testing.assert_array_equal(repair(dirty,spots,method='texture'),fixed)
    inplace=dirty.copy();assert repair(inplace,spots,copy=False,method='texture') is inplace
    np.testing.assert_array_equal(inplace,fixed);assert saved==spots


@pytest.mark.parametrize('shape',[(2,3),(64,87),(240,360)])
def test_constant_field_tiny_images_and_border_selection(shape):
    h,w=shape;image=np.full((h,w,3),.625,np.float32)
    spot=manual([[0,0],[.7,.08]],shape,min(12,min(shape)))
    mask=np.zeros(shape,np.uint8);paint(mask,ellipses(spot,shape));image[mask>0]=.1
    fixed=repair(image,[spot],method='texture')
    np.testing.assert_array_equal(fixed[mask==0],image[mask==0])
    assert np.isfinite(fixed).all()
    if np.any(mask==0):assert np.max(np.abs(fixed[mask>0]-.625))<1e-5


def test_all_selected_regions_are_excluded_as_texture_donors():
    from luma.dust_texture import restore
    h,w=120,180;clean=np.full((h,w,3),.55,np.float32);mask=np.zeros((h,w),np.uint8)
    mask[48:64,65:86]=255;mask[35:40,95:130]=255
    dirty=clean.copy();dirty[mask>0]=np.random.default_rng(30).uniform(0,4,(np.count_nonzero(mask),3))
    stats={};restored=restore(dirty,clean.copy(),mask,stats=stats)
    assert stats['transferred_pixels']>0
    np.testing.assert_allclose(restored,clean,atol=2e-7)
    # No valid neighboring donor: keep the supplied smooth repair exactly.
    all_mask=np.full((h,w),255,np.uint8);stats={};result=restore(dirty,clean.copy(),all_mask,stats=stats)
    assert stats['fallback_pixels']==h*w;np.testing.assert_array_equal(result,clean)


def test_cancelled_texture_does_not_write_partial_texture_and_rejects_unknown_method():
    from luma.dust_texture import restore
    clean,dirty,spots,mask,core=sample();smooth=repair(dirty,spots);before=smooth.copy()
    class CancelDuringWork:
        calls=0
        def is_set(self):self.calls+=1;return self.calls>8
    with pytest.raises(Cancelled):restore(dirty,smooth,mask,cancel=CancelDuringWork())
    np.testing.assert_array_equal(smooth,before)
    with pytest.raises(ValueError):repair(dirty,spots,method='invalid')


def test_method_survives_sidecar_export_and_old_edits_keep_smooth_pixels(tmp_path):
    from PIL import Image
    import tifffile
    from luma.engine import load_image,defaults,develop,export_image
    from luma.catalog import Catalog
    from luma.library import write_sidecar,read_sidecar
    from luma.processing import apply_retouch
    clean,dirty,spots,mask,_=sample();path=tmp_path/'film.png'
    Image.fromarray(np.uint8(np.clip(dirty,0,1)*255)).save(path)
    old=dict(type='dust',version=3,spots=spots)
    np.testing.assert_array_equal(apply_retouch(dirty,[old]),repair(dirty,spots))
    operation=dict(type='dust',version=4,repair_method='texture',spots=spots)
    np.testing.assert_array_equal(apply_retouch(dirty,[operation]),repair(dirty,spots,method='texture'))
    catalog=Catalog(tmp_path/'catalog')
    try:
        ident=catalog.add(path);settings=defaults();settings['retouch']=[operation];catalog.edit(ident,settings)
        sidecar=tmp_path/'film.xmp';write_sidecar(catalog.photo(ident),sidecar)
        restored=read_sidecar(sidecar)['settings'];assert restored['retouch']==settings['retouch']
        output=tmp_path/'out.tif';export_image(path,output,restored,format='TIFF 16-bit')
        expected=np.uint16(np.clip(develop(load_image(path)[0],settings),0,1)*65535+.5)
        np.testing.assert_array_equal(tifffile.imread(output),expected)
    finally:catalog.close()


def test_tone_changes_reuse_texture_repair_and_changed_method_invalidates_it(monkeypatch):
    from luma.engine import defaults,develop
    from luma.render_cache import DevelopmentCache
    import luma.dust_texture as module
    _,dirty,spots,_,_=sample();calls=[];real=module.restore
    def record(*args,**kwargs):calls.append(True);return real(*args,**kwargs)
    monkeypatch.setattr(module,'restore',record)
    s=defaults();s['retouch']=[dict(type='dust',version=4,repair_method='texture',spots=spots)]
    cache=DevelopmentCache(32*1024*1024);develop(dirty,s,cache=cache)
    s['exposure']=.5;cached=develop(dirty,s,cache=cache);assert len(calls)==1
    np.testing.assert_array_equal(cached,develop(dirty,s))
    s['retouch'][0]['repair_method']='smooth';smooth=develop(dirty,s,cache=cache)
    np.testing.assert_array_equal(smooth,develop(dirty,s));assert not np.array_equal(smooth,cached)
