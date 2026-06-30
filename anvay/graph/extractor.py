"""Deterministic graph extraction for code/docs resources.

This is deliberately syntax/convention based. LLM-proposed edges belong in a
later lower-confidence layer after the deterministic graph has eval coverage.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final

from tree_sitter import Node, Parser, Tree

from anvay.graph.models import GraphEdge, GraphExtraction, GraphNode, SourceRef
from anvay.ingest.chunker import _LANGS, _identifier_of, _lang_for
from anvay.ingest.models import Chunk, ResourceRef
from anvay.retrieval.repomap import _KIND_BY_NODE, _signature_of

EXTRACTOR_SCHEMA_VERSION: Final = "graph-v2"

_GRAPH_NS = uuid.UUID("1a41a1f2-a486-4aa7-bad5-a1a263ad91c2")
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/|_)(test|spec)[_.-]", re.I)
_ROUTE_RE = re.compile(
    r"@(?:[\w.]+)\.(get|post|put|patch|delete|options|head)\(\s*['\"]([^'\"]+)['\"]",
    re.I,
)
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", re.M)
_PY_FROM_IMPORT_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)$", re.M)
_JS_NAMED_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:([A-Za-z_$][\w$]*)\s*,?\s*)?(?:\{([^}]*)\})?[^'\"]*from\s+['\"]([^'\"]+)['\"]",
    re.M,
)
_JS_IMPORT_RE = re.compile(
    r"^\s*import(?:\s+type)?(?:[^'\"]*\s+from\s+)?['\"]([^'\"]+)['\"]|"
    r"require\(\s*['\"]([^'\"]+)['\"]\s*\)",
    re.M,
)
_CONFIG_READ_RE = re.compile(
    r"(?:os\.environ(?:\.get)?\(\s*['\"]([A-Z0-9_]+)['\"]|"
    r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]|"
    r"process\.env\.([A-Z0-9_]+))"
)
_DB_TABLE_RE = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([A-Za-z_][\w.]*|\"[^\"]+\")",
    re.I,
)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_DOC_PATH_RE = re.compile(r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|md|mdx|sql|yaml|yml|toml|json)\b")
_DOC_ROUTE_RE = re.compile(r"/[A-Za-z0-9_./:{}-]+")
_DOC_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})`")
_JIRA_METADATA_RE = re.compile(r'^\{"jira":\s*(\{.*\})\}\s*$', re.M)


def graph_extraction_version() -> str:
    payload = {
        "schema": EXTRACTOR_SCHEMA_VERSION,
        "langs": sorted(_LANGS),
        "route": _ROUTE_RE.pattern,
        "imports": [_PY_IMPORT_RE.pattern, _JS_IMPORT_RE.pattern],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def extract_resource_graph(
    *,
    product_id: str,
    source_key: str,
    resource: ResourceRef,
    content: str,
    indexed_at: str | None = None,
) -> GraphExtraction:
    now = indexed_at or datetime.now(UTC).isoformat()
    version = graph_extraction_version()
    source_ref = _source_ref(product_id, source_key, resource, now, content)
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}

    def add_node(
        kind: str,
        stable_id: str,
        *,
        labels: list[str] | None = None,
        properties: dict | None = None,
        refs: list[SourceRef] | None = None,
    ) -> str:
        node_labels = labels or [kind]
        existing = nodes.get(stable_id)
        if existing:
            existing.labels = sorted(set(existing.labels) | set(node_labels))
            existing.source_refs = _merge_refs(existing.source_refs, refs or [source_ref])
            existing.properties.update(properties or {})
            return stable_id
        nodes[stable_id] = GraphNode(
            product_id=product_id,
            stable_id=stable_id,
            labels=node_labels,
            properties=properties or {},
            source_refs=refs or [source_ref],
            last_seen=now,
        )
        return stable_id

    def add_edge(
        edge_type: str,
        from_id: str,
        to_id: str,
        *,
        properties: dict | None = None,
        refs: list[SourceRef] | None = None,
    ) -> str:
        stable_id = _edge_id(product_id, from_id, edge_type, to_id, resource.uri)
        edges[stable_id] = GraphEdge(
            product_id=product_id,
            stable_id=stable_id,
            type=edge_type,
            from_id=from_id,
            to_id=to_id,
            properties=properties or {},
            source_refs=refs or [source_ref],
            last_seen=now,
        )
        return stable_id

    source_id = _sid("source", product_id, source_key)
    repo_id = _sid("repo", product_id, resource.source_id)
    file_labels = _file_labels(resource.uri)
    file_id = _sid("file", product_id, resource.uri)
    module_id = _sid("module", product_id, _module_name(resource.uri))

    add_node("Source", source_id, properties={"source_key": source_key, "source_id": resource.source_id})
    add_node("Repository", repo_id, properties={"source_id": resource.source_id})
    add_node(
        file_labels[0],
        file_id,
        labels=file_labels,
        properties={
            "resource_uri": resource.uri,
            "mime": resource.mime,
            "name": PurePosixPath(resource.uri).name,
        },
    )
    add_edge("CONTAINS", source_id, repo_id)
    add_edge("CONTAINS", repo_id, file_id)

    if resource.mime == "text/x-jira-issue" or resource.uri.startswith("jira:"):
        _add_jira_issue(product_id, resource, content, source_id, now, source_ref, add_node, add_edge)

    lang = _lang_for(resource.uri)
    if lang:
        cfg = _LANGS[lang]
        tree = Parser(cfg.language).parse(content.encode("utf-8"))
        add_node("Module", module_id, properties={"name": _module_name(resource.uri)})
        add_edge("CONTAINS", file_id, module_id)
        symbol_ids = _extract_symbols(
            product_id=product_id,
            resource=resource,
            content=content,
            lang=lang,
            tree=tree,
            now=now,
            source_ref=source_ref,
        )
        for node in symbol_ids:
            add_node(node.labels[0], node.stable_id, labels=node.labels, properties=node.properties, refs=node.source_refs)
            add_edge("DECLARES", module_id, node.stable_id)
            if "Test" in node.labels:
                add_edge("COVERS", node.stable_id, module_id, properties={"coverage_hint": node.properties.get("name")})
        _add_import_edges(product_id, resource, content, module_id, add_node, add_edge)
        _add_call_edges(product_id, resource, content, tree, symbol_ids, add_edge)
        _add_route_edges(product_id, resource, content, file_id, symbol_ids, add_node, add_edge)
        _add_config_edges(product_id, resource, content, file_id, add_node, add_edge)

    if resource.uri.lower().endswith((".md", ".mdx")) or resource.mime == "text/markdown":
        _add_doc_headings(product_id, resource, content, file_id, now, source_ref, add_node, add_edge)
        _add_doc_reference_edges(product_id, resource, content, file_id, now, source_ref, add_node, add_edge)

    if "Migration" in file_labels or resource.uri.lower().endswith(".sql"):
        _add_db_edges(product_id, resource, content, file_id, add_node, add_edge)

    return GraphExtraction(
        product_id=product_id,
        source_key=source_key,
        resource_uri=resource.uri,
        extraction_version=version,
        nodes=sorted(nodes.values(), key=lambda n: n.stable_id),
        edges=sorted(edges.values(), key=lambda e: e.stable_id),
    )


def graph_node_ids_for_chunk(extraction: GraphExtraction, chunk: Chunk) -> list[str]:
    ids: list[str] = []
    for node in extraction.nodes:
        props = node.properties
        if props.get("resource_uri") == chunk.resource.uri:
            ids.append(node.stable_id)
            continue
        start = props.get("start_line")
        end = props.get("end_line")
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and start <= chunk.end_line
            and end >= chunk.start_line
        ):
            ids.append(node.stable_id)
    return sorted(set(ids))


def entity_ids_for_chunk(extraction: GraphExtraction, chunk: Chunk) -> list[str]:
    entity_ids = []
    for node in extraction.nodes:
        name = node.properties.get("name") or node.properties.get("path")
        if isinstance(name, str) and name and name in chunk.content:
            entity_ids.append(node.stable_id)
    return sorted(set(entity_ids))


def _extract_symbols(
    *,
    product_id: str,
    resource: ResourceRef,
    content: str,
    lang: str,
    tree: Tree,
    now: str,
    source_ref: SourceRef,
) -> list[GraphNode]:
    nodes: list[GraphNode] = []
    stack: list[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        kind = _KIND_BY_NODE.get(node.type)
        if kind:
            name = _identifier_of(node)
            if name:
                label = _symbol_label(kind)
                start = node.start_point[0] + 1
                end = node.end_point[0] + 1
                labels = [label]
                if _is_test_symbol(resource.uri, name):
                    labels.append("Test")
                nodes.append(
                    GraphNode(
                        product_id=product_id,
                        stable_id=_sid("symbol", product_id, resource.uri, name, label),
                        labels=labels,
                        properties={
                            "name": name,
                            "kind": kind,
                            "resource_uri": resource.uri,
                            "start_line": start,
                            "end_line": end,
                            "signature": _signature_of(node, content),
                        },
                        source_refs=[
                            source_ref.model_copy(
                                update={
                                    "anchor": f"{resource.uri}:{start}",
                                    "start_line": start,
                                    "end_line": end,
                                }
                            )
                        ],
                        last_seen=now,
                    )
                )
        stack.extend(reversed(node.named_children))
    return nodes


def _add_import_edges(product_id, resource, content, module_id, add_node, add_edge) -> None:
    pattern = _PY_IMPORT_RE if resource.uri.endswith(".py") else _JS_IMPORT_RE
    for match in pattern.finditer(content):
        imported = next((g for g in match.groups() if g), None)
        if not imported:
            continue
        imported_id = _sid("module", product_id, imported)
        add_node("Module", imported_id, properties={"name": imported, "external": not _looks_internal_import(imported, resource.uri)})
        add_edge("IMPORTS", module_id, imported_id, properties={"import": imported})


def _add_call_edges(product_id, resource, content, tree, symbols, add_edge) -> None:
    if resource.uri.lower().endswith((".go", ".rs", ".java")):
        return
    by_name = {
        str(symbol.properties.get("name")): symbol
        for symbol in symbols
        if symbol.properties.get("name")
    }
    imported = _imported_symbol_modules(product_id, resource, content)
    calls: list[tuple[int, str]] = []
    stack: list[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in {"call", "call_expression"}:
            name = _called_name(node, content)
            if name:
                calls.append((node.start_point[0] + 1, name))
        stack.extend(reversed(node.named_children))
    symbols_by_span = sorted(
        (
            int(symbol.properties.get("start_line", 0)),
            int(symbol.properties.get("end_line", 0)),
            symbol.stable_id,
        )
        for symbol in symbols
        if isinstance(symbol.properties.get("start_line"), int)
        and isinstance(symbol.properties.get("end_line"), int)
    )
    for line, callee_name in calls:
        caller_id = next(
            (stable_id for start, end, stable_id in symbols_by_span if start <= line <= end),
            None,
        )
        if not caller_id:
            continue
        callee = by_name.get(callee_name)
        if callee is not None:
            if caller_id != callee.stable_id:
                add_edge("CALLS", caller_id, callee.stable_id, properties={"callee": callee_name, "line": line})
            continue
        # Cross-module: the callee is imported from another module. Link the
        # caller to the already-created imported Module node (no orphan symbol),
        # so traversal/impact can follow the dependency. Real cross-file symbol
        # unification stays a product-level concern; here we record the edge.
        module_id = imported.get(callee_name)
        if module_id is not None:
            add_edge(
                "CALLS",
                caller_id,
                module_id,
                properties={"callee": callee_name, "line": line, "cross_module": True},
            )


def _imported_symbol_modules(product_id: str, resource, content: str) -> dict[str, str]:
    """Map imported leaf names to the Module node id they were imported from.

    Mirrors the module ids minted by `_add_import_edges` so a CALLS edge can
    target the same node. Handles Python `from M import a, b as c` and JS/TS
    `import D, {a, b as c} from 'M'`.
    """
    out: dict[str, str] = {}
    if resource.uri.endswith(".py"):
        for match in _PY_FROM_IMPORT_RE.finditer(content):
            module, names = match.group(1), match.group(2)
            module_id = _sid("module", product_id, module)
            for local_name in _parse_import_clause(names):
                out[local_name] = module_id
    else:
        for match in _JS_NAMED_IMPORT_RE.finditer(content):
            default_name, named, module = match.group(1), match.group(2), match.group(3)
            module_id = _sid("module", product_id, module)
            if default_name:
                out[default_name.strip()] = module_id
            for local_name in _parse_import_clause(named or ""):
                out[local_name] = module_id
    return out


def _parse_import_clause(clause: str) -> list[str]:
    """Extract bound local names from an import clause (`a, b as c, *`)."""
    names: list[str] = []
    for part in clause.replace("{", "").replace("}", "").split(","):
        token = part.strip().strip("()").strip()
        if not token or token == "*":
            continue
        # `original as alias` binds `alias`; bare `name` binds `name`.
        local = re.split(r"\s+as\s+", token)[-1].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", local):
            names.append(local)
    return names


def _add_route_edges(product_id, resource, content, file_id, symbols, add_node, add_edge) -> None:
    symbol_by_line = sorted(
        (int(s.properties.get("start_line", 0)), s.stable_id)
        for s in symbols
        if isinstance(s.properties.get("start_line"), int)
    )
    for match in _ROUTE_RE.finditer(content):
        method, path = match.group(1).upper(), match.group(2)
        line = content[: match.start()].count("\n") + 1
        api_id = _sid("api", product_id, method, _normalize_route(path), _module_name(resource.uri))
        add_node(
            "APIEndpoint",
            api_id,
            properties={"method": method, "path": path, "normalized_path": _normalize_route(path), "resource_uri": resource.uri, "start_line": line},
        )
        handler_id = next((sid for start, sid in symbol_by_line if start >= line), file_id)
        add_edge("HANDLES", handler_id, api_id, properties={"line": line})
        add_edge("EXPOSES", file_id, api_id, properties={"line": line})


def _add_config_edges(product_id, resource, content, file_id, add_node, add_edge) -> None:
    for match in _CONFIG_READ_RE.finditer(content):
        key = next((g for g in match.groups() if g), None)
        if not key:
            continue
        config_id = _sid("config", product_id, key)
        add_node("Config", config_id, properties={"name": key})
        add_edge("READS", file_id, config_id, properties={"key": key})


def _add_db_edges(product_id, resource, content, file_id, add_node, add_edge) -> None:
    for match in _DB_TABLE_RE.finditer(content):
        table = match.group(1).strip('"')
        table_id = _sid("dbtable", product_id, "default", table)
        add_node("DBTable", table_id, properties={"name": table, "schema": "default"})
        add_edge("WRITES", file_id, table_id, properties={"table": table})


def _add_doc_headings(product_id, resource, content, file_id, now, source_ref, add_node, add_edge) -> None:
    stack: list[tuple[int, str]] = []
    for match in _MD_HEADING_RE.finditer(content):
        line = content[: match.start()].count("\n") + 1
        title = match.group(2).strip()
        level = len(match.group(1))
        doc_id = _sid("document", product_id, resource.uri, title)
        add_node(
            "Document",
            doc_id,
            properties={"title": title, "level": level, "resource_uri": resource.uri, "start_line": line},
            refs=[source_ref.model_copy(update={"anchor": f"{resource.uri}:{line}", "start_line": line, "end_line": line})],
        )
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else file_id
        add_edge("CONTAINS", parent_id, doc_id)
        stack.append((level, doc_id))
    _ = now


def _add_doc_reference_edges(product_id, resource, content, file_id, now, source_ref, add_node, add_edge) -> None:
    doc_id = file_id
    headings = list(_MD_HEADING_RE.finditer(content))
    for index, heading in enumerate(headings or [None]):
        if heading is not None:
            title = heading.group(2).strip()
            doc_id = _sid("document", product_id, resource.uri, title)
            start = heading.end()
            start_line = content[: heading.start()].count("\n") + 1
        else:
            start = 0
            start_line = 1
        end = headings[index + 1].start() if heading is not None and index + 1 < len(headings) else len(content)
        section = content[start:end]
        for match in _DOC_PATH_RE.finditer(section):
            line = start_line + section[: match.start()].count("\n")
            target = match.group(0).strip("./")
            target_id = _sid("file", product_id, target)
            add_node("CodeFile", target_id, properties={"resource_uri": target, "name": PurePosixPath(target).name})
            ref = source_ref.model_copy(update={"anchor": f"{resource.uri}:{line}", "start_line": line, "end_line": line})
            add_edge("DOCUMENTS", doc_id, target_id, properties={"mention": target}, refs=[ref])
        for match in _DOC_ROUTE_RE.finditer(section):
            line = start_line + section[: match.start()].count("\n")
            route = match.group(0)
            route_id = _sid("api", product_id, "ANY", _normalize_route(route), _module_name(resource.uri))
            add_node("APIEndpoint", route_id, properties={"path": route, "normalized_path": _normalize_route(route)})
            ref = source_ref.model_copy(update={"anchor": f"{resource.uri}:{line}", "start_line": line, "end_line": line})
            add_edge("MENTIONS", doc_id, route_id, properties={"mention": route}, refs=[ref])
        for match in _CONFIG_READ_RE.finditer(section):
            line = start_line + section[: match.start()].count("\n")
            key = next((g for g in match.groups() if g), None)
            if not key:
                continue
            config_id = _sid("config", product_id, key)
            add_node("Config", config_id, properties={"name": key})
            ref = source_ref.model_copy(update={"anchor": f"{resource.uri}:{line}", "start_line": line, "end_line": line})
            add_edge("MENTIONS", doc_id, config_id, properties={"mention": key}, refs=[ref])
        for match in _DOC_SYMBOL_RE.finditer(section):
            line = start_line + section[: match.start()].count("\n")
            name = match.group(1)
            symbol_id = _sid("symbol", product_id, resource.uri, name, "Function")
            add_node("Function", symbol_id, properties={"name": name, "resource_uri": resource.uri})
            ref = source_ref.model_copy(update={"anchor": f"{resource.uri}:{line}", "start_line": line, "end_line": line})
            add_edge("MENTIONS", doc_id, symbol_id, properties={"mention": name}, refs=[ref])
    _ = now


def _add_jira_issue(product_id, resource, content, source_id, now, source_ref, add_node, add_edge) -> None:
    metadata = _jira_metadata(content)
    key = str(metadata.get("key") or resource.uri.rsplit(":", 1)[-1])
    issue_type = str(metadata.get("issue_type") or "")
    label = "Epic" if issue_type.lower() == "epic" else "JiraTicket"
    ticket_id = _sid("jira", product_id, _jira_site(resource.uri), key)
    add_node(
        label,
        ticket_id,
        labels=[label],
        properties={
            "key": key,
            "name": key,
            "summary": metadata.get("summary") or "",
            "status_name": metadata.get("status") or "",
            "issue_type": issue_type,
            "resource_uri": resource.uri,
            "url": metadata.get("url") or "",
            "created": metadata.get("created") or "",
            "updated": metadata.get("updated") or "",
            "labels": _csv_or_list(metadata.get("labels")),
            "components": _csv_or_list(metadata.get("components")),
        },
        refs=[source_ref],
    )
    add_edge("CONTAINS", source_id, ticket_id)
    parent = metadata.get("parent")
    if isinstance(parent, str) and parent:
        parent_id = _sid("jira", product_id, _jira_site(resource.uri), parent)
        add_node("Epic", parent_id, labels=["Epic"], properties={"key": parent, "name": parent})
        add_edge("PART_OF_FLOW", ticket_id, parent_id, properties={"relationship": "parent"})
    for actor_kind, actor_name in (("assignee", metadata.get("assignee")), ("reporter", metadata.get("reporter"))):
        if isinstance(actor_name, str) and actor_name:
            actor_id = _sid("actor", product_id, actor_name.lower())
            add_node("Actor", actor_id, properties={"name": actor_name})
            add_edge("ASSIGNED_TO", ticket_id, actor_id, properties={"role": actor_kind})
    mentioned = set()
    for field in ("linked_keys", "mentioned_keys"):
        value = metadata.get(field)
        if isinstance(value, list):
            mentioned.update(str(v) for v in value if v)
    for other_key in sorted(mentioned - {key}):
        other_id = _sid("jira", product_id, _jira_site(resource.uri), other_key)
        add_node("JiraTicket", other_id, properties={"key": other_key, "name": other_key})
        add_edge("RELATED_TO", ticket_id, other_id, properties={"relationship": "mentions"})
    _ = now


def _jira_metadata(content: str) -> dict:
    match = _JIRA_METADATA_RE.search(content)
    if not match:
        return {}
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    payload = raw.get("jira")
    return payload if isinstance(payload, dict) else {}


def _jira_site(uri: str) -> str:
    parts = uri.split(":")
    return parts[1] if len(parts) >= 3 else "unknown"


def _called_name(node: Node, content: str) -> str | None:
    target = node.child_by_field_name("function")
    if target is None and node.named_children:
        target = node.named_children[0]
    if target is None:
        return None
    if target.type in {"identifier", "attribute", "property_identifier"}:
        return _last_identifier(_node_text(target, content))
    if target.type in {"member_expression", "attribute"}:
        return _last_identifier(_node_text(target, content))
    return _last_identifier(_node_text(target, content))


def _node_text(node: Node, content: str) -> str:
    return content.encode("utf-8")[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _last_identifier(text: str) -> str | None:
    matches = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    return matches[-1] if matches else None


def _looks_internal_import(imported: str, uri: str) -> bool:
    if imported.startswith("."):
        return True
    first = imported.split(".", 1)[0]
    parts = [part for part in PurePosixPath(uri).parts[:-1] if part not in {".", ""}]
    return first in parts


def _csv_or_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _source_ref(product_id: str, source_key: str, resource: ResourceRef, now: str, content: str) -> SourceRef:
    _ = now
    return SourceRef(
        product_id=product_id,
        source_key=source_key,
        source_id=resource.source_id,
        resource_uri=resource.uri,
        anchor=f"{resource.uri}:1",
        start_line=1,
        end_line=max(content.count("\n") + 1, 1),
    )


def _file_labels(uri: str) -> list[str]:
    lower = uri.lower()
    labels = ["CodeFile"] if _lang_for(uri) else ["Document"]
    if _TEST_PATH_RE.search(uri):
        labels.append("Test")
    if "migration" in lower or lower.endswith(".sql"):
        labels.append("Migration")
    if lower.endswith((".env", ".ini", ".toml", ".yaml", ".yml", ".json")):
        labels.append("Config")
    return sorted(set(labels))


def _symbol_label(kind: str) -> str:
    if kind in {"class", "struct", "trait", "interface", "type", "enum", "contract"}:
        return "Class"
    return "Function"


def _is_test_symbol(uri: str, name: str) -> bool:
    return _TEST_PATH_RE.search(uri) is not None or name.startswith(("test_", "it_", "spec_"))


def _module_name(uri: str) -> str:
    path = PurePosixPath(uri)
    suffix = path.suffix
    raw = str(path.with_suffix("")) if suffix else uri
    return raw.strip("./").replace("/", ".")


def _normalize_route(path: str) -> str:
    return re.sub(r"\{[^}]+\}|:[A-Za-z_]\w*", "{}", path.rstrip("/") or "/")


def _sid(kind: str, product_id: str, *parts: str) -> str:
    return ":".join([kind, product_id, *[str(p).strip() for p in parts if str(p).strip()]])


def _edge_id(product_id: str, from_id: str, edge_type: str, to_id: str, uri: str) -> str:
    raw = f"{product_id}|{from_id}|{edge_type}|{to_id}|{uri}"
    return f"edge:{uuid.uuid5(_GRAPH_NS, raw)}"


def _merge_refs(left: list[SourceRef], right: list[SourceRef]) -> list[SourceRef]:
    by_anchor = {ref.anchor: ref for ref in left}
    for ref in right:
        by_anchor[ref.anchor] = ref
    return list(by_anchor.values())
