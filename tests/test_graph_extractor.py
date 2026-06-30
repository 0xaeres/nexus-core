from __future__ import annotations

from anvay.graph.extractor import extract_resource_graph, graph_node_ids_for_chunk
from anvay.ingest.chunker import chunk_resource
from anvay.ingest.models import ResourceRef


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


def test_python_resource_graph_extracts_intra_file_calls() -> None:
    resource = ResourceRef(source_id="local:test", uri="app.py", mime="text/x-python")
    graph = extract_resource_graph(
        product_id="prod",
        source_key="src",
        resource=resource,
        content=(
            "def read_token():\n"
            "    return load_policy()\n\n"
            "def load_policy():\n"
            "    return {}\n"
        ),
    )

    call_edges = [edge for edge in graph.edges if edge.type == "CALLS"]
    assert len(call_edges) == 1
    assert "read_token" in call_edges[0].from_id
    assert "load_policy" in call_edges[0].to_id


def test_python_resource_graph_resolves_imported_calls_to_module() -> None:
    resource = ResourceRef(source_id="local:test", uri="services/api.py", mime="text/x-python")
    graph = extract_resource_graph(
        product_id="prod",
        source_key="src",
        resource=resource,
        content=(
            "from billing.policy import load_policy\n\n"
            "def read_token():\n"
            "    return load_policy()\n"
        ),
    )
    cross = [
        edge
        for edge in graph.edges
        if edge.type == "CALLS" and edge.properties.get("cross_module")
    ]
    assert len(cross) == 1
    assert "read_token" in cross[0].from_id
    assert "billing.policy" in cross[0].to_id
    # The target module node must exist so the edge persists in the store.
    assert any(node.stable_id == cross[0].to_id for node in graph.nodes)


def test_intra_file_calls_take_precedence_over_imported_name() -> None:
    """A locally-defined symbol wins over a same-named import; no spurious cross-module edge is emitted."""
    resource = ResourceRef(source_id="local:test", uri="app.py", mime="text/x-python")
    graph = extract_resource_graph(
        product_id="prod",
        source_key="src",
        resource=resource,
        content=(
            "from billing.policy import load_policy\n\n"
            "def read_token():\n"
            "    return load_policy()\n\n"
            "def load_policy():\n"
            "    return {}\n"
        ),
    )
    call_edges = [edge for edge in graph.edges if edge.type == "CALLS"]
    assert len(call_edges) == 1
    assert not call_edges[0].properties.get("cross_module")


def test_markdown_graph_extracts_hierarchy_and_doc_references() -> None:
    resource = ResourceRef(source_id="local:test", uri="docs/auth.md", mime="text/markdown")
    graph = extract_resource_graph(
        product_id="prod",
        source_key="src",
        resource=resource,
        content=(
            "# Auth\n\n"
            "See `read_token` in services/auth/api.py and route /tokens/{token_id}.\n\n"
            "## Config\n\n"
            "Uses os.environ.get(\"TOKEN_SECRET\").\n"
        ),
    )

    edge_types = {edge.type for edge in graph.edges}
    titles = {node.properties.get("title") for node in graph.nodes}
    assert {"Auth", "Config"} <= titles
    assert "DOCUMENTS" in edge_types
    assert "MENTIONS" in edge_types
    assert any(edge.type == "CONTAINS" and "Auth" in edge.from_id and "Config" in edge.to_id for edge in graph.edges)


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


def _named_edges(graph, types: set[str]) -> set[tuple[str, str, str]]:
    """Resolve edges of the given types to (type, from_name, to_name) tuples.

    Names come from a node's most identifying property so the golden set is
    stable against stable_id formatting changes.
    """
    def name_of(stable_id: str) -> str:
        for node in graph.nodes:
            if node.stable_id == stable_id:
                props = node.properties
                return str(
                    props.get("name")
                    or props.get("title")
                    or props.get("normalized_path")
                    or props.get("path")
                    or props.get("key")
                    or props.get("resource_uri")
                    or stable_id.rsplit(":", 1)[-1]
                )
        return stable_id.rsplit(":", 1)[-1]

    return {
        (edge.type, name_of(edge.from_id), name_of(edge.to_id))
        for edge in graph.edges
        if edge.type in types
    }


def test_python_graph_golden_semantic_edges() -> None:
    """Golden: pin the deterministic semantic edge set for a multi-construct file.

    Guards graph-v2 extraction against silent regressions in symbol, route,
    import, config, and call edge derivation. Structural CONTAINS plumbing is
    asserted separately to keep this set focused on meaning-bearing edges.
    """
    content = (
        "import os\n"
        "from billing.policy import load_policy\n\n"
        "@router.post('/charge/{id}')\n"
        "def charge(id: str):\n"
        "    key = os.environ.get('STRIPE_KEY')\n"
        "    return load_policy()\n"
    )
    resource = ResourceRef(
        source_id="github:acme/platform", uri="services/billing/api.py", mime="text/x-python"
    )
    graph = extract_resource_graph(
        product_id="prod",
        source_key="src",
        resource=resource,
        content=content,
        indexed_at="2026-01-01T00:00:00+00:00",
    )

    declares = _named_edges(graph, {"DECLARES"})
    imports = _named_edges(graph, {"IMPORTS"})
    handles = _named_edges(graph, {"HANDLES"})
    reads = _named_edges(graph, {"READS"})
    calls = _named_edges(graph, {"CALLS"})

    assert declares == {("DECLARES", "services.billing.api", "charge")}
    assert imports == {
        ("IMPORTS", "services.billing.api", "os"),
        ("IMPORTS", "services.billing.api", "billing.policy"),
    }
    assert handles == {("HANDLES", "charge", "/charge/{}")}
    assert reads == {("READS", "api.py", "STRIPE_KEY")}
    # Imported call resolves to the source module node (cross-module).
    assert calls == {("CALLS", "charge", "billing.policy")}

    # Structural plumbing still present.
    structural = _named_edges(graph, {"CONTAINS", "EXPOSES"})
    assert ("EXPOSES", "api.py", "/charge/{}") in structural


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
