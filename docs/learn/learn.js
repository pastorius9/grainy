/* Learn Grainy: the practice windows of docs/learn. The maths follows Grainy's own code (named in each function);
   what a browser cannot redo (film looks, Select Sky, Auto tone/colour) comes from tools/learn_assets.py. */
'use strict';
(() => {
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const LN2 = Math.LN2;
const clamp = (v, lo, hi) => v < lo ? lo : v > hi ? hi : v;

/* ---------- language ---------- */
const onLanguage = [];
const lang = () => document.documentElement.dataset.lang;
function setLanguage(value) {
  document.documentElement.dataset.lang = value; document.documentElement.lang = value;
  $$('[data-set-lang]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.setLang === value)));
  try { localStorage.setItem('grainy-lang', value); } catch (e) {}
  onLanguage.forEach(f => f());
}
$$('[data-set-lang]').forEach(b => b.addEventListener('click', () => setLanguage(b.dataset.setLang)));
let savedLanguage = null;
try { savedLanguage = localStorage.getItem('grainy-lang'); } catch (e) {}
setLanguage(savedLanguage || ((navigator.language || '').toLowerCase().startsWith('ko') ? 'ko' : 'en'));
const t = (ko, en) => lang() === 'ko' ? ko : en;

/* ---------- progress ---------- */
const STORE = 'grainy-learn-v1';
let done = new Set();
try { done = new Set(JSON.parse(localStorage.getItem(STORE) || '[]')); } catch (e) {}
const allTasks = $$('[data-task]').map(li => li.dataset.task);
function mark(task) {
  if (done.has(task)) return;
  done.add(task);
  try { localStorage.setItem(STORE, JSON.stringify([...done])); } catch (e) {}
  refreshProgress();
}
function refreshProgress() {
  $$('[data-task]').forEach(li => li.classList.toggle('done', done.has(li.dataset.task)));
  $$('.lesson').forEach(section => {
    const tasks = $$('[data-task]', section).map(li => li.dataset.task);
    const complete = tasks.length > 0 && tasks.every(k => done.has(k));
    section.classList.toggle('complete', complete);
    const item = $(`#toc li[data-for="${section.id}"]`);
    if (item) item.classList.toggle('done', complete);
  });
  const count = allTasks.filter(k => done.has(k)).length;
  $('#progress').style.width = `${Math.round(100 * count / allTasks.length)}%`;
}
$('#reset').addEventListener('click', () => {
  done = new Set();
  try { localStorage.removeItem(STORE); } catch (e) {}
  refreshProgress();
});

/* ---------- lessons and navigation ---------- */
const lessons = $$('.lesson');
const toc = $('#toc');
lessons.forEach((section, i) => {
  const li = document.createElement('li'); li.dataset.for = section.id;
  li.innerHTML = `<a href="#${section.id}"><span class="num"><b>${i + 1}</b></span><span class="label"><span lang="ko">${section.dataset.titleKo}</span><span lang="en">${section.dataset.titleEn}</span></span></a>`;
  toc.appendChild(li);
});
const started = new Set();
let current = null;
function show(id, scroll) {
  const section = lessons.find(s => s.id === id) || lessons[0];
  current = section.id;
  lessons.forEach(s => s.classList.toggle('on', s === section));
  $$('#toc a').forEach(a => a.setAttribute('aria-current', String(a.getAttribute('href') === `#${section.id}`)));
  const i = lessons.indexOf(section);
  const prev = lessons[i - 1], next = lessons[i + 1];
  const title = s => `<span lang="ko">${s.dataset.titleKo}</span><span lang="en">${s.dataset.titleEn}</span>`;
  $('#pager').innerHTML = (prev ? `<a href="#${prev.id}">← ${title(prev)}</a>` : '') + (next ? `<a class="next" href="#${next.id}">${title(next)} →</a>` : '');
  if (!started.has(section.id) && MODULES[section.id]) { started.add(section.id); MODULES[section.id](); }
  if (scroll) window.scrollTo({ top: 0 });
}
window.addEventListener('hashchange', () => show(location.hash.slice(1), true));

/* ---------- colour maths ---------- */
// sRGB transfer, as engine.to_linear / to_srgb.
const DEC = new Float32Array(256);
for (let i = 0; i < 256; i++) { const c = i / 255; DEC[i] = c <= .04045 ? c / 12.92 : Math.pow((c + .055) / 1.055, 2.4); }
const ENC_N = 4096, ENC = new Float32Array(ENC_N + 2);
for (let i = 0; i <= ENC_N + 1; i++) { const x = Math.min(1, i / ENC_N); ENC[i] = x <= .0031308 ? x * 12.92 : 1.055 * Math.pow(x, 1 / 2.4) - .055; }
function enc(x) {
  if (!(x > 0)) return 0;
  if (x >= 1) return x === 1 ? 1 : 1.055 * Math.pow(x, 1 / 2.4) - .055;
  const f = x * ENC_N, i = f | 0;
  return ENC[i] + (ENC[i + 1] - ENC[i]) * (f - i);
}
const linearOf = v => v <= .04045 ? v / 12.92 : Math.pow((Math.max(v, 0) + .055) / 1.055, 2.4);
const LIN = new Float32Array(ENC_N + 2);
for (let i = 0; i <= ENC_N + 1; i++) LIN[i] = linearOf(Math.min(1, i / ENC_N));
function lin(v) {
  if (!(v > 0)) return 0;
  if (v >= 1) return v === 1 ? 1 : linearOf(v);
  const f = v * ENC_N, i = f | 0;
  return LIN[i] + (LIN[i + 1] - LIN[i]) * (f - i);
}
// processing.white_balance
const gains = (T, tint) => [2 ** (T / 180 + tint / 400), 2 ** (-tint / 250), 2 ** (-T / 180 + tint / 400)];
// engine.highlight_rolloff
function rolloff(x, white, knee = .5) {
  if (white <= 1 || x <= knee) return x;
  const slope = (white - knee) / (1 - knee), u = (x - knee) / (white - knee);
  return knee + (1 - knee) * (slope * u / (1 + (slope - 1) * u));
}
// np.interp over [[x, y], ...]
function interp(v, points) {
  if (v <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i++) {
    if (v <= points[i][0]) { const [x0, y0] = points[i - 1], [x1, y1] = points[i]; return x1 === x0 ? y1 : y0 + (y1 - y0) * (v - x0) / (x1 - x0); }
  }
  return points[points.length - 1][1];
}
const identity = points => points.every(([x, y]) => Math.abs(x - y) < 1e-6);
function table(points, size = 1024) {          // a curve as a lookup table over 0..1
  const out = new Float32Array(size + 1);
  for (let i = 0; i <= size; i++) out[i] = interp(i / size, points);
  return out;
}
const look = (lut, v) => { const f = clamp(v, 0, 1) * (lut.length - 1), i = f | 0; return i >= lut.length - 1 ? lut[lut.length - 1] : lut[i] + (lut[i + 1] - lut[i]) * (f - i); };

/* ---------- images ---------- */
let DATA = null;
const dataReady = fetch('data.json').then(r => r.json()).then(d => (DATA = d));
const cache = {};
function pixels(src) {
  if (!cache[src]) cache[src] = new Promise((resolve, reject) => {
    const img = new Image(); img.onload = () => {
      const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight;
      const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0);
      resolve(x.getImageData(0, 0, c.width, c.height));
    }; img.onerror = reject; img.src = src;
  });
  return cache[src];
}
function frame(canvas, w, h) { canvas.width = w; canvas.height = h; return canvas.getContext('2d'); }
function scheduler(fn) {
  let pending = false;
  return () => { if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; fn(); }); } };
}
function canvasPoint(canvas, e) {
  const r = canvas.getBoundingClientRect();
  return [clamp((e.clientX - r.left) / r.width, 0, 1), clamp((e.clientY - r.top) / r.height, 0, 1)];
}
function slider(id, value, onInput, format) {
  const input = $(`#${id}`), out = input.parentElement.querySelector('output');
  const show = () => { out.textContent = format ? format(+input.value) : (+input.value > 0 ? '+' : '') + input.value; };
  input.value = value; show();
  input.addEventListener('input', () => { show(); onInput(+input.value); });
  const label = input.parentElement.querySelector('label');
  if (label) label.addEventListener('dblclick', () => { input.value = value; show(); onInput(value); });
  return { set(v) { input.value = v; show(); }, get() { return +input.value; }, input };
}
const typing = () => { const a = document.activeElement; return a && (a.tagName === 'INPUT' && a.type !== 'range' || a.tagName === 'TEXTAREA' || a.isContentEditable); };

