'use strict';
/* =========================================================================
 * cloudlogs — vanilla JS log viewer
 *
 *   0  constants & tiny helpers
 *   1  state
 *   2  storage        (localStorage['cloudlogs.layout'])
 *   3  url state      (filters / sort / q in the location hash)
 *   4  api
 *   5  time rendering
 *   6  column helpers
 *   7  data flow      (fetch / paging / stale-guard)
 *   8  render: table
 *   9  render: filter panel
 *  10  popovers       (add filter, columns, timezone, context menu)
 *  11  drag: reorder + resize
 *  12  drawer
 *  13  context menu actions
 *  14  init
 * ========================================================================= */


/* ── 0. constants & tiny helpers ─────────────────────────────────────── */

var PAGE_SIZE   = 200;
var SCROLL_PAD  = 300;                 // px from bottom that triggers the next page
var DEBOUNCE_MS = 250;
var LS_KEY      = 'cloudlogs.layout';

var DEFAULT_PANEL   = ['level', 'logger', 'service', 'k8s_namespace', 'req_status_code'];
var DEFAULT_VISIBLE = ['time', 'level', 'service', 'k8s_namespace', 'logger', 'req_status_code', 'message'];

var DEFAULT_WIDTHS = {
  time: 188, app_time: 150, level: 74, service: 106, logger: 168,
  method: 120, thread: 170, src_line: 62,
  k8s_namespace: 190, k8s_pod: 200, k8s_container: 170, k8s_cluster: 160,
  req_status_code: 84, req_duration_ms: 92, req_path: 240,
  node_name: 180, x_trace_id: 230, source_file: 160
};
var WIDTH_DEFAULT = 150;
var WIDTH_MIN     = 48;

/* side panels: defaults match the CSS variables in style.css */
var PANEL_W_DEFAULT  = 288;
var PANEL_W_MIN      = 170;
var PANEL_W_MAX      = 720;
var DRAWER_W_DEFAULT = 460;
var DRAWER_W_MIN     = 260;
var DRAWER_W_MAX     = 980;

var SUP = ['', '¹', '²', '³', '⁴', '⁵', '⁶', '⁷', '⁸', '⁹'];

/* used only when Intl.supportedValuesOf('timeZone') is unavailable */
var FALLBACK_TZS = [
  'UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris', 'Europe/Madrid',
  'Europe/Rome', 'Europe/Warsaw', 'Europe/Bucharest', 'Europe/Moscow',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Sao_Paulo', 'Africa/Cairo', 'Africa/Johannesburg',
  'Asia/Jerusalem', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Bangkok',
  'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Seoul', 'Australia/Sydney', 'Pacific/Auckland'
];

function $(id) { return document.getElementById(id); }

function el(tag, cls, txt) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined && txt !== null) n.textContent = String(txt);
  return n;
}

function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

function debounce(fn, ms) {
  var t = null;
  return function () {
    var args = arguments, self = this;
    if (t) clearTimeout(t);
    t = setTimeout(function () { t = null; fn.apply(self, args); }, ms);
  };
}

function isBlank(v) { return v === null || v === undefined || v === ''; }


/* ── 1. state ────────────────────────────────────────────────────────── */

var state = {
  columns: [],            // [{name, kind, label, distinct, numeric, default_visible, default_filter}]
  colMap: {},

  filters: {},            // col -> {kind, ...}   (only non-empty ones are sent)
  panel: [],              // ordered card names — independent of table visibility
  cardCollapsed: {},      // col -> true
  facetSearch: {},        // col -> value-search text
  highlights: {},         // col -> [values]  shift-clicked facet values (see section 7b)

  q: '',
  query: '',              // the executed Lucene query (state.lq holds the draft)
  lq: '',                 // what is typed in the query box, run or not
  sort: [],               // [{col, dir}]

  rows: [],
  total: 0,
  grandTotal: null,
  facets: {},

  loading: false,
  queued: false,
  done: false,
  gen: 0,                 // bumped on every filter/sort change; stale pages are dropped

  selectedRow: null,
  lastHash: null,

  layout: {
    order: [],            // full column order (hidden ones included)
    widths: {},           // col -> px
    hidden: {},           // col -> true
    tz: 'Local',
    panelCollapsed: false,
    panelW: PANEL_W_DEFAULT,      // filter panel width, px
    drawerW: DRAWER_W_DEFAULT,    // detail drawer width, px
    lqH: 0                        // query box height, px (0 = the CSS default)
  }
};


/* ── 2. storage ──────────────────────────────────────────────────────── */

/* both side panels are sized by a CSS variable, so resizing them is just
   writing that variable — and clamping keeps a drag from swallowing the table */
function clampPanelW(v) {
  var max = Math.max(PANEL_W_MIN, Math.min(PANEL_W_MAX, window.innerWidth - 320));
  return Math.round(Math.max(PANEL_W_MIN, Math.min(max, v)));
}

function clampDrawerW(v) {
  var max = Math.max(DRAWER_W_MIN, Math.min(DRAWER_W_MAX, window.innerWidth - 360));
  return Math.round(Math.max(DRAWER_W_MIN, Math.min(max, v)));
}

function applyPanelWidths() {
  var root = document.documentElement;
  root.style.setProperty('--panel-w', clampPanelW(state.layout.panelW) + 'px');
  root.style.setProperty('--drawer-w', clampDrawerW(state.layout.drawerW) + 'px');
}

function loadLayout() {
  var raw = null;
  try { raw = window.localStorage.getItem(LS_KEY); } catch (e) { return; }
  if (!raw) return;
  var obj = null;
  try { obj = JSON.parse(raw); } catch (e) { return; }
  if (!obj || typeof obj !== 'object') return;

  var L = state.layout;
  if (Array.isArray(obj.order)) L.order = obj.order.filter(function (n) { return typeof n === 'string'; });
  if (obj.widths && typeof obj.widths === 'object') {
    Object.keys(obj.widths).forEach(function (k) {
      var w = Number(obj.widths[k]);
      if (isFinite(w) && w >= WIDTH_MIN) L.widths[k] = Math.round(w);
    });
  }
  if (obj.hidden && typeof obj.hidden === 'object' && !Array.isArray(obj.hidden)) {
    Object.keys(obj.hidden).forEach(function (k) { if (obj.hidden[k]) L.hidden[k] = true; });
  } else if (Array.isArray(obj.hidden)) {
    obj.hidden.forEach(function (k) { L.hidden[k] = true; });
  }
  if (typeof obj.tz === 'string' && obj.tz) L.tz = obj.tz;
  L.panelCollapsed = !!obj.panelCollapsed;
  if (isFinite(Number(obj.panelW))) L.panelW = clampPanelW(Number(obj.panelW));
  if (isFinite(Number(obj.drawerW))) L.drawerW = clampDrawerW(Number(obj.drawerW));
  if (isFinite(Number(obj.lqH)) && Number(obj.lqH) > 0) L.lqH = Math.round(Number(obj.lqH));
}

function saveLayout() {
  try { window.localStorage.setItem(LS_KEY, JSON.stringify(state.layout)); } catch (e) { /* quota / disabled */ }
}

function resetLayout() {
  try { window.localStorage.removeItem(LS_KEY); } catch (e) { /* ignore */ }
  state.layout = {
    order: [], widths: {}, hidden: {}, tz: 'Local', panelCollapsed: false,
    panelW: PANEL_W_DEFAULT, drawerW: DRAWER_W_DEFAULT, lqH: 0
  };
  applyDefaultLayout();
  applyPanelCollapsed();
  applyPanelWidths();
  renderHeader();
  renderRows(0);
  renderTzButton();
}


/* ── 3. url state ────────────────────────────────────────────────────────
 * #f=level:WARN,ERROR;k8s_namespace:pu-…-ram&q=404&sort=-time
 *
 * facet   col:v1,v2          (values percent-encoded)
 * number  col:#min..max      \
 * time    col:@from..to       > extensions of the documented facet form;
 * text    col:/value          / '~' instead of '/' means regex: true
 *
 * Highlights ride along in their own key, same facet form:
 * h=level:WARN,ERROR;service:ram
 * -------------------------------------------------------------------- */

function encodeFilterValue(v) { return encodeURIComponent(String(v)); }

function encodeHash() {
  var parts = [];
  var fs = [];
  Object.keys(state.filters).forEach(function (col) {
    var f = state.filters[col];
    if (!filterActive(f)) return;
    var key = encodeURIComponent(col);
    if (f.kind === 'facet') {
      fs.push(key + ':' + f.values.map(encodeFilterValue).join(','));
    } else if (f.kind === 'number') {
      fs.push(key + ':#' + (isBlank(f.min) ? '' : encodeFilterValue(f.min)) + '..' + (isBlank(f.max) ? '' : encodeFilterValue(f.max)));
    } else if (f.kind === 'time') {
      fs.push(key + ':@' + (isBlank(f.from) ? '' : encodeFilterValue(f.from)) + '..' + (isBlank(f.to) ? '' : encodeFilterValue(f.to)));
    } else {
      fs.push(key + ':' + (f.regex ? '~' : '/') + encodeFilterValue(f.value));
    }
  });
  if (fs.length) parts.push('f=' + fs.join(';'));

  var hs = [];
  Object.keys(state.highlights).forEach(function (col) {
    var vals = state.highlights[col];
    if (!vals || !vals.length) return;
    hs.push(encodeURIComponent(col) + ':' + vals.map(encodeFilterValue).join(','));
  });
  if (hs.length) parts.push('h=' + hs.join(';'));
  if (state.q) parts.push('q=' + encodeURIComponent(state.q));
  if (state.query) parts.push('lq=' + encodeURIComponent(state.query));
  if (state.sort.length) {
    parts.push('sort=' + state.sort.map(function (s) {
      return (s.dir === 'desc' ? '-' : '') + encodeURIComponent(s.col);
    }).join(','));
  }
  return parts.length ? '#' + parts.join('&') : '';
}

