"""Build a server-free copy of the heatmap that can be hosted on any static host."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from app import DB_DIR, TRACKS_DIR, app
from heatmap.dataset import write_dataset
from heatmap.tracks import cells_within, load_rides

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_HIDE_RADIUS_M = 300.0


def parse_zone(value: str) -> tuple[float, float, float]:
    """Parse a `lat,lon[,radius]` privacy zone."""
    parts = value.split(",")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("expected lat,lon or lat,lon,radius")
    try:
        lat, lon = float(parts[0]), float(parts[1])
        radius = float(parts[2]) if len(parts) == 3 else DEFAULT_HIDE_RADIUS_M
    except ValueError:
        raise argparse.ArgumentTypeError(f"could not read numbers from {value!r}") from None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise argparse.ArgumentTypeError(f"{lat},{lon} is not a valid coordinate")
    return lat, lon, radius


def build(destination: Path, tracks_dir: Path, db_dir: Path, hide=()) -> dict:
    rides, failed, duplicates = load_rides(tracks_dir, db_dir)
    if not rides:
        raise SystemExit(f"No rides found in {tracks_dir} or {db_dir}")

    blocked: set[tuple[int, int]] = set()
    for lat, lon, radius in hide:
        blocked |= cells_within(lat, lon, radius)

    destination.mkdir(parents=True, exist_ok=True)
    meta = write_dataset(destination / "data", rides, blocked)
    if not meta["rides"]:
        raise SystemExit("Every ride fell inside the private zones; nothing left to export")

    assets = destination / "static"
    assets.mkdir(exist_ok=True)
    for name in ("app.js", "style.css"):
        shutil.copy(BASE_DIR / "static" / name, assets / name)

    with app.test_request_context():
        from flask import render_template

        html = render_template(
            "index.html",
            static_export=True,
            ride_count=len(meta["rides"]),
            generated=meta["generated"],
        )
    # Relative asset paths keep the export working under a sub-path, e.g. GitHub Pages.
    (destination / "index.html").write_text(html.replace('"/static/', '"static/'))
    # Tells GitHub Pages to serve the folder as-is instead of running Jekyll over it.
    (destination / ".nojekyll").touch()

    return {
        "rides": len(meta["rides"]),
        "days": len(meta["days"]),
        "duplicates": duplicates,
        "failed": failed,
        "hidden": meta["hiddenCells"],
        "bytes": (destination / "data" / "rides.bin").stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=BASE_DIR / "dist")
    parser.add_argument("--tracks", type=Path, default=TRACKS_DIR)
    parser.add_argument("--databases", type=Path, default=DB_DIR)
    parser.add_argument(
        "--hide",
        type=parse_zone,
        action="append",
        default=[],
        metavar="LAT,LON[,RADIUS]",
        help=f"drop cells within RADIUS metres (default {DEFAULT_HIDE_RADIUS_M:.0f}) of a point; "
        "repeatable",
    )
    args = parser.parse_args()

    result = build(args.dest, args.tracks, args.databases, args.hide)
    print(
        f"Exported {result['rides']} rides on {result['days']} days "
        f"({result['bytes'] / 1024:.0f} KB of cells) to {args.dest}"
    )
    if args.hide:
        print(f"  hid {result['hidden']} cells inside {len(args.hide)} private zone(s)")
    for entry in result["failed"]:
        print(f"  skipped {entry['file']}: {entry['error']}")


if __name__ == "__main__":
    main()
