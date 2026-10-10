"""File-format regression fixtures, checked against samples and independent readers."""
import hashlib
import io
import struct
from pathlib import Path
import imagecodecs
import numpy as np
import piexif
import pytest
import tifffile
from PIL import Image, ImageCms
from luma.engine import load_image, export_image, defaults, to_linear, to_srgb, develop
from luma.colorio import profile, export_exif, xmp_metadata
from luma.rasterio import png_chunk


def png_with_chunks(path,pixels,chunks):
    data=imagecodecs.png_encode(pixels)
    path.write_bytes(data[:33]+b''.join(png_chunk(k,v) for k,v in chunks)+data[33:])


def test_png16_input_export_preserves_thousands_of_levels_and_source(tmp_path):
    # Dense adjacent values show any hidden RGB8 conversion immediately.
    pixels=np.repeat(np.arange(12000,16096,dtype=np.uint16)[None,:,None],3,-1)
    source=tmp_path/'dense.png';source.write_bytes(imagecodecs.png_encode(pixels))
    original=hashlib.sha256(source.read_bytes()).digest()
    linear,info=load_image(source)
    assert info['bit_depth']==16 and len(np.unique(linear[...,0]))==4096
    np.testing.assert_allclose(to_srgb(linear),pixels/65535,atol=2e-7)
    output=tmp_path/'export.png';export_image(source,output,defaults(),'PNG 16-bit')
    actual=imagecodecs.png_decode(output.read_bytes())
    assert actual.dtype==np.uint16
    np.testing.assert_array_equal(actual,pixels)
    assert Image.open(output).info['icc_profile']
    assert hashlib.sha256(source.read_bytes()).digest()==original


@pytest.mark.parametrize('orientation',range(1,9))
@pytest.mark.parametrize('planar,byteorder',[('contig','<'),('separate','>')])
def test_tiff_native_precision_orientation_and_planar(tmp_path,orientation,planar,byteorder):
    values=(np.arange(2*5*3).reshape(2,5,3)*1807).astype(np.uint16)
    source=tmp_path/'oriented.tif'
    tifffile.imwrite(source,np.moveaxis(values,-1,0) if planar=='separate' else values,
        photometric='rgb',planarconfig=planar,byteorder=byteorder,metadata=None,
        extratags=[(274,'H',1,orientation,False)])
    operations={2:Image.Transpose.FLIP_LEFT_RIGHT,3:Image.Transpose.ROTATE_180,
        4:Image.Transpose.FLIP_TOP_BOTTOM,5:Image.Transpose.TRANSPOSE,
        6:Image.Transpose.ROTATE_270,7:Image.Transpose.TRANSVERSE,8:Image.Transpose.ROTATE_90}
    expected=np.stack([np.asarray(Image.fromarray(values[...,c]).transpose(operations[orientation]))
        for c in range(3)],-1) if orientation!=1 else values
    linear,info=load_image(source)
    np.testing.assert_allclose(to_srgb(linear),expected/65535,atol=2e-7)
    assert (info['height'],info['width'])==expected.shape[:2]
    preview,small=load_image(source,max_size=3)
    assert max(preview.shape[:2])==3 and small['width']==info['width']


@pytest.mark.parametrize('photometric',['minisblack','miniswhite'])
def test_tiff_grayscale_16bit(tmp_path,photometric):
    pixels=np.arange(1024,dtype=np.uint16).reshape(16,64)*63
    source=tmp_path/'gray.tif';tifffile.imwrite(source,pixels,photometric=photometric)
    rgb,_=load_image(source)
    expected=pixels/65535 if photometric=='minisblack' else 1-pixels/65535
    np.testing.assert_allclose(to_srgb(rgb),np.repeat(expected[...,None],3,-1),atol=2e-7)


@pytest.mark.parametrize('associated',[False,True])
def test_tiff_rgba_linear_white_compositing(tmp_path,associated):
    samples=np.array([[[.8,.4,.2,0],[.8,.4,.2,.5],[.8,.4,.2,1]]],np.float32)
    stored=samples.copy()
    if associated:stored[...,:3]*=stored[...,3,None]
    pixels=np.uint16(stored*65535+.5)
    source=tmp_path/'alpha.tif';tifffile.imwrite(source,pixels,photometric='rgb',
        extrasamples='assocalpha' if associated else 'unassalpha')
    linear,info=load_image(source)
    expected=to_linear(samples[...,:3])*samples[...,3,None]+1-samples[...,3,None]
    np.testing.assert_allclose(linear,expected,atol=3e-5)
    assert info['alpha']