function writeHash() {
  var h = encodeHash();
  state.lastHash = h;
  if (h) {
    if (window.location.hash !== h) window.location.hash = h;
  } else if (window.location.hash) {
    // remove the hash without adding an empty entry that breaks back/forward
    window.location.hash = '';
  }
}

function decodeHash() {
  var out = { filters: {}, q: '', query: '', sort: [], highlights: {} };
  var h = window.location.hash || '';
  if (h.charAt(0) === '#') h = h.slice(1);
  if (!h) return out;

  h.split('&').forEach(function (chunk) {
    var eq = chunk.indexOf('=');
    if (eq < 0) return;
    var k = chunk.slice(0, eq), v = chunk.slice(eq + 1);

    if (k === 'q') {
      try { out.q = decodeURIComponent(v); } catch (e) { out.q = v; }

    } else if (k === 'lq') {
      try { out.query = decodeURIComponent(v); } catch (e) { out.query = v; }

    } else if (k === 'sort') {
      v.split(',').forEach(function (s) {
        if (!s) return;
        var dir = 'asc';
        if (s.charAt(0) === '-') { dir = 'desc'; s = s.slice(1); }
        else if (s.charAt(0) === '+') { s = s.slice(1); }
        var col;
        try { col = decodeURIComponent(s); } catch (e) { col = s; }
        if (col) out.sort.push({ col: col, dir: dir });
      });

    } else if (k === 'h') {
      v.split(';').forEach(function (part) {
        if (!part) return;
        var c = part.indexOf(':');
        if (c < 0) return;
        var col, vals;
        try { col = decodeURIComponent(part.slice(0, c)); } catch (e) { col = part.slice(0, c); }
        vals = part.slice(c + 1).split(',').filter(function (x) { return x !== ''; }).map(function (x) {
          try { return decodeURIComponent(x); } catch (e) { return x; }
        });
        if (col && vals.length) out.highlights[col] = vals;
      });

    } else if (k === 'f') {
      v.split(';').forEach(function (part) {
        if (!part) return;
        var c = part.indexOf(':');
        if (c < 0) return;
        var col;
        try { col = decodeURIComponent(part.slice(0, c)); } catch (e) { col = part.slice(0, c); }
        var rest = part.slice(c + 1);
        var lead = rest.charAt(0);
        var dec = function (s) { try { return decodeURIComponent(s); } catch (e) { return s; } };

        if (lead === '#' || lead === '@') {
          var body = rest.slice(1);
          var sep = body.indexOf('..');
          var a = sep < 0 ? body : body.slice(0, sep);
          var b = sep < 0 ? '' : body.slice(sep + 2);
          if (lead === '#') {
            out.filters[col] = {
              kind: 'number',
              min: a === '' ? null : Number(dec(a)),
              max: b === '' ? null : Number(dec(b))
            };
          } else {
            out.filters[col] = { kind: 'time', from: a === '' ? null : dec(a), to: b === '' ? null : dec(b) };
          }
        } else if (lead === '/' || lead === '~') {
          out.filters[col] = { kind: 'text', value: dec(rest.slice(1)), regex: lead === '~' };
        } else {
          out.filters[col] = {
            kind: 'facet',
            values: rest.split(',').filter(function (x) { return x !== ''; }).map(dec)
          };
        }
      });
    }
  });
  return out;
}

function applyHashState() {
  var s = decodeHash();
  state.filters = s.filters;
  state.q = s.q;
  state.sort = s.sort;
  state.highlights = s.highlights;
  state.query = s.query;
  state.lq = s.query;
  var lqbox = $('lq');
  if (lqbox && lqbox.value !== state.lq) lqbox.value = state.lq;
  markQueryDirty();
  var qbox = $('q');
  if (qbox && qbox.value !== state.q) qbox.value = state.q;
  // every filtered or highlighted column gets a card so the state is visible
  Object.keys(state.filters).forEach(function (c) { ensureCard(c, true); });
  Object.keys(state.highlights).forEach(function (c) { ensureCard(c, true); });
}


/* ── 4. api ──────────────────────────────────────────────────────────── */

function showError(msg) {
  var e = $('error');
  e.textContent = msg;
  e.hidden = false;
}
function clearError() { $('error').hidden = true; }

function setBusy(on) { $('spinner').hidden = !on; }

async function api(path, opts) {
  var res = await fetch(path, opts);
  if (!res.ok) {
    var body = '';
    try { body = (await res.text()).slice(0, 400); } catch (e) { /* ignore */ }
    throw new Error(path + ' → ' + res.status + ' ' + res.statusText + (body ? '\n' + body : ''));
  }
  return await res.json();
}

function postLogs(body) {
  return api('/api/logs', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body)
  });
}


/* ── 5. time rendering ───────────────────────────────────────────────── */

var TS_RE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?\s*(Z|z|[+-]\d{2}:?\d{2})?$/;
var fmtCache = {};

/* Returns {date, frac3, nanos} or null. A timestamp without an explicit
   offset is read as UTC (app_time in the sample data is UTC). */
function parseTs(s) {
  if (typeof s !== 'string' || !s) return null;
  var m = TS_RE.exec(s.trim());
  if (m) {
    var frac = (m[7] || '');
    var nanos = (frac + '000000000').slice(0, 9);
    var ms = nanos.slice(0, 3);
    var off = m[8] || 'Z';
    if (off === 'z') off = 'Z';
    var iso = m[1] + '-' + m[2] + '-' + m[3] + 'T' + m[4] + ':' + m[5] + ':' + m[6] + '.' + ms + off;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return { date: d, frac3: ms, nanos: frac };
  }
  var d2 = new Date(s);
  if (isNaN(d2.getTime())) return null;
  return { date: d2, frac3: ('00' + d2.getUTCMilliseconds()).slice(-3), nanos: '' };
}

function getFormatter(tz) {
  var key = tz || 'Local';
  if (fmtCache[key]) return fmtCache[key];
  var opts = {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23'
  };
  if (key !== 'Local') opts.timeZone = key;
  var f;
  try {
    f = new Intl.DateTimeFormat('en-GB', opts);
  } catch (e) {
    delete opts.timeZone;
    try { f = new Intl.DateTimeFormat('en-GB', opts); } catch (e2) { f = null; }
  }
  fmtCache[key] = f;
  return f;
}

function fmtTime(raw, tz) {
  var p = parseTs(raw);
  if (!p) return raw == null ? '' : String(raw);
  var f = getFormatter(tz === undefined ? state.layout.tz : tz);
  if (!f) return p.date.toISOString().replace('T', ' ').replace('Z', '');
  var got = {};
  f.formatToParts(p.date).forEach(function (part) { got[part.type] = part.value; });
  if (!got.year) return p.date.toISOString().replace('T', ' ').replace('Z', '');
  var hh = got.hour === '24' ? '00' : got.hour;
  return got.year + '-' + got.month + '-' + got.day + ' ' + hh + ':' + got.minute + ':' + got.second + '.' + p.frac3;
}

function timezoneList() {
  var list = [];
  try {
    if (typeof Intl.supportedValuesOf === 'function') list = Intl.supportedValuesOf('timeZone') || [];
  } catch (e) { list = []; }
  if (!list.length) list = FALLBACK_TZS.slice();
  var out = ['Local', 'UTC'];
  list.forEach(function (z) { if (z !== 'UTC') out.push(z); });
  return out;
}


/* ── 6. column helpers ───────────────────────────────────────────────── */

function normalizeColumns(payload) {
  var arr = Array.isArray(payload) ? payload
    : (payload && Array.isArray(payload.columns)) ? payload.columns
    : (payload && Array.isArray(payload.cols)) ? payload.cols
    : [];
  var out = [];
  arr.forEach(function (c) {
    if (!c || !c.name || c.name === '_raw') return;
    out.push({
      name: c.name,
      kind: c.kind || (c.numeric ? 'number' : 'text'),
      label: c.label || c.name,
      distinct: c.distinct,
      numeric: !!c.numeric,
      default_visible: !!c.default_visible,
      default_filter: !!c.default_filter
    });
  });
  return out;
}

