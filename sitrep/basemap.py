"""python -m sitrep map: download the Greater Sydney basemap for the offline map page.

Protomaps publishes a map of the whole world, built from OpenStreetMap every day, as one
large PMTiles file (about 140 GB). PMTiles is made for reading in pieces, so this command
reads only the index and the map tiles that cover Greater Sydney, about 40 MB, and writes
them into a small file of its own (data/map/sydney.pmtiles by default). sitrep then serves
that file to the map page, so the map keeps working with no internet at all.

Run it once, and again whenever you'd like newer streets (every few months is plenty).
Protomaps asks that its builds be downloaded rather than linked to from live pages, which
is what this does. The map data is © OpenStreetMap contributors, under the ODbL, and the
map page credits it.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
import os
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from pmtiles.tile import Compression, Entry, deserialize_directory, deserialize_header, zxy_to_tileid
from pmtiles.writer import Writer

from .config import Config, MapSettings

HEADER_BYTES = 16384  # the header and root directory always fit in the first 16 kB
MERGE_GAP = 256 * 1024  # fetch neighbouring tiles in one request if they're this close
MAX_REQUEST = 8 * 1024 * 1024
PARALLEL = 4


class MapError(Exception):
    """Something went wrong; the message says what, in plain English."""


def tile_ids(bbox: tuple[float, float, float, float], max_zoom: int) -> list[int]:
    """Every tile id covering bbox (west, south, east, north) from zoom 0 to max_zoom, in order."""
    west, south, east, north = bbox
    ids = []
    for z in range(max_zoom + 1):
        n = 1 << z
        x0, x1 = _tile_x(west, n), _tile_x(east, n)
        y0, y1 = _tile_y(north, n), _tile_y(south, n)  # tile rows count down from the north
        ids += [zxy_to_tileid(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    return sorted(ids)


def _tile_x(lon: float, n: int) -> int:
    return min(n - 1, max(0, int((lon + 180) / 360 * n)))


def _tile_y(lat: float, n: int) -> int:
    rad = math.radians(lat)
    return min(n - 1, max(0, int((1 - math.asinh(math.tan(rad)) / math.pi) / 2 * n)))


class Remote:
    """A PMTiles file on a web server, read in pieces with HTTP range requests."""

    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self.http = http
        self.url = url
        self.fetched = 0  # bytes downloaded so far

    async def read(self, offset: int, length: int) -> bytes:
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        for attempt in range(4):
            try:
                response = await self.http.get(self.url, headers=headers)
            except httpx.HTTPError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 206:
                self.fetched += len(response.content)
                return response.content
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                await asyncio.sleep(2 ** attempt * 5)
                continue
            if response.status_code == 200:
                raise MapError("The map server sent the whole file instead of a piece of it, which would be "
                               "140 GB. Stopped.")
            raise MapError(f"The map server answered HTTP {response.status_code} for {self.url}")
        raise MapError(f"Couldn't read from {self.url}")


@dataclass
class Extract:
    tiles: list[tuple[int, Entry]]  # (tile id, where its data is) for every tile found
    header: dict[str, Any]
    metadata: dict[str, Any]


async def find_tiles(remote: Remote, wanted: list[int]) -> Extract:
    """Read the index and find where each wanted tile's data is."""
    start = await remote.read(0, HEADER_BYTES)
    header = deserialize_header(start)
    if header["internal_compression"] != Compression.GZIP:
        raise MapError("This map file's index isn't gzip-compressed, which sitrep can't read")
    root = deserialize_directory(start[header["root_offset"]: header["root_offset"] + header["root_length"]])
    metadata_bytes = await remote.read(header["metadata_offset"], header["metadata_length"])
    metadata = json.loads(gzip.decompress(metadata_bytes))

    found: list[tuple[int, Entry]] = []

    async def walk(entries: list[Entry], lo: int, hi: int) -> None:
        """Visit the entries covering wanted ids from index lo up to (not including) hi."""
        leaves = []
        for i, entry in enumerate(entries):
            end = entries[i + 1].tile_id if i + 1 < len(entries) else math.inf
            if entry.run_length:
                end = entry.tile_id + entry.run_length
            a, b = bisect_left(wanted, entry.tile_id, lo, hi), bisect_left(wanted, end, lo, hi)
            if a == b:
                continue
            if entry.run_length:
                found.extend((wanted[k], entry) for k in range(a, b))
            else:
                leaves.append((entry, a, b))
        for entry, a, b in leaves:
            data = await remote.read(header["leaf_directory_offset"] + entry.offset, entry.length)
            await walk(deserialize_directory(data), a, b)

    await walk(root, 0, len(wanted))
    found.sort(key=lambda pair: pair[0])
    return Extract(found, header, metadata)