def test_tiff_float_unclipped_input_allows_exposure_recovery(tmp_path):
    pixels=np.array([[[.25,.5,1],[2,3,4]]],np.float32)
    source=tmp_path/'hdr.tif';tifffile.imwrite(source,pixels,photometric='rgb')
    linear,info=load_image(source)
    np.testing.assert_array_equal(linear,pixels)
    settings=defaults();settings['exposure']=-2
    np.testing.assert_allclose(develop(linear,settings),to_srgb(pixels/4),atol=1e-6)
    assert info['bit_depth']==32


def test_tiff_palette_uses_16bit_colormap(tmp_path):
    indices=np.arange(256,dtype=np.uint8).reshape(16,16)
    colormap=np.stack([np.arange(256)*251+3,np.arange(256)[::-1]*251+7,np.full(256,30555)]).astype(np.uint16)
    source=tmp_path/'palette.tif';tifffile.imwrite(source,indices,photometric='palette',colormap=colormap)
    rgb,_=load_image(source)
    np.testing.assert_allclose(to_srgb(rgb),np.moveaxis(colormap[:,indices],0,-1)/65535,atol=2e-7)


def test_tiff_lab_alpha_and_independent_pillow_conversion(tmp_path):
    # TIFF CIELAB signed chroma, with an unassociated alpha sample.
    samples=np.array([[[128,20,236,255],[192,246,30,128],[100,0,0,0]]],np.uint8)
    source=tmp_path/'lab.tif';tifffile.imwrite(source,samples,photometric='cielab',planarconfig='contig',extrasamples='unassalpha')
    linear,_=load_image(source)
    lab=Image.frombytes('LAB',(3,1),samples[...,:3].tobytes())
    reference=np.asarray(ImageCms.profileToProfile(lab,ImageCms.createProfile('LAB'),
        ImageCms.createProfile('sRGB'),outputMode='RGB',renderingIntent=1))/255
    alpha=samples[...,3,None]/255
    expected=to_linear(reference)*alpha+1-alpha
    np.testing.assert_allclose(linear,expected,atol=.006)


def test_tiff_ycbcr_rational_tags(tmp_path):
    pixels=np.array([[[80,128,128],[180,150,100]]],np.uint8)
    source=tmp_path/'ycbcr.tif'
    tifffile.imwrite(source,pixels,photometric='ycbcr',metadata=None,
        extratags=[(529,'2I',3,(299,1000,587,1000,114,1000),False),
            (532,'2I',6,(0,1,255,1,128,1,255,1,128,1,255,1),False)])
    rgb,_=load_image(source)
    y=pixels[...,0]/255;cb=(pixels[...,1].astype(float)-128)/254;cr=(pixels[...,2].astype(float)-128)/254
    expected=np.stack([y+1.402*cr,y-.344136*cb-.714136*cr,y+1.772*cb],-1)
    np.testing.assert_allclose(to_srgb(rgb),expected,atol=2e-6)


@pytest.mark.parametrize('channels',[1,3])
def test_tiff_colorimetry_transfer_function(tmp_path,channels):
    pixels=np.array([[[40,80,120],[170,210,240]]],np.uint8)
    source=tmp_path/'linear.tif';transfer=np.arange(256,dtype=np.uint16)*257
    tifffile.imwrite(source,pixels,photometric='rgb',metadata=None,extratags=[
        (301,'H',256*channels,np.tile(transfer,channels),False),(318,'2I',2,(3127,10000,3290,10000),False)])
    rgb,_=load_image(source)
    np.testing.assert_allclose(rgb,pixels/255,atol=2e-4)


@pytest.mark.parametrize('container',['png','tif'])
def test_grayscale_icc_is_transformed_before_rgb_expansion(tmp_path,container):
    pixels=np.array([[0,16384,32768,65535]],np.uint16)
    icc=imagecodecs.cms_profile('gray',whitepoint=(.3127,.329),gamma=1.)
    source=tmp_path/f'gray.{container}'
    if container=='png':
        import zlib
        png_with_chunks(source,pixels,[(b'iCCP',b'gray\0\0'+zlib.compress(icc))])
    else:tifffile.imwrite(source,pixels,photometric='minisblack',extratags=[(34675,'B',len(icc),icc,False)])
    rgb,_=load_image(source)
    np.testing.assert_allclose(rgb,np.repeat((pixels/65535)[...,None],3,-1),atol=3e-4)


