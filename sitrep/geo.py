"""Places: distances, and the 2 km grid that local signals are placed on.

Greater Sydney is divided into squares about 2 km on a side. Every local signal (an RFS
incident, a Live Traffic hazard, a geolocated headline) lands in one square, and two
signals back each other up when they're in the same square or neighbouring ones (see
models.area and scoring.scopes_related).

The squares are measured from a fixed corner south-west of Greater Sydney, with the
width of a degree of longitude taken at Sydney's latitude. They stay close to 2 km
across Greater Sydney and drift a little further away; that's fine for "the same area".
"""

from __future__ import annotations

import math
from typing import Any, Iterator

EARTH_KM = 6371.0088
CELL_KM = 2.0
GRID_ORIGIN = (-34.40, 149.90)  # (lat, lon) of the grid's south-west corner
GRID_REF_LAT = -33.87  # Sydney's latitude, where a degree of longitude is measured
KM_PER_DEG_LAT = 111.32


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The distance between two points, in kilometres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    """The (row, column) of the 2 km square a point is in."""
    lat_step = CELL_KM / KM_PER_DEG_LAT
    lon_step = CELL_KM / (KM_PER_DEG_LAT * math.cos(math.radians(GRID_REF_LAT)))
    return math.floor((lat - GRID_ORIGIN[0]) / lat_step), math.floor((lon - GRID_ORIGIN[1]) / lon_step)


def points(geometry: Any) -> Iterator[tuple[float, float]]:
    """Every point in a GeoJSON geometry, as (lat, lon), however deeply it's nested."""
    if not isinstance(geometry, dict):
        return
    kind = geometry.get("type")
    if kind == "Point":
        coords = geometry.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2 and all(isinstance(c, (int, float)) for c in coords[:2]):
            lon, lat = coords[0], coords[1]
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                yield float(lat), float(lon)
    elif kind == "GeometryCollection":
        for part in geometry.get("geometries") or []:
            yield from points(part)


def first_point(geometry: Any) -> tuple[float, float] | None:
    return next(points(geometry), None)
