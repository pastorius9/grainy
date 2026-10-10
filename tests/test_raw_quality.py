import io
import struct
import numpy as np
import piexif
from PIL import Image
from luma.rawcolor import reduce_false_color
from luma.engine import read_metadata


def test_false_colour_suppression_keeps_green_and_matches_one_full_frame_pass():
    import cv2
    rng = np.random.default_rng(3)
    y, x = np.mgrid[:700, :500].astype(np.float32)
    grey = .5 + .4 * np.sin(x / 3) * np.cos(y / 5)
    rgb = np.repeat(grey[..., None], 3, axis=2).astype(np.float32)
    speckle = rng.random(grey.shape) < .02
    rgb[speckle, 0] += .3;rgb[speckle, 2] -= .2                    # isolated demosaic colour errors
    out = reduce_false_color(rgb)
    assert np.array_equal(out[..., 1], rgb[..., 1])                # luminance detail (green) untouched
    chroma = lambda a: np.abs(a[..., 0] - a[..., 1]).mean() + np.abs(a[..., 2] - a[..., 1]).mean()
    assert chroma(out) < chroma(rgb) / 4
    full = rgb.copy()
    for c in (0, 2):
        full[..., c] = cv2.medianBlur(np.ascontiguousarray(rgb[..., c] - rgb[..., 1]), 3) + rgb[..., 1]
    assert np.array_equal(out, full)                               # row tiles with halo == one pass
    tiny = rng.random((2, 5, 3)).astype(np.float32)
    assert np.array_equal(reduce_false_color(tiny), tiny)


def _exif_tiff(ifd0=None, exif=None):
    blob = piexif.dump({'0th': ifd0 or {}, 'Exif': exif or {}})
    return blob[6:]    # strip 'Exif\0\0': a stand-alone TIFF


def _tiff(entries):
    """Little-endian TIFF with one IFD: entries are (tag, type, count, value bytes)."""
    count = len(entries);data_at = 8 + 2 + 12 * count + 4;body = b'';table = b''
    for tag, kind, n, value in sorted(entries):
        if len(value) <= 4:
            table += struct.pack('<HHI', tag, kind, n) + value.ljust(4, bytes(1))
        else:
            table += struct.pack('<HHII', tag, kind, n, data_at + len(body));body += value
    return b'II*' + bytes(1) + struct.pack('<I', 8) + struct.pack('<H', count) + table + struct.pack('<I', 0) + body


def test_cr3_and_raf_metadata_are_read_from_their_containers(tmp_path):
    ifd0 = {piexif.ImageIFD.Make: b'Canon', piexif.ImageIFD.Model: b'Canon EOS R6'}
    cmt1 = _exif_tiff(ifd0=ifd0)
    # CMT2 is an EXIF IFD written as its own TIFF: its tags sit in that TIFF's first IFD.
    cmt2 = _tiff([(0x829A, 5, 1, struct.pack('<II', 1, 125)), (0x8827, 3, 1, struct.pack('<HH', 800, 0)),
                  (0xA434, 2, 16, b'RF50mm F1.8 STM' + bytes(1))])
    box = lambda kind, data: struct.pack('>I', len(data) + 8) + kind + data
    cr3 = tmp_path/'a.CR3'
    cr3.write_bytes(box(b'ftyp', b'crx ') + box(b'moov', box(b'CMT1', cmt1) + box(b'CMT2', cmt2)) + b'\0' * 64)
    meta = read_metadata(cr3)
    assert meta['make'] == 'Canon' and meta['camera'] == 'Canon EOS R6' and meta['iso'] == 800
    assert meta['shutter'] == '1/125' and meta['lens'] == 'RF50mm F1.8 STM'
    jpeg = io.BytesIO()
    Image.new('RGB', (16, 16)).save(jpeg, 'JPEG', exif=piexif.dump({'0th': {piexif.ImageIFD.Make: b'FUJIFILM', piexif.ImageIFD.Model: b'X-T3'},
                                                                   'Exif': {piexif.ExifIFD.ISOSpeedRatings: 160}}))
    data = jpeg.getvalue()
    header = b'FUJIFILMCCD-RAW 0201FF383501' + b'\0' * (84 - 28) + struct.pack('>II', 100, len(data))
    raf = tmp_path/'b.RAF'
    raf.write_bytes(header + b'\0' * (100 - len(header)) + data)
    meta = read_metadata(raf)
    assert meta['make'] == 'FUJIFILM' and meta['camera'] == 'X-T3' and meta['iso'] == 160
    # Damaged containers yield no metadata instead of an error.
    (tmp_path/'c.RAF').write_bytes(b'FUJIFILMCCD-RAW' + b'\xff' * 90)
    (tmp_path/'d.CR3').write_bytes(b'\0' * 32)
    assert read_metadata(tmp_path/'c.RAF') == {} and read_metadata(tmp_path/'d.CR3') == {}


