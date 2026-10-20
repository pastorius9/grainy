"""Read the curves of a film datasheet's plots into numbers.

The datasheets (PDF) stay outside the repository (FOLDER below, or GRAINY_FILM_SHEETS); only what is read from
them, with the document, page and checksum, is kept: tools/film/sheets/<id>.json says where each plot is and how
its axes run, tools/film/traced/<id>.json holds the curves read from it.

Every page is rendered (pypdfium2) and each curve is followed through the dark pixels of the plot, from hints:
a few points along the curve in axis units. That works the same for vector and scanned sheets. Each plot's
traced curves are drawn over the plot (overlay) so they can be checked by eye.

    python tools/film/digitize.py show <id> <page> [--box x0 y0 x1 y1] [--grid 10]   page or crop with a ruler (PDF points)
    python tools/film/digitize.py words <id> <page> [--box ...]                      text and its position (vector sheets)
    python tools/film/digitize.py grid <id> <page> --box ...                          grid lines found in a plot
    python tools/film/digitize.py paths <id> <page> --box ... [--emit i j ...]       vector paths in a plot; --emit prints
                                                                                     those paths' points as hints_pt
    python tools/film/digitize.py extract <id> [<plot>...]                            trace, write traced/<id>.json and overlays
    python tools/film/digitize.py check <id> <plot> <line> x [x ...]                  read a traced line at x values

Coordinates are PDF points from the page's top-left corner (x right, y down), as pdfplumber reports them.

Sheet file (tools/film/sheets/<id>.json):
  {"id": "...", "pdf": "<file>.pdf" (default <id>.pdf), "dpi": 400,
   "plots": {"<plot>": {"page": 4, "box": [x0, y0, x1, y1],
                         "x": [[pt, value], [pt, value], ...], "y": [[pt, value], ...],
                         "xlog": false, "ylog": false,             (log axes: values are positive, mapped by log10)
                         "threshold": 140,                         (grey level counted as ink, 0-255)
                         "color": null,                            (or [r, g, b]: only ink near this colour)
                         "mask": [[x0, y0, x1, y1], ...],          (areas to ignore: legends, labels)
                         "step": 0.05,                             (x spacing of the output, axis units)
                         "window": 0.08, "strict": false,          (search distance from the expected y in y units;
                                                                    strict: follow the hints only, not the last ink)
                         "lines": {"<line>": {"hints": [[x, y], ...] in axis units, or "hints_pt": [[x, y], ...] in PDF points,
                                              "color": null, "threshold": null, "mask": []}}}},
   "meta": {...}}
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
FOLDER = Path(os.environ.get('GRAINY_FILM_SHEETS', ROOT.parents[2]/'film-datasheets'))   # beside the repository
SHEETS = ROOT/'sheets'
TRACED = ROOT/'traced'
OVERLAYS = Path(os.environ.get('GRAINY_FILM_OVERLAYS', FOLDER/'overlays'))
COLOURS = [(230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180), (70, 240, 240), (240, 50, 230)]
NAMED = {'r': (230, 25, 75), 'g': (40, 170, 60), 'b': (0, 110, 230), 'c': (0, 190, 200), 'm': (220, 0, 200), 'y': (240, 150, 0)}


def sheet(name):
    return json.loads((SHEETS/f'{name}.json').read_text('utf-8'))


def pdf_path(name, spec=None):
    return FOLDER/((spec or {}).get('pdf') or f'{name}.pdf')


def render(name, page, dpi, spec=None):
    """RGB array of a whole page at dpi."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path(name, spec)))
    try:
        image = doc[page-1].render(scale=dpi/72).to_pil().convert('RGB')
    finally:
        doc.close()
    return np.asarray(image)


def fit(pairs, log=False):
    """(scale, offset) with value(-ish) = scale*pt + offset, least squares over the calibration pairs."""
    pts = np.array([p[0] for p in pairs], float)
    values = np.array([p[1] for p in pairs], float)
    if log:
        values = np.log10(values)
    if len(pts) < 2 or np.ptp(pts) == 0:
        raise ValueError('an axis needs two calibration points at different positions')
    scale, offset = np.polyfit(pts, values, 1)
    return float(scale), float(offset)


