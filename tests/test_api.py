import gzip
import io
import json
from datetime import datetime

import pytest

import app as flask_app
from heatmap.tracks import BASE_LEVEL

from .conftest import millis, write_gpx, write_session_db

START = datetime(2024, 6, 1, 9, 0)
ROAD = [(52.0 + i * 0.0002, 4.0 + i * 0.0002) for i in range(8)]


@pytest.fixture
def client(tracks_dir, db_dir, monkeypatch):
    monkeypatch.setattr(flask_app, "TRACKS_DIR", tracks_dir)
    monkeypatch.setattr(flask_app, "DB_DIR", db_dir)
    flask_app._rides_cache.clear()
    flask_app._cells_cache.clear()
    flask_app._response_cache.clear()
    flask_app.app.config.update(TESTING=True)
    return flask_app.app.test_client()


def heatmap(client, **params):
    response = client.get("/api/heatmap", query_string=params)
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    return json.loads(gzip.decompress(response.data))


def seed_database(db_dir, sessions, bikes=()):
    return write_session_db(db_dir / "sessions.db", sessions=sessions, bikes=bikes)


def session(session_id, start, coords, **kwargs):
    return {
        "id": session_id,
        "start": millis(start),
        "points": [(lat, lon, millis(start) + i * 1000) for i, (lat, lon) in enumerate(coords)],
        **kwargs,
    }


