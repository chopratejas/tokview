"""Endpoint sanity tests for the FastAPI dashboard backend."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_db import _row
from tokview.dashboard import build_app
from tokview.db import Database
from tokview.pubsub import PubSub


@pytest.fixture
def client(tmp_path):
    db = Database(tmp_path / "dash.sqlite")
    import asyncio

    asyncio.get_event_loop().run_until_complete(db.open())
    # Seed a few rows
    asyncio.get_event_loop().run_until_complete(
        db.insert_request(_row(request_id="r-success", provider="anthropic", cost_usd=0.05))
    )
    asyncio.get_event_loop().run_until_complete(
        db.insert_request(_row(request_id="r-fail", provider="openai", cost_usd=0, status_code=500, completed=0))
    )
    pubsub = PubSub()
    app = build_app(db=db, pubsub=pubsub)
    with TestClient(app) as c:
        yield c
    asyncio.get_event_loop().run_until_complete(db.close())


def test_health_endpoint(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["uptime_seconds"] >= 0


def test_status_includes_request_count(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["requests_logged"] == 2


def test_calls_endpoint_paginates(client):
    r = client.get("/api/calls?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert {row["request_id"] for row in body["calls"]} == {"r-success", "r-fail"}


def test_calls_limit_validation(client):
    assert client.get("/api/calls?limit=0").status_code == 422
    assert client.get("/api/calls?limit=10000").status_code == 422


def test_summary_excludes_failure_cost(client):
    r = client.get("/api/summary")
    body = r.json()
    # Both rows landed today; only the success contributes to cost
    assert body["today"]["requests"] == 1
    assert body["mtd"]["cost_usd"] > 0


def test_providers_includes_failures(client):
    r = client.get("/api/providers")
    body = r.json()
    providers = {p["provider"] for p in body["providers"]}
    assert {"anthropic", "openai"} <= providers


def test_diagnostics_endpoint(client):
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert "metrics" in body
    assert body["metrics"]["total_requests"] == 2
    assert isinstance(body["recent_errors"], list)
    assert body["subscribers"] == 0  # nobody connected to SSE


def test_session_detail_returns_waterfall(client):
    # Both seeded rows share session_id "sess-1" (the _row default)
    r = client.get("/api/sessions/sess-1")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sess-1"
    assert len(body["calls"]) == 2
    assert body["summary"]["requests"] == 2
    assert body["summary"]["errors"] == 1
    assert body["summary"]["span_ms"] >= 0
    assert isinstance(body["insights"], list)


def test_session_detail_unknown_session_is_empty(client):
    r = client.get("/api/sessions/does-not-exist")
    assert r.status_code == 200
    body = r.json()
    assert body["calls"] == []
    assert body["summary"] is None


def test_insights_endpoint(client):
    r = client.get("/api/insights")
    assert r.status_code == 200
    body = r.json()
    assert "insights" in body
    assert "total_estimated_savings_usd" in body
    assert isinstance(body["insights"], list)


def test_latency_endpoint(client):
    r = client.get("/api/latency")
    assert r.status_code == 200
    body = r.json()
    assert "models" in body
    assert isinstance(body["models"], list)


def test_static_or_inline_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    # Either the SvelteKit shell or the inline HTML — both contain the brand name
    assert "tokview" in r.text