class Axes:
    def __init__(self, plot):
        self.xs, self.xo = fit(plot['x'], plot.get('xlog', False))
        self.ys, self.yo = fit(plot['y'], plot.get('ylog', False))
        self.xlog, self.ylog = plot.get('xlog', False), plot.get('ylog', False)

    def value(self, xpt, ypt):
        x = self.xs*np.asarray(xpt, float) + self.xo
        y = self.ys*np.asarray(ypt, float) + self.yo
        return (10**x if self.xlog else x), (10**y if self.ylog else y)

    def point(self, x, y):
        x = np.log10(x) if self.xlog else np.asarray(x, float)
        y = np.log10(y) if self.ylog else np.asarray(y, float)
        return (x - self.xo)/self.xs, (y - self.yo)/self.ys


def hint_points(axes, line):
    """Hints in PDF points: given in axis units ("hints") or read off the ruler ("hints_pt")."""
    if 'hints_pt' in line:
        return [(float(x), float(y)) for x, y in line['hints_pt']]
    return [tuple(float(v) for v in axes.point(x, y)) for x, y in line['hints']]


def ink(rgb, threshold, colour=None, tolerance=70):
    if colour is None:
        return rgb.mean(axis=2) < threshold
    return np.abs(rgb.astype(int) - np.asarray(colour)).max(axis=2) < tolerance


def without_grid(mask, fraction=.5):
    """Ink minus long straight horizontal and vertical lines (grid, frame, ticks)."""
    mask = mask.copy()
    rows = mask.mean(axis=1) > fraction
    cols = mask.mean(axis=0) > fraction
    for index in np.flatnonzero(rows):
        mask[max(0, index-1):index+2] = False
    for index in np.flatnonzero(cols):
        mask[:, max(0, index-1):index+2] = False
    return mask, np.flatnonzero(rows), np.flatnonzero(cols)


def runs(column):
    """(start, stop) of the ink runs in a boolean column."""
    padded = np.concatenate([[False], column, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2], edges[1::2]))


def trace(mask, hints_px, height, window=None, strict=False):
    """y (pixels, float, NaN where no ink) for each column from the first to the last hint.

    Each column takes the ink run nearest to where the curve is expected: between the hints, or (unless strict)
    halfway to the last run found. window (pixels) is how far from there ink still counts; where two curves run
    close together a small window and more hints keep the trace on its own curve."""
    hx = np.array([p[0] for p in hints_px], float)
    hy = np.array([p[1] for p in hints_px], float)
    order = np.argsort(hx)
    hx, hy = hx[order], hy[order]
    first, last = int(round(hx[0])), int(round(hx[-1]))
    window = window or max(6., .03*height)
    found = np.full(last-first+1, np.nan)
    previous, since = None, 99
    for i, column in enumerate(range(first, last+1)):
        if column < 0 or column >= mask.shape[1]:
            continue
        expected = float(np.interp(column, hx, hy))
        slope = abs(float(np.interp(column+1, hx, hy)) - expected)
        reach = window + 2.5*slope
        centre = expected if strict or previous is None or since > 8 or abs(previous-expected) > reach else .5*(previous+expected)
        best, distance = None, None
        for start, stop in runs(mask[:, column]):
            middle = (start+stop-1)/2
            if stop-start > max(3*reach, 40):
                continue                      # a block of text or a filled area, not a line
            gap = abs(middle-centre)
            if gap <= reach and (distance is None or gap < distance):
                best, distance = middle, gap
        if best is None:
            since += 1
            continue
        found[i] = best
        previous, since = best, 0
    return np.arange(first, last+1, dtype=float), despiked(found)


def despiked(rows, span=21, limit=4.):
    """rows with points that jump away from their neighbours' median (a hop onto a neighbouring curve) dropped."""
    good = np.flatnonzero(np.isfinite(rows))
    if len(good) < span:
        return rows
    values = rows[good]
    half = span//2
    padded = np.pad(values, half, mode='edge')
    median = np.median(np.lib.stride_tricks.sliding_window_view(padded, span), axis=1)
    out = rows.copy()
    out[good[np.abs(values - median) > limit]] = np.nan
    return out