def test_new_raw_imports_get_the_builtin_development_and_rules_override_it(tmp_path):
    import tifffile
    from luma.raw_defaults import resolve, linear_raw, BUILTIN_RAW, make_rule
    s, notes = resolve({'format': 'NEF'}, [], 'a.NEF')
    assert all(s[k] == v for k, v in BUILTIN_RAW.items()) and s['tone_version'] == 3 and notes
    assert resolve({'format': 'DNG', 'linear_raw': True}, [], 'a.DNG')[0]['exposure'] == .75
    # Luminance NR follows ISO (chosen on high-ISO CC0 raws); none at low or unknown ISO.
    nr = lambda iso: resolve({'format': 'NEF', 'iso': iso}, [], 'a.NEF')[0]['noise_luma']
    assert nr(None) == 0 and nr(100) == 0 and nr(400) == 0 and nr(3200) == 12 and nr(6400) == 25
    assert 0 < nr(800) < nr(1600) < nr(3200) and nr(32000) == 30 and nr(204800) == 30
    at = lambda iso: resolve({'format': 'NEF', 'iso': iso}, [], 'a.NEF')[0]
    assert (at(6400)['noise_color'], at(6400)['sharpen']) == (25, 60) and (at(None)['noise_color'], at(None)['sharpen']) == (25, 60)
    assert (at(32000)['noise_color'], at(32000)['sharpen']) == (50, 60) and 25 < at(12800)['noise_color'] < 50
    assert resolve({'format': 'NEF', 'iso': 3200}, [], 'a.NEF', base={})[0]['noise_luma'] == 0   # existing photo
    assert resolve({'format': 'JPG'}, [], 'a.jpg')[0]['exposure'] == 0                     # not RAW
    base = {'exposure': -.3}
    assert resolve({'format': 'NEF'}, [], 'a.NEF', base=base)[0]['exposure'] == -.3        # existing photo: untouched
    rule = make_rule({'make': 'Nikon', 'camera': 'Z 6'}, {'exposure': 0., 'sharpen': 10.}, scope='master', groups=['빛', '디테일'])
    s, _ = resolve({'format': 'NEF', 'make': 'Nikon', 'camera': 'Z 6'}, [rule], 'a.NEF')
    assert s['exposure'] == 0 and s['sharpen'] == 10                                         # user rule wins
    # LinearRaw (demosaiced) DNG versus CFA DNG, told apart by the raw IFD's PhotometricInterpretation.
    for name, photometric, expected in (('lin.dng', 34892, True), ('cfa.dng', 32803, False)):
        path = tmp_path/name
        with tifffile.TiffWriter(path) as tif:
            tif.write(np.zeros((8, 8, 3), np.uint8), photometric='rgb', subifds=1)
            tif.write(np.zeros((8, 8, 3) if expected else (8, 8), np.uint16), photometric=photometric)
        assert linear_raw(path) is expected
    assert linear_raw(tmp_path/'x.nef') is False


