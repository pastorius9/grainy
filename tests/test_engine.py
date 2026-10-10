import hashlib
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from luma.engine import defaults, load_image, develop, to_linear, to_srgb, export_image, rgb_to_hsv, hsv_to_rgb
from luma.catalog import Catalog
from luma.engine import auto_tone_settings


@pytest.fixture
def source(tmp_path):
    y,x=np.mgrid[0:96,0:144]
    pixels=np.stack([x*255/143,y*255/95,(x+y)*255/238],axis=-1).astype(np.uint8)
    path=tmp_path/'original.png'
    Image.fromarray(pixels).save(path)
    return path,pixels


def test_neutral_roundtrip_preserves_source_pixels(source):
    path,pixels=source
    linear,_=load_image(path)
    before=linear.copy()
    rendered=develop(linear,defaults())
    assert np.max(np.abs(rendered-pixels/255))<1e-5
    assert np.array_equal(linear,before)


def test_exposure_is_linear_stop_and_no_source_mutation():
    source=np.full((20,30,3),.1,np.float32)
    settings=defaults()
    settings['exposure']=1
    output=develop(source,settings)
    assert np.allclose(to_linear(output),.2,atol=1e-5)
    assert np.allclose(source,.1)


def test_black_endpoint_can_crush_midtones_without_dimmed_white():
    ramp=np.linspace(0,1,1001,dtype=np.float32)
    source=to_linear(np.repeat(ramp[None,:,None],3,axis=-1))
    settings=defaults()
    settings['blacks']=-100
    normal=develop(source,settings)[0,:,0]
    assert normal[300]==0 and normal[600]<.34
    assert normal[-1]>.999
    settings['blacks']=-200
    extended=develop(source,settings)[0,:,0]
    assert extended[750]==0
    assert extended[-1]>.999
    assert np.all(np.diff(extended)>=0)
    settings['blacks']=200
    lifted=develop(source,settings)[0,:,0]
    assert lifted[0]>.79 and lifted[-1]>.999


def test_white_endpoint_has_meaningful_extended_range():
    source=to_linear(np.full((12,16,3),.3,np.float32))
    settings=defaults()
    settings['whites']=200
    assert np.all(develop(source,settings)==1)
    settings['whites']=-200
    assert np.allclose(develop(source,settings),.075,atol=1e-5)


def test_black_white_extremes_are_monotonic_and_export_identical(source,tmp_path):
    path,_=source
    linear,_=load_image(path)
    settings=defaults()
    settings.update(blacks=-145,whites=125)
    expected=develop(linear,settings)
    out=tmp_path/'endpoints.png'
    export_image(path,out,settings,'PNG')
    with Image.open(out) as im:
        assert np.array_equal(np.asarray(im),np.uint8(expected*255+.5))


@pytest.mark.parametrize('low,high',[(.2,.7),(.45,.55),(.005,.035),(.88,.96),(0.,1.),(0.,.04)])
def test_auto_tone_leaves_three_percent_headroom_and_keeps_tonal_steps(low,high):
    gray=np.linspace(low,high,10000,dtype=np.float32).reshape(100,100)
    source=to_linear(np.repeat(gray[...,None],3,axis=-1))
    before=source.copy()
    settings,report=auto_tone_settings(source,defaults())
    result=develop(source,settings)
    assert report['status']=='applied'
    black,white=np.percentile(result,[.1,99.9])
    assert black==pytest.approx(.03,abs=.005) and white==pytest.approx(.97,abs=.005),(black,white)
    assert result.min()>=.03-1e-6 and result.max()<=.97+1e-6
    assert np.all(np.diff(result[...,0].ravel()[20:-20])>0)
    assert np.isfinite(result).all()
    assert np.array_equal(before,source)
    assert -200<=settings['blacks']<=200 and -200<=settings['whites']<=200
    assert settings['highlights']==0 and settings['shadows']==0


@pytest.mark.parametrize('format,suffix',[('PNG','.png'),('TIFF 16-bit','.tif')])
def test_auto_tone_margin_survives_export(tmp_path,format,suffix):
    gray=np.tile(np.arange(256,dtype=np.uint8),(32,1))
    path=tmp_path/'gradient.png'
    Image.fromarray(gray).convert('RGB').save(path)
    source,_=load_image(path)
    settings,_=auto_tone_settings(source,defaults())
    target=tmp_path/('auto-tone'+suffix)
    export_image(path,target,settings,format)
    exported,_=load_image(target)
    result=to_srgb(exported)
    assert np.allclose(result,develop(source,settings),atol=.5/255+1e-6)
    assert result.min()==pytest.approx(.03,abs=.5/255+1e-6)
    assert result.max()==pytest.approx(.97,abs=.5/255+1e-6)


def test_auto_tone_is_deterministic_and_ignores_isolated_outliers():
    gray=np.linspace(.25,.65,10000,dtype=np.float32).reshape(100,100)
    gray[0,0]=0; gray[-1,-1]=1
    source=to_linear(np.repeat(gray[...,None],3,axis=-1))
    settings=defaults(); settings.update(saturation=8,grain=4)
    first,report=auto_tone_settings(source,settings)
    second,_=auto_tone_settings(source,first)
    assert first==second
    assert first['saturation']==8 and first['grain']==4
    assert 60<report['input_black']<70 and 160<report['input_white']<170


