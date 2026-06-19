from __future__ import annotations

import os
import uuid

import pytest
from falkordb.edge import Edge
from falkordb.node import Node

from nexus.config import GraphStoreCfg
from nexus.graph.extractor import extract_resource_graph
from nexus.graph.store import FalkorGraphStore, _query_result_to_graph
from nexus.ingest.models import ResourceRef


class FakeQueryResult:
    def __init__(self, result_set):
        self.result_set = result_set


def test_falkordb_result_conversion_preserves_nodes_edges_and_source_refs() -> None:
    source_refs_js = (
        '[{"product_id":"prod","source_key":"source","source_id":"local:test",'
        '"resource_uri":"app.py","anchor":"app.py:1","start_line":1,"end_line":5}]'
    )
    source = Node(
        labels=["Function"],
        properties={
            "product_id": "prod",
            "stable_id": "symbol:prod:app.py:hello:Function",
            "name": "hello",
            "resource_uri": "app.py",
            "confidence": 1.0,
            "extraction_method": "deterministic",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "freshness": 1.0,
            "status": "active",
            "source_refs_js": source_refs_js,
        },
    )
    dest = Node(
        labels=["Module"],
        properties={
            "product_id": "prod",
            "stable_id": "module:prod:shared",
            "name": "shared",
            "confidence": 1.0,
            "extraction_method": "deterministic",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "freshness": 1.0,
            "status": "active",
            "source_refs_js": "[]",
        },
    )
    edge = Edge(
        source,
        "IMPORTS",
        dest,
        properties={
            "product_id": "prod",
            "stable_id": "edge:1",
            "from_id": "symbol:prod:app.py:hello:Function",
            "to_id": "module:prod:shared",
            "confidence": 1.0,
            "extraction_method": "deterministic",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "freshness": 1.0,
            "status": "active",
            "source_refs_js": source_refs_js,
        },
    )

    converted = _query_result_to_graph(FakeQueryResult([[source, dest, [edge]]]))

    assert [node.stable_id for node in converted.nodes] == [
        "module:prod:shared",
        "symbol:prod:app.py:hello:Function",
    ]
    assert converted.edges[0].type == "IMPORTS"
    assert converted.edges[0].from_id == "symbol:prod:app.py:hello:Function"
    assert converted.nodes[1].source_refs[0].anchor == "app.py:1"


@pytest.mark.asyncio
async def test_falkordb_store_contract_live() -> None:
    host = os.environ.get("NEXUS_FALKORDB_HOST")
    if not host:
        pytest.skip("set NEXUS_FALKORDB_HOST to run FalkorDB contract test")

    cfg = GraphStoreCfg(
        host=host,
        port=int(os.environ.get("NEXUS_FALKORDB_PORT", "6379")),
        username=os.environ.get("NEXUS_FALKORDB_USERNAME") or None,
        password=os.environ.get("NEXUS_FALKORDB_PASSWORD") or None,
        ssl=os.environ.get("NEXUS_FALKORDB_SSL") == "1",
        graph_prefix="nexus_test",
    )
    store = FalkorGraphStore(cfg)
    product_id = f"contract-{uuid.uuid4().hex[:8]}"
    resource = ResourceRef(
        source_id="local:contract",
        uri="app.py",
        mime="text/x-python",
    )
    extraction = extract_resource_graph(
        product_id=product_id,
        source_key="source",
        resource=resource,
        content=(
            "import os\n\n"
            "def hello():\n"
            "    token = os.environ.get('TOKEN_SECRET')\n"
            "    return token or 'missing'\n"
        ),
    )

    try:
        await store.ensure_schema()
        fact_ids = await store.upsert_resource_graph(extraction)
        assert fact_ids
        assert await store.retire_resource_graph(
            product_id=product_id,
            fact_ids=fact_ids[:1],
        ) == 1
        await store.delete_product(product_id=product_id)
    finally:
        await store.aclose()
