from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from nexus.api.app import app
from nexus.api.deps import get_config_dep, get_registry
from nexus.api.routes import agent
from nexus.config import NexusConfig
from nexus.graph.models import GraphRAGAnswer
from nexus.registry import Registry


def _config(tmp_path: Path) -> NexusConfig:
    return NexusConfig(
        models={
            "council": {"provider": "test", "model": "test", "url": "http://llm"},
            "light": {"provider": "test", "model": "test"},
            "embedding": {"provider": "test", "model": "test", "url": "http://embed"},
            "reranker": {"provider": "test", "model": "test", "url": "http://rerank"},
        },
        storage={
            "proposal_queue": tmp_path / "proposals.db",
            "council_checkpoint": tmp_path / "council.sqlite",
        },
    )


def test_product_agent_route_returns_graphrag_answer(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product({
        "id": "demo",
        "name": "Demo",
        "tagline": "",
        "owner": {},
        "onboardedAt": datetime.now(UTC).isoformat(),
    })
    cfg = _config(tmp_path)
    calls: dict[str, object] = {}

    class FakeGraph:
        async def ensure_schema(self) -> None:
            calls["schema"] = True

    class FakeToolState:
        def __init__(self, *, product, config):
            calls["state_product"] = product
            calls["state_model"] = config.models.council.model
            self.graph_store = FakeGraph()

        async def aclose(self) -> None:
            calls["closed"] = True

    async def fake_ask_product_graph(state, **kwargs):
        calls["query"] = kwargs["query"]
        calls["history"] = kwargs["history"]
        calls["mode"] = kwargs["mode"]
        calls["top_k"] = kwargs["top_k"]
        return GraphRAGAnswer(
            product_id="demo",
            query=kwargs["query"],
            answer=f"Graph says yes after {len(kwargs['history'])} prior messages.",
            graph_available=True,
        ).model_dump(mode="json")

    monkeypatch.setattr(agent, "ToolState", FakeToolState)
    monkeypatch.setattr(agent, "ask_product_graph", fake_ask_product_graph)
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: cfg
    try:
        client = TestClient(app)
        res = client.post(
            "/products/demo/agent/messages",
            json={
                "message": "What owns auth?",
                "history": [{"role": "user", "content": "Earlier question"}],
                "model": "deepseek-ai/DeepSeek-V4-Pro",
                "mode": "drift_lite",
                "top_k": 12,
            },
        )
        second = client.post(
            "/products/demo/agent/messages",
            json={
                "message": "And callers?",
                "session_id": res.json()["session_id"],
                "history": [
                    {"role": "user", "content": "What owns auth?"},
                    {"role": "assistant", "content": "Graph says yes."},
                ],
                "model": "deepseek-ai/DeepSeek-V4-Pro",
            },
        )
        replay = client.get(
            f"/products/demo/agent/sessions/{res.json()['session_id']}"
        )
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert res.status_code == 200
    assert second.status_code == 200
    assert res.json()["answer"] == "Graph says yes after 1 prior messages."
    assert second.json()["answer"] == "Graph says yes after 2 prior messages."
    assert res.json()["session_id"] == second.json()["session_id"]
    assert replay.status_code == 200
    assert [m["role"] for m in replay.json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert calls["state_product"] == "demo"
    assert calls["state_model"] == "deepseek-ai/DeepSeek-V4-Pro"
    assert calls["query"] == "And callers?"
    assert calls["mode"] == "auto"
    assert calls["top_k"] == 8
    assert calls["schema"] is True
    assert calls["closed"] is True


def test_product_agent_greeting_does_not_retrieve(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product({
        "id": "demo",
        "name": "Demo",
        "tagline": "",
        "owner": {},
        "onboardedAt": datetime.now(UTC).isoformat(),
    })
    cfg = _config(tmp_path)
    called = False

    async def fake_ask_product_graph(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(agent, "ask_product_graph", fake_ask_product_graph)
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: cfg
    try:
        client = TestClient(app)
        res = client.post("/products/demo/agent/messages", json={"message": "hey"})
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert res.status_code == 200
    assert res.json()["answer"].startswith("Hey.")
    assert called is False


def test_product_agent_lists_and_rejects_models(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.db")
    registry.upsert_product({
        "id": "demo",
        "name": "Demo",
        "tagline": "",
        "owner": {},
        "onboardedAt": datetime.now(UTC).isoformat(),
    })
    cfg = _config(tmp_path)
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_config_dep] = lambda: cfg
    try:
        client = TestClient(app)
        models = client.get("/products/demo/agent/models")
        bad = client.post(
            "/products/demo/agent/messages",
            json={"message": "hi", "model": "unknown/model"},
        )
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_config_dep, None)

    assert models.status_code == 200
    assert "deepseek-ai/DeepSeek-V4-Pro" in models.json()["models"]
    assert bad.status_code == 400
