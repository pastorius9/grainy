import numpy as np
import pytest
from PIL import Image
from luma.engine import export_image, defaults, jxl_distance
from luma.quick_export import FORMATS, SUFFIXES


@pytest.fixture
def source(tmp_path):
    y, x = np.mgrid[:120, :180]
    path = tmp_path/'s.png'
    Image.fromarray(np.uint8(np.stack([x/180*255, y/120*255, x*0+120], -1))).save(path)
    return path


def reference(path):
    return np.asarray(Image.open(path).convert('RGB'), np.float32)


def test_avif_export_keeps_colour_profile_and_metadata(source, tmp_path):
    out = tmp_path/'a.avif'
    assert export_image(source, out, defaults(), format='AVIF', quality=90, keep_metadata=True,
                        user_metadata={'title': 'T'}, keywords='a') == (180, 120)
    im = Image.open(out)
    assert im.format == 'AVIF' and im.info.get('icc_profile') and im.info.get('exif') and im.info.get('xmp')
    assert np.abs(np.asarray(im.convert('RGB'), np.float32)-reference(source)).mean() < 2
    with pytest.raises(FileExistsError):
        export_image(source, out, defaults(), format='AVIF')                       # never overwrites


def test_jpeg_xl_export_is_16_bit_srgb_with_metadata_in_the_container(source, tmp_path):
    import imagecodecs
    plain, meta = tmp_path/'p.jxl', tmp_path/'m.jxl'
    export_image(source, plain, defaults(), format='JPEG XL', quality=95)
    export_image(source, meta, defaults(), format='JPEG XL', quality=95, keep_metadata=True, user_metadata={'title': 'T'})
    assert plain.read_bytes()[:2] == b'\xff\x0a'                                    # bare codestream
    data = meta.read_bytes()
    assert data[:12] == bytes.fromhex('0000000c4a584c200d0a870a') and b'Exif' in data and b'xml ' in data
    for path in (plain, meta):
        pixels = imagecodecs.jpegxl_decode(path.read_bytes())
        assert pixels.dtype == np.uint16 and np.abs(pixels/257-reference(source)).mean() < 1.5
    with pytest.raises(ValueError):
        export_image(source, tmp_path/'x.jxl', defaults(), format='JPEG XL', color_space='Adobe RGB')
    assert jxl_distance(90) == pytest.approx(1.0) and jxl_distance(100) == pytest.approx(.1)
    assert {'AVIF', 'JPEG XL'} <= set(FORMATS) and SUFFIXES['AVIF'] == '.avif' and SUFFIXES['JPEG XL'] == '.jxl'