function applyDefaultLayout() {
  var L = state.layout;
  var names = state.columns.map(function (c) { return c.name; });

  // keep the persisted order, drop unknown names, append new columns
  var order = L.order.filter(function (n) { return names.indexOf(n) >= 0; });
  names.forEach(function (n) { if (order.indexOf(n) < 0) order.push(n); });
  if (!L.order.length) {
    // first run: default-visible columns first, in the documented order
    var pref = [];
    DEFAULT_VISIBLE.forEach(function (n) { if (names.indexOf(n) >= 0) pref.push(n); });
    state.columns.forEach(function (c) {
      if (c.default_visible && pref.indexOf(c.name) < 0) pref.push(c.name);
    });
    order = pref.concat(order.filter(function (n) { return pref.indexOf(n) < 0; }));

    var anyDefault = state.columns.some(function (c) { return c.default_visible; });
    state.columns.forEach(function (c) {
      var vis = anyDefault ? c.default_visible : DEFAULT_VISIBLE.indexOf(c.name) >= 0;
      if (!vis) L.hidden[c.name] = true;
    });
  }
  L.order = order;
}

function orderedColumns() {
  var out = [];
  state.layout.order.forEach(function (n) { if (state.colMap[n]) out.push(state.colMap[n]); });
  state.columns.forEach(function (c) { if (out.indexOf(c) < 0) out.push(c); });
  return out;
}

function visibleColumns() {
  return orderedColumns().filter(function (c) { return !state.layout.hidden[c.name]; });
}

/* null → auto width (message soaks up the remaining space) */
function colWidth(name) {
  var w = state.layout.widths[name];
  if (w) return w;
  if (name === 'message') return null;
  return DEFAULT_WIDTHS[name] || WIDTH_DEFAULT;
}


/* ── 7. data flow ────────────────────────────────────────────────────── */

function filterActive(f) {
  if (!f) return false;
  if (f.kind === 'facet') return Array.isArray(f.values) && f.values.length > 0;
  if (f.kind === 'number') return !isBlank(f.min) || !isBlank(f.max);
  if (f.kind === 'time') return !isBlank(f.from) || !isBlank(f.to);
  return !isBlank(f.value);
}

function activeFilters() {
  var out = {};
  Object.keys(state.filters).forEach(function (col) {
    var f = state.filters[col];
    if (!filterActive(f)) return;
    if (f.kind === 'facet') out[col] = { kind: 'facet', values: f.values.slice() };
    else if (f.kind === 'number') out[col] = { kind: 'number', min: isBlank(f.min) ? null : Number(f.min), max: isBlank(f.max) ? null : Number(f.max) };
    else if (f.kind === 'time') out[col] = { kind: 'time', from: isBlank(f.from) ? null : f.from, to: isBlank(f.to) ? null : f.to };
    else out[col] = { kind: 'text', value: f.value, regex: !!f.regex };
  });
  return out;
}

/* ── 7b. highlights ──────────────────────────────────────────────────────
   Shift-clicking a facet checkbox highlights that value instead of filtering
   on it: no row leaves the table, the ones that do not match are dimmed, and
   the matches keep their normal colours so they stand out in context.

   Highlights are OR-ed everywhere — across values AND across columns —
   because highlighting is additive attention, not narrowing. (Filters are the
   opposite: OR within a column, AND across them.)

   They are pure client state. The row set never changes, so a highlight never
   refetches; it only re-paints the rows already loaded.
   -------------------------------------------------------------------- */

function highlightsActive() {
  var cols = Object.keys(state.highlights);
  for (var i = 0; i < cols.length; i++) {
    var v = state.highlights[cols[i]];
    if (v && v.length) return true;
  }
  return false;
}

function highlightValues(col) { return state.highlights[col] || []; }

function isHighlighted(col, value) {
  return highlightValues(col).some(function (v) { return String(v) === String(value); });
}

function highlightValueCount() {
  return Object.keys(state.highlights).reduce(function (n, c) {
    return n + (state.highlights[c] || []).length;
  }, 0);
}

function rowHighlighted(row) {
  if (!row) return false;
  var cols = Object.keys(state.highlights);
  for (var i = 0; i < cols.length; i++) {
    var vals = state.highlights[cols[i]];
    if (!vals || !vals.length) continue;
    var cell = row[cols[i]];
    if (isBlank(cell)) continue;
    for (var j = 0; j < vals.length; j++) {
      if (String(vals[j]) === String(cell)) return true;
    }
  }
  return false;
}

function highlightedLoadedCount() {
  if (!highlightsActive()) return 0;
  return state.rows.reduce(function (n, r) { return n + (rowHighlighted(r) ? 1 : 0); }, 0);
}

function toggleHighlight(col, value) {
  var vals = highlightValues(col).slice();
  var i = vals.findIndex(function (v) { return String(v) === String(value); });
  if (i >= 0) vals.splice(i, 1); else vals.push(value);
  if (vals.length) state.highlights[col] = vals; else delete state.highlights[col];
  ensureCard(col, true);
  commitHighlights();
}

function clearColumnHighlight(col) {
  if (!state.highlights[col]) return;
  delete state.highlights[col];
  commitHighlights();
}

function clearHighlights() {
  if (!highlightsActive()) return;
  state.highlights = {};
  commitHighlights();
}

/* No refetch: re-paint the loaded rows in place, which also keeps the scroll
   position — losing it would defeat the point of highlighting in context. */
function commitHighlights() {
  renderPanel();
  writeHash();
  applyHighlightClasses();
  renderCount();
  renderHighlightBar();
}

function highlightClass(row) {
  if (!highlightsActive()) return '';
  return rowHighlighted(row) ? ' hl' : ' dim';
}

function applyHighlightClasses() {
  var on = highlightsActive();
  document.body.classList.toggle('hl-on', on);
  var trs = $('tbody').querySelectorAll('tr');
  for (var i = 0; i < trs.length; i++) {
    var tr = trs[i];
    var row = state.rows[Number(tr.dataset.i)];
    tr.classList.toggle('hl', on && rowHighlighted(row));
    tr.classList.toggle('dim', on && !rowHighlighted(row));
  }
}

/* the strip under the toolbar that says, in words, that this is a highlight
   and not a filter — the two are easy to confuse once rows start dimming */
function renderHighlightBar() {
  var bar = $('hlbar');
  if (!bar) return;
  clear(bar);
  if (!highlightsActive()) { bar.hidden = true; return; }
  bar.hidden = false;

  bar.appendChild(el('span', 'hl-lead', '◆ Highlighting'));
  bar.appendChild(el('span', 'hl-note', 'nothing is filtered out — non-matching rows are dimmed'));

  Object.keys(state.highlights).forEach(function (col) {
    (state.highlights[col] || []).forEach(function (v) {
      var chip = el('span', 'hl-chip');
      chip.appendChild(el('span', 'hl-chip-col', (state.colMap[col] && state.colMap[col].label) || col));
      chip.appendChild(el('span', 'hl-chip-val', String(v)));
      var x = el('span', 'hl-chip-x', '×');
      x.title = 'Remove this highlight';
      x.addEventListener('click', function () { toggleHighlight(col, v); });
      chip.appendChild(x);
      bar.appendChild(chip);
    });
  });

  var clr = el('button', 'btn btn-quiet hl-clear', 'Clear highlights');
  clr.addEventListener('click', clearHighlights);
  bar.appendChild(clr);
}


/* full refetch — bumps the generation so in-flight pages are discarded */
function refresh() {
  state.gen++;
  state.done = false;
  if (state.loading) { state.queued = true; return; }
  $('tablewrap').scrollTop = 0;
  fetchPage(0, false);
}

function loadMore() {
  if (state.loading || state.done || !state.rows.length) return;
  fetchPage(state.rows.length, true);
}

async function fetchPage(offset, append) {
  if (state.loading) return;                 // one request in flight at a time
  var gen = state.gen;
  state.loading = true;
  setBusy(true);
  renderFoot();

  try {
    var data = await postLogs({
      filters: activeFilters(),
      q: state.q,
      query: state.query,
      sort: state.sort,
      limit: PAGE_SIZE,
      offset: offset
    });

    if (gen !== state.gen) return;           // stale page — filters changed meanwhile

    clearError();
    var rows = Array.isArray(data.rows) ? data.rows : [];
    if (append) {
      var from = state.rows.length;
      state.rows = state.rows.concat(rows);
      renderRows(from);
    } else {
      state.rows = rows;
      renderRows(0);
    }
    state.total = (typeof data.total === 'number') ? data.total : state.rows.length;
    if (data.facets && typeof data.facets === 'object') state.facets = data.facets;
    clearQueryError();
    if (state.grandTotal === null && !Object.keys(activeFilters()).length && !state.q && !state.query) {
      state.grandTotal = state.total;
    }
    state.done = rows.length === 0 || state.rows.length >= state.total;
    renderPanel();
    renderCount();
  } catch (err) {
    if (gen === state.gen && !showQueryError(err)) {
      showError('Request failed: ' + (err && err.message ? err.message : String(err)));
    }
  } finally {
    state.loading = false;
    setBusy(false);
    renderFoot();
    if (state.queued) {
      state.queued = false;
      $('tablewrap').scrollTop = 0;
      fetchPage(0, false);
    } else if (gen === state.gen) {
      maybeLoadMore();                        // short first page in a tall window
    }
  }
}

