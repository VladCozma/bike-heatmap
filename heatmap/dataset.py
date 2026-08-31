"""Write the rides into a compact binary the browser can aggregate on its own."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from heatmap.tracks import BASE_LEVEL, Ride, ride_cells


def _varint(out: bytearray, value: int) -> None:
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def encode_rides(rides: list[Ride], blocked: set[tuple[int, int]] = frozenset()) -> tuple[bytes, dict]:
    """Return the cell blob plus the index describing it.

    Cells are sorted and delta encoded as zigzag varints, which keeps the whole
    dataset small enough to ship as a single file. Cells listed in `blocked` are
    left out entirely, so private areas never reach the export.
    """
    bikes: dict[str, int] = {}
    categories: dict[str, int] = {}
    days: dict[str, int] = {}

    def intern(table: dict[str, int], value: str) -> int:
        return table.setdefault(value, len(table))

    blob = bytearray()
    index: list[list[int]] = []
    hidden = 0

    for ride in rides:
        cells = ride_cells(ride)
        if blocked:
            kept = cells - blocked
            hidden += len(cells) - len(kept)
            cells = kept
        cells = sorted(cells)
        if not cells:
            continue
        previous_x = previous_y = 0
        for x, y in cells:
            _varint(blob, _zigzag(x - previous_x))
            _varint(blob, _zigzag(y - previous_y))
            previous_x, previous_y = x, y
        index.append(
            [
                intern(days, ride.day),
                intern(bikes, ride.bike),
                intern(categories, ride.category),
                len(cells),
            ]
        )

    meta = {
        "baseLevel": BASE_LEVEL,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "hiddenCells": hidden,
        "days": list(days),
        "bikes": list(bikes),
        "categories": list(categories),
        "rides": index,
    }
    return bytes(blob), meta


def write_dataset(
    destination: Path, rides: list[Ride], blocked: set[tuple[int, int]] = frozenset()
) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    blob, meta = encode_rides(rides, blocked)
    (destination / "rides.bin").write_bytes(blob)
    (destination / "rides.json").write_text(json.dumps(meta, separators=(",", ":")))
    return meta
