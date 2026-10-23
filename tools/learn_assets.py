"""The practice material of the interactive tutorial (docs/learn), computed by Grainy itself.

    python tools/learn_assets.py

The page redoes Grainy's maths for the sliders it offers (exposure, white balance, Blacks, Whites, Contrast,
the tone curve, saturation, channel curves, local edits); what it cannot redo in a browser comes from here:
- negative: the clear film and densest part Grainy measures on the page's sample scan, and its first brightness;
- basic: Auto tone and Auto colour of that converted scan (slider values and channel curve points);
- films: the garden photo through a set of film looks, each a JPEG the page blends with Film amount;
- sky: Select Sky's mask of the garden photo.
Screens of the app itself come from tests/test_learn_screens.py (GRAINY_LEARN_SCREENS=<folder>).
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luma import engine, negative, film_look          # noqa: E402

OUT = ROOT/'docs'/'learn'/'img'
SCAN = ROOT/'docs'/'site'/'negative-scan.jpg'
GARDEN = ROOT/'docs'/'site'/'negative-result.jpg'
GARDEN_CROP = (44, 30, 24, 40)        # left, top, right, bottom: the scan's film border left out
FILMS = ('kodak-portra-400', 'kodak-ektar-100', 'kodak-gold-200', 'fuji-superia-xtra-400', 'fuji-velvia-50',
         'kodak-kodachrome-64', 'kodak-ektachrome-e100', 'kodak-vision3-500t', 'cinestill-800t', 'kodak-tri-x-400',
         'ilford-hp5-plus')


def linear(path, crop=None):
    image = Image.open(path).convert('RGB')
    if crop:
        l, t, r, b = crop
        image = image.crop((l, t, image.width - r, image.height - b))
    return image, engine.to_linear(np.asarray(image, np.float32)/255)


def save(pixels, name, quality=88):
    Image.fromarray((np.clip(pixels, 0, 1)*255 + .5).astype(np.uint8)).save(OUT/name, quality=quality, optimize=True, progressive=True)


def rounded(value, digits=5):
    if isinstance(value, (list, tuple)):
        return [rounded(v, digits) for v in value]
    return round(float(value), digits)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    data = {}
    # Negative: as Grainy measures the sample scan (the page decodes the same JPEG and linearises it the same way).
    # The page's scan lies on its side; the practice copy is turned upright (as the photo on the project page).
    Image.open(SCAN).convert('RGB').transpose(Image.Transpose.ROTATE_90).save(OUT/'scan.jpg', quality=95)
    _, scan = linear(OUT/'scan.jpg')
    converted = negative.converted(scan, engine.defaults())
    data['negative'] = {'base': rounded(converted['negative_base'], 6), 'peak': rounded(converted['negative_peak'], 6),
                        'density': converted['negative_density'], 'range': converted['negative_range'],
                        'floor': negative.FLOOR}
    # Basic: Auto tone and Auto colour of the converted scan, as the two buttons would set them.
    tone, _ = engine.auto_tone_settings(scan, converted)
    from luma.auto_color import auto_color_settings
    colour, _ = auto_color_settings(scan, converted)
    data['auto_tone'] = {k: rounded(tone[k], 4) for k in ('exposure', 'blacks', 'whites')}
    data['auto_tone']['curve'] = rounded(tone['curve'], 4)
    data['auto_color'] = {'rgb_curves': rounded(colour['rgb_curves'], 4)}
    # Garden: the photo, its film looks (full strength) and its sky.
    image, garden = linear(GARDEN, GARDEN_CROP)
    image.save(OUT/'garden.jpg', quality=90, optimize=True, progressive=True)
    data['garden'] = {'width': image.width, 'height': image.height}
    films = []
    for name in FILMS:
        entry = film_look.entry(name)
        save(engine.develop(garden, {'film_look': name}), f'film-{name}.jpg')
        films.append({'id': name, 'name': entry['name'], 'maker': entry['maker'], 'kind': entry['kind'], 'iso': entry.get('iso')})
    data['films'] = films
    from luma.sky_mask import sky_mask, DEFAULTS as SKY
    developed = engine.develop(garden, {})
    mask = sky_mask(developed, {'type': 'sky', **SKY})
    Image.fromarray((np.clip(mask, 0, 1)*255 + .5).astype(np.uint8)).save(OUT/'garden-sky.png', optimize=True)
    (OUT.parent/'data.json').write_text(json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')
    total = sum(p.stat().st_size for p in OUT.iterdir())
    print(f'{len(list(OUT.iterdir()))} files, {total/1e6:.2f} MB; negative density {converted["negative_density"]}, '
          f'auto tone exposure {tone["exposure"]:.2f}, sky {float(mask.mean()):.2f} of the frame')


if __name__ == '__main__':
    main()
