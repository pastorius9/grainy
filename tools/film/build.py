"""Turn the traced datasheet curves into the stock and paper files Grainy ships (assets/film).

    python tools/film/build.py            all sheets that have traced curves
    python tools/film/build.py <id>...    only these

For each sheet (tools/film/sheets/<id>.json with its traced/<id>.json) this writes assets/film/stocks/<id>.json
(films) or assets/film/papers/<id>.json (papers and print films), then assets/film/index.json, the list the app
shows. A film that cannot be simulated yet (a slide without dye spectra, a negative whose paper is missing) is
reported and left out of the index.

Choices made here, not on the datasheets:
- Colour negatives print on Kodak Endura Premier, motion-picture negatives on Kodak Vision Color Print Film 2383,
  black-and-white negatives on Ilford Multigrade (grade 3); PRINTS below.
- A stock's grain becomes Grainy's Effects › Grain values: the amount follows the datasheet's granularity
  (Kodak's Print Grain Index for colour negatives, rms granularity otherwise), the size follows the film speed.
- Where a sheet prints no reference exposure, it is estimated from the film speed (log_h_ref below).
- A sheet with "derived_from" in its meta is a film sold without datasheet that is reported to be another stock
  (CineStill: Kodak Vision3 without its rem-jet backing): it takes that stock's built data, rated at its own speed,
  with its own print and halation (see luma/film_look.halation), and says so in its notes.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT.parents[1]/'assets'/'film'
PRINTS = {'colour-negative': 'kodak-endura-premier', 'cine': 'kodak-2383', 'bw-negative': 'ilford-multigrade-rc-deluxe'}
FILM_KINDS = ('colour-negative', 'colour-slide', 'bw-negative')
PAPER_KINDS = ('colour-paper', 'print-film', 'bw-paper')
LAYERS = {'c': 'r', 'm': 'g', 'y': 'b'}
KIND_TITLES = {'colour-negative': '컬러 네거티브', 'colour-slide': '컬러 슬라이드', 'bw-negative': '흑백 네거티브'}


def display_name(name):
    """The product name as people say it: without "Professional", "Film", stock codes in brackets or the
    motion-picture catalogue suffixes (Kodak Vision3 keeps its camera-film number)."""
    import re
    vision = re.match(r'Kodak VISION3 (\w+) Color Negative Film (\d{4})', name)
    if vision:
        return f'Kodak Vision3 {vision.group(1)} {vision.group(2)}'
    name = re.sub(r'\s*\([^)]*\)', '', name)
    name = re.sub(r'\s*/\s*[A-Z-]*\d{3,4}', '', name).replace('Color Reversal ', '')        # 5294 / 7294 -> 5294
    name = re.sub(r'\b(Professional|Film)\b', '', name)
    for old, new in (('FUJIFILM', 'Fujifilm'), ('ETERNA', 'Eterna'), ('PRO ', 'Pro '), ('Vista plus', 'Vista Plus'),
                     ('Ultra Max 400', 'UltraMax 400')):
        name = name.replace(old, new)
    return re.sub(r'\s+', ' ', name).strip()


def load(name):
    sheet = json.loads((ROOT/'sheets'/f'{name}.json').read_text('utf-8'))
    traced_path = ROOT/'traced'/f'{name}.json'
    traced = json.loads(traced_path.read_text('utf-8')) if traced_path.exists() else None
    return sheet, traced


def lines(traced, plot):
    return {k: v for k, v in (traced or {}).get('plots', {}).get(plot, {}).items() if ':' not in k}


def clean_curve(points, falling):
    """Sorted, one value per x, monotonic (rising for negatives and papers, falling for slides), lightly smoothed."""
    p = np.asarray(points, float)
    p = p[np.argsort(p[:, 0])]
    x, index = np.unique(np.round(p[:, 0], 4), return_index=True)
    y = p[index, 1]
    if len(y) >= 5:
        y = np.convolve(np.pad(y, 2, mode='edge'), np.ones(5)/5, mode='valid')
    y = np.minimum.accumulate(y) if falling else np.maximum.accumulate(y)
    return [[round(float(a), 4), round(float(b), 4)] for a, b in zip(x, y)]


def clean_spectrum(points):
    p = np.asarray(points, float)
    p = p[np.argsort(p[:, 0])]
    x, index = np.unique(np.round(p[:, 0], 2), return_index=True)
    return [[round(float(a), 2), round(float(b), 4)] for a, b in zip(x, p[index, 1])]


# Log exposure from the speed point to a normally exposed grey card: the mean over the Kodak sheets that print
# both (Portra 160/400/800, Ektar 100, Gold 200: 1.13 +- 0.03).
SPEED_TO_GREY = 1.13


def reference(meta, curves, falling):
    """The log exposure of a normally exposed mid grey. Printed on Kodak's sheets; otherwise found on the curve
    itself, many sheets having a relative exposure axis: SPEED_TO_GREY above the speed point, where the (green or only)
    curve is 0.1 above its minimum (ISO 5800 / ISO 6). Slides are placed by luma/film_look instead."""
    if meta.get('log_h_ref') is not None:
        return float(meta['log_h_ref'])
    p = np.asarray(curves.get('g') or curves.get('v'), float)
    p = p[np.argsort(p[:, 0])]
    if falling:
        return float(np.interp(1.0, p[::-1, 1], p[::-1, 0]))         # where the slide reads density 1
    y = np.maximum.accumulate(p[:, 1])
    speed = float(np.interp(y.min() + .1, y + np.arange(len(y))*1e-9, p[:, 0]))
    return speed + SPEED_TO_GREY


def intrinsic(points, light_name):
    """Log sensitivity per unit energy. A wedge spectrogram (Ilford, Foma) records the response to a tungsten
    light, whose energy rises steeply towards red: its log spectrum is taken away."""
    if not light_name:
        return points
    sys.path.insert(0, str(ROOT.parents[1]))
    from luma import film_look
    w = film_look.wavelengths()
    light = np.log10(film_look.light(light_name))
    return [[x, round(float(y - np.interp(x, w, light)), 4)] for x, y in points]


def grain(meta, kind):
    g = meta.get('grain') or {}
    if 'pgi' in g and g['pgi'] is not None:
        amount = (float(g['pgi']) - 18)*.9
    elif 'rms' in g and g['rms'] is not None:
        rms = float(g['rms'])
        amount = rms*4 if (kind == 'colour-negative' and meta.get('maker') == 'Fujifilm') else (rms - 3)*2
    else:
        return None
    iso = float(meta.get('iso') or 100)
    size = 20 + 6*math.log2(iso/100)
    return {'grain': round(float(np.clip(amount, 4, 60)), 1), 'grain_size': round(float(np.clip(size, 12, 45)), 1),
            'grain_roughness': 60. if kind == 'bw-negative' else 50.}


def derived(name, meta):
    base_path = ASSETS/'stocks'/f"{meta['derived_from']}.json"
    if not base_path.exists():
        return None, f"{meta['derived_from']} is not built yet"
    data = json.loads(base_path.read_text('utf-8'))
    # Rated at a higher speed, a mid grey gets less exposure on the same emulsion.
    shift = math.log10(float(meta['iso'])/float(data['iso'])) if meta.get('iso') and data.get('iso') else 0.
    data.update({'id': name, 'name': meta['name'], 'maker': meta.get('maker'), 'iso': meta.get('iso', data.get('iso')),
                 'cine': bool(meta.get('cine', False)), 'log_h_ref': round(data['log_h_ref'] - shift, 4),
                 'print': meta.get('print') or PRINTS['colour-negative'], 'notes': meta.get('notes', []) + data.get('notes', []),
                 'derived_from': meta['derived_from']})
    if meta.get('halation'):
        data['halation'] = meta['halation']
    g = grain({**meta, 'grain': meta.get('grain') or {}}, data['kind']) if meta.get('grain') else data.get('grain')
    if g:
        data['grain'] = g
    target = ASSETS/'stocks'/f'{name}.json'
    target.write_text(json.dumps(data, separators=(',', ':')), 'utf-8')
    return data, None


def build(name):
    sheet, traced = load(name)
    meta = sheet.get('meta', {})
    kind = meta.get('kind')
    if meta.get('derived_from'):
        return derived(name, meta)
    if traced is None:
        return None, 'no traced curves'
    curves = lines(traced, 'curves')
    # Sensitivity lines are named by layer (r, g, b); some sheets name them by the dye each layer forms.
    sensitivity = {LAYERS.get(k, k): v for k, v in lines(traced, 'sensitivity').items()}
    dyes = lines(traced, 'dyes')
    source = {'document': meta.get('document'), 'date': meta.get('date'), 'file': traced.get('document'),
              'url': traced.get('url'), 'sha256': traced.get('sha256')}
    if kind in PAPER_KINDS:
        data = {'id': name, 'name': meta.get('name'), 'maker': meta.get('maker'), 'kind': kind,
                'view': meta.get('view') or ('D55' if kind == 'print-film' else 'D50'),
                'curves': {k: clean_curve(v, False) for k, v in curves.items()},
                'dyes': {k: clean_spectrum(v) for k, v in dyes.items()},
                'sensitivity': {k: clean_spectrum(v) for k, v in sensitivity.items()},
                'source': source, 'notes': meta.get('notes', [])}
        if kind == 'bw-paper' and 'v' not in data['curves'] and data['curves']:
            # The grade used for printing: grade 3 if traced, else the middle one.
            names = sorted(data['curves'])
            data['curves']['v'] = data['curves'].get('grade-3') or data['curves'][names[len(names)//2]]
        target = ASSETS/'papers'/f'{name}.json'
    elif kind in FILM_KINDS:
        falling = kind == 'colour-slide'
        wanted = ('v',) if kind == 'bw-negative' else ('r', 'g', 'b')
        if kind == 'bw-negative' and 'v' not in curves and 'g' in curves:
            # Chromogenic (C-41) black and white publishes three Status M curves; the green one carries the image.
            curves = {**curves, 'v': curves['g']}
            if sensitivity and 'v' not in sensitivity and 'g' in sensitivity:
                sensitivity = {**sensitivity, 'v': sensitivity['g']}
        missing = [k for k in wanted if k not in curves]
        if missing:
            return None, f'missing characteristic curves {missing}'
        group = 'cine' if meta.get('cine') else kind
        data = {'id': name, 'name': meta['name'], 'maker': meta.get('maker'), 'kind': kind, 'cine': bool(meta.get('cine')),
                'iso': meta.get('iso'), 'balance': meta.get('balance') or 'daylight',
                'log_h_ref': round(reference(meta, curves, falling), 4),
                'curves': {k: clean_curve(curves[k], falling) for k in wanted},
                'sensitivity': {k: intrinsic(clean_spectrum(v), meta.get('sensitivity_light')) for k, v in sensitivity.items() if k in wanted},
                'source': source, 'notes': meta.get('notes', [])}
        if kind == 'colour-slide':
            if not all(k in dyes for k in 'cmy'):
                return None, 'a slide needs its cyan, magenta and yellow dye spectra'
            data['dyes'] = {k: clean_spectrum(v) for k, v in dyes.items() if k in ('c', 'm', 'y', 'min')}
            data['view'] = meta.get('view') or 'D50'
        else:
            data['print'] = meta.get('print') or PRINTS['cine' if group == 'cine' and kind == 'colour-negative' else kind]
            if not (ASSETS/'papers'/f"{data['print']}.json").exists():
                return None, f"its paper {data['print']} is not built yet"
        g = grain(meta, kind)
        if g:
            data['grain'] = g
        target = ASSETS/'stocks'/f'{name}.json'
    else:
        return None, f'unknown kind {kind!r}'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, separators=(',', ':')), 'utf-8')
    return data, None


def index():
    """assets/film/index.json: every stock that builds and simulates, grouped and in a stable order."""
    sys.path.insert(0, str(ROOT.parents[1]))
    from luma import film_look
    film_look.catalog.cache_clear()
    entries = []
    for path in sorted((ASSETS/'stocks').glob('*.json')):
        data = json.loads(path.read_text('utf-8'))
        paper = None
        if data.get('print'):
            paper = json.loads((ASSETS/'papers'/f"{data['print']}.json").read_text('utf-8'))
        try:
            film_look.Stock(data, paper)
        except Exception as error:
            print(f'{data["id"]}: does not simulate ({type(error).__name__}: {error})')
            continue
        group = 'cine' if data.get('cine') else data['kind']
        entries.append({'id': data['id'], 'name': display_name(data['name']), 'maker': data.get('maker'), 'kind': data['kind'],
                        'group': group, 'kind_title': KIND_TITLES[data['kind']], 'iso': data.get('iso'),
                        'print_name': display_name(paper['name']).replace(' Paper', '') if paper else None,
                        'grain': data.get('grain')})
    order = {k: i for i, k in enumerate(('colour-negative', 'colour-slide', 'bw-negative', 'cine'))}
    import re
    natural = lambda text: [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', text)]
    entries.sort(key=lambda e: (order[e['group']], e['maker'] or '', natural(e['name'])))
    (ASSETS/'index.json').write_text(json.dumps(entries, ensure_ascii=False, indent=1), 'utf-8')
    return entries


def main(names):
    sheets = names or sorted(p.stem for p in (ROOT/'sheets').glob('*.json'))
    # Papers first (films print on them), derived films last (they start from a built film).
    def order(n):
        meta = json.loads((ROOT/'sheets'/f'{n}.json').read_text('utf-8')).get('meta', {})
        return 0 if meta.get('kind') in PAPER_KINDS else 2 if meta.get('derived_from') else 1
    sheets.sort(key=order)
    for name in sheets:
        data, problem = build(name)
        print(f'{name}: {"built" if data else "skipped — " + problem}')
    entries = index()
    print(f'index: {len(entries)} stocks')


if __name__ == '__main__':
    main(sys.argv[1:])