def extract_plot(name, spec, plot_name, plot, image, dpi):
    scale = dpi/72
    x0, y0, x1, y1 = [round(v*scale) for v in plot['box']]
    crop = image[y0:y1, x0:x1]
    axes = Axes(plot)
    base_threshold = plot.get('threshold', 140)
    result = {}
    lines = plot['lines']
    for line_name, line in lines.items():
        mask = ink(crop, line.get('threshold') or base_threshold, line.get('color') or plot.get('color'))
        mask, _, _ = without_grid(mask)
        for mx0, my0, mx1, my1 in plot.get('mask', []) + line.get('mask', []):
            mask[max(0, round(my0*scale)-y0):max(0, round(my1*scale)-y0), max(0, round(mx0*scale)-x0):max(0, round(mx1*scale)-x0)] = False
        hints = [(px*scale - x0, py*scale - y0) for px, py in hint_points(axes, line)]
        window = line.get('window', plot.get('window'))           # axis units of y
        if window is not None:
            window = abs(window/axes.ys)*scale if not plot.get('ylog') else abs(window/axes.ys)*scale
        columns, rows = trace(mask, hints, crop.shape[0], window, line.get('strict', plot.get('strict', False)))
        good = np.isfinite(rows)
        if good.sum() < 3:
            raise ValueError(f'{plot_name}/{line_name}: no ink found along the hints')
        xs, ys = axes.value((columns[good] + x0)/scale, (rows[good] + y0)/scale)
        step = line.get('step', plot.get('step'))
        if step:
            # Resample on a regular grid of x (axis units) inside the traced range; short gaps are bridged.
            grid = np.arange(np.floor(xs.min()/step + 1e-6)*step, np.ceil(xs.max()/step - 1e-6)*step + step/2, step)
            ys = np.interp(grid, xs, ys)
            xs = grid
        result[line_name] = [[round(float(x), 4), round(float(y), 4)] for x, y in zip(xs, ys)]
        result[line_name + ':coverage'] = round(float(good.mean()), 3)
    return result, (x0, y0, x1, y1)


def overlay(name, plot_name, plot, image, dpi, box, traced):
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = box
    crop = Image.fromarray(image[y0:y1, x0:x1].copy())
    draw = ImageDraw.Draw(crop)
    axes = Axes(plot)
    scale = dpi/72
    for i, (line_name, points) in enumerate((k, v) for k, v in traced.items() if ':' not in k):
        colour = NAMED.get(line_name, COLOURS[i % len(COLOURS)])
        pts = []
        for x, y in points:
            px, py = axes.point(x, y)
            pts.append((px*scale - x0, py*scale - y0))
        if len(pts) > 1:
            draw.line(pts, fill=colour, width=max(2, round(scale)))
        for px, py in hint_points(axes, plot['lines'][line_name]):
            cx, cy = px*scale - x0, py*scale - y0
            draw.ellipse([cx-6, cy-6, cx+6, cy+6], outline=colour, width=2)
        if pts:
            draw.text((pts[-1][0]+4, pts[-1][1]-12), line_name, fill=colour)
    OVERLAYS.mkdir(parents=True, exist_ok=True)
    if crop.width > 1600:
        crop = crop.resize((1600, round(crop.height*1600/crop.width)))
    target = OVERLAYS/f'{name}-{plot_name}.png'
    crop.save(target)
    return target


def extract(name, only=()):
    spec = sheet(name)
    dpi = spec.get('dpi', 400)
    data = spec_digest(name, spec)
    traced_path = TRACED/f'{name}.json'
    previous = json.loads(traced_path.read_text('utf-8')) if traced_path.exists() else {}
    plots = dict(previous.get('plots', {}))
    pages = {}
    for plot_name, plot in spec['plots'].items():
        if only and plot_name not in only:
            continue
        if plot['page'] not in pages:
            pages[plot['page']] = render(name, plot['page'], dpi, spec)
        traced, box = extract_plot(name, spec, plot_name, plot, pages[plot['page']], dpi)
        plots[plot_name] = traced
        target = overlay(name, plot_name, plot, pages[plot['page']], dpi, box, traced)
        coverage = {k.split(':')[0]: v for k, v in traced.items() if k.endswith(':coverage')}
        print(f'{plot_name}: {len([k for k in traced if ":" not in k])} lines, coverage {coverage} -> {target}')
    TRACED.mkdir(exist_ok=True)
    traced_path.write_text(json.dumps({**data, 'plots': plots}, indent=1), 'utf-8')
    return traced_path


