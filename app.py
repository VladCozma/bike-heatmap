"""Flask app that renders a heatmap of bike rides from local GPS tracks."""

from __future__ import annotations

import gzip
import json
import os
from collections import Counter
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from heatmap.tracks import (
    DB_SUFFIXES,
    DEFAULT_LEVEL,
    FILE_SUFFIXES,
    cell_counts,
    clamp_level,
    coarsen,
    load_rides,
    pass_histogram,
    points_from_counts,
    ride_cells,
)

BASE_DIR = Path(__file__).resolve().parent
TRACKS_DIR = Path(os.environ.get("TRACKS_DIR", BASE_DIR / "tracks")).resolve()
DB_DIR = Path(os.environ.get("DB_DIR", BASE_DIR / "databases")).resolve()
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

_rides_cache: dict[tuple, tuple] = {}
_cells_cache: dict[tuple, list] = {}
_response_cache: dict[tuple, bytes] = {}


def _signature() -> tuple:
    entries = []
    for directory, suffixes in ((TRACKS_DIR, FILE_SUFFIXES), (DB_DIR, DB_SUFFIXES)):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in suffixes:
                stat = path.stat()
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def _rides(signature: tuple):
    cached = _rides_cache.get(signature)
    if cached is None:
        TRACKS_DIR.mkdir(parents=True, exist_ok=True)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        cached = load_rides(TRACKS_DIR, DB_DIR)
        _rides_cache.clear()
        _cells_cache.clear()
        _response_cache.clear()
        _rides_cache[signature] = cached
    return cached


def _cells(signature: tuple, rides):
    cached = _cells_cache.get(signature)
    if cached is None:
        cached = [ride_cells(ride) for ride in rides]
        _cells_cache.clear()
        _cells_cache[signature] = cached
    return cached


def _facets(rides) -> dict:
    return {
        "bikes": sorted(Counter(r.bike for r in rides).items()),
        # "categories": sorted(Counter(r.category for r in rides).items()),
        "years": sorted(Counter(r.year for r in rides).items(), reverse=True),
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/heatmap")
def api_heatmap():
    try:
        level = clamp_level(int(request.args.get("level", DEFAULT_LEVEL)))
    except ValueError:
        level = DEFAULT_LEVEL

    bikes = set(request.args.getlist("bike"))
    categories = set(request.args.getlist("cat"))
    years = set(request.args.getlist("year"))
    passes = set(request.args.getlist("pass"))

    signature = _signature()
    cache_key = (
        signature,
        level,
        tuple(sorted(bikes)),
        tuple(sorted(categories)),
        tuple(sorted(years)),
        tuple(sorted(passes)),
    )
    payload = _response_cache.get(cache_key)
    if payload is None:
        rides, failed, duplicates = _rides(signature)
        cells = _cells(signature, rides)

        selected = 0
        by_day: dict[str, set] = {}
        for index, ride in enumerate(rides):
            if (
                (not bikes or ride.bike in bikes)
                and (not categories or ride.category in categories)
                and (not years or ride.year in years)
            ):
                selected += 1
                by_day.setdefault(ride.day, set()).update(coarsen(cells[index], level))

        counts = cell_counts(by_day.values())
        points = points_from_counts(counts, level, passes)
        bounds = None
        if points:
            lats = [p.lat for p in points]
            lons = [p.lon for p in points]
            bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]

        payload = gzip.compress(
            json.dumps(
                {
                    "points": [
                        [round(p.lat, 5), round(p.lon, 5), round(p.weight, 3)] for p in points
                    ],
                    "bounds": bounds,
                    "rides": selected,
                    "days": len(by_day),
                    "totalRides": len(rides),
                    "duplicates": duplicates,
                    "failed": failed,
                    "facets": _facets(rides) | {"passes": pass_histogram(counts)},
                }
            ).encode(),
            5,
        )
        if len(_response_cache) > 8:
            _response_cache.clear()
        _response_cache[cache_key] = payload

    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(payload))},
    )


def _save_uploads(directory: Path, allowed: set[str]) -> Response:
    directory.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []

    for storage in request.files.getlist("files"):
        name = secure_filename(storage.filename or "")
        if not name or Path(name).suffix.lower() not in allowed:
            rejected.append(storage.filename or "(unnamed)")
            continue
        target = (directory / name).resolve()
        if target.parent != directory:
            rejected.append(name)
            continue
        storage.save(target)
        saved.append(name)

    return jsonify({"saved": saved, "rejected": rejected})


@app.post("/api/tracks")
def upload_tracks():
    return _save_uploads(TRACKS_DIR, FILE_SUFFIXES)


@app.post("/api/databases")
def upload_databases():
    return _save_uploads(DB_DIR, DB_SUFFIXES)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
