import argparse
import json
import math
from datetime import datetime

import pytest

from export_static import DEFAULT_HIDE_RADIUS_M, build, parse_zone
from heatmap.dataset import encode_rides
from heatmap.tracks import (
    BASE_LEVEL,
    Ride,
    _haversine as haversine,
    _inverse_mercator,
    _mercator,
    cells_within,
    ride_cells,
)

from .conftest import millis, write_gpx, write_session_db

START = datetime(2024, 6, 1, 9, 0)
ROAD = [(52.0 + i * 0.0002, 4.0 + i * 0.0002) for i in range(8)]
# Starts at the same place but runs ~1.3 km north, well clear of a 300 m zone.
LONG_ROAD = [(52.0 + i * 0.0002, 4.0) for i in range(60)]


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    scale = 1 << BASE_LEVEL
    x, y = _mercator(lat, lon)
    return int(x * scale), int(y * scale)


def cell_centre(x: int, y: int) -> tuple[float, float]:
    scale = 1 << BASE_LEVEL
    return _inverse_mercator((x + 0.5) / scale, (y + 0.5) / scale)


def decode(blob: bytes, counts: list[int]) -> list[set[tuple[int, int]]]:
    """Mirror of the browser's varint reader, used to prove the encoding round-trips."""
    cursor = 0

    def read() -> int:
        nonlocal cursor
        result = 0
        shift = 0
        while True:
            byte = blob[cursor]
            cursor += 1
            result |= (byte & 127) << shift
            if not byte & 128:
                return result
            shift += 7

    rides = []
    for count in counts:
        cells = set()
        x = y = 0
        for _ in range(count):
            dx = read()
            dy = read()
            x += -(dx + 1) // 2 if dx % 2 else dx // 2
            y += -(dy + 1) // 2 if dy % 2 else dy // 2
            cells.add((x, y))
        rides.append(cells)
    return rides


class TestEncoding:
    def test_cells_survive_a_round_trip(self):
        rides = [
            Ride(segments=[ROAD], start_ms=millis(START), bike="Ridley"),
            Ride(segments=[[(48.0, 11.0), (48.001, 11.001)]], start_ms=millis(START)),
        ]

        blob, meta = encode_rides(rides)
        decoded = decode(blob, [entry[2] for entry in meta["rides"]])

        assert decoded == [ride_cells(ride) for ride in rides]

    def test_metadata_is_interned(self):
        rides = [
            Ride(segments=[ROAD], start_ms=millis(START), bike="Ridley"),
            Ride(segments=[ROAD], start_ms=millis(START), bike="MTB"),
        ]

        _, meta = encode_rides(rides)

        assert meta["bikes"] == ["Ridley", "MTB"]
        assert meta["days"] == ["2024-06-01"]
        assert all(len(entry) == 3 for entry in meta["rides"])  # [dayIndex, bikeIndex, cellCount]

    def test_base_level_is_recorded(self):
        _, meta = encode_rides([Ride(segments=[ROAD], start_ms=millis(START))])

        assert meta["baseLevel"] == BASE_LEVEL

    def test_rides_without_cells_are_skipped(self):
        blob, meta = encode_rides([Ride(segments=[], start_ms=millis(START))])

        assert blob == b""
        assert meta["rides"] == []


