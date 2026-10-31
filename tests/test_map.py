"""The map: downloading the basemap for an area, and serving it and the points to the map page."""

from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient
from pmtiles.reader import MemorySource, Reader
from pmtiles.tile import Compression, Entry, TileType, tileid_to_zxy, zxy_to_tileid
from pmtiles.writer import Writer

from sitrep.app import create_app
from sitrep.basemap import MapError, download, latest_build, plan_requests, tile_ids
from sitrep.config import Config, MapSettings, StorageSettings
from sitrep.models import Assessment, Category, Level
from sitrep.runtime import Runtime

SYDNEY = (150.9, -34.0, 151.3, -33.7)


def world_archive(max_zoom: int = 8) -> bytes:
    """A small made-up world map: every tile up to max_zoom, each holding its own z/x/y as text."""
    buf = io.BytesIO()
    writer = Writer(buf)
    ids = sorted(zxy_to_tileid(z, x, y) for z in range(max_zoom + 1) for x in range(1 << z) for y in range(1 << z))
    for tile_id in ids:
        writer.write_tile(tile_id, "{}/{}/{}".format(*tileid_to_zxy(tile_id)).encode())
    writer.finalize({"tile_type": TileType.MVT, "tile_compression": Compression.NONE}, {"name": "test world"})
    return buf.getvalue()


def ranged(data: bytes, log: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        spec = request.headers.get("Range", "")
        if log is not None:
            log.append(spec)
        start, end = (int(x) for x in spec.removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=data[start: end + 1])
    return handler


# --- Which tiles cover an area ---


def test_tile_ids_cover_the_area_at_every_zoom():
    ids = tile_ids(SYDNEY, 3)
    assert ids[0] == zxy_to_tileid(0, 0, 0)
    assert zxy_to_tileid(1, 1, 1) in ids  # east of Greenwich, south of the equator
    assert zxy_to_tileid(3, 7, 4) in ids  # Sydney's tile at zoom 3
    assert len(ids) == 4 and ids == sorted(ids)


def test_requests_merge_neighbouring_tiles():
    entries = [Entry(1, 0, 100, 1), Entry(2, 100, 50, 1), Entry(3, 10_000_000, 10, 1), Entry(4, 0, 100, 1)]
    assert plan_requests(entries) == [(0, 150), (10_000_000, 10)]


# --- Downloading ---


async def test_download_copies_only_the_area(tmp_path):
    source = world_archive(8)
    log: list[str] = []
    out = tmp_path / "map" / "sydney.pmtiles"
    async with httpx.AsyncClient(transport=httpx.MockTransport(ranged(source, log))) as http:
        result = await download(http, "https://maps.example/world.pmtiles", MapSettings(bbox=list(SYDNEY), max_zoom=8),
                                out, say=lambda _: None)
    wanted = tile_ids(SYDNEY, 8)
    assert result["tiles"] == len(wanted)
    assert result["downloaded"] < len(source)  # only pieces of the world were fetched
    reader = Reader(MemorySource(out.read_bytes()))
    assert reader.get(8, 235, 153) == b"8/235/153"  # Sydney at zoom 8
    assert reader.get(8, 0, 0) is None  # nowhere near Sydney
    header = reader.header()
    assert (header["min_zoom"], header["max_zoom"]) == (0, 8)
    assert reader.metadata()["name"] == "test world"
    assert not out.with_name("sydney.pmtiles.part").exists()


async def test_a_server_that_ignores_ranges_is_stopped(tmp_path):
    source = world_archive(2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=source))) as http:
        with pytest.raises(MapError, match="whole file"):
            await download(http, "https://maps.example/world.pmtiles", MapSettings(), tmp_path / "x.pmtiles",
                           say=lambda _: None)


async def test_latest_build_is_the_newest():
    builds = [{"key": "20261029.pmtiles"}, {"key": "20261031.pmtiles"}, {"key": "20261030.pmtiles"}, {"key": "x.json"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=builds))) as http:
        assert await latest_build(http, "https://builds.example/builds.json") == "20261031.pmtiles"


# --- Serving the map page ---


@pytest.fixture
def client(tmp_path):
    cfg = Config(storage=StorageSettings(db_path=str(tmp_path / "test.db")),
                 map=MapSettings(tiles_path=str(tmp_path / "map" / "sydney.pmtiles")))
    runtime = Runtime(cfg)
    with TestClient(create_app(runtime)) as test_client:
        yield test_client, runtime, tmp_path


def test_map_api_has_home_and_points(client):
    test_client, runtime, _ = client
    runtime.board.register("rfs", "NSW RFS incidents", Category.OFFICIAL, None, 600)
    runtime.board.report("rfs", Assessment(Level.NORMAL, "quiet", {"map": [
        {"kind": "rfs", "title": "HENLEY MARINE DR, FIVE DOCK", "mark": "info", "lat": -33.86, "lon": 151.13}]}))
    data = test_client.get("/api/map").json()
    assert data["home"] == {"name": "Macquarie Park", "lat": -33.77878, "lon": 151.12425}
    assert data["basemap"] == {"url": "tiles/sydney.pmtiles", "present": False, "made_at": None}
    (point,) = data["points"]
    assert (point["source"], point["title"], point["live"]) == ("rfs", "HENLEY MARINE DR, FIVE DOCK", True)


def test_basemap_is_served_in_pieces(client):
    test_client, _, tmp_path = client
    assert test_client.get("/tiles/sydney.pmtiles").status_code == 404  # not downloaded yet
    (tmp_path / "map" / "sydney.pmtiles").write_bytes(bytes(range(256)) * 4)
    piece = test_client.get("/tiles/sydney.pmtiles", headers={"Range": "bytes=10-19"})
    assert piece.status_code == 206 and piece.content == bytes(range(10, 20))


def test_map_page_and_libraries_are_local(client):
    test_client, _, _ = client
    page = test_client.get("/map.html")
    assert page.status_code == 200
    head = page.text.split("<body")[0]
    assert "http" not in head.replace('http-equiv', "")  # nothing from the internet
    for asset in ("/map.js", "/map.css", "/vendor/maplibre-gl/maplibre-gl.mjs", "/vendor/maplibre-gl/maplibre-gl-worker.mjs",
                  "/vendor/pmtiles/pmtiles.js", "/vendor/basemaps/basemaps.js",
                  "/vendor/basemaps-assets/fonts/Noto%20Sans%20Regular/0-255.pbf",
                  "/vendor/basemaps-assets/sprites/v4/dark.json"):
        assert test_client.get(asset).status_code == 200, asset
    # Browsers only run a module if it's served as JavaScript.
    assert test_client.get("/vendor/maplibre-gl/maplibre-gl.mjs").headers["content-type"].startswith("text/javascript")