@pytest.mark.parametrize('container',['jpg','tif'])
def test_cmyk_icc_matches_independent_pillow_transform(tmp_path,container):
    icc=(Path(__file__).parent/'fixtures/lcms-test-cmyk.icc').read_bytes()
    pixels=np.array([[[0,0,0,0],[80,30,10,20],[0,255,120,10],[20,50,100,160]]],np.uint8)
    source=tmp_path/f'cmyk.{container}'
    if container=='jpg':Image.frombytes('CMYK',(4,1),pixels.tobytes()).save(source,quality=100,icc_profile=icc)
    else:tifffile.imwrite(source,pixels,photometric='separated',extratags=[(34675,'B',len(icc),icc,False)])
    with Image.open(source) as reference:
        expected=np.asarray(ImageCms.profileToProfile(reference,ImageCms.ImageCmsProfile(io.BytesIO(icc)),
            ImageCms.createProfile('sRGB'),outputMode='RGB',renderingIntent=1))/255
    linear,_=load_image(source)
    np.testing.assert_allclose(np.clip(to_srgb(linear),0,1),expected,atol=.006)


@pytest.mark.parametrize('mode',['1','L','P','LA','RGBA'])
def test_png_low_bit_palette_and_transparency(tmp_path,mode):
    source=tmp_path/'small.png'
    if mode=='1':im=Image.fromarray(np.array([[False,True],[True,False]]))
    elif mode=='P':
        im=Image.fromarray(np.array([[0,1],[2,0]],np.uint8)).convert('P')
        im.putpalette([20,100,200,80,90,100,240,180,30]+[0]*759);im.info['transparency']=bytes([0,128,255])
    else:im=Image.new(mode,(2,2),{'L':80,'LA':(80,128),'RGBA':(100,140,180,128)}[mode])
    im.save(source,bits=2 if mode=='P' else 8)
    rgba=np.asarray(Image.open(source).convert('RGBA'),np.float32)/255
    expected=to_linear(rgba[...,:3])*rgba[...,3,None]+1-rgba[...,3,None]
    actual,_=load_image(source)
    np.testing.assert_allclose(actual,expected,atol=1e-6)


def test_png_gamma_color_precedence_and_cicp(tmp_path):
    samples=np.full((2,3,3),128,np.uint8);source=tmp_path/'tags.png'
    png_with_chunks(source,samples,[(b'gAMA',struct.pack('>I',100000))])
    linear,_=load_image(source);np.testing.assert_allclose(linear,128/255,atol=2e-4)
    png_with_chunks(source,samples,[(b'gAMA',struct.pack('>I',100000)),(b'sRGB',b'\0')])
    linear,_=load_image(source);np.testing.assert_allclose(linear,to_linear(samples/255),atol=2e-4)
    png_with_chunks(source,samples,[(b'gAMA',struct.pack('>I',100000)),(b'cICP',bytes([1,13,0,1]))])
    linear,_=load_image(source);np.testing.assert_allclose(linear,to_linear(samples/255),atol=2e-4)
    png_with_chunks(source,samples,[(b'cICP',bytes([9,16,0,1]))])
    with pytest.raises(ValueError,match='cICP'):load_image(source)


@pytest.mark.parametrize('tag',['icc','chroma','cicp'])
def test_png_wide_color_tags_and_precedence(tmp_path,tag):
    import zlib
    samples=np.array([[[11000,52000,24000],[51000,19000,45000]]],np.uint16)
    source=tmp_path/'wide.png';icc=profile('Display P3')
    if tag=='icc':chunks=[(b'iCCP',b'P3\0\0'+zlib.compress(icc)),(b'gAMA',struct.pack('>I',100000))]
    elif tag=='chroma':
        chunks=[(b'cHRM',struct.pack('>8I',31270,32900,68000,32000,26500,69000,15000,6000))]
    else:chunks=[(b'cICP',bytes([12,13,0,1])),(b'iCCP',b'sRGB\0\0'+zlib.compress(profile('sRGB')))]
    png_with_chunks(source,samples,chunks)
    rgb,_=load_image(source,working_space='ProPhoto')
    settings=defaults();settings['working_space']='ProPhoto'
    np.testing.assert_allclose(develop(rgb,settings,output_space='Display P3'),samples/65535,atol=2e-4)


