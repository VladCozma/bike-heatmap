import sqlite3
from datetime import datetime, timedelta

import pytest

from heatmap.tracks import (
    BASE_LEVEL,
    MIN_LEVEL,
    UNKNOWN,
    Ride,
    clamp_level,
    coarsen,
    heat_points,
    load_rides,
    pass_bucket,
    pass_histogram,
    points_from_counts,
    read_session_db,
    read_track,
    ride_cells,
)

from .conftest import millis, write_gpx, write_session_db

HOME = (52.0, 4.0)
NEXT_STREET = (52.002, 4.002)


def line_from(origin, count=6, step=0.0002):
    lat, lon = origin
    return [(lat + i * step, lon + i * step) for i in range(count)]


def ride_from(coords, **kwargs) -> Ride:
    return Ride(segments=[list(coords)], **kwargs)


class TestGpx:
    def test_reads_points_and_start_time(self, tracks_dir):
        start = datetime(2024, 5, 1, 8, 30)
        path = write_gpx(tracks_dir / "ride.gpx", line_from(HOME), start=start)

        ride = read_track(path)

        assert len(ride.segments) == 1
        assert len(ride.segments[0]) == 6
        assert ride.segments[0][0] == pytest.approx(HOME)
        assert ride.start_ms == pytest.approx(millis(start), abs=1000)
        assert ride.bike == UNKNOWN
        assert ride.source == "ride.gpx"

    def test_missing_timestamps_leave_start_unset(self, tracks_dir):
        path = write_gpx(tracks_dir / "no-time.gpx", line_from(HOME))

        ride = read_track(path)

        assert ride.start_ms is None
        assert ride.year == UNKNOWN
        # Undated files still group separately rather than collapsing into one day.
        assert ride.day == "file:no-time.gpx"

    def test_unsupported_extension_is_rejected(self, tracks_dir):
        path = tracks_dir / "ride.fit"
        path.write_bytes(b"")

        with pytest.raises(ValueError, match="Unsupported track format"):
            read_track(path)


class TestSessionDatabase:
    def test_reads_metadata_and_coordinates(self, db_dir):
        start = datetime(2024, 3, 2, 9, 0)
        path = write_session_db(
            db_dir / "sessions.db",
            sessions=[
                {
                    "id": 1,
                    "start": millis(start),
                    "cat": 2,
                    "used_bike": 7,
                    "points": [
                        (lat, lon, millis(start) + i * 1000)
                        for i, (lat, lon) in enumerate(line_from(HOME))
                    ],
                }
            ],
            bikes=[(7, "Ridley")],
        )

        rides = list(read_session_db(path))

        assert len(rides) == 1
        ride = rides[0]
        assert ride.bike == "Ridley"
        assert ride.category == "Category 2"
        assert ride.year == "2024"
        assert ride.day == "2024-03-02"
        assert ride.segments[0][0] == pytest.approx(HOME)

    def test_each_session_becomes_its_own_ride(self, db_dir):
        start = datetime(2024, 3, 2, 9, 0)
        later = start + timedelta(hours=5)
        path = write_session_db(
            db_dir / "sessions.db",
            sessions=[
                {
                    "id": 1,
                    "start": millis(start),
                    "points": [(52.0, 4.0, millis(start))],
                },
                {
                    "id": 2,
                    "start": millis(later),
                    "points": [(52.1, 4.1, millis(later))],
                },
            ],
        )

        assert len(list(read_session_db(path))) == 2

    def test_long_pause_splits_segments(self, db_dir):
        start = datetime(2024, 3, 2, 9, 0)
        points = [(lat, lon, millis(start) + i * 1000) for i, (lat, lon) in enumerate(line_from(HOME))]
        resumed = millis(start) + 2 * 3600 * 1000
        points += [(lat, lon, resumed + i * 1000) for i, (lat, lon) in enumerate(line_from(NEXT_STREET))]
        path = write_session_db(
            db_dir / "sessions.db",
            sessions=[{"id": 1, "start": millis(start), "points": points}],
        )

        ride = next(iter(read_session_db(path)))

        assert len(ride.segments) == 2

    def test_missing_bike_falls_back_to_unknown(self, db_dir):
        start = datetime(2024, 3, 2, 9, 0)
        path = write_session_db(
            db_dir / "sessions.db",
            sessions=[{"id": 1, "start": millis(start), "points": [(52.0, 4.0, millis(start))]}],
        )

        ride = next(iter(read_session_db(path)))

        assert ride.bike == UNKNOWN
        assert ride.category == UNKNOWN

    def test_rejects_database_without_expected_tables(self, db_dir):
        path = db_dir / "other.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE notes (id integer)")
        connection.close()

        with pytest.raises(ValueError, match="sessions"):
            list(read_session_db(path))


