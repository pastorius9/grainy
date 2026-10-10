import numpy as np
from PIL import Image
from test_studio_ui import app, wait
from luma.engine import defaults, develop
from luma.studio import FILM_BALANCE


def grey(settings):
    return develop(np.full((8, 8, 3), .2, np.float32), settings)[0, 0]


def test_conversions_shift_grey_the_way_the_filters_do_and_cancel_each_other():
    base = defaults()
    def converted(key):
        reference, light = FILM_BALANCE[key]
        return grey({**base, 'kelvin_enabled': True, 'wb_reference': reference, 'kelvin': light})
    warm, cool, neutral = converted('tungsten'), converted('daylight'), grey(base)
    assert warm[0] > neutral[0] and warm[2] < neutral[2]            # tungsten film in daylight is blue: add warmth (85)
    assert cool[0] < neutral[0] and cool[2] > neutral[2]            # daylight film under tungsten is yellow: add blue (80A)
    assert abs(float(warm[1]-neutral[1])) < 1e-4 and abs(float(cool[1]-neutral[1])) < 1e-4       # green anchors brightness
    from luma.processing import white_balance
    both = white_balance({**base, 'kelvin_enabled': True, 'wb_reference': 3200., 'kelvin': 5500.}) \
        * white_balance({**base, 'kelvin_enabled': True, 'wb_reference': 5500., 'kelvin': 3200.})
    assert np.allclose(both, 1, atol=1e-5)


def test_buttons_toggle_follow_the_settings_and_undo(app, tmp_path):
    from luma.app import MainWindow
    path = tmp_path/'a.png'; Image.fromarray(np.full((60, 90, 3), 120, np.uint8)).save(path)
    w = MainWindow(tmp_path/'data')
    try:
        ident = w.catalog.add(path); w.refresh_lists(); w.activate(ident); w.show()
        wait(lambda: w.source is not None and not w.render_running and not w.library_loading)
        tungsten, daylight = w.studio.film_buttons['tungsten'], w.studio.film_buttons['daylight']
        tungsten.click()
        assert (w.settings['kelvin_enabled'], w.settings['wb_reference'], w.settings['kelvin']) == (True, 3200., 5500.)
        assert tungsten.isChecked() and not daylight.isChecked() and w.studio.kelvin.isChecked()
        assert w.catalog.histories(ident)[0]['label'] == '텅스텐 → 데이라이트'
        daylight.click()                                            # the other direction replaces it
        assert (w.settings['wb_reference'], w.settings['kelvin']) == (5500., 3200.) and daylight.isChecked() and not tungsten.isChecked()
        daylight.click()                                            # again: off
        assert not w.settings['kelvin_enabled'] and not daylight.isChecked()
        w.undo()
        assert w.settings['kelvin_enabled'] and daylight.isChecked()
        w.adjustments['kelvin'].set_value(4000); w.settings['kelvin'] = 4000.; w.load_controls()
        assert not daylight.isChecked() and not tungsten.isChecked()  # a custom Kelvin value is neither conversion
    finally:
        wait(lambda: not w.render_running); w.close()
