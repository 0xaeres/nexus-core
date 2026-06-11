from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from nexus.api.app import app
from nexus.api.deps import get_proposal_queue, get_registry
from nexus.api.routes import council
from nexus.council.queue import ProposalQueue
from nexus.council.runner import HUB
from nexus.registry import Registry


def test_council_stream_route_matches_before_session_detail(tmp_path: Path) -> None:
    queue = ProposalQueue(tmp_path / "queue.db")
    registry = Registry(tmp_path / "registry.db")
    queue.record_session(
        session_id="cs_done",
        product_id="p",
        topic="topic",
        proposal_id=None,
        deliberation=[],
        costs=[],
        started_at="2026-05-24T00:00:00Z",
        completed_at="2026-05-24T00:00:01Z",
    )
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        client = TestClient(app)
        response = client.get("/council/sessions/cs_done/stream")
    finally:
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_registry, None)

    assert response.status_code == 200
    assert "event: session_start" in response.text


def test_council_stream_route_accepts_live_session_before_queue_record(
    tmp_path: Path,
) -> None:
    queue = ProposalQueue(tmp_path / "queue.db")
    registry = Registry(tmp_path / "registry.db")

    async def fake_stream_events(session_id: str):
        yield {"event": "session_start", "data": f'{{"session_id":"{session_id}"}}'}

    original = council.stream_events
    council.stream_events = fake_stream_events
    HUB._live.add("cs_live")
    app.dependency_overrides[get_proposal_queue] = lambda: queue
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        client = TestClient(app)
        response = client.get("/council/sessions/cs_live/stream")
    finally:
        HUB._live.discard("cs_live")
        council.stream_events = original
        app.dependency_overrides.pop(get_proposal_queue, None)
        app.dependency_overrides.pop(get_registry, None)

    assert response.status_code == 200
    assert "event: session_start" in response.text