function maybeLoadMore() {
  var w = $('tablewrap');
  if (!w) return;
  if (w.scrollHeight - w.scrollTop - w.clientHeight < SCROLL_PAD) loadMore();
}

function commitFilters() {
  renderPanel();          // immediate feedback; counts refresh when the data lands
  writeHash();
  refresh();
}


/* ── 7c. query bar ───────────────────────────────────────────────────────
   A Lucene-style expression, parsed and evaluated server-side (see
   cloudlogs/lucene.py). Unlike the panel filters it is not tied to one column,
   so it is the only place to express OR across columns, NOT, and grouping.

   It is ANDed with the panel filters and the quick-search box, so the two
   never fight: the query narrows, the panel narrows further.

   The box runs on Ctrl/Cmd+Enter or the Run button, never on every keystroke —
   a half-typed query is usually a syntax error, and flashing red at someone
   mid-word is worse than useless.
   -------------------------------------------------------------------- */

var QUERY_EXAMPLES = [
  ['level:WARN', 'a facet field matches the whole value'],
  ['message:timeout', 'text fields match a substring'],
  ['"connection refused"', 'quoted phrase, any column'],
  ['level:(WARN OR ERROR)', 'field-scoped group'],
  ['a AND b · a OR b · NOT a', 'also &&, ||, !, +a, -b'],
  ['req_duration_ms:[100 TO 500]', 'range — {} excludes the bound'],
  ['req_duration_ms:>=100', 'open-ended comparison'],
  ['time:2026-07-09', 'a partial timestamp means that whole period'],
  ['time:[2026-07-09T08:00:00Z TO *]', '* is unbounded'],
  ['k8s_pod:pu-epa-*-ram-*', '* and ? wildcards'],
  ['logger:/Get.*Service/', 'regular expression'],
  ['req_path:"/v3/records"', 'quote values that contain / or spaces']
];

function markQueryDirty() {
  var btn = $('lq-run');
  if (!btn) return;
  btn.classList.toggle('dirty', (state.lq || '') !== (state.query || ''));
}

function clearQueryError() {
  var box = $('lq-error');
  if (box) { box.hidden = true; clear(box); }
  var ta = $('lq');
  if (ta) ta.classList.remove('bad');
}

/* A 400 from /api/logs carrying {"detail":{"kind":"query"}} is the user's
   query, not a broken server — render it under the box with a caret at the
   offset the parser reported. Returns true when it handled the error. */
function showQueryError(err) {
  var msg = err && err.message ? err.message : '';
  var i = msg.indexOf('{');
  if (i < 0) return false;
  var detail = null;
  try { detail = JSON.parse(msg.slice(i)).detail; } catch (e) { return false; }
  if (!detail || detail.kind !== 'query') return false;

  var box = $('lq-error');
  var ta = $('lq');
  if (!box) return false;
  clear(box);
  ta.classList.add('bad');
  box.hidden = false;
  box.appendChild(el('span', null, 'query: ' + (detail.error || 'invalid query')));

  var pos = Number(detail.pos);
  if (isFinite(pos) && pos >= 0 && state.query) {
    var line = state.query.split('\n')[0];
    box.appendChild(document.createElement('br'));
    box.appendChild(el('span', 'caret', line.slice(0, 60)));
    box.appendChild(document.createElement('br'));
    box.appendChild(el('span', 'caret', new Array(Math.min(pos, 60) + 1).join(' ') + '^'));
    try { ta.focus(); ta.setSelectionRange(pos, pos); } catch (e) { /* ignore */ }
  }
  return true;
}

function runQuery() {
  var ta = $('lq');
  var text = ta ? ta.value : '';
  if (text === state.query) { markQueryDirty(); return; }
  state.lq = text;
  state.query = text;
  markQueryDirty();
  clearQueryError();
  commitFilters();
}

function clearQuery() {
  var ta = $('lq');
  if (ta) ta.value = '';
  state.lq = '';
  if (!state.query) { markQueryDirty(); return; }
  state.query = '';
  clearQueryError();
  markQueryDirty();
  commitFilters();
}

function applyQueryHeight() {
  var ta = $('lq');
  if (ta && state.layout.lqH > 0) ta.style.height = state.layout.lqH + 'px';
}

function openQueryHelp(anchor) {
  openPop({
    anchor: anchor,
    render: function (body) {
      var wrap = el('div', 'qhelp');
      wrap.appendChild(el('h4', null, 'syntax'));
      QUERY_EXAMPLES.forEach(function (pair) {
        var row = el('div', 'qh-row');
        var ex = el('div', 'qh-ex');
        var code = el('code', null, pair[0]);
        code.title = 'click to insert';
        code.style.cursor = 'pointer';
        code.addEventListener('click', function () { insertIntoQuery(pair[0]); });
        ex.appendChild(code);
        row.appendChild(ex);
        row.appendChild(el('div', 'qh-txt', pair[1]));
        wrap.appendChild(row);
      });

      wrap.appendChild(el('h4', null, 'not supported'));
      wrap.appendChild(el('div', 'qh-txt',
        '^boost and ~fuzzy — results are not scored or ranked, so they have nothing to act on.'));

      wrap.appendChild(el('h4', null, 'fields (click to insert)'));
      var fields = el('div');
      state.columns.forEach(function (c) {
        var chip = el('span', 'qh-field', c.name);
        chip.title = c.kind;
        chip.addEventListener('click', function () { insertIntoQuery(c.name + ':'); });
        fields.appendChild(chip);
      });
      wrap.appendChild(fields);
      body.appendChild(wrap);
    }
  });
}

function insertIntoQuery(text) {
  var ta = $('lq');
  if (!ta) return;
  var start = ta.selectionStart, end = ta.selectionEnd;
  if (typeof start !== 'number') { ta.value += (ta.value ? ' ' : '') + text; }
  else {
    var pad = (start > 0 && ta.value.charAt(start - 1) !== ' ' && ta.value.charAt(start - 1) !== '') ? ' ' : '';
    ta.value = ta.value.slice(0, start) + pad + text + ta.value.slice(end);
    var caret = start + pad.length + text.length;
    try { ta.focus(); ta.setSelectionRange(caret, caret); } catch (e) { /* ignore */ }
  }
  state.lq = ta.value;
  markQueryDirty();
}

function installQueryBar() {
  var ta = $('lq');
  if (!ta) return;
  applyQueryHeight();

  ta.addEventListener('input', function () { state.lq = ta.value; markQueryDirty(); });
  ta.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runQuery(); }
    else if (e.key === 'Escape') { ta.blur(); }
  });

  // the browser's own resize grip; remember where the user left it
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(debounce(function () {
      var h = Math.round(ta.getBoundingClientRect().height);
      if (h > 0 && h !== state.layout.lqH) { state.layout.lqH = h; saveLayout(); }
    }, 300));
    ro.observe(ta);
  }

  $('lq-run').addEventListener('click', runQuery);
  $('lq-clear').addEventListener('click', clearQuery);
  $('lq-help').addEventListener('click', function (e) { openQueryHelp(e.currentTarget); });
}


/* ── 8. render: table ────────────────────────────────────────────────── */

function renderCount() {
  var total = state.total;
  var grand = state.grandTotal === null ? total : state.grandTotal;
  var c = $('count');
  clear(c);
  c.appendChild(el('b', null, total.toLocaleString()));
  c.appendChild(document.createTextNode(' of ' + grand.toLocaleString() + ' matching'));
  if (highlightsActive()) {
    var hn = highlightedLoadedCount();
    var span = el('span', 'count-hl', '◆ ' + hn.toLocaleString() + ' highlighted');
    span.title = 'of the ' + state.rows.length.toLocaleString() + ' rows loaded so far';
    c.appendChild(span);
  }
}

function renderFoot() {
  var f = $('tablefoot');
  if (state.loading) { f.textContent = 'loading…'; return; }
  if (!state.rows.length) { f.textContent = 'no matching rows'; return; }
  f.textContent = state.done
    ? 'end of results — ' + state.rows.length.toLocaleString() + ' rows loaded'
    : 'scroll for more…';
}

function renderHeader() {
  var cols = visibleColumns();
  var cg = $('cg'), hrow = $('hrow');
  clear(cg); clear(hrow);
  var minW = 0;

  cols.forEach(function (c) {
    var w = colWidth(c.name);
    var col = document.createElement('col');
    if (w != null) { col.style.width = w + 'px'; minW += w; }
    else { minW += 260; }
    cg.appendChild(col);

    var th = document.createElement('th');
    th.dataset.col = c.name;
    th.draggable = true;
    th.title = c.label + '  (' + c.kind + ')  — click to sort, shift-click to add a sort key';

    var lbl = el('span', 'th-label', c.label);
    th.appendChild(lbl);

    var si = sortIndex(c.name);
    if (si >= 0) {
      th.appendChild(document.createTextNode(' '));
      th.appendChild(el('span', 'sort-dir', state.sort[si].dir === 'asc' ? '↑' : '↓'));
      if (state.sort.length > 1) th.appendChild(el('span', 'sort-ord', SUP[si + 1] || String(si + 1)));
    }

    th.appendChild(el('div', 'rz'));
    hrow.appendChild(th);
  });

  $('logtable').style.minWidth = Math.max(minW, 320) + 'px';
}

