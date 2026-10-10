from copy import deepcopy
import numpy as np
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from luma.widgets import Histogram,clipping_channels,histogram_bins
from test_studio_ui import app,window,wait


def test_clipping_uses_exact_8bit_endpoints_not_the_first_or_last_128_bins():
    image=np.array([[[1/255]*3,[254/255]*3]],dtype=np.float32)
    assert histogram_bins(image)[0][0] and histogram_bins(image)[0][-1]
    assert clipping_channels(image)==((False,)*3,(False,)*3)
    image[0,0]=[0,.5,.5];image[0,1]=[.5,1,1]
    assert clipping_channels(image)==((True,False,False),(False,True,True))


def test_single_unsampled_pixel_still_lights_indicator(app):
    image=np.full((900,900,3),.5,np.float32)
    image[1,1]=[-.1,0,0];image[2,2]=[1,1.1,1]
    assert not histogram_bins(image)[0][0] and not histogram_bins(image)[0][-1]
    histogram=Histogram();histogram.resize(300,104);histogram.set_image(image)
    assert histogram.channels==((True,)*3,(True,)*3)
    assert '#ffffff' in histogram.shadow_button.styleSheet()
    histogram.clear();assert not histogram.shadow_button.isEnabled() and not any(histogram.channels[0])


def test_hover_is_temporary_and_click_pins_each_overlay(app):
    histogram=Histogram();histogram.set_image(np.ones((2,2,3),np.float32));events=[]
    histogram.clippingChanged.connect(lambda shadow,highlight:events.append((shadow,highlight)))
    QApplication.sendEvent(histogram.highlight_button,QEvent(QEvent.Type.Enter))
    assert events[-1]==(False,True) and not histogram.highlight_button.isChecked()
    QApplication.sendEvent(histogram.highlight_button,QEvent(QEvent.Type.Leave));assert events[-1]==(False,False)
    histogram.shadow_button.click();assert events[-1]==(True,False)
    histogram.highlight_button.click();assert events[-1]==(True,True)
    histogram.set_overlay(False,False);assert events[-1]==(False,False)


def test_live_edits_crop_and_reset_refresh_clipping_with_histogram(window):
    w=window;before=deepcopy(w.settings)
    w.set_setting('exposure',5);w.finish_interaction()
    wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert w.histogram.channels==clipping_channels(w.view.on_screen)
    assert any(w.histogram.channels[1])
    w.reset_edits();wait(lambda:not w.render_running and not w.refine_timer.isActive())
    assert w.histogram.channels==clipping_channels(w.view.on_screen)
    assert not any(w.histogram.channels[1])
    w.histogram.highlight_button.click()
    assert w.view.highlight_clipping and not w.view.shadow_clipping and w.clipping_button.isChecked()
    w.toggle_clipping(False);assert not w.view.clipping
    assert w.settings==before
    # Cropped-out endpoint pixels cannot light the histogram indicators.
    image=np.full((100,100,3),.5,np.float32);image[:10]=1;image[-10:]=0
    w.view.crop_mode=True
    w.view.crop_values=lambda:[.2,.2,.8,.8]
    w.update_histogram(image);assert w.histogram.channels==((False,)*3,(False,)*3)
    w.view.crop_mode=False;w.update_histogram(image)
    assert w.histogram.channels==((True,)*3,(True,)*3)


def _reference_clipping(image, ceiling=1.):
    return (tuple(bool(np.any(image[..., c] < 1/510)) for c in range(3)),
            tuple(bool(np.any(image[..., c] >= ceiling-1/510)) for c in range(3)))


def test_parallel_clipping_flags_match_every_pixel_comparison():
    rng = np.random.default_rng(1)
    for trial in range(200):
        h, w = rng.integers(1, 600, 2)
        a = rng.uniform(.0019, .998, (h, w, 3)).astype(np.float32)
        for _ in range(rng.integers(0, 4)):
            a[rng.integers(h), rng.integers(w), rng.integers(3)] = rng.choice([0, 1, np.nan, 1/510, 1-1/510, 1.2])
        ceiling = float(rng.choice([1., 1.3]))
        assert clipping_channels(a, ceiling) == _reference_clipping(a, ceiling), trial
    assert clipping_channels(np.full((700, 900, 3), np.nan, np.float32)) == ((False,)*3, (False,)*3)
    view = np.random.default_rng(2).random((800, 1000, 4), dtype=np.float32)[:, ::2, :3]   # strided input
    assert clipping_channels(view) == _reference_clipping(view)


def test_parallel_qimage_bytes_match_serial_conversion():
    from luma.widgets import qimage
    rgb = np.random.default_rng(3).normal(.5, .5, (700, 901, 3)).astype(np.float32)
    image = qimage(rgb, display=False)
    expected = np.uint8(np.clip(rgb, 0, 1)*255+.5)
    data = np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes()).reshape(700, image.bytesPerLine())[:, :901*3]
    assert np.array_equal(data.reshape(700, 901, 3), expected)