class TestBuild:
    def test_writes_a_self_contained_site(self, tmp_path, tracks_dir, db_dir):
        write_gpx(tracks_dir / "ride.gpx", ROAD, start=START)
        dest = tmp_path / "dist"

        result = build(dest, tracks_dir, db_dir)

        assert result["rides"] == 1
        assert (dest / "index.html").exists()
        assert (dest / "static" / "app.js").exists()
        assert (dest / "static" / "style.css").exists()
        assert (dest / "data" / "rides.bin").exists()
        assert json.loads((dest / "data" / "rides.json").read_text())["rides"]

    def test_page_points_at_the_data_and_drops_upload_controls(self, tmp_path, tracks_dir, db_dir):
        write_gpx(tracks_dir / "ride.gpx", ROAD, start=START)

        build(tmp_path / "dist", tracks_dir, db_dir)
        html = (tmp_path / "dist" / "index.html").read_text()

        assert "window.STATIC_DATA = 'data/'" in html
        assert 'src="static/app.js"' in html
        # Nothing may point at the removed server.
        assert "/api/" not in html
        assert "upload-tracks" not in html
        assert "id=\"reload\"" not in html

    def test_database_rides_are_exported_with_metadata(self, tmp_path, tracks_dir, db_dir):
        write_session_db(
            db_dir / "sessions.db",
            sessions=[
                {
                    "id": 1,
                    "start": millis(START),
                    "cat": 1,
                    "used_bike": 7,
                    "points": [
                        (lat, lon, millis(START) + i * 1000) for i, (lat, lon) in enumerate(ROAD)
                    ],
                }
            ],
            bikes=[(7, "Ridley")],
        )

        build(tmp_path / "dist", tracks_dir, db_dir)
        meta = json.loads((tmp_path / "dist" / "data" / "rides.json").read_text())

        assert meta["bikes"] == ["Ridley"]
        # categories removed; no longer tracked

    def test_empty_input_is_refused(self, tmp_path, tracks_dir, db_dir):
        with pytest.raises(SystemExit):
            build(tmp_path / "dist", tracks_dir, db_dir)


class TestPrivacyZone:
    HOME = (52.0, 4.0)

    def test_zone_covers_the_radius_and_no_more(self):
        blocked = cells_within(*self.HOME, 300)

        assert self.HOME[0], "sanity"
        # Every blocked cell centre must sit inside the circle, with a cell of slack.
        for x, y in blocked:
            lat, lon = cell_centre(x, y)
            assert haversine(*self.HOME, lat, lon) <= 300
        # And a point well outside must not be blocked.
        assert cell_of(52.01, 4.01) not in blocked

    def test_cells_near_home_are_dropped(self):
        ride = Ride(segments=[LONG_ROAD], start_ms=millis(START))
        everything = ride_cells(ride)
        blocked = cells_within(*self.HOME, 300)

        _, meta = encode_rides([ride], blocked)

        assert meta["hiddenCells"] > 0
        assert meta["rides"][0][2] == len(everything - blocked)

    def test_export_leaves_no_hidden_cell_in_the_data(self, tmp_path, tracks_dir, db_dir):
        write_gpx(tracks_dir / "ride.gpx", LONG_ROAD, start=START)
        blocked = cells_within(*self.HOME, 300)

        build(tmp_path / "dist", tracks_dir, db_dir, hide=[(*self.HOME, 300)])
        meta = json.loads((tmp_path / "dist" / "data" / "rides.json").read_text())
        blob = (tmp_path / "dist" / "data" / "rides.bin").read_bytes()
        exported = set().union(*decode(blob, [entry[2] for entry in meta["rides"]]))

        assert exported
        assert exported & blocked == set()

    def test_a_ride_that_never_leaves_the_zone_disappears(self, tmp_path, tracks_dir, db_dir):
        write_gpx(tracks_dir / "ride.gpx", [self.HOME, (52.0001, 4.0001)], start=START)

        with pytest.raises(SystemExit, match="private zones"):
            build(tmp_path / "dist", tracks_dir, db_dir, hide=[(*self.HOME, 300)])

    def test_no_zone_means_nothing_is_hidden(self):
        _, meta = encode_rides([Ride(segments=[ROAD], start_ms=millis(START))])

        assert meta["hiddenCells"] == 0


class TestZoneParsing:
    def test_radius_defaults_when_omitted(self):
        assert parse_zone("52.0,4.0") == (52.0, 4.0, DEFAULT_HIDE_RADIUS_M)

    def test_explicit_radius_is_used(self):
        assert parse_zone("52.0,4.0,500") == (52.0, 4.0, 500.0)

    @pytest.mark.parametrize("value", ["52.0", "a,b", "52.0,4.0,1,2", "95.0,4.0", "52.0,200.0"])
    def test_bad_input_is_rejected(self, value):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_zone(value)