@pytest.mark.parametrize('container',['png','tif'])
def test_malformed_icc_is_not_silently_ignored(tmp_path,container):
    pixels=np.full((2,3,3),120,np.uint8);source=tmp_path/f'broken.{container}'
    icc=b'not-an-icc'
    if container=='png':Image.fromarray(pixels).save(source,icc_profile=icc)
    else:tifffile.imwrite(source,pixels,photometric='rgb',extratags=[(34675,'B',len(icc),icc,False)])
    with pytest.raises(ValueError,match='ICC'):load_image(source)


@pytest.mark.parametrize('format,suffix',[('PNG 16-bit','.png'),('PNG','.png'),('TIFF 8-bit','.tif'),('TIFF 16-bit','.tif'),('JPEG','.jpg')])
def test_export_metadata_png_orientation_gps_zero_and_keywords(tmp_path,format,suffix):
    source=tmp_path/'source.png'
    data={'0th':{piexif.ImageIFD.Orientation:6,piexif.ImageIFD.Make:b'Fixture'},
        'Exif':{piexif.ExifIFD.FNumber:(28,10),piexif.ExifIFD.ISOSpeedRatings:400},
        'GPS':{},'1st':{},'thumbnail':None}
    Image.new('RGB',(12,8),(80,120,160)).save(source,exif=piexif.dump(data))
    rgb,info=load_image(source)
    assert rgb.shape==(12,8,3) and info['make']=='Fixture' and info['aperture']==2.8
    out=tmp_path/('out'+suffix)
    export_image(source,out,defaults(),format,keep_metadata=True,
        user_metadata={'latitude':0.,'longitude':0.,'caption':'시험 사진'},keywords='사진, 여행')
    if format.startswith('TIFF'):
        with tifffile.TiffFile(out) as tf:
            assert tf.pages[0].dtype==(np.uint16 if format=='TIFF 16-bit' else np.uint8)
            xmp=tf.pages[0].tags[700].value.decode()
            assert '시험 사진' in xmp and 'GPSLatitude="0.0"' in xmp and 'Fixture' in xmp
            assert tf.pages[0].tags[34665].value['ISOSpeedRatings']==400
            assert tf.pages[0].tags[34853].value['GPSLatitude']==(0,1,0,1,0,1)
    else:
        with Image.open(out) as im:
            exif=im.getexif();assert exif.get(274)==1 and exif.get(271)=='Fixture'
            assert im.size==(8,12) and exif.get_ifd(34853)[2]==(0.,0.,0.)
            assert exif.get_ifd(34665)[34855]==400
    _,info=load_image(out)
    assert info['latitude']==0 and info['longitude']==0 and info['make']=='Fixture' and info['iso']==400


def test_explicit_gps_clear_and_zero_in_xmp(tmp_path):
    source=tmp_path/'gps.jpg';Image.new('RGB',(4,3)).save(source)
    exif=export_exif(source,{'latitude':37.,'longitude':127.})
    piexif.insert(exif,str(source))
    cleared=piexif.load(export_exif(source,{'latitude':'','longitude':None}))
    assert not any(key in cleared['GPS'] for key in (1,2,3,4))
    assert b'GPSLatitude="0"' in xmp_metadata({'latitude':0})


def test_jpeg_metadata_failure_removes_partial_output_only(tmp_path,monkeypatch):
    source=tmp_path/'source.png';Image.new('RGB',(4,3)).save(source)
    output=tmp_path/'out.jpg';before=source.read_bytes()
    def fail(*args):raise ValueError('test metadata write error')
    monkeypatch.setattr('luma.colorio.jpeg_iptc',fail)
    with pytest.raises(ValueError,match='metadata'):export_image(source,output,defaults(),'JPEG',keep_metadata=True)
    assert not output.exists() and source.read_bytes()==before
    output.write_bytes(b'preserve')
    with pytest.raises(FileExistsError):export_image(source,output,defaults(),'JPEG',keep_metadata=True)
    assert output.read_bytes()==b'preserve'


