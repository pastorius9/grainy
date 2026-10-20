# Film datasheet data

Grainy's film looks are simulated from the curves that film makers publish in their technical datasheets. This
folder holds what was read from those datasheets and the tool that reads them. The datasheets themselves (PDF) are
not part of the repository: they stay in a local folder (`GRAINY_FILM_SHEETS`, by default `film-datasheets`
beside the repository folder, with `manifest.json` recording each file's URL and SHA-256).
Nothing here is fitted to another program's rendering.

- `digitize.py` — renders a datasheet page and follows each curve of a plot through its ink, from a few hints.
- `sheets/<id>.json` — for one film (or paper): where each plot is, how its axes run, the hints, and the
  datasheet's facts (`meta`). Written by hand.
- `traced/<id>.json` — the curves read from the plots (output of `digitize.py extract`), with the document's
  name, URL and checksum.

## Reading a datasheet (how to write `sheets/<id>.json`)

Run everything from the repository root with `.venv\Scripts\python.exe` and `PYTHONUTF8=1`.

1. Find the plot pages: `survey.json` in the datasheet folder lists, per page, how many vector curves and images
   it has and which keywords appear. Look at a page:
   `python tools/film/digitize.py show <pdf-id> <page>` (prints the PNG path; open it with the Read tool).
   A crop with a fine ruler and grid in PDF points:
   `python tools/film/digitize.py show <pdf-id> <page> --box x0 y0 x1 y1 --grid 10 --dpi 300`.
   Coordinates are PDF points from the page's top-left corner (x right, y down).
2. Calibrate the axes. `python tools/film/digitize.py grid <pdf-id> <page> --box x0 y0 x1 y1` prints the
   positions of the long grid lines and the plot frame inside the box; match them with the tick labels
   (for vector sheets `python tools/film/digitize.py words <pdf-id> <page> --box ...` prints each tick label's
   position; a label's centre is shifted by a leading minus sign, so prefer grid lines and frame edges).
   Give at least two `[pt, value]` pairs per axis, as far apart as possible (`"x"` and `"y"`).
3. `box` is the inside of the plot frame (a point or two inside it). `mask` lists rectangles to ignore inside the
   box: legends, labels such as "Yellow-Forming Layer", text boxes, arrows.
4. For each curve give `hints`: points along it in axis units (or `hints_pt` in PDF points, read off the ruler),
   On vector sheets (survey.json shows vector curves on the page), the curves' own paths make the best hints:
   `python tools/film/digitize.py paths <pdf-id> <page> --box x0 y0 x1 y1` lists the paths in the plot (points,
   extent, colour, dash); `... --emit i [j ...]` prints the chosen path(s), joined in order, as a `hints_pt` list.
   A curve drawn in several pieces needs all its pieces in `--emit`. Scanned sheets need hand-placed hints.
   from its first to its last x. Put more hints where curves cross, touch, turn sharply, or where a dashed curve
   runs close to another one; there, also set a small `window` (in y units) and `"strict": true`.
5. `python tools/film/digitize.py extract <id>` traces every plot, writes `traced/<id>.json` and prints the
   overlay PNG of each plot (`overlays/<id>-<plot>.png` in the datasheet folder). Open every overlay and check
   that each coloured line lies on its own black curve from end to end, and that the hint circles sit on the
   curve. Fix hints, masks or windows and run again until it does. `coverage` is the share of columns where ink
   was found; below about 0.8, look closely (dashed lines are lower, that is expected).
6. `python tools/film/digitize.py check <id> <plot> <line> x...` prints the traced value at given x, to compare
   with values printed on the sheet (for example Kodak's "Log H Ref" and the grey-card density table).

## Plot and line names

| plot | x axis | y axis | lines |
|---|---|---|---|
| `curves` | log exposure (lux·s, as printed) | density | colour: `r`, `g`, `b` (the curve labelled R / red / cyan dye is `r`); black-and-white: `v` |
| `sensitivity` | wavelength (nm) | log sensitivity, the numbers as printed on the axis | colour: `r`, `g`, `b` (red-sensitive = cyan-forming layer = `r`); black-and-white: `v` |
| `dyes` | wavelength (nm) | density as printed | `c`, `m`, `y` for the cyan, magenta, yellow dye; `min` (minimum density / D-min), `mid` (midscale neutral), `neutral` (a visual neutral) when plotted |

Use `"step": 0.05` for `curves` and `"step": 5` for `sensitivity` and `dyes`. A plot with only some of these
lines is fine; a plot that the datasheet does not have is left out. Black-and-white sheets often show several
development times: trace the one the sheet calls normal or recommended as `v`, and others as `v-<developer>-<minutes>`
(for example `v-d76-9`). A sheet whose spectral sensitivity is only a wedge spectrogram photograph has no
`sensitivity` plot. If the x axis of `curves` is relative log exposure (no absolute lux·s), say so in `meta.notes`.

## `meta`

```json
"meta": {
  "name": "Kodak Portra 400",            // as the product is named; for a sheet with several films, one sheet file per film
  "maker": "Kodak",                       // Kodak, Fujifilm, Ilford, Harman, Foma, Agfa
  "kind": "colour-negative",             // colour-negative, colour-slide, bw-negative, colour-paper, print-film, bw-paper
  "cine": false,                          // a motion-picture stock
  "iso": 400,                             // box speed (ISO / EI as rated for the curves)
  "balance": "daylight",                  // the exposure light of the curves: "daylight" or "tungsten" (give the kelvin in notes)
  "log_h_ref": -1.44,                     // Kodak's "Log H Ref" or any printed normal exposure; null when not printed
  "densitometry": "Status M",             // as printed
  "process": "C-41",                      // and, for black and white, the developer and time of the traced `v` curve
  "grain": {"pgi": 37},                   // Kodak Print Grain Index for 135 at 4x6; or {"rms": 8} (diffuse rms granularity, give the aperture/density in notes)
  "print_on": "Kodak Endura Premier",     // a paper the sheet recommends, or null
  "document": "E-4050",                   // publication number as printed
  "date": "2025-01",                      // revision date as printed
  "notes": ["anything a reader of the numbers must know: extrapolations, curves that overlap, values that could not be read"]
}
```

Keep helper scripts of your own in a subfolder of the scratchpad named after your task (other agents share
the scratchpad). Facts only from the datasheet itself. If the sheet is not for the film its file name suggests, say so in `notes`
and name the sheet after the film it is for.

## Building the app's film data

`pip install -r tools/film/requirements.txt` (tools only; the app does not need them), then:

- `python tools/film/build.py` turns every sheet with traced curves into `assets/film/stocks/<id>.json` or
  `assets/film/papers/<id>.json` and writes `assets/film/index.json`, the list the app shows. It reports films it
  leaves out (no paper yet, a slide without dye spectra). The choices it makes that are not on the datasheets
  (which paper prints which film, grain amounts, reference exposures where none is printed) are listed at its top.
- `python tools/film/colorimetry.py` rebuilds `assets/film/colorimetry.json` from the CIE files (see
  `assets/licenses/Film/NOTICE.txt`).
- `tests/test_film_look.py` checks every shipped stock.

How a stock becomes a look is described at the top of `luma/film_look.py`.