/* ---------- lesson 1: the tour ---------- */
const PARTS = [
  ['library_button', '라이브러리', 'Library', '한 롤을 한눈에 보며 별점·표시를 붙이고 고르는 화면입니다.', 'See a whole roll at once, rate, flag and pick.'],
  ['develop_button', '현상', 'Develop', '사진을 한 장씩 크게 보며 보정하는 화면입니다. 라이브러리와 현상은 같은 보관함을 보는 두 가지 방법입니다.', 'Edit one photo at a time. Library and Develop are two views of the same catalog.'],
  ['import', '사진 가져오기', 'Import photos', '폴더나 파일을 보관함에 더합니다. 원본 파일은 옮기거나 바꾸지 않습니다. 등록한 폴더에 새 사진이 생기면 자동으로 들어옵니다.', 'Add folders or files to the catalog. Originals are never moved or changed; new photos in registered folders come in automatically.'],
  ['export_button', '내보내기', 'Export', '보정한 사진을 JPEG, TIFF, AVIF 등 새 파일로 저장합니다.', 'Save edited photos as new JPEG, TIFF, AVIF… files.'],
  ['feedback', '피드백', 'Feedback', '문제점이나 아이디어를 바로 보냅니다. 반영되면 업데이트 후 알려 드립니다.', 'Send a problem or an idea; you are told when it ships.'],
  ['folders', '폴더', 'Folders', '등록한 폴더들입니다. 폴더를 누르면 그 안의 사진만 봅니다.', 'Your registered folders; click one to see only its photos.'],
  ['presets', '프리셋', 'Presets', '필름 룩 82종이 회사별 폴더에 있고, 직접 저장한 프리셋은 기타 프리셋에 있습니다.', 'The 82 film looks in a folder per maker, your own presets under Other presets.'],
  ['photo', '사진', 'Photo', '보정 결과가 바로 보입니다. 휠로 확대하고, 확대한 채로 끌어서 움직입니다.', 'Your edits show at once. Scroll to zoom, drag to move around.'],
  ['filmstrip', '필름 스트립', 'Filmstrip', '같은 목록의 사진들입니다. 눌러서 다음 사진으로 넘어갑니다.', 'The photos of the current list; click to move to another one.'],
  ['histogram', '히스토그램', 'Histogram', '밝기 분포입니다. 양쪽 삼각형을 누르면 하얗게·까맣게 날아간 곳을 표시합니다.', 'The spread of brightness; the triangles show clipped highlights and shadows.'],
  ['panels', '보정 패널', 'Edit panels', '기본·필름·톤 곡선·색상 혼합·디테일… 패널을 펼쳐 씁니다. 제목줄 오른쪽 스위치로 패널을 잠시 끌 수 있고, 보정이 들어간 패널에는 작은 점이 붙습니다.', 'Basic, Film, Tone curve, Color mixer, Detail… Each header has a switch to turn the panel off for a moment, and a small dot when it holds edits.'],
  ['compare', '비교', 'Compare', '보정 전후나 두 사진을 나란히 봅니다. 단축키 C.', 'Before and after, or two photos, side by side. Key: C.'],
  ['before', '원본 비교', 'Before / after', '누르고 있는 동안 원본을 봅니다. 단축키 \\.', 'Shows the original while held. Key: \\.'],
];
function startTour() {
  const shot = $('#tour-shot'), img = $('img', shot), legend = $('#tour-legend'), info = $('#tour-info');
  const seen = new Set();
  let rects = {};
  function choose(name) {
    const part = PARTS.find(p => p[0] === name); if (!part) return;
    seen.add(name);
    $$('.spot', shot).forEach(s => { s.setAttribute('aria-pressed', String(s.dataset.part === name)); s.classList.toggle('seen', seen.has(s.dataset.part)); });
    $$('button', legend).forEach(b => { b.setAttribute('aria-pressed', String(b.dataset.part === name)); b.classList.toggle('seen', seen.has(b.dataset.part)); });
    info.innerHTML = `<h3><span lang="ko">${part[1]}</span><span lang="en">${part[2]}</span></h3><p><span lang="ko">${part[3]}</span><span lang="en">${part[4]}</span></p>`;
    if (seen.size >= 6) mark('tour.six');
    if (name === 'panels') mark('tour.panels');
    if (seen.has('library_button') && seen.has('develop_button')) mark('tour.library');
  }
  function build() {
    img.src = img.dataset[lang()];
    $$('.spot', shot).forEach(s => s.remove());
    fetch(`img/screens-${lang()}.json`).then(r => r.json()).then(d => {
      rects = d.parts;
      PARTS.forEach(([name], i) => {
        const r = rects[name]; if (!r) return;
        const b = document.createElement('button'); b.type = 'button'; b.className = 'spot'; b.dataset.part = name;
        Object.assign(b.style, { left: `${r[0] * 100}%`, top: `${r[1] * 100}%`, width: `${r[2] * 100}%`, height: `${r[3] * 100}%` });
        b.innerHTML = `<span class="dot">${i + 1}</span>`;
        b.setAttribute('aria-label', lang() === 'ko' ? PARTS[i][1] : PARTS[i][2]);
        b.addEventListener('click', () => choose(name));
        b.classList.toggle('seen', seen.has(name));
        shot.appendChild(b);
      });
    });
    legend.innerHTML = PARTS.map(([name, ko, en], i) => `<button type="button" data-part="${name}" class="${seen.has(name) ? 'seen' : ''}">${i + 1} · <span lang="ko">${ko}</span><span lang="en">${en}</span></button>`).join('');
    $$('button', legend).forEach(b => b.addEventListener('click', () => choose(b.dataset.part)));
  }
  build(); onLanguage.push(build);
}