def spec_digest(name, spec):
    path = pdf_path(name, spec)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = FOLDER/'manifest.json'
    url = None
    if manifest.exists():
        entry = json.loads(manifest.read_text('utf-8')).get(path.stem, {})
        url = entry.get('url')
        if entry.get('sha256') and entry['sha256'] != digest:
            raise ValueError(f'{path.name} differs from the downloaded copy recorded in manifest.json')
    return {'id': name, 'document': path.name, 'url': url, 'sha256': digest}


def show(name, page, box=None, grid=None, dpi=200):
    """Page (or crop) with a ruler in PDF points along the top and left edges."""
    from PIL import Image, ImageDraw
    image = render(name, page, dpi)
    scale = dpi/72
    if box:
        x0, y0, x1, y1 = box
    else:
        x0, y0, x1, y1 = 0, 0, image.shape[1]/scale, image.shape[0]/scale
    crop = Image.fromarray(image[round(y0*scale):round(y1*scale), round(x0*scale):round(x1*scale)])
    margin = 44
    canvas = Image.new('RGB', (crop.width + margin, crop.height + margin), 'white')
    canvas.paste(crop, (margin, margin))
    draw = ImageDraw.Draw(canvas)
    step = grid or (10 if (x1-x0) < 300 else 20)
    for value in np.arange(np.ceil(x0/step)*step, x1, step):
        px = margin + (value-x0)*scale
        major = round(value) % (step*5) == 0
        draw.line([(px, margin-(12 if major else 5)), (px, margin)], fill='black')
        if grid:
            draw.line([(px, margin), (px, canvas.height)], fill=(150, 220, 255) if not major else (80, 160, 255))
        if major:
            draw.text((px-10, 4), f'{value:g}', fill='black')
    for value in np.arange(np.ceil(y0/step)*step, y1, step):
        py = margin + (value-y0)*scale
        major = round(value) % (step*5) == 0
        draw.line([(margin-(12 if major else 5), py), (margin, py)], fill='black')
        if grid:
            draw.line([(margin, py), (canvas.width, py)], fill=(150, 220, 255) if not major else (80, 160, 255))
        if major:
            draw.text((2, py-6), f'{value:g}', fill='black')
    OVERLAYS.mkdir(parents=True, exist_ok=True)
    suffix = '' if not box else '-' + '-'.join(str(round(v)) for v in box)
    target = OVERLAYS/f'show-{name}-p{page}{suffix}.png'
    canvas.save(target)
    print(target, canvas.size)


def words(name, page, box=None):
    import pdfplumber
    with pdfplumber.open(pdf_path(name)) as doc:
        p = doc.pages[page-1]
        for w in p.extract_words(keep_blank_chars=False):
            cx, cy = (w['x0']+w['x1'])/2, (w['top']+w['bottom'])/2
            if box and not (box[0] <= cx <= box[2] and box[1] <= cy <= box[3]):
                continue
            print(f"{w['text']!r:24s} x {w['x0']:.1f}-{w['x1']:.1f} (centre {cx:.1f})  y {w['top']:.1f}-{w['bottom']:.1f} (centre {cy:.1f})")


def grid(name, page, box, dpi=400, threshold=140):
    image = render(name, page, dpi)
    scale = dpi/72
    x0, y0, x1, y1 = [round(v*scale) for v in box]
    mask = ink(image[y0:y1, x0:x1], threshold)
    _, rows, cols = without_grid(mask)

    def groups(indices, origin):
        out = []
        for index in indices:
            if out and index - out[-1][-1] <= 2:
                out[-1].append(index)
            else:
                out.append([index])
        return [round(float(np.mean(g) + origin)/scale, 2) for g in out]
    print('horizontal lines at y =', groups(rows, y0))
    print('vertical lines at x =', groups(cols, x0))