function sortIndex(col) {
  for (var i = 0; i < state.sort.length; i++) if (state.sort[i].col === col) return i;
  return -1;
}

function levelPill(v) {
  var s = String(v).toUpperCase();
  var span = el('span', 'pill pill-' + s, s);
  return span;
}

function statusSpan(v) {
  var n = Number(v);
  var cls = 'st-2xx';
  if (n >= 500) cls = 'st-5xx';
  else if (n >= 400) cls = 'st-4xx';
  else if (n >= 300) cls = 'st-3xx';
  return el('span', cls, v);
}

function cellText(col, val) {
  if (col.kind === 'time') return fmtTime(val);
  if (typeof val === 'boolean') return val ? 'true' : 'false';
  if (typeof val === 'object') { try { return JSON.stringify(val); } catch (e) { return String(val); } }
  return String(val);
}

function buildCell(col, row) {
  var val = row ? row[col.name] : null;
  var td = document.createElement('td');
  td.dataset.col = col.name;
  td.className = 'c-' + col.name.replace(/[^a-zA-Z0-9_-]/g, '_');

  if (isBlank(val)) {
    td.appendChild(el('span', 'null', '—'));
    return td;
  }
  if (col.name === 'level') {
    td.appendChild(levelPill(val));
  } else if (col.name === 'req_status_code') {
    td.className += ' num';
    td.appendChild(statusSpan(val));
  } else if (col.kind === 'time') {
    var t = cellText(col, val);
    td.textContent = t;
    td.title = String(val);
  } else if (col.numeric || col.kind === 'number') {
    td.className += ' num';
    td.textContent = cellText(col, val);
  } else {
    var txt = cellText(col, val);
    td.textContent = txt;
    if (txt.length > 40) td.title = txt;
  }
  return td;
}

function buildRow(row, i, cols) {
  var tr = document.createElement('tr');
  tr.dataset.i = String(i);
  var lvl = row && row.level ? String(row.level).toUpperCase() : '';
  if (lvl) tr.className = 'lvl-' + lvl;
  if (row && row.parse_ok === false) tr.className += ' parse-bad';
  tr.className += highlightClass(row);
  (cols || visibleColumns()).forEach(function (c) { tr.appendChild(buildCell(c, row)); });
  return tr;
}

/* from === 0 rebuilds the body, otherwise appends the newly fetched page */
function renderRows(from) {
  var tb = $('tbody');
  if (!from) clear(tb);
  var cols = visibleColumns();
  var frag = document.createDocumentFragment();
  for (var i = from || 0; i < state.rows.length; i++) frag.appendChild(buildRow(state.rows[i], i, cols));
  tb.appendChild(frag);
  document.body.classList.toggle('hl-on', highlightsActive());
  if (state.selectedRow !== null) markSelected(state.selectedRow);
}

function markSelected(i) {
  var tb = $('tbody');
  var prev = tb.querySelector('tr.selected');
  if (prev) prev.classList.remove('selected');
  var tr = tb.querySelector('tr[data-i="' + i + '"]');
  if (tr) tr.classList.add('selected');
}


/* ── 9. render: filter panel ─────────────────────────────────────────── */

function activeCount(f) {
  if (!f) return 0;
  if (f.kind === 'facet') return (f.values || []).length;
  if (f.kind === 'number') return (isBlank(f.min) ? 0 : 1) + (isBlank(f.max) ? 0 : 1);
  if (f.kind === 'time') return (isBlank(f.from) ? 0 : 1) + (isBlank(f.to) ? 0 : 1);
  return isBlank(f.value) ? 0 : 1;
}

function ensureCard(col, silent) {
  if (state.panel.indexOf(col) < 0) state.panel.push(col);
  if (!silent) renderPanel();
}

function removeCard(col) {
  var i = state.panel.indexOf(col);
  if (i >= 0) state.panel.splice(i, 1);
  var had = filterActive(state.filters[col]);
  var hadHl = highlightValues(col).length > 0;
  delete state.filters[col];              // removing a card clears its filter
  delete state.highlights[col];           // ...and its highlights
  delete state.facetSearch[col];
  renderPanel();
  if (had) commitFilters();
  else if (hadHl) commitHighlights();
  else writeHash();
}

function setFilter(col, f) {
  if (f === null) delete state.filters[col];
  else state.filters[col] = f;
  commitFilters();
}

/* facet values fall back to the loaded page when the API returns no facet
   block for this column (e.g. a card added for a column with no filter yet) */
function facetValues(col) {
  var api = state.facets[col];
  if (Array.isArray(api) && api.length) return { list: api, approx: false };
  var counts = {}, order = [];
  state.rows.forEach(function (r) {
    var v = r[col];
    if (isBlank(v)) return;
    var k = String(v);
    if (counts[k] === undefined) { counts[k] = 0; order.push(k); }
    counts[k]++;
  });
  var list = order.map(function (k) { return { value: k, count: counts[k] }; });
  list.sort(function (a, b) { return b.count - a.count; });
  return { list: list, approx: true };
}

function captureFocus() {
  var a = document.activeElement;
  if (!a || !a.dataset || !a.dataset.fk) return null;
  if (!$('cards').contains(a)) return null;      // only panel widgets get rebuilt
  var f = { fk: a.dataset.fk, start: null, end: null };
  try { f.start = a.selectionStart; f.end = a.selectionEnd; } catch (e) { /* not a text input */ }
  return f;
}