/* ---------- lesson 2: negative ---------- */
// negative.apply: light = exp(range*ln2*(d/span - 1) + density*ln2), d = log(base/value), capped at the densest part.
function negativeTo(out, logs, n, base, peak, density, range, floor) {
  const scale = [], lb = [];
  for (let c = 0; c < 3; c++) {
    const p = Math.min(Math.max(peak[c], floor), base[c] * .9);
    scale[c] = range * LN2 / Math.log(base[c] / p); lb[c] = Math.log(base[c]);
  }
  const offset = (density - range) * LN2, cap = density * LN2;
  for (let i = 0; i < n; i++) for (let c = 0; c < 3; c++) {
    const k = i * 3 + c;
    out[k] = Math.exp(Math.min((lb[c] - logs[k]) * scale[c] + offset, cap));
  }
  return out;
}
async function scanLogs() {
  const [img] = await Promise.all([pixels('img/scan.jpg'), dataReady]);
  const n = img.width * img.height, logs = new Float32Array(n * 3), floor = DATA.negative.floor;
  for (let i = 0; i < n; i++) for (let c = 0; c < 3; c++) logs[i * 3 + c] = Math.log(Math.max(DEC[img.data[i * 4 + c]], floor));
  return { img, n, logs };
}
function startNegative() {
  const canvas = $('#neg-canvas'), badge = $('#neg-badge'), invert = $('#neg-invert');
  scanLogs().then(({ img, n, logs }) => {
    const ctx = frame(canvas, img.width, img.height), out = ctx.createImageData(img.width, img.height), light = new Float32Array(n * 3);
    const neg = DATA.negative, state = { on: false, density: neg.density, range: neg.range };
    const density = slider('neg-density', neg.density, v => { state.density = v; if (Math.abs(v - neg.density) > .09) mark('negative.density'); draw(); });
    const range = slider('neg-range', neg.range, v => { state.range = v; if (Math.abs(v - neg.range) > .2) mark('negative.range'); draw(); });
    const draw = scheduler(() => {
      if (!state.on) { ctx.putImageData(img, 0, 0); badge.style.display = 'block'; return; }
      badge.style.display = 'none';
      negativeTo(light, logs, n, neg.base, neg.peak, state.density, state.range, neg.floor);
      for (let i = 0; i < n; i++) {
        for (let c = 0; c < 3; c++) out.data[i * 4 + c] = Math.round(enc(Math.min(light[i * 3 + c], 1)) * 255);
        out.data[i * 4 + 3] = 255;
      }
      ctx.putImageData(out, 0, 0);
    });
    invert.addEventListener('click', () => {
      state.on = !state.on; invert.setAttribute('aria-pressed', String(state.on));
      density.input.disabled = range.input.disabled = !state.on;
      if (state.on) mark('negative.invert');
      draw();
    });
    draw();
  });
}

