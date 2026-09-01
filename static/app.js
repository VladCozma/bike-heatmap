const GRADIENT_STOPS = [
  [0.0, [0, 170, 70]],
  [0.25, [120, 210, 40]],
  [0.5, [230, 220, 30]],
  [0.75, [250, 150, 20]],
  [1.0, [225, 30, 25]]
];
const SHADES = 48;

// Colours are quantised so that cells sharing a day count share an exact colour.
const PALETTE = Array.from({ length: SHADES }, (_, i) => {
  const t = i / (SHADES - 1);
  let s = 0;
  while (s < GRADIENT_STOPS.length - 2 && t > GRADIENT_STOPS[s + 1][0]) s++;
  const [t0, c0] = GRADIENT_STOPS[s];
  const [t1, c1] = GRADIENT_STOPS[s + 1];
  const f = (t - t0) / (t1 - t0);
  const channel = (k) => Math.round(c0[k] + (c1[k] - c0[k]) * f);
  return `rgb(${channel(0)}, ${channel(1)}, ${channel(2)})`;
});

const TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// Paints one flat-coloured disc per cell, busiest last, then blurs the whole
// canvas. A stretch ridden the same number of days keeps a single colour.
const CellLayer = L.Layer.extend({
  initialize() {
    this._points = [];
  },

  onAdd(map) {
    this._canvas = L.DomUtil.create('canvas', 'cell-canvas');
    map.getPanes().overlayPane.appendChild(this._canvas);
    map.on('moveend zoomend resize', this._redraw, this);
    this._redraw();
  },

  onRemove(map) {
    map.off('moveend zoomend resize', this._redraw, this);
    this._canvas.remove();
  },

  setPoints(points) {
    this._points = points.slice().sort((a, b) => a[2] - b[2]);
    this._redraw();
  },

  redraw() {
    this._redraw();
  },

  _redraw() {
    const map = this._map;
    if (!map) return;

    const size = map.getSize();
    const canvas = this._canvas;
    canvas.width = size.x;
    canvas.height = size.y;
    canvas.style.filter = `blur(${blur.value}px)`;
    L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]));

    const ctx = canvas.getContext('2d');
    const r = Number(radius.value);
    let shade = -1;

    for (const [lat, lon, weight] of this._points) {
      const p = map.latLngToContainerPoint([lat, lon]);
      if (p.x < -r || p.y < -r || p.x > size.x + r || p.y > size.y + r) continue;

      const next = Math.min(SHADES - 1, Math.round(weight * (SHADES - 1)));
      if (next !== shade) {
        ctx.fillStyle = PALETTE[next];
        shade = next;
      }
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }
});

const map = L.map('map', { zoomControl: true }).setView([48.2, 11.6], 11);
L.tileLayer(TILE_URL, { attribution: ATTRIBUTION, maxZoom: 19 }).addTo(map);
let points = [];

const el = (id) => document.getElementById(id);
const radius = el('radius');
const blur = el('blur');
const cell = el('cell');

const cellLayer = new CellLayer().addTo(map);

function renderHeat() {
  cellLayer.setPoints(points);
}

const FILTERS = {
  bike: { param: 'bike', facet: 'bikes', selected: new Set() },
//   cat: { param: 'cat', facet: 'categories', selected: new Set() },
  year: { param: 'year', facet: 'years', selected: new Set() },
  pass: { param: 'pass', facet: 'passes', selected: new Set() }
};

// Disjoint bands of "days ridden"; must match PASS_BUCKETS in heatmap/tracks.py.
const PASS_BUCKETS = [
  ['1 day', 1, 1],
  ['2-5 days', 2, 5],
  ['6+ days', 6, Infinity]
];

function passBucket(count) {
  for (const [label, low, high] of PASS_BUCKETS) {
    if (count >= low && count <= high) return label;
  }
  return PASS_BUCKETS[PASS_BUCKETS.length - 1][0];
}

function gridLevel() {
  // Cells track the zoom level so slow, twisty stretches collapse into one cell
  // instead of piling up extra points and looking hotter.
  return Math.round(map.getZoom() + Math.log2(256 / Number(cell.value)));
}

function filterQuery() {
  const params = new URLSearchParams({ level: gridLevel() });
  for (const { param, selected } of Object.values(FILTERS)) {
    for (const value of selected) params.append(param, value);
  }
  return params;
}

