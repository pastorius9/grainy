"""Profile curves must retain their shape and stop at their authored endpoints."""
import json
import os
import sqlite3

import numpy as np
import pytest
from scipy.interpolate import CubicSpline

from luma import dcp, profile_library
from test_rawcolor import make_dcp


@pytest.mark.parametrize('endian', ['<', '>'])
@pytest.mark.parametrize('points', [
    [[0, 0], [.2, .3], [.6, .85], [.96898657, 1]],
    [[.12, .05], [.28, .45], [.65, .8], [.87, .92]],
    [[.15, .15], [.85, .85]],
    [[.15, .15], [.45, .45], [.85, .85]],
    [[.25, .7], [.75, .2]],
])
def test_authored_spline_with_constant_extension(points, endian):
    profile = dcp.parse(make_dcp([(50940, 11, np.ravel(points))], endian))
    curve = profile['curve']
    x = np.linspace(0, 1, 20001, dtype=np.float32)
    image = np.repeat(x[None, :, None], 3, axis=-1)
    original = image.copy()
    result = dcp.tone_map(image, curve)
    spline = CubicSpline(curve[:, 0], curve[:, 1], bc_type='natural')
    expected = np.clip(spline(np.clip(x, curve[0, 0], curve[-1, 0])), 0, 1)
    # Only a single dense-LUT cell at each constant-extension join can straddle
    # the corner; the authored interior remains the natural cubic spline.
    np.testing.assert_allclose(result[0, :, 0], expected, atol=8e-6, rtol=0)
    left = x < curve[0, 0] - 1 / 65535
    right = x > curve[-1, 0] + 1 / 65535
    np.testing.assert_array_equal(result[0, left, 0], np.float32(curve[0, 1]))
    np.testing.assert_array_equal(result[0, right, 0], np.float32(curve[-1, 1]))
    np.testing.assert_array_equal(image, original)
    assert np.isfinite(result).all()


@pytest.mark.parametrize('points', [
    [0, 0], [0, 0, .9], [0, 0, .8, 1, .8, 1],
    [0, 0, 1.01, 1], [-.01, 0, .9, 1], [0, -.01, .9, 1],
    [0, 0, .9, 1.01], [0, 0, float('nan'), 1],
    [0, 0, .9, float('inf')], [.9, 1, .8, .9],
])
def test_invalid_curves_still_rejected(points):
    with pytest.raises(ValueError):
        dcp.parse(make_dcp([(50940, 11, points)]))


def test_truncated_curve_preserves_middle_channel_ratio():
    curve = np.array([[.1, .04], [.3, .5], [.7, .8], [.9, .95]])
    image = np.array([[[.02, .3, .96], [.04, .04, .04], [.98, .98, .98]]], np.float32)
    result = dcp.tone_map(image, curve)
    ratio = (image[0, 0, 1] - image[0, 0, 0]) / (image[0, 0, 2] - image[0, 0, 0])
    assert (result[0, 0, 1] - .04) / (.95 - .04) == pytest.approx(ratio, abs=1e-7)
    np.testing.assert_allclose(result[0, 1], .04, atol=1e-7)
    np.testing.assert_allclose(result[0, 2], .95, atol=1e-7)


def test_previously_rejected_profile_index_rechecks_unchanged_file(tmp_path):
    path = tmp_path / 'truncated.dcp'
    path.write_bytes(make_dcp([(50940, 11, [0, 0, .5, .7, .96898657, 1])]))
    cache = tmp_path / 'index.sqlite'
    first = profile_library.scan([tmp_path], cache)
    assert first['parsed'] == 1 and not first['profiles'][0]['error']
    stat = path.stat()
    legacy_stamp = f'1:{stat.st_mtime_ns}:{stat.st_size}'
    rejected = {**first['profiles'][0], 'error': 'DCP 톤 곡선 좌표가 잘못되었습니다.'}
    with sqlite3.connect(cache) as db:
        db.execute('UPDATE profiles SET stamp=?,summary=? WHERE path=?',
                   (legacy_stamp, json.dumps(rejected), os.path.normcase(str(path.resolve()))))
    second = profile_library.scan([tmp_path], cache)
    assert second['parsed'] == 1 and not second['profiles'][0]['error']
    assert profile_library.load_selected(second['profiles'][0], {'camera': 'Test Camera'})
    assert profile_library.scan([tmp_path], cache)['parsed'] == 0