def test_guided_colour_noise_reduction_removes_blotches_without_bleeding_across_edges():
    import cv2
    from luma.processing import chroma_guided, detail_tools
    from luma.engine import normalized, defaults
    rng = np.random.default_rng(5)
    blotches = cv2.GaussianBlur(rng.normal(0, 12, (240, 240, 2)).astype(np.float32), (0, 0), 2)
    # A red/green edge with a strong and with no luminance step (colours of equal brightness).
    for light, dark in ((70, 30), (50, 50)):
        lab = np.zeros((240, 240, 3), np.float32)
        lab[:, :120] = (light, 40, 20);lab[:, 120:] = (dark, -30, -10)
        clean = lab.copy()
        lab[..., 1:] += blotches                                  # high-ISO colour noise: pixel-wide blobs
        out = chroma_guided(lab.copy(), 25)
        assert np.array_equal(out[..., 0], lab[..., 0])           # luminance untouched
        error = lambda a: np.abs(a[:, 20:100, 1:] - clean[:, 20:100, 1:]).mean()
        assert error(out) < error(lab) / 3
        # Colour stays on its side of the edge (an L*-only guide left 7 and 2 here for 40 and -30).
        assert out[:, 119, 1].mean() > 20 and out[:, 120, 1].mean() < -10
        assert out[:, 116, 1].mean() > 28 and out[:, 124, 1].mean() < -18
    assert np.array_equal(chroma_guided(lab.copy(), 0), lab)                   # slider 0: no change
    s = normalized({**defaults(), 'noise_color': 25})
    rgb = np.clip(np.random.default_rng(1).random((64, 64, 3)), 0, 1).astype(np.float32)
    assert detail_tools(rgb, s).shape == rgb.shape


def test_panasonic_rw2_iso_comes_from_its_ifd0_tag(tmp_path):
    # Panasonic RW2: TIFF-like header 'IIU\0', ISO in IFD0 tag 0x0017 (no EXIF ISOSpeedRatings).
    body = _tiff([(0x010F, 2, 10, b'Panasonic' + bytes(1)), (0x0017, 3, 1, struct.pack('<HH', 3200, 0))])
    path = tmp_path/'a.RW2'
    path.write_bytes(b'IIU' + bytes(1) + body[4:])
    meta = read_metadata(path)
    assert meta.get('iso') == 3200 and meta.get('make') == 'Panasonic'
    (tmp_path/'b.tif').write_bytes(body)                        # tag 0x0017 means nothing in other files
    assert 'iso' not in read_metadata(tmp_path/'b.tif')


def test_wavelet_luminance_noise_reduction_adapts_to_the_photo_and_keeps_edges():
    import cv2
    from luma.processing import luma_wavelet
    rng = np.random.default_rng(8)
    y, x = np.mgrid[:256, :256].astype(np.float32)
    clean = np.where(x < 128, 30., 70.).astype(np.float32) + 8 * np.sin(y / 3) * (x > 200)    # edge + fine texture
    blotchy = lambda sigma: clean + cv2.GaussianBlur(rng.normal(0, sigma, clean.shape).astype(np.float32), (0, 0), .8) * 1.6
    for sigma in (2., 6.):                                      # a quieter and a noisier photo
        noisy = blotchy(sigma)
        out = luma_wavelet(noisy, 50)
        flat = (slice(20, 236), slice(20, 110))
        assert (out - clean)[flat].std() < (noisy - clean)[flat].std() / 2.5            # noise reduced by the measured level
        mid = lambda a: float((cv2.GaussianBlur(a, (0, 0), 1.5) - cv2.GaussianBlur(a, (0, 0), 6))[flat].std())
        assert mid(out - clean) < mid(noisy - clean) / 2                                  # no leftover blotches
        assert abs(out[:, 124].mean() - 30) < 2.5 and abs(out[:, 132].mean() - 70) < 2.5   # the edge stays sharp
        texture = (slice(20, 236), slice(210, 250))
        corr = lambda a: np.corrcoef(a[texture].ravel(), clean[texture].ravel())[0, 1]
        assert corr(out) > max(corr(noisy), .85)                                          # texture kept, noise removed
    assert np.allclose(luma_wavelet(noisy, 0), noisy, atol=1e-4)                         # slider 0: unchanged