class TestIndex:
    def test_serves_the_map_page(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert b"Bike Heatmap" in response.data


class TestHeatmapEndpoint:
    def test_empty_folders_return_no_points(self, client):
        data = heatmap(client)

        assert data["points"] == []
        assert data["bounds"] is None
        assert data["totalRides"] == 0

    def test_reports_rides_days_and_bounds(self, client, db_dir):
        seed_database(
            db_dir,
            [
                session(1, START, ROAD, used_bike=7, cat=1),
                session(2, START.replace(hour=18), ROAD, used_bike=7, cat=1),
            ],
            bikes=[(7, "Ridley")],
        )

        data = heatmap(client, level=BASE_LEVEL)

        assert data["totalRides"] == 2
        # Both rides happened on the same day, so they count once.
        assert data["days"] == 1
        assert data["points"]
        (south, west), (north, east) = data["bounds"]
        # Bounds sit on cell centres, so they land within a cell of the recorded track.
        assert south == pytest.approx(52.0, abs=1e-3)
        assert west == pytest.approx(4.0, abs=1e-3)
        assert north > south and east > west

    def test_weights_grow_with_the_number_of_days(self, client, db_dir):
        one_day = START
        another_day = START.replace(day=2)
        seed_database(
            db_dir,
            [
                session(1, one_day, ROAD),
                session(2, another_day, ROAD[:4]),
            ],
        )

        data = heatmap(client, level=BASE_LEVEL)
        weights = [weight for _, _, weight in data["points"]]

        assert max(weights) == pytest.approx(1.0)
        assert min(weights) < 1.0

    def test_facets_list_every_bike_category_and_year(self, client, db_dir):
        seed_database(
            db_dir,
            [
                session(1, START, ROAD, used_bike=7, cat=1),
                session(2, datetime(2023, 4, 1, 9, 0), ROAD, used_bike=8, cat=2),
            ],
            bikes=[(7, "Ridley"), (8, "MTB")],
        )

        facets = heatmap(client)["facets"]

        assert dict(facets["bikes"]) == {"Ridley": 1, "MTB": 1}
        assert dict(facets["categories"]) == {"Category 1": 1, "Category 2": 1}
        assert dict(facets["years"]) == {"2024": 1, "2023": 1}

    def test_filters_by_bike(self, client, db_dir):
        seed_database(
            db_dir,
            [
                session(1, START, ROAD, used_bike=7),
                session(2, datetime(2023, 4, 1, 9, 0), ROAD, used_bike=8),
            ],
            bikes=[(7, "Ridley"), (8, "MTB")],
        )

        data = heatmap(client, bike="MTB")

        assert data["rides"] == 1
        assert data["totalRides"] == 2

    def test_filters_combine_across_groups(self, client, db_dir):
        seed_database(
            db_dir,
            [
                session(1, START, ROAD, used_bike=7, cat=1),
                session(2, datetime(2023, 4, 1, 9, 0), ROAD, used_bike=7, cat=2),
            ],
            bikes=[(7, "Ridley")],
        )

        assert heatmap(client, bike="Ridley", year="2024")["rides"] == 1
        assert heatmap(client, bike="Ridley", year="2023", cat="Category 1")["rides"] == 0

    def test_selecting_several_values_in_one_group_widens_the_result(self, client, db_dir):
        seed_database(
            db_dir,
            [
                session(1, START, ROAD, used_bike=7),
                session(2, datetime(2023, 4, 1, 9, 0), ROAD, used_bike=8),
            ],
            bikes=[(7, "Ridley"), (8, "MTB")],
        )

        response = client.get("/api/heatmap?bike=Ridley&bike=MTB")
        data = json.loads(gzip.decompress(response.data))

        assert data["rides"] == 2

    def test_coarser_levels_produce_fewer_cells(self, client, db_dir):
        seed_database(db_dir, [session(1, START, ROAD)])

        fine = heatmap(client, level=BASE_LEVEL)
        coarse = heatmap(client, level=BASE_LEVEL - 4)

        assert len(coarse["points"]) < len(fine["points"])

    def test_out_of_range_and_invalid_levels_are_handled(self, client, db_dir):
        seed_database(db_dir, [session(1, START, ROAD)])

        assert heatmap(client, level=99)["points"] == heatmap(client, level=BASE_LEVEL)["points"]
        assert heatmap(client, level="not-a-number")["points"]

    def test_new_files_invalidate_the_cache(self, client, tracks_dir, db_dir):
        seed_database(db_dir, [session(1, START, ROAD)])
        before = heatmap(client)["totalRides"]

        write_gpx(tracks_dir / "extra.gpx", ROAD, start=datetime(2020, 1, 5, 10, 0))

        assert heatmap(client)["totalRides"] == before + 1


class TestUploads:
    def upload(self, client, endpoint, name, content=b"<gpx></gpx>"):
        return client.post(
            endpoint,
            data={"files": (io.BytesIO(content), name)},
            content_type="multipart/form-data",
        )

    def test_track_upload_is_stored(self, client, tracks_dir):
        response = self.upload(client, "/api/tracks", "ride.gpx")

        assert response.get_json() == {"saved": ["ride.gpx"], "rejected": []}
        assert (tracks_dir / "ride.gpx").exists()

    def test_database_upload_is_stored(self, client, db_dir):
        response = self.upload(client, "/api/databases", "sessions.db")

        assert response.get_json()["saved"] == ["sessions.db"]
        assert (db_dir / "sessions.db").exists()

    def test_wrong_extension_is_rejected(self, client, tracks_dir):
        response = self.upload(client, "/api/tracks", "notes.txt")

        assert response.get_json() == {"saved": [], "rejected": ["notes.txt"]}
        assert list(tracks_dir.iterdir()) == []

    def test_databases_are_not_accepted_as_tracks(self, client, tracks_dir):
        response = self.upload(client, "/api/tracks", "sessions.db")

        assert response.get_json()["rejected"] == ["sessions.db"]
        assert list(tracks_dir.iterdir()) == []

    def test_directory_traversal_is_neutralised(self, client, tracks_dir, tmp_path):
        response = self.upload(client, "/api/tracks", "../../escaped.gpx")

        assert response.get_json()["saved"] == ["escaped.gpx"]
        assert (tracks_dir / "escaped.gpx").exists()
        assert not (tmp_path.parent / "escaped.gpx").exists()

    def test_uploaded_track_shows_up_on_the_map(self, client, tracks_dir):
        gpx = write_gpx(tracks_dir / "tmp.gpx", ROAD, start=START)
        payload = gpx.read_bytes()
        gpx.unlink()

        self.upload(client, "/api/tracks", "ride.gpx", payload)

        assert heatmap(client)["totalRides"] == 1