/* ---------- lesson 3: exposure and colour ---------- */
// engine._tone_encoded + _develop_tone_pixels (Blacks, Whites, Contrast, curve) + _develop_color_mix (saturation) + color_tools (channel curves)
function develop(out, light, n, s, tables) {
  const g = gains(s.temperature, s.tint), e = 2 ** s.exposure;
  const white = Math.min(e * Math.max(...g), 4);           // engine.rolloff_white
  const k = [g[0] * e, g[1] * e, g[2] * e];
  const black = clamp(s.blacks, -200, 200) / 250, whites = 2 ** (clamp(s.whites, -200, 200) / 100), contrast = 1 + s.contrast / 125;
  const sat = 1 + s.saturation / 100;
  const v = [0, 0, 0];
  for (let i = 0; i < n; i++) {
    for (let c = 0; c < 3; c++) {
      let x = rolloff(light[i * 3 + c] * k[c], white);
      x = enc(x);
      if (black < 0) x = Math.max(x + black, 0) / (1 + black); else if (black > 0) x = x * (1 - black) + black;
      x *= whites;
      if (contrast !== 1) x = (x - .5) * contrast + .5;
      if (tables.curve) x = look(tables.curve, x);
      v[c] = clamp(x, 0, 1);
    }
    if (sat !== 1) { const gray = v[0] * .2126 + v[1] * .7152 + v[2] * .0722; for (let c = 0; c < 3; c++) v[c] = gray + (v[c] - gray) * sat; }
    for (let c = 0; c < 3; c++) {
      let x = tables.channels ? look(tables.channels[c], v[c]) : v[c];
      out[i * 4 + c] = Math.round(clamp(x, 0, 1) * 255);
    }
    out[i * 4 + 3] = 255;
  }
}
// Grainy's eyedropper (app.sample_wb): ratios of the shown colour, in light, onto Temperature and Tint.
function eyedropper(state, rgb) {
  const [r, g, b] = rgb.map(v => Math.max(linearOf(v + .001), 1e-4));
  state.temperature = clamp(state.temperature + 90 * Math.log2(b / r), -100, 100);
  state.tint = clamp(state.tint + Math.log2(g / Math.sqrt(r * b)) / (1 / 400 + 1 / 250), -100, 100);
}
function meanColour(data, w, x0, y0, x1, y1) {        // eyedropper.mean_colour: clipped and black pixels left out
  let sum = [0, 0, 0], count = 0;
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
    const p = (y * w + x) * 4, px = [data[p] / 255, data[p + 1] / 255, data[p + 2] / 255];
    if (Math.max(...px) >= .98 || Math.min(...px) <= .005) continue;
    sum = sum.map((s, c) => s + px[c]); count++;
  }
  return count ? sum.map(s => s / count) : null;
}
function startBasic() {
  const canvas = $('#basic-canvas'), stage = $('#basic-stage'), badge = $('#basic-badge'), hint = $('#basic-hint'), picker = $('#basic-picker');
  scanLogs().then(({ img, n, logs }) => {
    const neg = DATA.negative;
    const light = negativeTo(new Float32Array(n * 3), logs, n, neg.base, neg.peak, neg.density, neg.range, neg.floor);
    const ctx = frame(canvas, img.width, img.height), out = ctx.createImageData(img.width, img.height);
    const plain = ctx.createImageData(img.width, img.height);
    const fresh = () => ({ exposure: 0, temperature: 0, tint: 0, contrast: 0, whites: 0, blacks: 0, saturation: 0, curve: null, channels: null });
    const state = fresh(), tables = { curve: null, channels: null };
    develop(plain.data, light, n, fresh(), {});
    let before = false;
    const controls = {};
    const draw = scheduler(() => {
      if (before) { ctx.putImageData(plain, 0, 0); badge.style.display = 'block'; return; }
      badge.style.display = 'none';
      develop(out.data, light, n, state, tables); ctx.putImageData(out, 0, 0);
    });
    for (const [key, digits] of [['exposure', 2], ['temperature', 0], ['tint', 0], ['contrast', 0], ['whites', 0], ['blacks', 0], ['saturation', 0]]) {
      controls[key] = slider(`basic-${key}`, 0, v => { state[key] = v; draw(); }, v => (v > 0 ? '+' : '') + v.toFixed(digits));
    }
    const sync = () => Object.entries(controls).forEach(([k, c]) => c.set(Math.round(state[k] * 100) / 100));
    $('#basic-auto-tone').addEventListener('click', () => {
      const a = DATA.auto_tone; Object.assign(state, { exposure: a.exposure, blacks: a.blacks, whites: a.whites });
      tables.curve = identity(a.curve) ? null : table(a.curve); sync(); draw(); mark('basic.autotone');
    });
    $('#basic-auto-color').addEventListener('click', () => {
      tables.channels = DATA.auto_color.rgb_curves.map(p => table(p)); draw(); mark('basic.autocolor');
    });
    const setPicking = on => { picker.setAttribute('aria-pressed', String(on)); stage.classList.toggle('pick', on); hint.hidden = !on; };
    picker.addEventListener('click', () => setPicking(picker.getAttribute('aria-pressed') !== 'true'));
    let drag = null;
    canvas.addEventListener('pointerdown', e => {
      if (picker.getAttribute('aria-pressed') !== 'true') return;
      drag = { start: canvasPoint(canvas, e), x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointerup', e => {
      if (!drag) return;
      const end = canvasPoint(canvas, e), w = canvas.width, h = canvas.height;
      const moved = Math.hypot(e.clientX - drag.x, e.clientY - drag.y) >= 4;
      const shown = ctx.getImageData(0, 0, w, h).data;
      let rgb;
      if (moved) {
        const [x0, x1] = [drag.start[0], end[0]].sort((a, b) => a - b), [y0, y1] = [drag.start[1], end[1]].sort((a, b) => a - b);
        rgb = meanColour(shown, w, Math.floor(x0 * (w - 1)), Math.floor(y0 * (h - 1)), Math.ceil(x1 * (w - 1)), Math.ceil(y1 * (h - 1)));
      } else {
        const px = Math.min(w - 1, Math.floor(end[0] * w)), py = Math.min(h - 1, Math.floor(end[1] * h));
        let sum = [0, 0, 0], count = 0;
        for (let y = Math.max(0, py - 3); y <= Math.min(h - 1, py + 3); y++) for (let x = Math.max(0, px - 3); x <= Math.min(w - 1, px + 3); x++) {
          const p = (y * w + x) * 4; sum = sum.map((s, c) => s + shown[p + c] / 255); count++;
        }
        rgb = sum.map(s => s / count);
      }
      drag = null;
      if (!rgb) return;
      eyedropper(state, rgb); sync(); setPicking(false); draw(); mark('basic.picker');
    });
    const toggleBefore = on => { before = on; draw(); if (on) mark('basic.before'); };
    const beforeButton = $('#basic-before');
    beforeButton.addEventListener('pointerdown', () => toggleBefore(true));
    ['pointerup', 'pointerleave'].forEach(k => beforeButton.addEventListener(k, () => before && toggleBefore(false)));
    document.addEventListener('keydown', e => {
      if (current !== 'basic' || typing() || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === '\\') { toggleBefore(!before); e.preventDefault(); }
      if (e.key.toLowerCase() === 'w') { setPicking(picker.getAttribute('aria-pressed') !== 'true'); e.preventDefault(); }
    });
    draw();
  });
}

/* ---------- lesson 4: film looks ---------- */
const KIND = { 'colour-negative': ['컬러 네거티브', 'colour negative'], 'colour-slide': ['컬러 슬라이드', 'colour slide'], 'bw-negative': ['흑백', 'black and white'] };
function startFilm() {
  const canvas = $('#film-canvas'), list = $('#film-list'), note = $('#film-note'), currentLabel = $('#film-current');
  Promise.all([pixels('img/garden.jpg'), dataReady]).then(([plain]) => {
    const ctx = frame(canvas, plain.width, plain.height), out = ctx.createImageData(plain.width, plain.height);
    let film = null, chosen = null, amount = 100, entryShown = null;
    const label = () => {
      const entry = entryShown;
      currentLabel.textContent = entry ? entry.name : t('필름 없음', 'No film');
      note.textContent = entry ? `${t(KIND[entry.kind][0], KIND[entry.kind][1])}${entry.iso ? ` · ISO ${entry.iso}` : ''}` : t('필름을 고르면 결과가 바로 보입니다.', 'Choose a film to see it at once.');
    };
    onLanguage.push(label);
    const strength = slider('film-strength', 100, v => { amount = v; if (v >= 30 && v <= 70 && chosen) mark('film.strength'); draw(); }, v => `${v}`);
    // Grainy blends a film look over the ordinary rendering in display values (engine._tone_encoded).
    const draw = scheduler(() => {
      if (!film) { ctx.putImageData(plain, 0, 0); return; }
      const s = amount / 100, a = plain.data, b = film.data, o = out.data;
      for (let i = 0; i < a.length; i++) o[i] = (i & 3) === 3 ? 255 : Math.round(a[i] + (b[i] - a[i]) * s);
      ctx.putImageData(out, 0, 0);
    });
    function choose(entry) {
      chosen = entry ? entry.id : null;
      $$('button', list).forEach(b => b.setAttribute('aria-pressed', String(b.dataset.id === (chosen || ''))));
      strength.input.disabled = !entry;
      entryShown = entry || null; label();
      if (!entry) { film = null; draw(); return; }
      pixels(`img/film-${entry.id}.jpg`).then(data => { if (chosen === entry.id) { film = data; draw(); } });
      mark({ 'colour-negative': 'film.negative', 'colour-slide': 'film.slide', 'bw-negative': 'film.bw' }[entry.kind]);
    }
    const makers = [...new Set(DATA.films.map(f => f.maker))];
    list.innerHTML = `<button type="button" data-id="" aria-pressed="true"><span lang="ko">필름 없음</span><span lang="en">No film</span></button>` +
      makers.map(m => `<details${m === 'Kodak' ? ' open' : ''}><summary>${m} (${DATA.films.filter(f => f.maker === m).length})</summary>` +
        DATA.films.filter(f => f.maker === m).map(f => `<button type="button" data-id="${f.id}">${f.name.startsWith(m + ' ') ? f.name.slice(m.length + 1) : f.name}</button>`).join('') + '</details>').join('');
    $$('button', list).forEach(b => b.addEventListener('click', () => choose(DATA.films.find(f => f.id === b.dataset.id))));
    choose(null); draw();
  });
}

/* ---------- lesson 5: masks ---------- */
// processing.apply_local for one layer: exposure in light, then contrast, temperature and saturation in display values.
function localEdit(image, mask, n, edit) {          // in place: each pixel reads its three values before writing them
  const e = 2 ** edit.exposure, contrast = 1 + edit.contrast / 125, warm = edit.temperature / 400, sat = 1 + edit.saturation / 100;
  const a = [0, 0, 0];
  for (let i = 0; i < n; i++) {
    const m = mask[i]; if (m < .002) continue;
    const k = i * 3;
    for (let c = 0; c < 3; c++) a[c] = (enc(lin(image[k + c]) * e) - .5) * contrast + .5;
    a[0] += warm; a[2] -= warm;
    const gray = a[0] * .2126 + a[1] * .7152 + a[2] * .0722;
    for (let c = 0; c < 3; c++) image[k + c] = image[k + c] * (1 - m) + (gray + (a[c] - gray) * sat) * m;
  }
}
function startMasks() {
  const canvas = $('#mask-canvas'), stage = $('#mask-stage'), hint = $('#mask-hint'), layersBox = $('#mask-layers'), controlsBox = $('#mask-controls');
  const overlayButton = $('#mask-overlay');
  Promise.all([pixels('img/garden.jpg'), pixels('img/garden-sky.png')]).then(([plain, skyImage]) => {
    const w = plain.width, h = plain.height, n = w * h;
    const ctx = frame(canvas, w, h), out = ctx.createImageData(w, h);
    const sky = new Float32Array(n); for (let i = 0; i < n; i++) sky[i] = skyImage.data[i * 4] / 255;
    const base = new Float32Array(n * 3); for (let i = 0; i < n; i++) for (let c = 0; c < 3; c++) base[i * 3 + c] = plain.data[i * 4 + c] / 255;
    const layers = [], kept = []; let selected = null, overlay = true;
    const blank = { exposure: 0, contrast: 0, temperature: 0, saturation: 0 };
    const controls = {};
    const draw = scheduler(() => {
      // Layers apply in order, as in Grainy; the result up to the layer being edited is kept between frames.
      const key = l => JSON.stringify(l.edit) + l.version;
      let start = 0, result = base;
      while (start < layers.length && kept[start] && kept[start].key === key(layers[start])) { result = kept[start].pixels; start++; }
      kept.length = start;
      for (let j = start; j < layers.length; j++) {
        const layer = layers[j], next = Float32Array.from(result);
        if (Object.values(layer.edit).some(v => v)) localEdit(next, layer.mask, n, layer.edit);
        kept.push({ key: key(layer), pixels: next }); result = next;
      }
      const tint = overlay && selected ? selected.mask : null;
      for (let i = 0; i < n; i++) {
        for (let c = 0; c < 3; c++) {
          let v = clamp(result[i * 3 + c], 0, 1);
          if (tint) v = v * (1 - .5 * tint[i]) + (c === 0 ? .9 : .12) * .5 * tint[i];
          out.data[i * 4 + c] = Math.round(v * 255);
        }
        out.data[i * 4 + 3] = 255;
      }
      ctx.putImageData(out, 0, 0);
    });
    const check = () => {
      const s = layers.find(l => l.kind === 'sky'), b = layers.find(l => l.kind === 'brush');
      if (s) mark('masks.sky');
      if (s && s.edit.exposure <= -.2) mark('masks.darken');
      if (b && b.edit.exposure >= .15) { let covered = 0; for (let i = 0; i < n; i += 7) covered += b.mask[i] > .5; if (covered * 7 > n * .004) mark('masks.brush'); }
    };
    for (const [key, digits] of [['exposure', 2], ['contrast', 0], ['temperature', 0], ['saturation', 0]]) {
      controls[key] = slider(`mask-${key}`, 0, v => { if (selected) { selected.edit[key] = v; check(); draw(); } }, v => (v > 0 ? '+' : '') + v.toFixed(digits));
    }
    const size = slider('mask-size', 7, () => {}, v => `${v}%`);
    function select(layer) {
      selected = layer;
      controlsBox.hidden = !layer;
      $$('button', layersBox).forEach(b => b.setAttribute('aria-pressed', String(layers[+b.dataset.i] === layer)));
      if (layer) {
        Object.entries(controls).forEach(([k, c]) => c.set(layer.edit[k]));
        $('#mask-title').innerHTML = layer.kind === 'sky' ? '<span lang="ko">하늘 마스크</span><span lang="en">SKY MASK</span>' : '<span lang="ko">브러시 마스크</span><span lang="en">BRUSH MASK</span>';
        $('#mask-size-row').hidden = layer.kind !== 'brush';
      }
      const painting = layer && layer.kind === 'brush';
      stage.classList.toggle('pick', !!painting); hint.hidden = !painting;
      draw();
    }
    function list() {
      layersBox.innerHTML = layers.map((l, i) => `<button type="button" data-i="${i}">${i + 1}. ${l.kind === 'sky' ? '<span lang="ko">하늘</span><span lang="en">Sky</span>' : '<span lang="ko">브러시</span><span lang="en">Brush</span>'}</button>`).join('');
      $$('button', layersBox).forEach(b => b.addEventListener('click', () => select(layers[+b.dataset.i])));
    }
    function add(kind) {
      let layer = layers.find(l => l.kind === kind);
      if (!layer) {
        layer = { kind, mask: kind === 'sky' ? sky : new Float32Array(n), edit: { ...blank, exposure: kind === 'brush' ? .5 : 0 }, version: 0 };
        layers.push(layer); list();
      }
      select(layer); check();
    }
    $('#mask-sky').addEventListener('click', () => add('sky'));
    $('#mask-brush').addEventListener('click', () => add('brush'));
    function setOverlay(on) { overlay = on; overlayButton.setAttribute('aria-pressed', String(on)); if (!on && layers.length) mark('masks.overlay'); draw(); }
    overlayButton.addEventListener('click', () => setOverlay(!overlay));
    let painting = false;
    function stamp(e) {          // a soft round brush (Grainy's default feather is broad)
      const [px, py] = canvasPoint(canvas, e), r = size.get() / 100 * w, cx = px * w, cy = py * h;
      const m = selected.mask;
      for (let y = Math.max(0, Math.floor(cy - r)); y <= Math.min(h - 1, Math.ceil(cy + r)); y++) for (let x = Math.max(0, Math.floor(cx - r)); x <= Math.min(w - 1, Math.ceil(cx + r)); x++) {
        const d = Math.hypot(x - cx, y - cy) / r; if (d >= 1) continue;
        const v = d < .3 ? 1 : 1 - (d - .3) / .7, i = y * w + x, s = v * v * (3 - 2 * v);
        if (s > m[i]) m[i] = s;
      }
      selected.version++; draw();
    }
    canvas.addEventListener('pointerdown', e => { if (!selected || selected.kind !== 'brush') return; painting = true; canvas.setPointerCapture(e.pointerId); stamp(e); });
    canvas.addEventListener('pointermove', e => { if (painting) stamp(e); });
    canvas.addEventListener('pointerup', () => { if (painting) { painting = false; check(); } });
    document.addEventListener('keydown', e => {
      if (current !== 'masks' || typing() || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key.toLowerCase() === 'o') { setOverlay(!overlay); e.preventDefault(); }
      if (e.key.toLowerCase() === 'k') { add('brush'); e.preventDefault(); }
    });
    draw();
  });
}

/* ---------- lesson 6: keys ---------- */
const KEYS = [
  ['\\', '\\', '보정 전후 바꿔 보기', 'Before / after'],
  ['c', 'C', '나란히 비교', 'Compare side by side'],
  ['j', 'J', '클리핑 표시 켜기/끄기', 'Show / hide clipping'],
  ['z', 'Z', '1:1과 화면 맞춤 번갈아 보기', 'Switch between 1:1 and fit'],
  ['f', 'F', '전체 화면 (Esc로 나옴)', 'Full screen (Esc leaves)'],
  ['Tab', 'Tab', '양옆 패널 숨기기/보이기 (Shift+Tab: 전부)', 'Hide / show the side panels (Shift+Tab: all)'],
  ['l', 'L', '조명 끄기: 사진만 보기', 'Lights out: the photo only'],
  ['w', 'W', '화이트 밸런스 스포이드', 'White balance eyedropper'],
  ['k', 'K', '새 브러시 마스크', 'New brush mask'],
  ['m', 'M', '새 선형 그레이디언트', 'New linear gradient'],
  ['M', 'Shift+M', '새 방사형 그레이디언트', 'New radial gradient'],
  ['q', 'Q', '복구(얼룩 지우기)', 'Healing'],
  ['r', 'R', '크롭', 'Crop'],
  ['o', 'O', '마스크 표시 켜기/끄기', 'Show / hide the mask'],
  ['g', 'G', '라이브러리로', 'Library'],
  ['d', 'D', '현상으로', 'Develop'],
  ['p', 'P', '선택 표시', 'Pick'],
  ['x', 'X', '제외 표시', 'Reject'],
  ['5', '0–5', '별점', 'Rating'],
  ['6', '6–9', '색 라벨 (빨강·노랑·초록·파랑)', 'Color label (red, yellow, green, blue)'],
  ['[', '[ ]', '브러시 크기', 'Brush size'],
  ['?', '?', '단축키 목록', 'Shortcut list'],
];
function startKeys() {
  const canvas = $('#keys-canvas'), stage = $('#keys-stage'), list = $('#keys-list'), toast = $('#keys-toast'), mode = $('#keys-mode');
  const hit = new Set(); let before = false, compare = false, timer = 0;
  Promise.all([pixels('img/garden.jpg'), pixels('img/film-kodak-portra-400.jpg')]).then(([plain, edited]) => {
    const draw = () => {
      $$('canvas', stage).forEach(c => c !== canvas && c.remove());
      $('.split', stage)?.remove();
      if (compare) {
        canvas.style.display = 'none';
        const split = document.createElement('div'); split.className = 'split';
        for (const img of [plain, edited]) { const c = document.createElement('canvas'); frame(c, img.width, img.height).putImageData(img, 0, 0); split.appendChild(c); }
        stage.appendChild(split);
      } else {
        canvas.style.display = '';
        frame(canvas, plain.width, plain.height).putImageData(before ? plain : edited, 0, 0);
      }
      mode.innerHTML = compare ? '<span lang="ko">비교 · 왼쪽 보정 전</span><span lang="en">Compare · left is before</span>' : before ? '<span lang="ko">보정 전</span><span lang="en">Before</span>' : '<span lang="ko">현상</span><span lang="en">Develop</span>';
    };
    function press(key) {
      const k = KEYS.find(x => x[0] === key || (key >= '0' && key <= '5' && x[0] === '5') || (key >= '6' && key <= '9' && x[0] === '6') || (key === ']' && x[0] === '['));
      if (!k) return;
      if (key === '\\') { before = !before; compare = false; mark('keys.before'); draw(); }
      if (key === 'c') { compare = !compare; before = false; mark('keys.compare'); draw(); }
      if (key === '?') { mark('keys.help'); showList(); }
      hit.add(k[0]); if (hit.size >= 5) mark('keys.five');
      $$('.key', list).forEach(b => { if (b.dataset.key === k[0]) { b.classList.add('hit', 'flash'); setTimeout(() => b.classList.remove('flash'), 350); } });
      toast.innerHTML = `<kbd>${k[1]}</kbd> <span lang="ko">${k[2]}</span><span lang="en">${k[3]}</span>`; toast.classList.add('show');
      clearTimeout(timer); timer = setTimeout(() => toast.classList.remove('show'), 1600);
    }
    function showList() {
      const box = document.createElement('div');
      box.style.cssText = 'position:absolute;inset:12px;background:rgba(21,24,29,.97);border:1px solid #907b60;padding:14px 16px;overflow:auto;font-size:13.5px;z-index:2';
      box.innerHTML = `<b style="color:#f4e7d4"><span lang="ko">단축키 목록</span><span lang="en">Shortcuts</span></b> <button type="button" class="tool" style="float:right">✕</button>` +
        `<div style="display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin-top:10px">${KEYS.map(k => `<kbd>${k[1]}</kbd><span><span lang="ko">${k[2]}</span><span lang="en">${k[3]}</span></span>`).join('')}</div>`;
      $('button', box).addEventListener('click', () => box.remove());
      stage.appendChild(box);
    }
    list.innerHTML = KEYS.map(k => `<button type="button" class="key" data-key="${k[0]}"><kbd>${k[1]}</kbd><span><span lang="ko">${k[2]}</span><span lang="en">${k[3]}</span></span></button>`).join('');
    $$('.key', list).forEach(b => b.addEventListener('click', () => press(b.dataset.key)));
    document.addEventListener('keydown', e => {
      if (current !== 'keys' || typing() || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'Tab') { press('Tab'); return; }                  // the page keeps Tab for moving around
      const key = e.key === 'M' || (e.key.toLowerCase() === 'm' && e.shiftKey) ? 'M' : e.key.length === 1 ? (e.key === '?' ? '?' : e.key.toLowerCase()) : null;
      if (!key) return;
      if (KEYS.some(x => x[0] === key) || (key >= '0' && key <= '9') || key === ']') { press(key); e.preventDefault(); }
    });
    draw();
  });
}

/* ---------- lesson 7: library ---------- */
function startLibrary() {
  const grid = $('#lib-grid'), filters = $('#lib-filters'), count = $('#lib-count');
  dataReady.then(() => {
    const photos = ['img/garden.jpg', ...['kodak-portra-400', 'fuji-velvia-50', 'kodak-kodachrome-64', 'kodak-tri-x-400', 'cinestill-800t'].map(id => `img/film-${id}.jpg`)]
      .map((src, i) => ({ src, name: `roll-01_${String(i + 1).padStart(2, '0')}.jpg`, stars: 0, flag: 0 }));
    let selected = 0, filter = 'all';
    const visible = p => filter === 'all' || (filter === 'picked' && p.flag === 1) || (filter === 'four' && p.stars >= 4) || (filter === 'rejected' && p.flag === -1);
    function render() {
      grid.innerHTML = photos.map((p, i) => `<div class="tile${i === selected ? ' sel' : ''}${p.flag === -1 ? ' rejected' : ''}${visible(p) ? '' : ' hidden'}" data-i="${i}"><img src="${p.src}" alt="" loading="lazy"><div class="meta"><span class="stars">${'★'.repeat(p.stars)}</span><span class="flag ${p.flag === 1 ? 'pick' : p.flag === -1 ? 'reject' : ''}">${p.flag === 1 ? '⚑' : p.flag === -1 ? '✕' : ''}</span></div></div>`).join('');
      $$('.tile', grid).forEach(tile => tile.addEventListener('click', () => { selected = +tile.dataset.i; grid.focus(); render(); }));
      const shown = photos.filter(visible).length;
      count.innerHTML = `<span lang="ko">${shown}장</span><span lang="en">${shown} photos</span>`;
      if (photos.some(p => p.stars === 5)) mark('library.five');
      if (photos.filter(p => p.flag === 1).length >= 2) mark('library.pick');
      if (photos.some(p => p.flag === -1)) mark('library.reject');
    }
    $$('button', filters).forEach(b => b.addEventListener('click', () => {
      filter = b.dataset.filter; $$('button', filters).forEach(x => x.setAttribute('aria-pressed', String(x === b)));
      if (filter === 'picked' && photos.some(p => p.flag === 1)) mark('library.filter');
      render();
    }));
    document.addEventListener('keydown', e => {
      if (current !== 'library' || typing() || e.ctrlKey || e.metaKey || e.altKey) return;
      const p = photos[selected], key = e.key.toLowerCase();
      if (key >= '0' && key <= '5') p.stars = +key;
      else if (key === 'p') p.flag = 1;
      else if (key === 'x') p.flag = -1;
      else if (key === 'u') p.flag = 0;
      else if (e.key === 'ArrowRight') selected = Math.min(photos.length - 1, selected + 1);
      else if (e.key === 'ArrowLeft') selected = Math.max(0, selected - 1);
      else return;
      e.preventDefault(); render();
    });
    render();
  });
}

/* ---------- lesson 8 ---------- */
function startAi() { ['ai.read', 'ai.read2', 'ai.read3'].forEach(mark); }

const MODULES = { tour: startTour, negative: startNegative, basic: startBasic, film: startFilm, masks: startMasks, keys: startKeys, library: startLibrary, ai: startAi };
refreshProgress();
show(location.hash.slice(1) || 'tour', false);
})();
