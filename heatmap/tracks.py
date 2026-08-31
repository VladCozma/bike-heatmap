"""Load GPS tracks and aggregate them into weighted heatmap points."""

from __future__ import annotations

import math
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import gpxpy

FILE_SUFFIXES = {".gpx", ".tcx"}
DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}

# Cells live on a Web-Mercator grid; the base level is ~19 m at the equator, ~12 m at 52°N.
BASE_LEVEL = 21
MIN_LEVEL = 6
DEFAULT_LEVEL = 18
# Straight-line gaps longer than this get interpolated so tracks stay continuous.
INTERPOLATION_STEP_METERS = 5.0
EARTH_RADIUS_M = 6_371_000.0
# Bike-computer databases store coordinates as microdegrees.
MICRODEGREES = 1e-6
# A pause longer than this starts a new segment instead of a straight connecting line.
SEGMENT_GAP_SECONDS = 600

UNKNOWN = "Unknown"
# The database stores activity categories as plain integers; rename them here if you like.
CATEGORY_LABELS: dict[int, str] = {}
# Disjoint bands of "days ridden", used to filter the map by how well travelled a cell is.
PASS_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1 day", 1, 1),
    ("2-5 days", 2, 5),
    ("6+ days", 6, None),
)


@dataclass(frozen=True)
class HeatPoint:
    lat: float
    lon: float
    weight: float


@dataclass
class Ride:
    segments: list[list[tuple[float, float]]] = field(repr=False)
    start_ms: int | None = None
    bike: str = UNKNOWN
    category: str = UNKNOWN
    source: str = ""
    # Time of the first GPS fix; a session's stored start time can differ from its export.
    first_fix_ms: int | None = None

    @property
    def year(self) -> str:
        if self.start_ms is None:
            return UNKNOWN
        return str(datetime.fromtimestamp(self.start_ms / 1000).year)

    @property
    def day(self) -> str:
        """Calendar day used to group rides, so one day never counts twice."""
        if self.start_ms is None:
            return f"file:{self.source}"
        return datetime.fromtimestamp(self.start_ms / 1000).strftime("%Y-%m-%d")


def category_label(cat: int | None) -> str:
    if cat is None:
        return UNKNOWN
    return CATEGORY_LABELS.get(cat, f"Category {cat}")


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _gpx_start_ms(gpx) -> int | None:
    times = [p.time for p in gpx.walk(only_points=True) if p.time is not None]
    return int(min(times).timestamp() * 1000) if times else None


def _parse_gpx(path: Path) -> tuple[list[list[tuple[float, float]]], int | None]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        gpx = gpxpy.parse(handle)

    segments: list[list[tuple[float, float]]] = []
    for track in gpx.tracks:
        for segment in track.segments:
            pts = [(p.latitude, p.longitude) for p in segment.points]
            if pts:
                segments.append(pts)
    for route in gpx.routes:
        pts = [(p.latitude, p.longitude) for p in route.points]
        if pts:
            segments.append(pts)
    return segments, _gpx_start_ms(gpx)


def _parse_tcx(path: Path) -> tuple[list[list[tuple[float, float]]], int | None]:
    ns = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    root = ET.parse(path).getroot()  # noqa: S314 - local, user-provided files
    segments: list[list[tuple[float, float]]] = []
    for lap in root.iterfind(".//tcx:Lap", ns):
        pts: list[tuple[float, float]] = []
        for tp in lap.iterfind(".//tcx:Trackpoint/tcx:Position", ns):
            lat = tp.findtext("tcx:LatitudeDegrees", namespaces=ns)
            lon = tp.findtext("tcx:LongitudeDegrees", namespaces=ns)
            if lat and lon:
                pts.append((float(lat), float(lon)))
        if pts:
            segments.append(pts)
    return segments, None


def read_track(path: Path) -> Ride:
    """Read a single GPX/TCX file into a ride."""
    suffix = path.suffix.lower()
    if suffix == ".gpx":
        segments, start_ms = _parse_gpx(path)
    elif suffix == ".tcx":
        segments, start_ms = _parse_tcx(path)
    else:
        raise ValueError(f"Unsupported track format: {path.suffix}")
    return Ride(segments=segments, start_ms=start_ms, source=path.name, first_fix_ms=start_ms)