function restoreFocus(f) {
  if (!f) return;
  var n = document.querySelector('[data-fk="' + f.fk.replace(/"/g, '\\"') + '"]');
  if (!n) return;
  try {
    n.focus();
    if (f.start !== null && n.setSelectionRange) n.setSelectionRange(f.start, f.end);
  } catch (e) { /* ignore */ }
}

function renderPanel() {
  var wrap = $('cards');
  var focus = captureFocus();
  var scroll = wrap.scrollTop;
  clear(wrap);
  state.panel.forEach(function (name) { wrap.appendChild(buildCard(name)); });
  wrap.scrollTop = scroll;
  restoreFocus(focus);
}

function buildCard(name) {
  var col = state.colMap[name] || { name: name, kind: 'text', label: name, numeric: false };
  var f = state.filters[name];
  var card = el('div', 'card' + (state.cardCollapsed[name] ? ' collapsed' : ''));
  card.dataset.col = name;

  var head = el('div', 'card-head');
  head.appendChild(el('span', 'card-arrow', state.cardCollapsed[name] ? '▸' : '▾'));
  head.appendChild(el('span', 'card-name', col.label || name));
  var n = activeCount(f);
  if (n > 0) {
    var fb = el('span', 'badge badge-filter', String(n));
    fb.title = n + (n === 1 ? ' value filtered' : ' values filtered') + ' — non-matching rows are hidden';
    head.appendChild(fb);
  }
  var hn = highlightValues(name).length;
  if (hn > 0) {
    card.className += ' has-hl';
    var hb = el('span', 'badge badge-hl', '◆ ' + hn);
    hb.title = hn + (hn === 1 ? ' value highlighted' : ' values highlighted') + ' — non-matching rows are dimmed, not hidden';
    head.appendChild(hb);
  }
  head.appendChild(el('span', 'card-kind', col.kind));
  var x = el('span', 'card-x', '×');
  x.title = 'Remove card (clears its filter and highlights)';
  x.addEventListener('click', function (ev) { ev.stopPropagation(); removeCard(name); });
  head.appendChild(x);
  head.addEventListener('click', function () {
    state.cardCollapsed[name] = !state.cardCollapsed[name];
    renderPanel();
  });
  card.appendChild(head);

  var body = el('div', 'card-body');
  if (col.kind === 'facet') buildFacetWidget(body, col, f);
  else if (col.kind === 'number') buildNumberWidget(body, col, f);
  else if (col.kind === 'time') buildTimeWidget(body, col, f);
  else buildTextWidget(body, col, f);
  card.appendChild(body);
  return card;
}

function buildFacetWidget(body, col, f) {
  var name = col.name;
  var selected = (f && f.kind === 'facet' && f.values) ? f.values.slice() : [];
  var res = facetValues(name);
  var list = res.list.slice();

  // selected values that the API did not return still need a (checked) row
  selected.forEach(function (v) {
    var found = list.some(function (o) { return String(o.value) === String(v); });
    if (!found) list.push({ value: v, count: 0 });
  });

  if (list.length > 12) {
    var sb = el('input', 'input');
    sb.type = 'text';
    sb.placeholder = 'search values…';
    sb.dataset.fk = 'fs:' + name;
    sb.value = state.facetSearch[name] || '';
    sb.addEventListener('input', debounce(function () {
      state.facetSearch[name] = sb.value;
      renderPanel();
    }, 150));
    body.appendChild(sb);
  }

  var needle = (state.facetSearch[name] || '').toLowerCase();
  var shown = needle ? list.filter(function (o) { return String(o.value).toLowerCase().indexOf(needle) >= 0; }) : list;

  var box = el('div', 'facet-list');
  if (!shown.length) box.appendChild(el('div', 'facet-empty', res.approx ? 'no values in the loaded rows' : 'no values'));

  shown.forEach(function (o) {
    var cnt = Number(o.count) || 0;
    var lit = isHighlighted(name, o.value);
    var row = el('label', 'facet-row' + (cnt === 0 ? ' zero' : '') + (lit ? ' lit' : ''));
    row.title = lit
      ? 'Highlighted — shift-click to stop highlighting'
      : 'Click to filter · shift-click to highlight instead';

    /* Shift-click means "highlight", not "filter". Captured on the label so it
       runs before the checkbox's default toggle, which preventDefault cancels
       — otherwise the value would end up filtered AND highlighted. */
    row.addEventListener('click', function (ev) {
      if (!ev.shiftKey) return;
      ev.preventDefault();
      ev.stopPropagation();
      toggleHighlight(name, o.value);
    }, true);

    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = selected.some(function (v) { return String(v) === String(o.value); });
    cb.addEventListener('change', function () {
      var vals = (state.filters[name] && state.filters[name].values) ? state.filters[name].values.slice() : [];
      var i = vals.findIndex(function (v) { return String(v) === String(o.value); });
      if (cb.checked) { if (i < 0) vals.push(o.value); }
      else if (i >= 0) vals.splice(i, 1);
      setFilter(name, vals.length ? { kind: 'facet', values: vals } : null);
    });
    row.appendChild(cb);
    var mark = el('span', 'facet-mark', lit ? '◆' : '');
    mark.title = lit ? 'highlighted' : '';
    row.appendChild(mark);
    var v = el('span', 'facet-val', String(o.value));
    v.title = String(o.value);
    row.appendChild(v);
    row.appendChild(el('span', 'facet-cnt', cnt.toLocaleString()));
    box.appendChild(row);
  });
  body.appendChild(box);
  body.appendChild(el('div', 'hint hint-shift', 'click = filter (hides others) · shift-click = ◆ highlight (dims others)'));
  if (res.approx && res.list.length) body.appendChild(el('div', 'hint', 'counts from loaded rows'));
}

function buildNumberWidget(body, col, f) {
  var name = col.name;
  var wrap = el('div', 'range');
  var mk = function (which, val, ph) {
    var i = el('input', 'input');
    i.type = 'text';
    i.inputMode = 'numeric';
    i.placeholder = ph;
    i.dataset.fk = 'n:' + name + ':' + which;
    i.value = isBlank(val) ? '' : String(val);
    i.addEventListener('input', debounce(function () {
      var cur = state.filters[name] && state.filters[name].kind === 'number'
        ? state.filters[name] : { kind: 'number', min: null, max: null };
      var raw = i.value.trim();
      var num = raw === '' ? null : Number(raw);
      if (raw !== '' && !isFinite(num)) return;
      var next = { kind: 'number', min: cur.min, max: cur.max };
      next[which] = num;
      setFilter(name, (isBlank(next.min) && isBlank(next.max)) ? null : next);
    }, DEBOUNCE_MS));
    return i;
  };
  wrap.appendChild(mk('min', f && f.min, 'min'));
  wrap.appendChild(el('span', 'hint', '–'));
  wrap.appendChild(mk('max', f && f.max, 'max'));
  body.appendChild(wrap);
}

function buildTimeWidget(body, col, f) {
  var name = col.name;
  var mk = function (which, val, ph) {
    var line = el('div', 'row-line');
    line.appendChild(el('span', 'hint', which === 'from' ? 'from' : 'to'));
    var i = el('input', 'input');
    i.type = 'text';
    i.placeholder = ph;
    i.dataset.fk = 't:' + name + ':' + which;
    i.value = isBlank(val) ? '' : String(val);
    i.addEventListener('input', debounce(function () {
      var cur = state.filters[name] && state.filters[name].kind === 'time'
        ? state.filters[name] : { kind: 'time', from: null, to: null };
      var next = { kind: 'time', from: cur.from, to: cur.to };
      next[which] = i.value.trim() === '' ? null : i.value.trim();
      setFilter(name, (isBlank(next.from) && isBlank(next.to)) ? null : next);
    }, DEBOUNCE_MS));
    line.appendChild(i);
    return line;
  };
  body.appendChild(mk('from', f && f.from, '2026-07-09T08:00:00Z'));
  body.appendChild(mk('to', f && f.to, '2026-07-09T09:00:00Z'));
}

function buildTextWidget(body, col, f) {
  var name = col.name;
  var line = el('div', 'row-line');
  var i = el('input', 'input');
  i.type = 'text';
  i.placeholder = 'contains…';
  i.dataset.fk = 'x:' + name;
  i.value = f && f.value ? String(f.value) : '';
  var apply = debounce(function () {
    var rx = !!(state.filters[name] && state.filters[name].regex);
    setFilter(name, i.value === '' ? null : { kind: 'text', value: i.value, regex: rx });
  }, DEBOUNCE_MS);
  i.addEventListener('input', apply);
  line.appendChild(i);
  body.appendChild(line);

  var lab = el('label', 'chk');
  var cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!(f && f.regex);
  cb.dataset.fk = 'xr:' + name;
  cb.addEventListener('change', function () {
    var val = i.value;
    if (val === '') { // remember the toggle even with an empty box
      state.filters[name] = { kind: 'text', value: '', regex: cb.checked };
      renderPanel();
      return;
    }
    setFilter(name, { kind: 'text', value: val, regex: cb.checked });
  });
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode('regex'));
  body.appendChild(lab);
}


/* ── 10. popovers ────────────────────────────────────────────────────── */

var curPop = null;

function closePop() {
  if (!curPop) return;
  if (curPop.parentNode) curPop.parentNode.removeChild(curPop);
  curPop = null;
  document.removeEventListener('mousedown', onDocDown, true);
  document.removeEventListener('keydown', onPopKey, true);
}

function onDocDown(e) { if (curPop && !curPop.contains(e.target)) closePop(); }
function onPopKey(e) { if (e.key === 'Escape') closePop(); }

/* opts: {x, y, anchor, search, items:[{label, sub, disabled, checked, onPick}], keepOpen} */
function openPop(opts) {
  closePop();
  var pop = el('div', 'pop');
  var listBox = el('div', 'pop-list');

  function paint(needle) {
    clear(listBox);
    if (opts.render) { opts.render(listBox); return; }   // free-form popover
    var n = (needle || '').toLowerCase();
    var items = opts.items();
    var shown = 0;
    items.forEach(function (it) {
      if (it.sep) { listBox.appendChild(el('div', 'pop-sep')); return; }
      if (n && String(it.label).toLowerCase().indexOf(n) < 0) return;
      shown++;
      var row = el('div', 'pop-item' + (it.disabled ? ' disabled' : ''));
      if (it.checked !== undefined) {
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!it.checked;
        cb.tabIndex = -1;
        row.appendChild(cb);
      }
      row.appendChild(el('span', null, it.label));
      if (it.sub) row.appendChild(el('span', 'sub', it.sub));
      if (!it.disabled) {
        row.addEventListener('click', function () {
          it.onPick();
          if (opts.keepOpen) paint(searchBox ? searchBox.value : '');
          else closePop();
        });
      }
      listBox.appendChild(row);
    });
    if (!shown) listBox.appendChild(el('div', 'facet-empty', 'nothing matches'));
  }

  var searchBox = null;
  if (opts.search) {
    searchBox = el('input', 'input');
    searchBox.type = 'text';
    searchBox.placeholder = opts.search;
    searchBox.addEventListener('input', function () { paint(searchBox.value); });
    pop.appendChild(searchBox);
  }
  pop.appendChild(listBox);
  paint('');
  document.body.appendChild(pop);

  var x = opts.x, y = opts.y;
  if (opts.anchor) {
    var r = opts.anchor.getBoundingClientRect();
    x = r.left; y = r.bottom + 4;
  }
  var pr = pop.getBoundingClientRect();
  if (x + pr.width > window.innerWidth - 8) x = Math.max(8, window.innerWidth - pr.width - 8);
  if (y + pr.height > window.innerHeight - 8) y = Math.max(8, window.innerHeight - pr.height - 8);
  pop.style.left = Math.max(8, x) + 'px';
  pop.style.top = Math.max(8, y) + 'px';

  curPop = pop;
  setTimeout(function () {
    document.addEventListener('mousedown', onDocDown, true);
    document.addEventListener('keydown', onPopKey, true);
    if (searchBox) searchBox.focus();
  }, 0);
}

function openAddFilter(anchor) {
  openPop({
    anchor: anchor,
    search: 'search columns…',
    items: function () {
      return orderedColumns().map(function (c) {
        var present = state.panel.indexOf(c.name) >= 0;
        return {
          label: c.label || c.name,
          sub: present ? 'added' : c.kind,
          disabled: present,
          onPick: function () { ensureCard(c.name); }
        };
      });
    }
  });
}

function openColumnsPicker(anchor) {
  openPop({
    anchor: anchor,
    search: 'search columns…',
    keepOpen: true,
    items: function () {
      return orderedColumns().map(function (c) {
        return {
          label: c.label || c.name,
          checked: !state.layout.hidden[c.name],
          onPick: function () {
            if (!state.layout.hidden[c.name] && visibleColumns().length <= 1) return;  // keep one column
            if (state.layout.hidden[c.name]) delete state.layout.hidden[c.name];
            else state.layout.hidden[c.name] = true;   // width is kept for when it comes back
            saveLayout();
            renderHeader();
            renderRows(0);
          }
        };
      });
    }
  });
}

function renderTzButton() { $('tz-btn').textContent = 'tz: ' + state.layout.tz + ' ▾'; }

function openTzPicker(anchor) {
  var zones = timezoneList();
  openPop({
    anchor: anchor,
    search: 'search timezones…',
    items: function () {
      return zones.map(function (z) {
        return {
          label: z,
          sub: z === state.layout.tz ? '✓' : '',
          onPick: function () {
            state.layout.tz = z;
            saveLayout();
            renderTzButton();
            renderRows(0);
            if (state.selectedRow !== null) renderDrawer();
          }
        };
      });
    }
  });
}


/* ── 11. drag: reorder + resize ──────────────────────────────────────── */

var dragCol = null;
var suppressClick = false;

function installHeaderInteractions() {
  var hrow = $('hrow');

  hrow.addEventListener('dragstart', function (e) {
    var th = e.target.closest ? e.target.closest('th') : null;
    if (!th) return;
    dragCol = th.dataset.col;
    th.classList.add('dragging');
    try {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragCol);
    } catch (err) { /* ignore */ }
  });

  hrow.addEventListener('dragover', function (e) {
    if (!dragCol) return;
    var th = e.target.closest ? e.target.closest('th') : null;
    if (!th || th.dataset.col === dragCol) return;
    e.preventDefault();
    try { e.dataTransfer.dropEffect = 'move'; } catch (err) { /* ignore */ }
    var r = th.getBoundingClientRect();
    var after = (e.clientX - r.left) > r.width / 2;
    clearDropMarks();
    th.classList.add(after ? 'drag-over-right' : 'drag-over-left');
  });

  hrow.addEventListener('dragleave', function (e) {
    var th = e.target.closest ? e.target.closest('th') : null;
    if (th) { th.classList.remove('drag-over-left'); th.classList.remove('drag-over-right'); }
  });

  hrow.addEventListener('drop', function (e) {
    if (!dragCol) return;
    e.preventDefault();
    var th = e.target.closest ? e.target.closest('th') : null;
    if (th && th.dataset.col !== dragCol) {
      var r = th.getBoundingClientRect();
      var after = (e.clientX - r.left) > r.width / 2;
      moveColumn(dragCol, th.dataset.col, after);
    }
    clearDropMarks();
    dragCol = null;
    suppressClick = true;
  });

  hrow.addEventListener('dragend', function () {
    clearDropMarks();
    var d = hrow.querySelector('th.dragging');
    if (d) d.classList.remove('dragging');
    dragCol = null;
  });

  hrow.addEventListener('click', function (e) {
    if (e.target.classList && e.target.classList.contains('rz')) return;
    if (suppressClick) { suppressClick = false; return; }
    var th = e.target.closest ? e.target.closest('th') : null;
    if (!th) return;
    cycleSort(th.dataset.col, e.shiftKey);
  });

  hrow.addEventListener('mousedown', function (e) {
    if (!e.target.classList || !e.target.classList.contains('rz')) return;
    var th = e.target.closest('th');
    if (!th) return;
    e.preventDefault();
    startResize(th, e.clientX);
  });
}

