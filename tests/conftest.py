import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import gpxpy
import gpxpy.gpx
import pytest

MICRODEGREES = 1_000_000


@pytest.fixture
def tracks_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "tracks"
    directory.mkdir()
    return directory


@pytest.fixture
def db_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "databases"
    directory.mkdir()
    return directory


def write_gpx(path: Path, points, start: datetime | None = None, step_seconds: int = 5) -> Path:
    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack()
    segment = gpxpy.gpx.GPXTrackSegment()
    gpx.tracks.append(track)
    track.segments.append(segment)
    for index, (lat, lon) in enumerate(points):
        time = None if start is None else start + timedelta(seconds=index * step_seconds)
        segment.points.append(gpxpy.gpx.GPXTrackPoint(lat, lon, time=time))
    path.write_text(gpx.to_xml())
    return path


def write_session_db(path: Path, sessions, bikes=(), *, with_bikes_table: bool = True) -> Path:
    """Build a minimal bike-computer database.

    `sessions` is a list of dicts with `id`, `start`, `cat`, `used_bike` and `points`,
    where each point is `(lat, lon, epoch_millis)`.
    """
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sessions (_id integer primary key, starttime long, cat integer, "
        "bike text, used_bike integer)"
    )
    connection.execute(
        "CREATE TABLE tracks (_id integer primary key autoincrement, lat integer, lon integer, "
        "time integer, session_id integer)"
    )
    if with_bikes_table:
        connection.execute("CREATE TABLE bikes (_id integer primary key, name text)")
        connection.executemany("INSERT INTO bikes VALUES (?, ?)", bikes)

    for session in sessions:
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (
                session["id"],
                session.get("start"),
                session.get("cat"),
                session.get("bike"),
                session.get("used_bike", 0),
            ),
        )
        connection.executemany(
            "INSERT INTO tracks (lat, lon, time, session_id) VALUES (?, ?, ?, ?)",
            [
                (round(lat * MICRODEGREES), round(lon * MICRODEGREES), time, session["id"])
                for lat, lon, time in session["points"]
            ],
        )

    connection.commit()
    connection.close()
    return path


def millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)
