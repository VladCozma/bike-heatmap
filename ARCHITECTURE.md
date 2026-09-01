# Bike Heatmap — Architecture & Design

## Overview

Bike Heatmap visualises your cycling history as an interactive map where colour intensity represents how many distinct days you've ridden through a cell. Unlike other heatmaps that shade by frequency or distance, this one answers: *Where are my regular routes?* Rare one-off detours stay green; the paths you take every week glow red.

The app runs locally without ever uploading your data. It exports to a static GitHub Pages site or works as a server with live upload.

## Data Flow

### 1. Loading Rides

Sources:
- **GPX/TCX files**: Track files from handhelds or phone apps, read into the `tracks/` folder
- **SQLite databases**: Bike computer backups (Android layout: `sessions` + `tracks` tables)

Entry point: `heatmap/tracks.py::load_rides()`

```
Load Rides → Read all GPX/TCX files + databases
           → Deduplicate (same ride in both formats)
           → Return list[Ride], failed entries, duplicate count
```

Deduplication matches on the timestamp of the first GPS fix (within a ±2 minute window). The database copy wins because it carries metadata (bike name, category).

### 2. Ride to Cells

A `Ride` is a set of GPS coordinates with metadata:

```python
@dataclass
class Ride:
    segments: list[list[tuple[float, float]]]  # (lat, lon) arrays
    start_ms: int                               # epoch ms
    bike: str                                   # bike name or "Unknown"
    category: str                               # activity type ("Category 0", etc.)
    first_fix_ms: int                           # first GPS timestamp
```

Each ride's path is converted to a set of grid cells:

```
ride_cells(ride) → 
  For each segment:
    Densify (interpolate gaps > 5 m)
    For each (lat, lon):
      Convert to Web-Mercator at BASE_LEVEL (level 21)
      Add cell (x, y) to set
  Return frozenset of cells
```

**Why Web-Mercator?** It nests perfectly: cells at level *L* are 2^(L−21) times larger than base-level cells, so a level 18 cell contains exactly 8×8=64 base cells. This lets the same API serve all zoom levels efficiently.

**Interpolation:** Tracks with gaps > 5 m are interpolated (to avoid teleports when recordings pause) but not connected across >2 km jumps (bad fixes, etc.).

### 3. Day Grouping & Cell Counts

All cells from a single ride on the same calendar day are grouped together:

```
by_day[ride.day] = set of cells
```

Then, for each unique cell, count how many distinct days it was ridden:

```
cell_counts = {}
for day_cells in by_day.values():
    for cell in day_cells:
        cell_counts[cell] += 1
```

A cell ridden on 3 separate days has count = 3, even if one day had 10 passes through it.

### 4. Heatmap Points

Cells are converted to visual points:

```
points_from_counts(counts, level, passes=[]) →
  busiest = max(counts.values())
  top = log(1 + busiest)
  
  for (x, y), count in counts:
    if passes and pass_bucket(count) not in passes:
      continue  # Filter by "1 day" / "2-5 days" / "6+ days"
    
    lat, lon = inverse_mercator(x, y)
    weight = log(1 + count) / top
    yield HeatPoint(lat, lon, weight)
```

**Logarithmic scale:** One day gives ~0.3, five days ~0.7, ten days ~0.95. This keeps rare cells visible while highlighting regulars.

**Colour stability:** Normalisation happens against *all* cells before filtering, so hiding the "6+ days" band doesn't recolour the remaining cells.

## Grid System

### Levels

- **BASE_LEVEL = 21**: ~19 m square at equator, ~12 m at 52°N
- **MIN_LEVEL = 6**: ~2 km square
- **DEFAULT_LEVEL = 18**: ~150 m square
- **MAX = BASE_LEVEL**: Finest detail

Cells nest perfectly: level *L* has cells that are 2^(21−*L*) base cells wide, squared.

### Coarsening

When the user zooms out or requests level *L* < 21:

```
coarsen(base_cells, level) →
  shift = 21 - L
  return {(x >> shift, y >> shift) for (x, y) in base_cells}
```

This groups every 2^shift × 2^shift base cells into one coarser cell.

## Filtering

Three orthogonal dimensions:

1. **Bike** (ride.bike): Which bicycle was used
2. **Category** (ride.category): Activity type (database field)
3. **Year** (ride.year): Calendar year extracted from timestamp
4. **Days ridden** (pass_bucket): How many distinct days ridden (1, 2–5, 6+)

Within a group: OR logic (Ridley OR MTB)  
Across groups: AND logic (Ridley AND 2024 AND "6+ days")

### Pass Buckets

```python
PASS_BUCKETS = (
    ("1 day", 1, 1),
    ("2-5 days", 2, 5),
    ("6+ days", 6, None),
)
```

Disjoint bands. A cell with count 1 goes into "1 day", count 3 into "2-5 days", count 10 into "6+ days". The bands always sum to the total.

## Backend (Flask)

### `/` (GET)

Serves `index.html` with jinja2 context (for server mode vs. static export).

### `/api/heatmap` (GET)

Query parameters:
- `level`: Grid level (clamped to MIN_LEVEL..BASE_LEVEL)
- `bike`, `cat`, `year`: Facet filters (repeatable, OR within group)
- `pass`: Pass bucket filter ("1 day", "2-5 days", "6+ days")

Returns (gzipped JSON):