/* Drag a grip to resize the panel it sits next to. The filter panel grows to
   the right of its left edge, the drawer grows to the left of its right edge,
   hence the opposite signs. Double-click restores the default width. */
function installPanelResize(gripId, opts) {
  var grip = $(gripId);
  if (!grip) return;

  grip.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    e.preventDefault();
    var startX = e.clientX;
    var startW = opts.get();
    grip.classList.add('dragging');
    document.body.classList.add('resizing');

    function onMove(ev) {
      var delta = (ev.clientX - startX) * opts.sign;
      opts.set(opts.clamp(startW + delta));
      applyPanelWidths();
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      grip.classList.remove('dragging');
      document.body.classList.remove('resizing');
      saveLayout();
      maybeLoadMore();      // a wider table may now show fewer rows than it fits
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  grip.addEventListener('dblclick', function () {
    opts.set(opts.def);
    applyPanelWidths();
    saveLayout();
  });
}

function installPanelResizers() {
  installPanelResize('panel-grip', {
    sign: 1,
    def: PANEL_W_DEFAULT,
    clamp: clampPanelW,
    get: function () { return state.layout.panelW; },
    set: function (v) { state.layout.panelW = v; }
  });
  installPanelResize('drawer-grip', {
    sign: -1,
    def: DRAWER_W_DEFAULT,
    clamp: clampDrawerW,
    get: function () { return state.layout.drawerW; },
    set: function (v) { state.layout.drawerW = v; }
  });
}

function clearDropMarks() {
  var marks = $('hrow').querySelectorAll('.drag-over-left, .drag-over-right');
  for (var i = 0; i < marks.length; i++) {
    marks[i].classList.remove('drag-over-left');
    marks[i].classList.remove('drag-over-right');
  }
}

function moveColumn(src, target, after) {
  var order = orderedColumns().map(function (c) { return c.name; });
  var si = order.indexOf(src);
  if (si < 0) return;
  order.splice(si, 1);
  var ti = order.indexOf(target);
  if (ti < 0) return;
  order.splice(after ? ti + 1 : ti, 0, src);
  state.layout.order = order;
  saveLayout();
  renderHeader();
  renderRows(0);
}

function startResize(th, startX) {
  var name = th.dataset.col;
  var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
  var startW = th.getBoundingClientRect().width;
  var cols = $('cg').children;
  th.draggable = false;                       // don't start an HTML5 drag mid-resize
  document.body.classList.add('resizing');

  function onMove(e) {
    var w = Math.max(WIDTH_MIN, Math.round(startW + (e.clientX - startX)));
    state.layout.widths[name] = w;
    if (cols[idx]) cols[idx].style.width = w + 'px';
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('mouseup', onUp, true);
    document.body.classList.remove('resizing');
    th.draggable = true;
    suppressClick = true;
    saveLayout();
    renderHeader();                            // recompute the table min-width
  }
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('mouseup', onUp, true);
}

/* click cycles asc → desc → off; shift-click appends a key */
function cycleSort(col, shift) {
  var i = sortIndex(col);
  if (shift) {
    if (i < 0) state.sort.push({ col: col, dir: 'asc' });
    else if (state.sort[i].dir === 'asc') state.sort[i].dir = 'desc';
    else state.sort.splice(i, 1);
  } else {
    if (i < 0 || state.sort.length > 1) state.sort = [{ col: col, dir: 'asc' }];
    else if (state.sort[0].dir === 'asc') state.sort = [{ col: col, dir: 'desc' }];
    else state.sort = [];
  }
  renderHeader();
  writeHash();
  refresh();                                   // server-side sort; scroll resets to top
}


/* ── 12. drawer ──────────────────────────────────────────────────────── */

/* The row identifier for GET /api/row/{idx}. The API contract does not name
   the field, so the usual candidates are probed; without one the drawer
   still renders from the row already in hand (minus _raw). */
function rowIndexOf(row) {
  if (!row) return null;
  var keys = ['_idx', 'idx', '_i', '_index', '_row', '_id'];
  for (var i = 0; i < keys.length; i++) {
    var v = row[keys[i]];
    if (typeof v === 'number' && isFinite(v)) return v;
    if (typeof v === 'string' && /^\d+$/.test(v)) return Number(v);
  }
  return null;
}

var drawerData = null;

async function openDrawer(i) {
  var row = state.rows[i];
  if (!row) return;
  state.selectedRow = i;
  markSelected(i);
  $('drawer').hidden = false;
  $('drawer-grip').hidden = false;
  drawerData = row;
  renderDrawer('loading full record…');

  var idx = rowIndexOf(row);
  if (idx === null) { renderDrawer(); return; }
  try {
    var full = await api('/api/row/' + encodeURIComponent(idx));
    if (state.selectedRow !== i) return;
    if (full && typeof full === 'object') drawerData = full.row && typeof full.row === 'object' ? full.row : full;
    renderDrawer();
  } catch (e) {
    renderDrawer('could not load _raw: ' + (e.message || e));
  }
}

function closeDrawer() {
  $('drawer').hidden = true;
  $('drawer-grip').hidden = true;
  state.selectedRow = null;
  var prev = $('tbody').querySelector('tr.selected');
  if (prev) prev.classList.remove('selected');
}

function kvRow(grid, k, v, cls) {
  grid.appendChild(el('div', 'k', k));
  var d = el('div', 'v' + (cls ? ' ' + cls : ''));
  d.textContent = v;
  grid.appendChild(d);
}

function renderDrawer(note) {
  var body = $('drawer-body');
  var row = drawerData;
  clear(body);
  if (!row) return;

  if (note) body.appendChild(el('div', 'hint', note));

  /* timestamps */
  body.appendChild(el('h4', null, 'timestamp'));
  var tg = el('div', 'kv');
  kvRow(tg, 'time (' + state.layout.tz + ')', fmtTime(row.time));
  kvRow(tg, 'time (raw ns)', row.time == null ? '—' : String(row.time));
  var pa = parseTs(row.app_time), pt = parseTs(row.time);
  kvRow(tg, 'app_time', row.app_time == null ? '—' : String(row.app_time));
  if (pa && pt) {
    var d = (pt.date.getTime() - pa.date.getTime()) / 1000;
    var extraNs = pt.nanos ? Number('0.' + pt.nanos) - (pt.date.getUTCMilliseconds() / 1000) : 0;
    var delta = d + (isFinite(extraNs) ? extraNs : 0);
    kvRow(tg, 'delta (time − app_time)', (delta >= 0 ? '+' : '') + delta.toFixed(6) + ' s');
  }
  body.appendChild(tg);

  /* every normalized field */
  body.appendChild(el('h4', null, 'fields'));
  var grid = el('div', 'kv');
  var seen = {};
  orderedColumns().forEach(function (c) {
    seen[c.name] = true;
    var v = row[c.name];
    if (isBlank(v)) kvRow(grid, c.name, '—', 'null');
    else kvRow(grid, c.name, typeof v === 'object' ? JSON.stringify(v) : String(v));
  });
  Object.keys(row).forEach(function (k) {
    if (seen[k] || k === '_raw') return;
    var v = row[k];
    if (isBlank(v)) kvRow(grid, k, '—', 'null');
    else kvRow(grid, k, typeof v === 'object' ? JSON.stringify(v) : String(v));
  });
  body.appendChild(grid);

  /* pretty-printed _raw */
  body.appendChild(el('h4', null, '_raw'));
  var pre = el('pre');
  if (row._raw === undefined) {
    pre.textContent = '(not loaded)';
  } else {
    try { pre.textContent = JSON.stringify(row._raw, null, 2); }
    catch (e) { pre.textContent = String(row._raw); }
  }
  body.appendChild(pre);
}


/* ── 13. context menu actions ────────────────────────────────────────── */

function copyText(txt) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).catch(function () { fallbackCopy(txt); });
  } else fallbackCopy(txt);
}