def test_auto_tone_analyzes_crop_and_does_not_turn_flat_image_into_noise():
    source=to_linear(np.repeat(np.tile(np.linspace(.05,.95,200,dtype=np.float32),(100,1))[...,None],3,axis=-1))
    settings=defaults(); settings['crop']=[.25,0,.75,1]
    adjusted,report=auto_tone_settings(source,settings)
    assert report['input_black']>60 and report['input_white']<195
    assert adjusted['crop']==settings['crop']
    settings=defaults(); settings['exposure']=.5
    adjusted,report=auto_tone_settings(np.full((80,80,3),.15,np.float32),settings)
    assert adjusted==settings and report['status']=='flat'


def test_crop_after_rotation_and_export_pixel_dimensions(source,tmp_path):
    path,_=source
    settings=defaults()
    settings.update(rotation=1,crop=[.25,.25,.75,.75])
    original=hashlib.sha256(path.read_bytes()).hexdigest()
    result=export_image(path,tmp_path/'edited.png',settings,'PNG')
    assert result==(48,72)
    with Image.open(tmp_path/'edited.png') as im:
        assert im.size==(48,72)
        assert 'icc_profile' in im.info
    assert hashlib.sha256(path.read_bytes()).hexdigest()==original


def test_export_never_overwrites_source_or_existing_file(source,tmp_path):
    path,_=source
    before=path.read_bytes()
    with pytest.raises(ValueError):
        export_image(path,path,defaults(),'PNG')
    target=tmp_path/'exists.png'
    target.write_bytes(b'keep-me')
    with pytest.raises(FileExistsError):
        export_image(path,target,defaults(),'PNG')
    assert path.read_bytes()==before
    assert target.read_bytes()==b'keep-me'


def test_tiff_16_bit_roundtrip_and_resize(source,tmp_path):
    import tifffile
    path,_=source
    target=tmp_path/'sixteen.tif'
    export_image(path,target,defaults(),'TIFF 16-bit',longest=72)
    data=tifffile.imread(target)
    assert data.dtype==np.uint16
    assert data.shape==(48,72,3)
    decoded,_=load_image(target)
    assert np.max(np.abs(to_srgb(decoded)-data/65535))<1e-5


def test_color_mixer_hue_roundtrip():
    rgb=np.random.default_rng(4).random((64,64,3),dtype=np.float32)
    actual=hsv_to_rgb(*rgb_to_hsv(rgb))
    assert np.max(np.abs(actual-rgb))<1e-5


def test_extreme_edits_are_finite_and_deterministic(source):
    linear,_=load_image(source[0])
    settings=defaults()
    settings.update(exposure=5,contrast=100,temperature=-100,tint=100,grain=80,
                    clarity=100,sharpen=100,vignette=100,hsl=[[100,-100,100] for _ in range(8)])
    output=develop(linear,settings)
    assert np.isfinite(output).all()
    assert output.min()>=0 and output.max()<=1
    assert np.array_equal(output,develop(linear,settings))


def test_catalog_reopen_preserves_edits_rating_keywords_and_presets(source,tmp_path):
    cat=Catalog(tmp_path/'catalog')
    ident=cat.add(source[0])
    assert cat.add(source[0])==ident
    settings=defaults()
    settings.update(exposure=.73,crop=[.1,.2,.8,.9])
    cat.edit(ident,settings,'test')
    cat.update(ident,rating=5,keywords='여행, 필름',flag=1)
    cat.save_preset('test-preset',settings)
    cat.close()
    cat=Catalog(tmp_path/'catalog')
    photo=cat.photo(ident)
    assert photo['settings']==settings
    assert photo['rating']==5 and photo['keywords']=='여행, 필름' and photo['flag']==1
    assert len(cat.histories(ident))==1
    assert cat.presets()['test-preset']['exposure']==.73
    assert cat.presets()['test-preset']['crop'] is None
    cat.close()


def test_geometry_copies_unless_develop_asks_for_a_view(monkeypatch):
    from luma.engine import geometry
    monkeypatch.setenv('GRAINY_GPU', '0')
    a = np.random.default_rng(2).random((40, 60, 3), dtype=np.float32)
    kept = a.copy()
    plain = geometry(a, defaults())
    assert plain is not a and plain.flags.writeable and not np.shares_memory(plain, a) and np.array_equal(plain, a)
    view = geometry(a, defaults(), copy=False)
    assert np.shares_memory(view, a) and not view.flags.writeable and a.flags.writeable
    for changed in ({'rotation': 1}, {'flip': True}, {'distortion': 5}, {'crop': [.1, .1, .9, .9]}):
        result = geometry(a, {**defaults(), **changed}, copy=False)
        assert not np.shares_memory(result, a) and np.array_equal(result, geometry(a, {**defaults(), **changed})), changed
    # A writable source stays untouched through the CPU stages.
    for edits in ({'exposure': .5}, {'exposure': .5, 'saturation': 20, 'sharpen': 30, 'grain': 10}):
        develop(a, {**defaults(), **edits})
        assert np.array_equal(a, kept)