def plan_requests(entries: list[Entry]) -> list[tuple[int, int]]:
    """Group the tiles' data into as few range requests as is sensible: (offset, length)."""
    spans = sorted({(e.offset, e.length) for e in entries})
    requests: list[list[int]] = []
    for offset, length in spans:
        if requests and offset - (requests[-1][0] + requests[-1][1]) <= MERGE_GAP and (
            offset + length - requests[-1][0] <= MAX_REQUEST
        ):
            requests[-1][1] = max(requests[-1][1], offset + length - requests[-1][0])
        else:
            requests.append([offset, length])
    return [(o, n) for o, n in requests]


async def download(
    http: httpx.AsyncClient, url: str, settings: MapSettings, out: Path,
    say: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Copy the tiles covering settings.bbox up to settings.max_zoom from url into out."""
    remote = Remote(http, url)
    wanted = tile_ids(tuple(settings.bbox), settings.max_zoom)
    say(f"Reading the index of {url} ...")
    extract = await find_tiles(remote, wanted)
    if not extract.tiles:
        raise MapError("None of Greater Sydney's tiles are in that map file")
    header = extract.header
    requests = plan_requests([entry for _, entry in extract.tiles])
    total = sum(n for _, n in requests)
    say(f"Found {len(extract.tiles):,} tiles. Downloading about {total / 1e6:.0f} MB in {len(requests)} pieces ...")

    pieces: dict[int, bytes] = {}
    limit = asyncio.Semaphore(PARALLEL)
    last_said = time.monotonic()

    async def fetch(offset: int, length: int) -> None:
        nonlocal last_said
        async with limit:
            pieces[offset] = await remote.read(header["tile_data_offset"] + offset, length)
        if time.monotonic() - last_said > 5:
            last_said = time.monotonic()
            say(f"  {remote.fetched / 1e6:.0f} of about {total / 1e6:.0f} MB")

    await asyncio.gather(*(fetch(o, n) for o, n in requests))
    starts = sorted(pieces)

    def tile_data(entry: Entry) -> bytes:
        start = starts[bisect_right(starts, entry.offset) - 1]
        at = entry.offset - start
        return pieces[start][at: at + entry.length]

    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    west, south, east, north = settings.bbox
    with part.open("wb") as f:
        writer = Writer(f)
        for tile_id, entry in extract.tiles:
            writer.write_tile(tile_id, tile_data(entry))
        writer.finalize(
            {
                "tile_type": header["tile_type"],
                "tile_compression": header["tile_compression"],
                "min_lon_e7": int(west * 1e7), "min_lat_e7": int(south * 1e7),
                "max_lon_e7": int(east * 1e7), "max_lat_e7": int(north * 1e7),
                "center_zoom": 10,
                "center_lon_e7": int((west + east) / 2 * 1e7), "center_lat_e7": int((south + north) / 2 * 1e7),
            },
            {**extract.metadata, "sitrep": {"source": url, "bbox": list(settings.bbox), "max_zoom": settings.max_zoom,
                                            "made_at": time.time()}},
        )
    os.replace(part, out)
    return {"tiles": len(extract.tiles), "downloaded": remote.fetched, "size": out.stat().st_size}


async def latest_build(http: httpx.AsyncClient, builds_url: str) -> str:
    """The file name of Protomaps' newest daily build, such as '20261029.pmtiles'."""
    response = await http.get(builds_url)
    response.raise_for_status()
    builds = response.json()
    keys = sorted(b["key"] for b in builds if isinstance(b, dict) and str(b.get("key", "")).endswith(".pmtiles"))
    if not keys:
        raise MapError(f"Couldn't find a map build listed at {builds_url}")
    return keys[-1]


async def run(cfg: Config, say: Callable[[str], None] = print) -> int:
    s = cfg.map
    out = Path(s.tiles_path)
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": cfg.http.user_agent},
                                 follow_redirects=True) as http:
        try:
            build = await latest_build(http, s.builds_url)
            url = s.build_url.format(build=build)
            say(f"Newest Protomaps build: {build}")
            result = await download(http, url, s, out, say)
        except (MapError, httpx.HTTPError, ValueError) as exc:
            say(f"Couldn't make the map: {exc}")
            return 1
    say(f"Done: {result['tiles']:,} tiles, {result['size'] / 1e6:.0f} MB, saved to {out}.")
    say("Open the dashboard and click Map. If sitrep is running, it picks the file up straight away.")
    return 0