function fallbackCopy(txt) {
  var ta = document.createElement('textarea');
  ta.value = txt;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) { /* ignore */ }
  document.body.removeChild(ta);
}

function filterForValue(colName, value) {
  var col = state.colMap[colName] || { name: colName, kind: 'text' };
  ensureCard(colName, true);
  if (col.kind === 'facet') setFilter(colName, { kind: 'facet', values: [value] });
  else if (col.kind === 'number') setFilter(colName, { kind: 'number', min: Number(value), max: Number(value) });
  else if (col.kind === 'time') setFilter(colName, { kind: 'time', from: String(value), to: String(value) });
  else setFilter(colName, { kind: 'text', value: String(value), regex: false });
}

/* The API's facet filter is an OR-of-values include list — there is no
   "exclude" field in the contract. "Filter out" is therefore implemented as
   "select every other value of this facet", which is the exact complement.
   It is only offered for facet columns, since number/time/text filters have
   no negation in the API. */
function filterOutValue(colName, value) {
  var res = facetValues(colName);
  var others = res.list
    .map(function (o) { return o.value; })
    .filter(function (v) { return String(v) !== String(value); });
  if (!others.length) return;
  ensureCard(colName, true);
  setFilter(colName, { kind: 'facet', values: others });
}

function openCellMenu(e, td, row) {
  var colName = td.dataset.col;
  var col = state.colMap[colName] || { name: colName, kind: 'text' };
  var raw = row ? row[colName] : null;
  var shown = isBlank(raw) ? '' : (col.kind === 'time' ? String(raw) : cellText(col, raw));

  openPop({
    x: e.clientX, y: e.clientY,
    items: function () {
      return [
        { label: colName + (shown ? ' = ' + (shown.length > 34 ? shown.slice(0, 34) + '…' : shown) : ' (empty)'), disabled: true },
        { sep: true },
        {
          label: 'Filter for value', disabled: isBlank(raw),
          onPick: function () { filterForValue(colName, raw); }
        },
        {
          label: 'Filter out value',
          sub: col.kind === 'facet' ? '' : 'facets only',
          disabled: isBlank(raw) || col.kind !== 'facet',
          onPick: function () { filterOutValue(colName, raw); }
        },
        {
          label: isHighlighted(colName, raw) ? 'Remove highlight' : 'Highlight value',
          sub: col.kind === 'facet' ? 'dims other rows' : 'facets only',
          disabled: isBlank(raw) || col.kind !== 'facet',
          onPick: function () { ensureCard(colName, true); toggleHighlight(colName, raw); }
        },
        { label: 'Copy value', disabled: isBlank(raw), onPick: function () { copyText(String(raw)); } }
      ];
    }
  });
}


/* ── 14. init ────────────────────────────────────────────────────────── */

function applyPanelCollapsed() {
  document.body.classList.toggle('panel-collapsed', !!state.layout.panelCollapsed);
}

function setPanelCollapsed(v) {
  state.layout.panelCollapsed = !!v;
  applyPanelCollapsed();
  saveLayout();
}

function installGlobalHandlers() {
  $('panel-hide').addEventListener('click', function () { setPanelCollapsed(true); });
  $('panel-show').addEventListener('click', function () { setPanelCollapsed(false); });
  $('add-filter').addEventListener('click', function (e) { openAddFilter(e.currentTarget); });
  $('cols-btn').addEventListener('click', function (e) { openColumnsPicker(e.currentTarget); });
  $('tz-btn').addEventListener('click', function (e) { openTzPicker(e.currentTarget); });
  $('reload').addEventListener('click', function () { state.grandTotal = null; refresh(); });
  $('drawer-close').addEventListener('click', closeDrawer);
  $('reset-layout').addEventListener('click', resetLayout);
  $('clear-filters').addEventListener('click', function () {
    state.filters = {};
    state.highlights = {};          // the button clears every selection...
    state.q = '';
    state.query = '';               // ...including the query box
    state.lq = '';
    $('q').value = '';
    if ($('lq')) $('lq').value = '';
    clearQueryError();
    markQueryDirty();
    renderPanel();
    renderHighlightBar();
    commitFilters();
  });

  $('q').addEventListener('input', debounce(function () {
    state.q = $('q').value;
    commitFilters();
  }, DEBOUNCE_MS));

  var wrap = $('tablewrap');
  wrap.addEventListener('scroll', function () {
    if (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < SCROLL_PAD) loadMore();
  });

  var tb = $('tbody');
  tb.addEventListener('click', function (e) {
    var tr = e.target.closest ? e.target.closest('tr') : null;
    if (!tr) return;
    openDrawer(Number(tr.dataset.i));
  });
  tb.addEventListener('contextmenu', function (e) {
    var td = e.target.closest ? e.target.closest('td') : null;
    var tr = e.target.closest ? e.target.closest('tr') : null;
    if (!td || !tr) return;
    e.preventDefault();
    openCellMenu(e, td, state.rows[Number(tr.dataset.i)]);
  });

  window.addEventListener('hashchange', function () {
    var h = window.location.hash || '';
    if (h === (state.lastHash || '')) return;    // our own write
    state.lastHash = h;
    applyHashState();
    renderPanel();
    renderHighlightBar();
    renderHeader();
    state.gen++;
    state.done = false;
    if (state.loading) { state.queued = true; return; }
    $('tablewrap').scrollTop = 0;
    fetchPage(0, false);
  });

  window.addEventListener('resize', function () {
    applyPanelWidths();      // re-clamp so a narrow window cannot hide the table
    maybeLoadMore();
  });
  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !curPop && !$('drawer').hidden) closeDrawer();
  });

  installHeaderInteractions();
  installPanelResizers();
  installQueryBar();
}

async function init() {
  loadLayout();
  applyPanelCollapsed();
  applyPanelWidths();
  renderTzButton();
  installGlobalHandlers();

  try {
    var cols = await api('/api/columns');
    state.columns = normalizeColumns(cols);
  } catch (e) {
    showError('Could not load /api/columns: ' + (e.message || e));
    state.columns = [];
  }
  state.colMap = {};
  state.columns.forEach(function (c) { state.colMap[c.name] = c; });

  applyDefaultLayout();
  saveLayout();

  // panel opens with the default_filter columns (falling back to the plan's list)
  var panel = state.columns.filter(function (c) { return c.default_filter; }).map(function (c) { return c.name; });
  if (!panel.length) panel = DEFAULT_PANEL.filter(function (n) { return state.colMap[n]; });
  state.panel = panel;

  state.lastHash = window.location.hash || '';
  applyHashState();

  renderHeader();
  renderPanel();
  renderHighlightBar();

  // baseline (unfiltered) count for the "N of M matching" header
  try {
    var base = await postLogs({ filters: {}, q: '', sort: [], limit: 1, offset: 0 });
    if (typeof base.total === 'number') state.grandTotal = base.total;
  } catch (e) { /* non-fatal */ }

  await fetchPage(0, false);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
