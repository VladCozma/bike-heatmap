# Bike Heatmap

A local web app that draws a heatmap of your bike rides. Colour runs from green to
red based on **how many separate days** you rode through a spot, so a route you
take every week glows red while a one-off detour stays green.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running

```sh
.venv/bin/python app.py
```

Then open <http://127.0.0.1:5000>. The server only listens on localhost.

## Adding rides

Two sources are supported, and you can mix them freely:

| What | Where it goes | How to add it |
| --- | --- | --- |
| `.gpx` / `.tcx` files | `tracks/` | Copy them in, or use **Load GPX / TCX tracks** |
| Bike computer SQLite backup (`.db`) | `databases/` | Copy it in, or use **Load bike computer DB** |

After copying files in by hand, press **Reload**. Uploads through the buttons
refresh the map automatically.

A database is expected to have the Android bike-computer (<https://bikecomputer.roproducts.de>) layout: a `sessions`
table plus a `tracks` table with `lat`/`lon` stored as microdegrees. Each session
becomes one ride, and the bike name and category are read from `used_bike` and
`cat`.

If the same ride exists both as a session in the database and as a GPX export,
it is counted once. Duplicates are matched on the time of the first GPS fix, and
the database copy wins because it carries more metadata.

## Controls

- **Segment width** — how thick the drawn track is, in pixels.
- **Fuzziness** — how much the finished image is blurred.
- **Cell size** — the size of one grid square in pixels. Smaller shows more
  detail; larger merges parallel lanes and GPS scatter into a single line.
- **Dark basemap** — inverts the OpenStreetMap tiles.
- **Bike / Category / Year** — tick values to narrow the map. Ticks within one
  group are combined with OR, and the three groups are combined with AND. The
  number next to each value is how many rides carry it.

The grid follows the zoom level, so zooming in re-fetches finer cells. Detail
stops increasing past roughly zoom 16, which is the finest grid that gets
computed (about 12 m squares at mid latitudes).

## How the colouring works

Every ride is reduced to the set of grid cells it passes through, so riding the
same street three times in one day still counts as one visit. Cells are then
grouped by calendar day and each cell is weighted by its number of distinct
days, on a log scale so a single day stays visible.

Rendering paints one flat-coloured disc per cell, drawing the busiest cells last
so overlapping discs never add up, then blurs the whole canvas once. That keeps a
stretch of road a single uniform colour instead of shading it by how densely the
points happen to sit.

## Category names

The database stores categories as bare integers with no labels, so they appear as
"Category 0", "Category 1" and so on. To give them real names, fill in
`CATEGORY_LABELS` at the top of `heatmap/tracks.py`:

```python
CATEGORY_LABELS = {0: "Training", 1: "Tour", 2: "MTB"}
```

## Tests

```sh
.venv/bin/python -m pytest
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRACKS_DIR` | `./tracks` | Where GPX/TCX files are read from and uploaded to |
| `DB_DIR` | `./databases` | Where `.db` backups are read from and uploaded to |

## Notes

Your ride data never leaves your machine; only map tiles are fetched, from
OpenStreetMap. `tracks/` and `databases/` contents are gitignored.

The app runs Flask's development server, which is fine for local use but should
not be exposed to a network.