function renderFilters(facets) {
  for (const [name, filter] of Object.entries(FILTERS)) {
    const entries = facets[filter.facet] || [];
    const available = new Set(entries.map(([value]) => value));
    for (const value of filter.selected) {
      if (!available.has(value)) filter.selected.delete(value);
    }

    const container = el(`${name}-options`);
    container.replaceChildren(
      ...entries.map(([value, count]) => {
        const label = document.createElement('label');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = value;
        box.checked = filter.selected.has(value);
        box.addEventListener('change', () => {
          box.checked ? filter.selected.add(value) : filter.selected.delete(value);
          loadHeatmap();
        });
        label.append(box, document.createTextNode(` ${value}`));
        const badge = document.createElement('span');
        badge.className = 'count';
        badge.textContent = count;
        label.append(badge);
        return label;
      })
    );

    el(`${name}-badge`).textContent = filter.selected.size
      ? `${filter.selected.size} selected`
      : 'all';
  }
}

// A static export ships the rides as one file and aggregates them here instead
// of calling the API, so the page can be hosted without a server.
const STATIC_DATA = window.STATIC_DATA || null;
const MIN_LEVEL = 6;
const Y_RANGE = 2 ** 21;
let dataset = null;

async function loadDataset() {
  const meta = await (await fetch(`${STATIC_DATA}rides.json`)).json();
  const bytes = new Uint8Array(await (await fetch(`${STATIC_DATA}rides.bin`)).arrayBuffer());

  let cursor = 0;
  const readVarint = () => {
    let result = 0;
    let shift = 0;
    let byte;
    do {
      byte = bytes[cursor++];
      result += (byte & 127) * 2 ** shift;
      shift += 7;
    } while (byte & 128);
    return result;
  };
  const unzigzag = (v) => (v % 2 ? -(v + 1) / 2 : v / 2);

  const rides = meta.rides.map(([dayIndex, bikeIndex, /* catIndex, */ count]) => {
    const xs = new Int32Array(count);
    const ys = new Int32Array(count);
    let x = 0;
    let y = 0;
    for (let i = 0; i < count; i++) {
      x += unzigzag(readVarint());
      y += unzigzag(readVarint());
      xs[i] = x;
      ys[i] = y;
    }
    const day = meta.days[dayIndex];
    return {
      day,
      bike: meta.bikes[bikeIndex],
    //   category: meta.categories[catIndex],
      year: day.startsWith('file:') ? 'Unknown' : day.slice(0, 4),
      xs,
      ys
    };
  });

  dataset = { meta, rides };
}

function tally(values) {
  const counts = new Map();
  for (const value of values) counts.set(value, (counts.get(value) || 0) + 1);
  return [...counts].sort((a, b) => (a[0] < b[0] ? -1 : 1));
}