class TestLoadRides:
    def test_combines_both_sources(self, tracks_dir, db_dir):
        write_gpx(tracks_dir / "ride.gpx", line_from(HOME), start=datetime(2024, 1, 1, 7, 0))
        start = datetime(2024, 6, 1, 9, 0)
        write_session_db(
            db_dir / "sessions.db",
            sessions=[{"id": 1, "start": millis(start), "points": [(52.0, 4.0, millis(start))]}],
        )

        rides, failed, duplicates = load_rides(tracks_dir, db_dir)

        assert len(rides) == 2
        assert failed == []
        assert duplicates == 0

    def test_gpx_export_of_a_database_session_is_dropped(self, tracks_dir, db_dir):
        start = datetime(2024, 6, 1, 9, 0)
        coords = line_from(HOME)
        write_gpx(tracks_dir / "export.gpx", coords, start=start)
        write_session_db(
            db_dir / "sessions.db",
            sessions=[
                {
                    "id": 1,
                    "start": millis(start),
                    "used_bike": 7,
                    "points": [
                        (lat, lon, millis(start) + i * 1000) for i, (lat, lon) in enumerate(coords)
                    ],
                }
            ],
            bikes=[(7, "Ridley")],
        )

        rides, _, duplicates = load_rides(tracks_dir, db_dir)

        assert duplicates == 1
        # The database copy wins, so the richer metadata survives.
        assert [ride.bike for ride in rides] == ["Ridley"]

    def test_unreadable_file_is_reported_without_failing(self, tracks_dir, db_dir):
        (tracks_dir / "broken.gpx").write_text("not xml at all")
        write_gpx(tracks_dir / "good.gpx", line_from(HOME), start=datetime(2024, 1, 1, 7, 0))

        rides, failed, _ = load_rides(tracks_dir, db_dir)

        assert len(rides) == 1
        assert [entry["file"] for entry in failed] == ["broken.gpx"]

    def test_missing_directories_are_tolerated(self, tmp_path):
        rides, failed, duplicates = load_rides(tmp_path / "nope", tmp_path / "also-nope")

        assert (rides, failed, duplicates) == ([], [], 0)


class TestGrid:
    def test_nearby_points_share_a_base_cell(self):
        ride = ride_from([(52.0, 4.0), (52.00002, 4.00002)])

        assert len(ride_cells(ride)) == 1

    def test_gaps_between_fixes_are_filled_in(self):
        # Two fixes ~110 m apart should produce a continuous run of cells, not two dots.
        ride = ride_from([(52.0, 4.0), (52.001, 4.0)])

        assert len(ride_cells(ride)) > 5

    def test_teleports_are_not_connected(self):
        near = ride_cells(ride_from([(52.0, 4.0), (52.0005, 4.0)]))
        far = ride_cells(ride_from([(52.0, 4.0), (60.0, 12.0)]))

        assert len(far) == 2
        assert len(near) > 2

    def test_coarser_levels_nest_inside_the_base_grid(self):
        cells = ride_cells(ride_from(line_from(HOME, count=40)))

        fine = coarsen(cells, BASE_LEVEL - 1)
        coarse = coarsen(cells, BASE_LEVEL - 2)

        assert len(coarse) <= len(fine) <= len(cells)
        assert {(x >> 1, y >> 1) for x, y in fine} == coarse

    def test_level_is_clamped_to_the_available_range(self):
        assert clamp_level(BASE_LEVEL + 5) == BASE_LEVEL
        assert clamp_level(0) == MIN_LEVEL
        assert clamp_level(BASE_LEVEL - 3) == BASE_LEVEL - 3

    def test_coarsen_never_refines_beyond_the_base_level(self):
        cells = ride_cells(ride_from(line_from(HOME)))

        assert coarsen(cells, BASE_LEVEL + 4) == set(cells)


