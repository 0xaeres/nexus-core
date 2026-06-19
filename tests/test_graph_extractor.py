from __future__ import annotations

from nexus.graph.extractor import extract_resource_graph, graph_node_ids_for_chunk
from nexus.ingest.chunker import chunk_resource
from nexus.ingest.models import ResourceRef


def test_python_resource_graph_extracts_symbols_routes_imports_and_config() -> None:
    content = """import os
from billing.client import ChargeClient

@router.get("/tokens/{token_id}")
def read_token(token_id: str):
    secret = os.environ.get("TOKEN_SECRET")
    return {"token_id": token_id, "secret": secret}

class TokenPolicy:
    pass
"""
    resource = ResourceRef(
        source_id="github:acme/platform",
        uri="services/auth/api.py",
        mime="text/x-python",
    )

    graph = extract_resource_graph(
        product_id="prod",
        source_key="src",
        resource=resource,
        content=content,
        indexed_at="2026-01-01T00:00:00+00:00",
    )

    labels = {label for node in graph.nodes for label in node.labels}
    edge_types = {edge.type for edge in graph.edges}
    names = {node.properties.get("name") for node in graph.nodes}
    paths = {node.properties.get("path") for node in graph.nodes}

    assert {"Source", "Repository", "CodeFile", "Module", "Function", "Class"} <= labels
    assert "APIEndpoint" in labels
    assert "Config" in labels
    assert {"CONTAINS", "DECLARES", "IMPORTS", "HANDLES", "EXPOSES", "READS"} <= edge_types
    assert "read_token" in names
    assert "TokenPolicy" in names
    assert "/tokens/{token_id}" in paths or any(
        node.properties.get("path") == "/tokens/{token_id}" for node in graph.nodes
    )


def test_chunk_graph_node_ids_include_overlapping_symbols() -> None:
    content = (
        "def alpha():\n"
        "    values = ['alpha'] * 30\n"
        "    return ','.join(values)\n\n"
        "def beta():\n"
        "    values = ['beta'] * 30\n"
        "    return ','.join(values)\n"
    )
    resource = ResourceRef(
        source_id="local:test",
        uri="app.py",
        mime="text/x-python",
    )
    chunks = chunk_resource("prod", resource, content)
    graph = extract_resource_graph(
        product_id="prod",
        source_key="src",
        resource=resource,
        content=content,
    )

    alpha_chunk = next(chunk for chunk in chunks if chunk.context_path == "alpha")
    ids = graph_node_ids_for_chunk(graph, alpha_chunk)

    assert any("alpha" in stable_id for stable_id in ids)


def test_jira_issue_graph_extracts_ticket_actors_and_related_edges() -> None:
    content = (
        '{"jira": {"key": "AUTH-2", "summary": "Token work", '
        '"status": "In Progress", "issue_type": "Story", '
        '"assignee": "Ava Owner", "reporter": "Rae Reporter", '
        '"parent": "AUTH-1", "linked_keys": ["AUTH-3"], '
        '"mentioned_keys": ["AUTH-4"], "labels": "auth", "components": "api"}}\n\n'
        "# AUTH-2 Token work\n\nImplement auth token changes."
    )
    resource = ResourceRef(
        source_id="jira:example.atlassian.net",
        uri="jira:example.atlassian.net:AUTH-2",
        mime="text/x-jira-issue",
    )

    graph = extract_resource_graph(
        product_id="prod",
        source_key="jira:example",
        resource=resource,
        content=content,
    )

    labels = {label for node in graph.nodes for label in node.labels}
    edge_types = {edge.type for edge in graph.edges}
    keys = {node.properties.get("key") for node in graph.nodes}
    actors = {node.properties.get("name") for node in graph.nodes if "Actor" in node.labels}

    assert "JiraTicket" in labels
    assert "Actor" in labels
    assert "Owner" not in labels
    assert {"AUTH-2", "AUTH-1", "AUTH-3", "AUTH-4"} <= keys
    assert {"Ava Owner", "Rae Reporter"} <= actors
    assert {"ASSIGNED_TO", "RELATED_TO", "PART_OF_FLOW"} <= edge_types