function staticHeatmap() {
  const level = Math.max(MIN_LEVEL, Math.min(gridLevel(), dataset.meta.baseLevel));
  const shift = dataset.meta.baseLevel - level;

  const counts = new Map();
  const seenPerDay = new Map();
  let selected = 0;

  for (const ride of dataset.rides) {
    if (FILTERS.bike.selected.size && !FILTERS.bike.selected.has(ride.bike)) continue;
    // if (FILTERS.cat.selected.size && !FILTERS.cat.selected.has(ride.category)) continue;
    if (FILTERS.year.selected.size && !FILTERS.year.selected.has(ride.year)) continue;
    selected++;

    let seen = seenPerDay.get(ride.day);
    if (!seen) {
      seen = new Set();
      seenPerDay.set(ride.day, seen);
    }
    for (let i = 0; i < ride.xs.length; i++) {
      const key = (ride.xs[i] >> shift) * Y_RANGE + (ride.ys[i] >> shift);
      if (seen.has(key)) continue; // one day never counts twice
      seen.add(key);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }

  const scale = 2 ** level;
  // Spreading the counts into Math.max overflows the stack once there are many cells.
  let busiest = 0;
  for (const count of counts.values()) if (count > busiest) busiest = count;
  const top = Math.log1p(busiest);

  const wanted = FILTERS.pass.selected;
  const histogram = new Map(PASS_BUCKETS.map(([label]) => [label, 0]));
  const points = [];
  let south = 90;
  let west = 180;
  let north = -90;
  let east = -180;

  for (const [key, count] of counts) {
    const bucket = passBucket(count);
    histogram.set(bucket, histogram.get(bucket) + 1);
    if (wanted.size && !wanted.has(bucket)) continue;

    const x = Math.floor(key / Y_RANGE);
    const y = key - x * Y_RANGE;
    const lon = ((x + 0.5) / scale) * 360 - 180;
    const lat = (Math.atan(Math.sinh(Math.PI * (1 - (2 * (y + 0.5)) / scale))) * 180) / Math.PI;
    points.push([lat, lon, Math.log1p(count) / top]);
    if (lat < south) south = lat;
    if (lat > north) north = lat;
    if (lon < west) west = lon;
    if (lon > east) east = lon;
  }

  return {
    points,
    bounds: points.length ? [[south, west], [north, east]] : null,
    rides: selected,
    days: seenPerDay.size,
    totalRides: dataset.rides.length,
    failed: [],
    facets: {
      bikes: tally(dataset.rides.map((r) => r.bike)),
    //   categories: tally(dataset.rides.map((r) => r.category)),
      years: tally(dataset.rides.map((r) => r.year)).reverse(),
      passes: [...histogram]
    }
  };
}

let pendingRequest = 0;

async function loadHeatmap(fit = false) {
  const token = ++pendingRequest;
  el('stats').textContent = 'Loading rides…';

  let data;
  if (STATIC_DATA) {
    if (!dataset) await loadDataset();
    data = staticHeatmap();
  } else {
    const response = await fetch(`/api/heatmap?${filterQuery()}`);
    if (!response.ok) {
      el('stats').textContent = 'Failed to load rides.';
      return;
    }
    data = await response.json();
  }
  if (token !== pendingRequest) return; // a newer request already won

  points = data.points;
  renderHeat();
  renderFilters(data.facets);

  if (fit && data.bounds) {
    map.fitBounds(data.bounds, { padding: [30, 30] });
  }

  const failed = data.failed.length ? ` · ${data.failed.length} unreadable` : '';
  const scope = data.rides === data.totalRides ? '' : ` of ${data.totalRides}`;
  el('stats').textContent = data.totalRides
    ? `${data.rides}${scope} rides on ${data.days} days · ` +
      `${points.length.toLocaleString()} cells${failed}`
    : 'No rides found yet — load some tracks or a database.';
}

for (const input of [radius, blur]) {
  input.addEventListener('input', () => {
    el(`${input.id}-value`).textContent = input.value;
    cellLayer.redraw();
  });
}

cell.addEventListener('change', () => {
  el('cell-value').textContent = cell.value;
  loadHeatmap();
});
cell.addEventListener('input', () => {
  el('cell-value').textContent = cell.value;
});

let zoomTimer = null;
map.on('zoomend', () => {
  clearTimeout(zoomTimer);
  zoomTimer = setTimeout(() => loadHeatmap(), 250);
});

function applyBasemapTheme(dark) {
  map.getPane('tilePane').classList.toggle('dark-tiles', dark);
}

el('dark').addEventListener('change', (event) => applyBasemapTheme(event.target.checked));
applyBasemapTheme(el('dark').checked);

el('clear-filters').addEventListener('click', () => {
  for (const filter of Object.values(FILTERS)) filter.selected.clear();
  loadHeatmap(true);
});

async function uploadTo(endpoint, input) {
  const files = input.files;
  if (!files.length) return;

  const form = new FormData();
  for (const file of files) form.append('files', file);

  el('stats').textContent = `Uploading ${files.length} file(s)…`;
  const response = await fetch(endpoint, { method: 'POST', body: form });
  input.value = '';
  if (!response.ok) {
    el('stats').textContent = 'Upload failed.';
    return;
  }
  await loadHeatmap(true);
}

// The static export has no server behind it, so these controls are not rendered.
if (!STATIC_DATA) {
  el('reload').addEventListener('click', () => loadHeatmap(true));
  el('upload-tracks').addEventListener('change', (event) => uploadTo('/api/tracks', event.target));
  el('upload-db').addEventListener('change', (event) => uploadTo('/api/databases', event.target));
}

loadHeatmap(true);
