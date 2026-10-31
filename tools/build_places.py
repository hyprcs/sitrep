"""Build sitrep/data/places_nsw.csv: every NSW suburb and locality with a point, from the ABS.

    python tools/build_places.py

You only need this to refresh the list (the ABS updates suburbs every five years). It
downloads about 1 MB from the ABS's public map service: the Suburbs and Localities 2026
(ASGS Edition 4) points for NSW, and the outline of Greater Sydney, then writes one row
per place:

    name         the place's name, cleaned ("Richmond", not "Richmond (NSW)")
    qualifier    what tells same-named NSW places apart ("Central Coast"), if anything
    lat, lon     the ABS's point for the place (always inside it)
    sydney       1 if the point is inside Greater Sydney
    elsewhere    1 if another state has a place with this name too (the ABS adds
                 "(NSW)" to the name exactly then), so a headline naming it may be
                 about somewhere else
    sqkm         the place's area, which helps tell a suburb from a vast rural locality
    code         the ABS suburb and locality code

The data is © Commonwealth of Australia (Australian Bureau of Statistics), under CC BY 4.0;
sitrep/data/README.md says so.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import httpx

SERVICE = "https://geo.abs.gov.au/arcgis/rest/services/ASGS2026"
OUT = Path(__file__).resolve().parent.parent / "sitrep" / "data" / "places_nsw.csv"
USER_AGENT = "sitrep-bot (self-hosted situational-awareness dashboard; tools/build_places.py)"


def query(http: httpx.Client, layer: str, where: str, fields: str, offset: int = 0) -> dict:
    response = http.get(f"{SERVICE}/{layer}/query", params={
        "where": where, "outFields": fields, "outSR": 4326, "f": "geojson",
        "resultOffset": offset, "resultRecordCount": 2000, "orderByFields": "OBJECTID",
    })
    response.raise_for_status()
    return response.json()


def inside(x: float, y: float, rings: list[list[list[float]]]) -> bool:
    """Is (x, y) inside a polygon given as an outer ring followed by holes? (Ray casting.)"""
    def in_ring(ring: list[list[float]]) -> bool:
        hit = False
        for i in range(len(ring)):
            (x1, y1), (x2, y2) = ring[i][:2], ring[i - 1][:2]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                hit = not hit
        return hit
    return in_ring(rings[0]) and not any(in_ring(hole) for hole in rings[1:])


def main() -> int:
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as http:
        outline = query(http, "GCCSA/MapServer/1", "GCCSA_CODE_2026='1GSYD'", "GCCSA_CODE_2026")
        polygons = []
        for feature in outline["features"]:
            geometry = feature["geometry"]
            polygons += geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        if not polygons:
            print("The ABS didn't send Greater Sydney's outline.", file=sys.stderr)
            return 1

        features, offset = [], 0
        while True:
            page = query(http, "SAL/MapServer/2", "STATE_CODE_2026='1'", "SAL_CODE_2026,SAL_NAME_2026,AREA_ALBERS_SQKM",
                         offset)
            features += page["features"]
            if len(page["features"]) < 2000:
                break
            offset += 2000

    rows = []
    for feature in features:
        props, geometry = feature["properties"], feature.get("geometry")
        if not geometry:
            continue  # a couple of "no usual address" entries have no place on the map
        coords = geometry["coordinates"]
        x, y = coords[0] if geometry["type"] == "MultiPoint" else coords
        raw = props["SAL_NAME_2026"].strip()
        match = re.fullmatch(r"(.*?)\s*\((?:(.*?) - )?NSW\)", raw)
        name, qualifier = (match.group(1), match.group(2) or "") if match else (raw, "")
        rows.append({
            "name": name, "qualifier": qualifier, "lat": round(y, 5), "lon": round(x, 5),
            "sydney": int(any(inside(x, y, poly) for poly in polygons)),
            "elsewhere": int(bool(match) and not match.group(2)),
            "sqkm": round(float(props.get("AREA_ALBERS_SQKM") or 0), 2), "code": props["SAL_CODE_2026"],
        })
    rows.sort(key=lambda r: (r["name"], r["qualifier"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows):,} NSW places ({sum(r['sydney'] for r in rows):,} in Greater Sydney) to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