def sampled_path(o):
    """Points along a pdfplumber path, with its Bezier segments sampled (their control points are not on it)."""
    path = o.get('path')
    if not path:
        return [(float(x), float(y)) for x, y in o.get('pts', [])]
    out = []
    for segment in path:
        op, coords = segment[0], [tuple(map(float, c)) for c in segment[1:]]
        if op in ('m', 'l'):
            out.append(coords[0])
        elif op in ('c', 'v', 'y') and out:
            # c: two control points; v: the first is the current point; y: the second is the end point.
            p0 = out[-1]
            p1, p2, p3 = coords if op == 'c' else (p0, *coords) if op == 'v' else (coords[0], coords[1], coords[1])
            for u in np.linspace(.125, 1, 8):
                v = 1 - u
                out.append((v**3*p0[0] + 3*v*v*u*p1[0] + 3*v*u*u*p2[0] + u**3*p3[0],
                            v**3*p0[1] + 3*v*v*u*p1[1] + 3*v*u*u*p2[1] + u**3*p3[1]))
    return out


def paths(name, page, box, emit=None, spacing=2.):
    """Vector paths (pdfplumber curves and lines) inside a plot. Paths drawn in pieces are listed piece by piece;
    --emit joins the chosen ones in order and prints points about `spacing` points apart, ready for hints_pt."""
    import pdfplumber
    with pdfplumber.open(pdf_path(name)) as doc:
        p = doc.pages[page-1]
        found = []
        width = (box[2]-box[0]) if box else p.width
        height = (box[3]-box[1]) if box else p.height
        for kind, objects in (('curve', p.curves), ('line', p.lines)):
            for o in objects:
                pts = sampled_path(o)
                if len(pts) < 2:
                    continue
                xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
                if box and not (box[0]-1 <= min(xs) and max(xs) <= box[2]+1 and box[1]-1 <= min(ys) and max(ys) <= box[3]+1):
                    continue
                span_x, span_y = max(xs)-min(xs), max(ys)-min(ys)
                straight = span_x < .5 or span_y < .5
                if straight and (max(span_x, span_y) < 3 or span_x > .6*width or span_y > .6*height):
                    continue                               # ticks and grid lines; short flat curve pieces stay
                found.append((kind, o, pts))
    if emit is None:
        for i, (kind, o, pts) in enumerate(found):
            xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
            colour = o.get('stroking_color')
            dash = o.get('dash')
            print(f'{i:3d} {kind:5s} {len(pts):4d} pts  x {min(xs):6.1f}-{max(xs):6.1f}  y {min(ys):6.1f}-{max(ys):6.1f}  '
                  f'width {o.get("linewidth", 0):.2f}  colour {colour}  dash {dash}')
        return
    points = []
    for i in emit:
        pts = found[i][2]
        if points and abs(pts[0][0] - points[-1][0]) > abs(pts[-1][0] - points[-1][0]):
            pts = pts[::-1]                              # joined end to end
        points.extend(pts)
    out = [points[0]]
    for q in points[1:]:
        if np.hypot(q[0]-out[-1][0], q[1]-out[-1][1]) >= spacing:
            out.append(q)
    if out[-1] != points[-1]:
        out.append(points[-1])
    print(json.dumps([[round(x, 2), round(y, 2)] for x, y in out]))


def check(name, plot, line, xs):
    data = json.loads((TRACED/f'{name}.json').read_text('utf-8'))['plots'][plot][line]
    points = np.array(data)
    for x in xs:
        print(f'{x:g}: {np.interp(x, points[:, 0], points[:, 1]):.4f}')


def main(argv):
    command, args = argv[0], argv[1:]

    def option(flag, count=1, cast=float):
        if flag not in args:
            return None
        i = args.index(flag)
        values = [cast(v) for v in args[i+1:i+1+count]]
        del args[i:i+1+count]
        return values if count > 1 else values[0]
    if command == 'show':
        box = option('--box', 4); gridstep = option('--grid'); dpi = option('--dpi', cast=int) or 200
        show(args[0], int(args[1]), box, gridstep, dpi)
    elif command == 'words':
        box = option('--box', 4)
        words(args[0], int(args[1]), box)
    elif command == 'grid':
        box = option('--box', 4); threshold = option('--threshold', cast=int) or 140
        grid(args[0], int(args[1]), box, threshold=threshold)
    elif command == 'paths':
        box = option('--box', 4)
        emit = None
        if '--emit' in args:
            i = args.index('--emit'); emit = [int(v) for v in args[i+1:]]; del args[i:]
        paths(args[0], int(args[1]), box, emit)
    elif command == 'extract':
        extract(args[0], set(args[1:]))
    elif command == 'check':
        check(args[0], args[1], args[2], [float(v) for v in args[3:]])
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