def test_srgb_output_is_clipped_once_with_identical_pixels():
    from luma.engine import output_rgb
    a = np.random.default_rng(3).normal(.5, .6, (50, 70, 3)).astype(np.float32)
    s = {**defaults(), 'exposure': .8, 'contrast': 40}
    assert np.array_equal(develop(a, s), output_rgb(develop(a, s, output_space=None)))


def _read_tiff(path):
    import tifffile
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        return page.asarray(), page.tags.get(34675).value, page.photometric


def test_grayscale_export_writes_neutral_photos_as_one_channel_with_matching_tones(source, tmp_path):
    import imagecodecs
    source = source[0]
    from luma.colorio import profile
    mono = {**defaults(), 'monochrome': True, 'exposure': .3, 'contrast': 20}
    for fmt, ext in (('TIFF 8-bit', 'tif'), ('TIFF 16-bit', 'tif')):
        rgb, gray = tmp_path/f'rgb-{fmt[-6:]}.{ext}', tmp_path/f'gray-{fmt[-6:]}.{ext}'
        export_image(source, rgb, mono, fmt)
        export_image(source, gray, mono, fmt, grayscale=True)
        rgb_pixels, _, _ = _read_tiff(rgb)
        gray_pixels, icc, photometric = _read_tiff(gray)
        assert gray_pixels.ndim == 2 and int(photometric) == 1 and imagecodecs.cms_info(icc)['colorspace'] == 'gray'
        assert np.array_equal(gray_pixels, rgb_pixels[..., 1]) and np.array_equal(rgb_pixels[..., 0], rgb_pixels[..., 1])
    # The gray profile displays each level exactly like sRGB (v, v, v).
    levels = np.arange(256, dtype=np.uint8)[None, :]
    shown = imagecodecs.cms_transform(levels, profile('Gray sRGB'), profile('sRGB'), colorspace='gray', outcolorspace='rgb',
                                      outdtype='uint8', intent=1)[0]
    assert np.array_equal(shown, np.repeat(np.arange(256)[:, None], 3, axis=1))
    jpeg = tmp_path/'gray.jpg'
    export_image(source, jpeg, mono, 'JPEG', grayscale=True, keep_metadata=True, user_metadata={'caption': 'mono'})
    with Image.open(jpeg) as image:
        assert image.mode == 'L' and imagecodecs.cms_info(image.info['icc_profile'])['colorspace'] == 'gray'
    # Colour photos, other output spaces and the option switched off stay RGB.
    for settings, space, flag in ((defaults(), 'sRGB', True), (mono, 'Display P3', True), (mono, 'sRGB', False)):
        target = tmp_path/f'rgb-{space}-{flag}-{len(settings)}.jpg'
        export_image(source, target, settings, 'JPEG', color_space=space, grayscale=flag)
        with Image.open(target) as image:
            assert image.mode == 'RGB'


def test_dithered_8bit_export_is_reproducible_keeps_pure_black_white_and_tracks_gradients(tmp_path):
    from luma.engine import _quantize8_dithered, _quantize
    # A very shallow ramp: plain rounding gives wide flat bands; dithering follows the ramp on average.
    x = np.linspace(.40, .41, 1200, dtype=np.float32)
    ramp = np.repeat(np.repeat(x[None, :, None], 300, axis=0), 3, axis=2)
    plain = _quantize(ramp.copy(), 255, np.uint8).astype(np.float32)
    dithered = _quantize8_dithered(ramp).astype(np.float32)
    assert np.array_equal(dithered, _quantize8_dithered(ramp))                  # fixed noise
    assert np.array_equal(dithered[..., 0], dithered[..., 2])                   # same noise in every channel
    target = x*255
    column_error = lambda q: np.abs(q[..., 0].mean(axis=0) - target)
    assert column_error(dithered).mean() < column_error(plain).mean() / 3
    assert np.abs(dithered - ramp*255).max() <= 1.5
    extremes = np.zeros((40, 50, 3), np.float32);extremes[:, 25:] = 1
    assert np.array_equal(_quantize8_dithered(extremes), np.uint8(extremes*255))


def test_previous_export_settings_without_new_options_default_to_on():
    from luma.quick_export import previous, preset
    class Catalog:
        def __init__(self, value):self.value = value
        def preference(self, key):return self.value
    old = dict(folder='C:/out', format='JPEG', quality=90, longest=0, color_space='sRGB', keep_metadata=False, naming='{stem}')
    assert previous(Catalog(old))['grayscale'] is True and previous(Catalog(old))['dither'] is True
    assert previous(Catalog({**old, 'dither': False}))['dither'] is False
    assert previous(Catalog({**old, 'dither': 'yes'})) is None
    assert preset('small')['grayscale'] and preset('large')['dither']
