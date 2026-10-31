"""The web app end to end: start it with no sources and call every endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sitrep.app import create_app
from sitrep.config import Config, ConfigError, StorageSettings
from sitrep.runtime import Runtime


@pytest.fixture
def client(tmp_path):
    cfg = Config(storage=StorageSettings(db_path=str(tmp_path / "test.db")))
    with TestClient(create_app(Runtime(cfg))) as test_client:
        yield test_client


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_state_with_no_sources(client):
    state = client.get("/api/state").json()
    assert state["overall"]["level"] == "unknown"
    assert [c["id"] for c in state["categories"]] == [
        "connectivity", "network", "official", "cyber", "news", "social", "stress", "sensors",
    ]
    # Worked out inside sitrep from the other sources, so always there.
    assert [s["id"] for s in state["sources"]] == ["stories", "convergence", "terms"]
    # Emerging terms need a week of history, which is a blind spot until then.
    assert [g["id"] for g in state["gaps"]] == ["terms"]
    assert state["gaps"][0]["text"].startswith("Emerging terms: Learning what's usual: 7 days to go")


def test_timeline_and_uptime(client):
    assert client.get("/api/timeline").json() == {"events": []}
    uptime = client.get("/api/uptime?hours=24&bucket_minutes=30").json()
    assert uptime["bucket_seconds"] == 1800
    assert len(uptime["buckets"]) == 48


def test_dashboard_page_is_served_locally(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>sitrep</title>" in page.text
    assert "http" not in page.text.split("<body>")[0].replace("http-equiv", "")  # nothing from a CDN
    for asset in ("/style.css", "/app.js", "/favicon.svg"):
        assert client.get(asset).status_code == 200


def test_page_files_are_checked_for_updates(client):
    # After a rebuild, the new page shows on the next reload, not hours later from the browser's cache.
    for path in ("/", "/style.css", "/app.js"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    # Asking again with the file's tag gets a quick "not modified", so checking costs next to nothing.
    tag = client.get("/style.css").headers["etag"]
    again = client.get("/style.css", headers={"If-None-Match": tag})
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"


def test_ingest_unknown_source_is_404(client):
    assert client.post("/api/ingest/balcony", json={"sirens": 1}).status_code == 404


def test_unknown_source_in_config_is_a_clear_error():
    with pytest.raises(ConfigError, match="sources.nope: unknown source"):
        Runtime(Config(sources={"nope": {}}))


def test_bad_source_settings_are_a_clear_error():
    with pytest.raises(ConfigError, match="sources.probes.router"):
        Runtime(Config(sources={"probes": {}}))