class TestHeatPoints:
    def test_cell_centres_land_near_the_original_track(self):
        cells = ride_cells(ride_from([(52.0, 4.0)]))

        point = heat_points([cells], BASE_LEVEL)[0]

        assert point.lat == pytest.approx(52.0, abs=1e-4)
        assert point.lon == pytest.approx(4.0, abs=1e-4)

    def test_a_single_day_gets_the_full_weight(self):
        cells = ride_cells(ride_from(line_from(HOME)))

        weights = [point.weight for point in heat_points([cells], BASE_LEVEL)]

        assert weights == [pytest.approx(1.0)] * len(weights)

    def test_more_days_means_more_weight(self):
        shared = ride_cells(ride_from(line_from(HOME)))
        other = ride_cells(ride_from(line_from(NEXT_STREET)))

        points = heat_points([shared, shared | other], BASE_LEVEL)
        by_cell = {(round(p.lat, 4), round(p.lon, 4)): p.weight for p in points}

        twice = max(by_cell.values())
        once = min(by_cell.values())
        assert twice == pytest.approx(1.0)
        assert 0 < once < twice

    def test_empty_input_produces_no_points(self):
        assert heat_points([], BASE_LEVEL) == []
        assert heat_points([frozenset()], BASE_LEVEL) == []


class TestPassBuckets:
    def test_boundaries_are_disjoint_and_complete(self):
        assert [pass_bucket(n) for n in (1, 2, 5, 6, 100)] == [
            "1 day",
            "2-5 days",
            "2-5 days",
            "6+ days",
            "6+ days",
        ]

    def test_histogram_always_lists_every_bucket(self):
        histogram = pass_histogram({(0, 0): 1})

        assert [label for label, _ in histogram] == ["1 day", "2-5 days", "6+ days"]
        assert dict(histogram)["1 day"] == 1
        assert dict(histogram)["6+ days"] == 0

    def test_filtering_keeps_only_the_wanted_bands(self):
        counts = {(0, 0): 1, (1, 0): 3, (2, 0): 9}

        once = points_from_counts(counts, BASE_LEVEL, ["1 day"])
        often = points_from_counts(counts, BASE_LEVEL, ["6+ days"])
        both = points_from_counts(counts, BASE_LEVEL, ["1 day", "6+ days"])

        assert len(once) == len(often) == 1
        assert len(both) == 2

    def test_colours_do_not_shift_when_bands_are_hidden(self):
        counts = {(0, 0): 1, (1, 0): 9}

        all_weights = {p.weight for p in points_from_counts(counts, BASE_LEVEL)}
        rare_only = points_from_counts(counts, BASE_LEVEL, ["1 day"])

        # Normalisation is against the full range, so rare cells stay dim even
        # when the busy ones are hidden.
        assert rare_only[0].weight < 1.0
        assert rare_only[0].weight in all_weights

    def test_no_selection_keeps_everything(self):
        counts = {(0, 0): 1, (1, 0): 3, (2, 0): 9}

        assert len(points_from_counts(counts, BASE_LEVEL)) == 3


class TestRideMetadata:
    def test_rides_on_the_same_day_share_a_day_key(self):
        morning = Ride(segments=[], start_ms=millis(datetime(2024, 7, 4, 8, 0)))
        evening = Ride(segments=[], start_ms=millis(datetime(2024, 7, 4, 19, 30)))

        assert morning.day == evening.day == "2024-07-04"

    def test_year_comes_from_the_start_time(self):
        assert Ride(segments=[], start_ms=millis(datetime(2021, 12, 31, 23, 0))).year == "2021"
