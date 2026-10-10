"""Art-directed RGB approximations of photographic filters, not spectral profiles."""
import numpy as np

# Stable catalog IDs, labels and linear RGB transmission at full density.
FILTERS = (
    ('warming85', '(85) 웜 필터', (1., .58, .22)),
    ('warming81', '(81) 웜 필터', (1., .88, .64)),
    ('cooling80', '(80) 쿨 필터', (.28, .57, 1.)),
    ('cooling82', '(82) 쿨 필터', (.69, .87, 1.)),
    ('red', '레드', (1., .12, .08)),
    ('orange', '오렌지', (1., .43, .05)),
    ('yellow', '옐로', (1., .93, .07)),
    ('green', '그린', (.12, 1., .18)),
    ('cyan', '시안', (.10, 1., 1.)),
    ('blue', '블루', (.10, .22, 1.)),
    ('violet', '바이올렛', (.50, .12, 1.)),
    ('magenta', '마젠타', (1., .10, .70)),
    ('dark_red', '다크 레드', (.60, .05, .06)),
    ('navy_blue', '네이비 블루', (.06, .12, .58)),
    ('emerald_green', '에메랄드 그린', (.06, .65, .32)),
    ('chrome_yellow', '크롬 옐로', (1., .66, .04)),
    ('marine_blue', '마린 블루', (.05, .48, .80)),
)
TRANSMISSION = {key: rgb for key, _, rgb in FILTERS}
KEYS = ('photo_filter', 'photo_filter_enabled', 'photo_filter_density', 'photo_filter_luminosity')


def apply(image, settings):
    if not settings.get('photo_filter_enabled'):
        return image
    rgb = TRANSMISSION.get(settings.get('photo_filter'))
    if rgb is None:
        return image
    try:
        density = float(settings.get('photo_filter_density', 25)) / 100
    except (ValueError, TypeError):
        return image
    if not np.isfinite(density) or density <= 0:
        return image
    from .engine import to_linear, to_srgb
    gain = 1 + min(density, 1) * (np.array(rgb, np.float32) - 1)
    linear = to_linear(image)
    filtered = linear * gain
    if settings.get('photo_filter_luminosity', True):
        weights = np.array((.28804, .71187, .00009) if settings.get('working_space') == 'ProPhoto'
                           else (.2126, .7152, .0722), np.float32)
        if settings.get('monochrome'):
            # Compensate neutral gray, retaining color-dependent B&W contrast.
            filtered /= gain @ weights
            filtered = np.repeat((filtered @ weights)[..., None], 3, axis=-1)
        else:
            luminance = linear @ weights
            changed = filtered @ weights
            filtered *= (luminance / np.maximum(changed, 1e-12))[..., None]
            # Fit highlights around the same luminance instead of clipping.
            delta = filtered - luminance[..., None]
            upper = (1 - luminance) / np.maximum(delta.max(axis=-1), 1e-12)
            lower = luminance / np.maximum(-delta.min(axis=-1), 1e-12)
            amount = np.clip(np.minimum(upper, lower), 0, 1)
            filtered = luminance[..., None] + delta * amount[..., None]
    return to_srgb(np.maximum(filtered, 0))