@pytest.mark.parametrize('compression',['lzw','deflate','jpeg'])
def test_tiff_compressed_pixel_decoding(tmp_path,compression):
    pixels=np.random.default_rng(8).integers(20,220,(24,32,3),dtype=np.uint8)
    source=tmp_path/'compressed.tif'
    tifffile.imwrite(source,pixels,photometric='rgb',compression=compression)
    rgb,_=load_image(source)
    # libtiff through Pillow gives an independent container/photometric path.
    with Image.open(source) as image:expected=np.asarray(image.convert('RGB'))/255
    np.testing.assert_allclose(to_srgb(rgb),expected,atol=2/255 if compression=='jpeg' else 2e-7)


def test_jpeg_reduced_preview_keeps_full_oriented_metadata(tmp_path):
    source=tmp_path/'preview.jpg'
    exif=piexif.dump({'0th':{274:6},'Exif':{},'GPS':{},'1st':{},'thumbnail':None})
    Image.new('RGB',(1600,1200),(90,130,170)).save(source,quality=100,exif=exif)
    rgb,info=load_image(source,max_size=100)
    assert rgb.shape==(100,75,3) and (info['width'],info['height'])==(1200,1600)
    expected=np.broadcast_to([90/255,130/255,170/255],rgb.shape)
    np.testing.assert_allclose(to_srgb(rgb),expected,atol=2/255)


def _old_linear(pixels, icc, working_space):
    # The full-frame reference path: float samples through rasterio._linear.
    from luma.rasterio import _linear, _normalise
    return _linear(_normalise(pixels), 'gray' if pixels.shape[-1] == 1 else 'rgb', icc, working_space, {})


@pytest.mark.parametrize('name', ['Display P3', 'sRGB', 'Adobe RGB'])
def test_eight_bit_planar_transform_equals_float_input_path(name):
    from luma.colorio import profile
    from luma.native_color import convert8_planar
    rng = np.random.default_rng(4)
    # Every level of every channel, plus random colours.
    ramp = np.stack(np.meshgrid(np.arange(256), np.arange(256), indexing='ij'), -1).reshape(256, 256, 2)
    pixels = np.concatenate([np.dstack([ramp, (ramp[..., :1]*7 + ramp[..., 1:]*13) % 256]),
                             rng.integers(0, 256, (256, 256, 3))], axis=0).astype(np.uint8)
    for working in ('sRGB', 'ProPhoto'):
        expected = _old_linear(pixels, profile(name), working)
        planes = convert8_planar(pixels, profile(name), profile('Linear ProPhoto' if working == 'ProPhoto' else 'Linear sRGB'))
        assert np.array_equal(np.moveaxis(planes, 0, -1), expected)


def test_gray_level_table_equals_whole_frame_transform_and_previews_match(tmp_path):
    import imagecodecs
    from luma.rasterio import read_bitmap, _gray8_table
    from luma.engine import resize_float
    icc = imagecodecs.cms_profile('gray', transferfunction=np.linspace(0, 1, 1024, dtype=np.float32)**2.2)
    rng = np.random.default_rng(8)
    gray = np.concatenate([np.arange(256, dtype=np.uint8).repeat(8).reshape(8, 256).repeat(4, axis=0),
                           rng.integers(0, 256, (900, 256), dtype=np.uint8)])
    gray = np.tile(gray, (1, 6))
    for working in ('sRGB', 'ProPhoto'):
        expected = _old_linear(gray[..., None], icc, working)
        table = _gray8_table(icc, working, {})
        assert np.array_equal(table[gray], expected)
    path = tmp_path/'gray.jpg'
    exif = Image.Exif();exif[274] = 6
    Image.fromarray(gray, 'L').save(path, quality=95, icc_profile=icc, exif=exif)
    with Image.open(path) as image:
        decoded = np.asarray(image)[..., None]
    full, info = read_bitmap(path)
    reference = np.ascontiguousarray(np.rot90(_old_linear(decoded, icc, 'sRGB'), -1))
    assert np.array_equal(full, reference) and info['source_orientation'] == 6
    preview, _ = read_bitmap(path, max_size=400)
    assert np.array_equal(preview, resize_float(reference, 400))