def read_session_db(path: Path):
    """Yield one `Ride` per recorded session in a bike-computer database.

    Expects the Android bike-computer layout: a `sessions` table plus a `tracks`
    table holding `lat`/`lon` microdegrees, `time` epoch millis and `session_id`.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"sessions", "tracks"} <= tables:
            raise ValueError("Database has no 'sessions'/'tracks' tables")

        bike_expression = "COALESCE(NULLIF(s.bike, ''), b.name)" if "bikes" in tables else "s.bike"
        join = "LEFT JOIN bikes b ON b._id = s.used_bike" if "bikes" in tables else ""
        meta = {
            row[0]: (row[1], row[2], row[3])
            for row in connection.execute(
                f"SELECT s._id, s.starttime, s.cat, {bike_expression} FROM sessions s {join}"
            )
        }

        rows = connection.execute(
            "SELECT session_id, lat, lon, time FROM tracks "
            "WHERE lat IS NOT NULL AND lon IS NOT NULL AND (lat <> 0 OR lon <> 0) "
            "ORDER BY session_id, time, _id"
        )

        session_id = None
        segments: list[list[tuple[float, float]]] = []
        segment: list[tuple[float, float]] = []
        previous_time: int | None = None
        first_time: int | None = None

        def build() -> Ride | None:
            if not segments:
                return None
            starttime, cat, bike = meta.get(session_id, (None, None, None))
            return Ride(
                segments=list(segments),
                start_ms=starttime or first_time,
                bike=bike or UNKNOWN,
                category=category_label(cat),
                source=path.name,
                first_fix_ms=first_time,
            )

        for row_session, lat, lon, timestamp in rows:
            if row_session != session_id:
                if segment:
                    segments.append(segment)
                ride = build()
                if ride:
                    yield ride
                session_id, segments, segment, previous_time = row_session, [], [], None
                first_time = timestamp
            elif (
                previous_time is not None
                and timestamp is not None
                and abs(timestamp - previous_time) > SEGMENT_GAP_SECONDS * 1000
            ):
                segments.append(segment)
                segment = []

            segment.append((lat * MICRODEGREES, lon * MICRODEGREES))
            previous_time = timestamp

        if segment:
            segments.append(segment)
        ride = build()
        if ride:
            yield ride
    finally:
        connection.close()


def load_rides(tracks_dir: Path, db_dir: Path) -> tuple[list[Ride], list[dict], int]:
    """Collect every ride from the databases and track files, dropping duplicates."""
    rides: list[Ride] = []
    failed: list[dict] = []
    starts: set[int] = set()
    duplicates = 0

    def accept(ride: Ride) -> None:
        nonlocal duplicates
        if not ride.segments:
            return
        stamp = ride.first_fix_ms or ride.start_ms
        if stamp is not None:
            # The same ride often exists both in the session database and as a GPX export.
            minute = stamp // 60_000
            if starts & {minute - 2, minute - 1, minute, minute + 1, minute + 2}:
                duplicates += 1
                return
            starts.add(minute)
        rides.append(ride)

    # Databases come first so their richer metadata wins over plain GPX exports.
    for path in sorted(db_dir.rglob("*")) if db_dir.is_dir() else []:
        if path.is_file() and path.suffix.lower() in DB_SUFFIXES:
            try:
                for ride in read_session_db(path):
                    accept(ride)
            except Exception as exc:  # noqa: BLE001 - one bad source must not kill the map
                failed.append({"file": path.name, "error": str(exc)})

    for path in sorted(tracks_dir.rglob("*")) if tracks_dir.is_dir() else []:
        if path.is_file() and path.suffix.lower() in FILE_SUFFIXES:
            try:
                accept(read_track(path))
            except Exception as exc:  # noqa: BLE001
                failed.append({"file": path.name, "error": str(exc)})

    return rides, failed, duplicates


def _densify(segment: list[tuple[float, float]], step_m: float):
    previous: tuple[float, float] | None = None
    for lat, lon in segment:
        if previous is not None:
            distance = _haversine(previous[0], previous[1], lat, lon)
            if distance > step_m:
                # Skip teleports (paused recordings, bad fixes) instead of drawing through them.
                if distance < 2_000:
                    steps = int(distance // step_m)
                    for i in range(1, steps):
                        f = i / steps
                        yield (
                            previous[0] + (lat - previous[0]) * f,
                            previous[1] + (lon - previous[1]) * f,
                        )
        yield (lat, lon)
        previous = (lat, lon)


def _mercator(lat: float, lon: float) -> tuple[float, float]:
    """Web-Mercator coordinates in the unit square, so grids at different levels nest."""
    x = (lon + 180.0) / 360.0
    s = math.sin(math.radians(max(min(lat, 85.05112878), -85.05112878)))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


def _inverse_mercator(x: float, y: float) -> tuple[float, float]:
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))
    return lat, lon


def ride_cells(ride: Ride) -> frozenset[tuple[int, int]]:
    """The distinct base-level grid cells a ride passes through."""
    scale = 1 << BASE_LEVEL
    visited: set[tuple[int, int]] = set()
    for segment in ride.segments:
        for lat, lon in _densify(segment, INTERPOLATION_STEP_METERS):
            x, y = _mercator(lat, lon)
            visited.add((int(x * scale), int(y * scale)))
    return frozenset(visited)


def coarsen(cells: frozenset[tuple[int, int]], level: int) -> set[tuple[int, int]]:
    """Project base-level cells onto a coarser grid level."""
    shift = BASE_LEVEL - clamp_level(level)
    if shift <= 0:
        return set(cells)
    return {(x >> shift, y >> shift) for x, y in cells}


def cells_within(lat: float, lon: float, radius_m: float) -> set[tuple[int, int]]:
    """Every base-level cell whose centre lies within `radius_m` of a point."""
    scale = 1 << BASE_LEVEL
    lat_margin = radius_m / 111_320.0
    lon_margin = lat_margin / max(math.cos(math.radians(lat)), 1e-6)

    corners = [
        _mercator(lat - lat_margin, lon - lon_margin),
        _mercator(lat + lat_margin, lon + lon_margin),
    ]
    xs = sorted(int(x * scale) for x, _ in corners)
    ys = sorted(int(y * scale) for _, y in corners)

    blocked = set()
    for x in range(xs[0], xs[1] + 1):
        for y in range(ys[0], ys[1] + 1):
            centre_lat, centre_lon = _inverse_mercator((x + 0.5) / scale, (y + 0.5) / scale)
            if _haversine(lat, lon, centre_lat, centre_lon) <= radius_m:
                blocked.add((x, y))
    return blocked


def clamp_level(level: int) -> int:
    return max(MIN_LEVEL, min(int(level), BASE_LEVEL))


def cell_counts(day_cells) -> dict[tuple[int, int], int]:
    """How many distinct days each grid cell was ridden."""
    counts: dict[tuple[int, int], int] = {}
    for cells in day_cells:
        for cell in cells:
            counts[cell] = counts.get(cell, 0) + 1
    return counts


def pass_bucket(count: int) -> str:
    for label, low, high in PASS_BUCKETS:
        if count >= low and (high is None or count <= high):
            return label
    return PASS_BUCKETS[-1][0]


def pass_histogram(counts: dict[tuple[int, int], int]) -> list[list]:
    """Cells per bucket, always listing every bucket so the filter stays stable."""
    tally = {label: 0 for label, _, _ in PASS_BUCKETS}
    for count in counts.values():
        tally[pass_bucket(count)] += 1
    return [[label, tally[label]] for label, _, _ in PASS_BUCKETS]


def points_from_counts(
    counts: dict[tuple[int, int], int], level: int, passes=()
) -> list[HeatPoint]:
    """Turn day counts into weighted points, optionally keeping only some buckets."""
    if not counts:
        return []

    scale = 1 << clamp_level(level)
    # Normalise against every cell, so hiding buckets does not recolour the rest.
    top = math.log1p(max(counts.values()))
    wanted = set(passes)
    result: list[HeatPoint] = []
    for (x, y), count in counts.items():
        if wanted and pass_bucket(count) not in wanted:
            continue
        lat, lon = _inverse_mercator((x + 0.5) / scale, (y + 0.5) / scale)
        result.append(HeatPoint(lat, lon, math.log1p(count) / top))
    return result


def heat_points(day_cells, level: int, passes=()) -> list[HeatPoint]:
    """Weight each grid cell by the number of distinct days it was ridden, normalised to 0..1."""
    return points_from_counts(cell_counts(day_cells), level, passes)