```json
{
  "points": [[lat, lon, weight], ...],
  "bounds": [[south, west], [north, east]],
  "rides": 45,
  "days": 30,
  "totalRides": 228,
  "failed": [{"file": "...", "error": "..."}],
  "facets": {
    "bikes": [["MTB", 22], ["Ridley", 152], ...],
    "categories": [["Category 1", 120], ...],
    "years": [["2024", 150], ["2023", 78]],
    "passes": [["1 day", 35125], ["2-5 days", 7716], ["6+ days", 2664]]
  }
}
```

**Caching:** Keyed by (file signature, level, bikes, categories, years, passes). Invalidated when files change.

## Frontend (JavaScript)

### Dynamic Updates

```
User action (zoom, filter, slider) 
  → loadHeatmap() 
  → fetch /api/heatmap?...
  → renderHeat()
  → renderFilters()
```

### Rendering

Each point is a canvas circle filled with HSL colour (green → red). Points are drawn in count order (busiest last) so overlaps don't add up. Then the whole canvas is blurred for a smooth effect.

```javascript
ctx.filter = `blur(${blur}px)`;
for (const [lat, lon, weight] of points) {
  hue = (1 - weight) * 120;  // red at 1, green at 0
  ctx.fillStyle = `hsl(${hue}, 100%, 50%)`;
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, 2*Math.PI);
  ctx.fill();
}
```

### Filter UI

Built dynamically from the facets returned by the API. Filters work both in click mode (reload on change) and debounced slider mode (cell size).

## Static Export

### Problem Solved

The server needs ~475 MB RAM to build a response at max zoom. That's too much for free tiers. Solution: precompute everything and ship to the browser.

### Encoding

All rides are packed into one binary file using delta-encoding and zigzag varints:

```
for ride in rides:
  for cell in sorted_cells:
    dx = cell.x - previous.x
    dy = cell.y - previous.y
    varint(zigzag(dx))
    varint(zigzag(dy))
    previous = cell
```

Result: **626 KB gzipped** for 763k cells across 228 rides. Served as `data/rides.bin`.

Metadata goes in `data/rides.json`:

```json
{
  "baseLevel": 21,
  "rides": [[dayIndex, bikeIndex, catIndex, cellCount], ...],
  "days": ["2024-06-01", ...],
  "bikes": ["MTB", "Ridley", ...],
  "categories": ["Category 0", ...]
}
```

### Browser-Side Aggregation

The browser decodes the binary and computes what the server used to:

```javascript
async function staticHeatmap() {
  const blob = await fetch('data/rides.bin').then(r => r.arrayBuffer());
  const meta = await fetch('data/rides.json').then(r => r.json());
  
  const rides = decode(blob, meta);  // varint → cells
  const counts = {};
  for (const ride of rides) {
    if (!passesFilter(ride)) continue;
    for (const cell of ride.cells) {
      counts[cell] += 1;
    }
  }
  return points_from_counts(counts, level, passes);
}
```

The static export is built by `export_static.py` and deployed to a branch with no history (`gh-pages`), keeping source and output separate.

## Privacy Zone

Before export, cells within a radius of a point can be dropped:

```
.venv/bin/python export_static.py --hide 52.39655,4.64584,300
```

Cells are identified by checking if their centre lies within the circle:

```
cells_within(lat, lon, radius_m) →
  for x in x_range:
    for y in y_range:
      cell_centre_lat, cell_centre_lon = mercator(x, y)
      if haversine(lat, lon, cell_centre_lat, cell_centre_lon) <= radius:
        blocked.add((x, y))
```

Then, when encoding rides, blocked cells are omitted entirely — they never reach the export file.

## Testing

68 tests covering:
- **Parsing**: GPX, TCX, databases
- **Geometry**: Cell nesting, interpolation, coarsening
- **Aggregation**: Day grouping, counting, filtering
- **Encoding**: Varint round-trips, cell recovery
- **API**: Gzipped responses, facets, filters
- **Static build**: Site generation, no server leakage
- **Privacy**: Zone geometry, no blocked cells survive export

Run with:
```sh
.venv/bin/python -m pytest
```

## Architecture Decisions

### Why Web-Mercator?
Standard for web maps. Cells nest perfectly across zoom levels, so the same grid structure works from 2 km to 12 m.

### Why log scaling for weight?
A single day must stay visible (weight > 0) or it looks like you never rode there. Log scale compresses the range so one day ≈ 0.3, ten days ≈ 0.95, avoiding a narrow top-heavy range.

### Why count distinct days, not frequency?
Tells you where you *regularly* go, not where you ride *hardest*. A quiet commute you do 200 times glows brighter than an epic weekend ride, which is the point.

### Why delta-encoding?
GPS coordinates cluster geographically, so deltas are small. Varint-packing then gets 50% compression even before gzip, reducing the binary 30% more.

### Why no streaming response?
The browser needs the full points array to normalise weights (for colour stability across filter changes). Building a streaming format would complicate caching and wasn't worth it for a 2 MB file.

## Deployment

### Local Server
```sh
.venv/bin/python app.py
# http://127.0.0.1:5000
```

### GitHub Pages (Free, Public)
```sh
.venv/bin/python export_static.py --hide LAT,LON,RADIUS
cd ../bike-heatmap-pages
git add -A && git commit -m "..." && git push origin gh-pages
# https://username.github.io/bike-heatmap/
```

### Other Hosts
Any static host works (Netlify, Cloudflare Pages, etc.). Upload `dist/`.

## Future Improvements

- **Heatmap radius** per zoom level (automatic or user-configurable)
- **Elevation coloring** if available from tracks
- **Time-of-day filter** (morning rides, evening commutes)
- **Speed-based weighting** (fast = more weight)
- **KML export** for use in other tools
- **Multi-user** (combine rides from multiple bikes/users)
