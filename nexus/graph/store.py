"""GraphStore factory and FalkorDB adapter."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from nexus.config import GraphStoreCfg, NexusConfig
from nexus.graph.models import GraphEdge, GraphExtraction, GraphNode, GraphQueryResult, SourceRef

log = logging.getLogger(__name__)

_LABELS = (
    "Product",
    "Source",
    "Repository",
    "Service",
    "UIApp",
    "UIScreen",
    "APIEndpoint",
    "Module",
    "CodeFile",
    "Function",
    "Class",
    "DBTable",
    "Migration",
    "EventTopic",
    "Config",
    "FeatureFlag",
    "Test",
    "Document",
    "ADR",
    "Runbook",
    "JiraTicket",
    "Epic",
    "PR",
    "Commit",
    "Incident",
    "Owner",
    "Actor",
    "Team",
    "ErrorSignature",
    "ProductFlow",
)
_EDGE_TYPES = (
    "CONTAINS",
    "DECLARES",
    "IMPORTS",
    "CALLS",
    "DEPENDS_ON",
    "HANDLES",
    "EXPOSES",
    "READS",
    "WRITES",
    "PRODUCES",
    "CONSUMES",
    "COVERS",
    "DOCUMENTS",
    "CONSTRAINS",
    "OWNS",
    "ASSIGNED_TO",
    "IMPLEMENTS",
    "RESOLVES",
    "CHANGED",
    "AFFECTS",
    "MENTIONS",
    "RELATED_TO",
    "PART_OF_FLOW",
)


def create_graph_store(config: NexusConfig) -> FalkorGraphStore:
    return FalkorGraphStore(config.graph_store)


class FalkorGraphStore:
    def __init__(self, cfg: GraphStoreCfg):
        self.cfg = cfg
        self._pool = None
        self._db = None
        self._schema_ready: set[str] = set()

    async def ensure_schema(self) -> None:
        self._connect()

    async def health(self) -> bool:
        try:
            graph = self._graph("_health")
            await graph.ro_query("RETURN 1", timeout=self.cfg.timeout_ms)
            return True
        except Exception:
            log.exception("falkordb health check failed")
            return False

    async def _ensure_product_schema(self, product_id: str) -> None:
        if product_id in self._schema_ready:
            return
        graph = self._graph(product_id)
        graph_name = _graph_name(self.cfg.graph_prefix, product_id)
        for label in _LABELS:
            await self._ignore_existing(
                graph.query(
                    f"CREATE INDEX FOR (n:{_ident(label)}) ON (n.stable_id, n.product_id)",
                    timeout=self.cfg.timeout_ms,
                )
            )
            await self._ignore_existing(
                graph.query(
                    f"CREATE INDEX FOR (n:{_ident(label)}) ON (n.status)",
                    timeout=self.cfg.timeout_ms,
                )
            )
            await self._create_constraint(graph_name, label)
        for rel_type in _EDGE_TYPES:
            await self._ignore_existing(
                graph.query(
                    f"CREATE INDEX FOR ()-[r:{_ident(rel_type)}]-() ON (r.stable_id, r.product_id)",
                    timeout=self.cfg.timeout_ms,
                )
            )
            await self._ignore_existing(
                graph.query(
                    f"CREATE INDEX FOR ()-[r:{_ident(rel_type)}]-() ON (r.status)",
                    timeout=self.cfg.timeout_ms,
                )
            )
        self._schema_ready.add(product_id)

    async def upsert_resource_graph(
        self,
        extraction: GraphExtraction,
        *,
        previous_fact_ids: list[str] | None = None,
    ) -> list[str]:
        await self._ensure_product_schema(extraction.product_id)
        graph = self._graph(extraction.product_id)
        for node in extraction.nodes:
            await self._upsert_node(graph, node)
        for edge in extraction.edges:
            await self._upsert_edge(graph, edge)
        active_ids = extraction.fact_ids
        stale = sorted(set(previous_fact_ids or []) - set(active_ids))
        if stale:
            await self.retire_resource_graph(product_id=extraction.product_id, fact_ids=stale)
        return active_ids

    async def retire_resource_graph(self, *, product_id: str, fact_ids: list[str]) -> int:
        if not fact_ids:
            return 0
        graph = self._graph(product_id)
        params = {"product_id": product_id, "ids": fact_ids}
        await graph.query(
            "MATCH (n) WHERE n.product_id = $product_id AND n.stable_id IN $ids "
            "SET n.status = 'stale' RETURN count(n)",
            params,
            timeout=self.cfg.timeout_ms,
        )
        await graph.query(
            "MATCH ()-[r]-() WHERE r.product_id = $product_id AND r.stable_id IN $ids "
            "SET r.status = 'stale' RETURN count(r)",
            params,
            timeout=self.cfg.timeout_ms,
        )
        return len(fact_ids)

    async def delete_product(self, *, product_id: str) -> int:
        graph = self._graph(product_id)
        await graph.query(
            "MATCH (n) WHERE n.product_id = $product_id DETACH DELETE n",
            {"product_id": product_id},
            timeout=self.cfg.timeout_ms,
        )
        return 0

    async def resolve_entity(
        self,
        *,
        product_id: str,
        mention: str,
        limit: int = 10,
    ) -> GraphQueryResult:
        graph = self._graph(product_id)
        await self._ensure_product_schema(product_id)
        result = await graph.ro_query(
            "MATCH (n) WHERE n.product_id = $product_id AND n.status = 'active' "
            "AND (n.stable_id = $mention "
            "OR toLower(coalesce(n.name, '')) CONTAINS toLower($mention) "
            "OR toLower(coalesce(n.resource_uri, '')) CONTAINS toLower($mention) "
            "OR toLower(coalesce(n.path, '')) CONTAINS toLower($mention)) "
            "RETURN n LIMIT $limit",
            {"product_id": product_id, "mention": mention, "limit": limit},
            timeout=self.cfg.timeout_ms,
        )
        return _query_result_to_graph(result)

    async def traverse(
        self,
        *,
        product_id: str,
        seed_ids: list[str],
        edge_types: list[str] | None = None,
        max_depth: int = 2,
        limit: int = 50,
    ) -> GraphQueryResult:
        if not seed_ids:
            return GraphQueryResult()
        await self._ensure_product_schema(product_id)
        graph = self._graph(product_id)
        type_clause = ""
        if edge_types:
            safe_types = "|".join(_ident(t) for t in edge_types if t in _EDGE_TYPES)
            if safe_types:
                type_clause = f":{safe_types}"
        query = (
            "MATCH p=(seed)-[r"
            f"{type_clause}*1..{max(1, min(max_depth, 5))}]-(n) "
            "WHERE seed.product_id = $product_id AND seed.stable_id IN $seed_ids "
            "AND n.product_id = $product_id AND n.status = 'active' "
            "RETURN seed, n, r LIMIT $limit"
        )
        result = await graph.ro_query(
            query,
            {"product_id": product_id, "seed_ids": seed_ids, "limit": limit},
            timeout=self.cfg.timeout_ms,
        )
        return _query_result_to_graph(result)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()

    def _connect(self) -> None:
        if self._db is not None:
            return
        from falkordb.asyncio import FalkorDB
        from redis.asyncio import BlockingConnectionPool, SSLConnection

        kwargs: dict[str, Any] = {
            "host": self.cfg.host,
            "port": self.cfg.port,
            "max_connections": self.cfg.max_connections,
            "timeout": None,
            "decode_responses": True,
        }
        if self.cfg.ssl:
            kwargs["connection_class"] = SSLConnection
        if self.cfg.username:
            kwargs["username"] = self.cfg.username
        if self.cfg.password:
            kwargs["password"] = self.cfg.password
        self._pool = BlockingConnectionPool(**kwargs)
        self._db = FalkorDB(connection_pool=self._pool)

    def _graph(self, product_id: str):
        self._connect()
        assert self._db is not None
        return self._db.select_graph(_graph_name(self.cfg.graph_prefix, product_id))

    async def _upsert_node(self, graph, node: GraphNode) -> None:
        labels = ":".join(_ident(label) for label in node.labels)
        props = _props(node)
        await graph.query(
            f"MERGE (n:{labels} {{stable_id: $stable_id}}) "
            "SET n += $props RETURN n.stable_id",
            {"stable_id": node.stable_id, "props": props},
            timeout=self.cfg.timeout_ms,
        )

    async def _upsert_edge(self, graph, edge: GraphEdge) -> None:
        props = _props(edge)
        rel_type = _ident(edge.type)
        await graph.query(
            "MATCH (a {stable_id: $from_id}), (b {stable_id: $to_id}) "
            "WHERE a.product_id = $product_id AND b.product_id = $product_id "
            f"MERGE (a)-[r:{rel_type} {{stable_id: $stable_id}}]->(b) "
            "SET r += $props RETURN r.stable_id",
            {
                "product_id": edge.product_id,
                "from_id": edge.from_id,
                "to_id": edge.to_id,
                "stable_id": edge.stable_id,
                "props": props,
            },
            timeout=self.cfg.timeout_ms,
        )

    async def _create_constraint(self, graph_name: str, label: str) -> None:
        self._connect()
        assert self._db is not None
        create = getattr(self._db, "create_constraint", None)
        if create is None:
            return
        try:
            await create(
                graph_name,
                "UNIQUE",
                "NODE",
                label,
                ["stable_id"],
            )
        except Exception as e:
            if _already_exists(e):
                return
            raise

    @staticmethod
    async def _ignore_existing(awaitable) -> None:
        try:
            await awaitable
        except Exception as e:
            if not _already_exists(e):
                raise


def _props(obj: GraphNode | GraphEdge) -> dict[str, Any]:
    data = obj.model_dump(mode="json")
    data.pop("labels", None)
    data.pop("type", None)
    data["source_refs_js"] = json.dumps(data.pop("source_refs", []), sort_keys=True)
    props = data.pop("properties", {})
    for key, value in props.items():
        if (
            isinstance(value, (str, int, float, bool))
            or value is None
            or (
                isinstance(value, list)
                and all(isinstance(v, (str, int, float, bool)) for v in value)
            )
        ):
            data[key] = value
        else:
            data[key] = json.dumps(value, sort_keys=True)
    return data


def _query_result_to_graph(result) -> GraphQueryResult:
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    paths: list[dict[str, Any]] = []
    for record in getattr(result, "result_set", []) or []:
        node_ids_before = set(nodes)
        edge_ids_before = set(edges)
        _collect_graph_values(record, nodes, edges)
        new_nodes = [sid for sid in nodes if sid not in node_ids_before]
        new_edges = [sid for sid in edges if sid not in edge_ids_before]
        if new_nodes or new_edges:
            paths.append({"node_ids": new_nodes, "edge_ids": new_edges})
    return GraphQueryResult(
        nodes=sorted(nodes.values(), key=lambda n: n.stable_id),
        edges=sorted(edges.values(), key=lambda e: e.stable_id),
        paths=paths,
    )


def _collect_graph_values(value, nodes: dict[str, GraphNode], edges: dict[str, GraphEdge]) -> None:
    converted_node = _falkor_node_to_graph(value)
    if converted_node is not None:
        nodes[converted_node.stable_id] = converted_node
        return
    converted_edge = _falkor_edge_to_graph(value)
    if converted_edge is not None:
        edges[converted_edge.stable_id] = converted_edge
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_graph_values(item, nodes, edges)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_graph_values(item, nodes, edges)


def _falkor_node_to_graph(value) -> GraphNode | None:
    labels = getattr(value, "labels", None)
    properties = getattr(value, "properties", None)
    if not isinstance(properties, dict) or labels is None:
        return None
    stable_id = properties.get("stable_id")
    product_id = properties.get("product_id")
    if not isinstance(stable_id, str) or not isinstance(product_id, str):
        return None
    props = dict(properties)
    source_refs = _source_refs_from_props(props)
    known = {
        "product_id",
        "stable_id",
        "source_refs_js",
        "confidence",
        "extraction_method",
        "last_seen",
        "freshness",
        "status",
    }
    return GraphNode(
        product_id=product_id,
        stable_id=stable_id,
        labels=list(labels or []),
        properties={k: v for k, v in props.items() if k not in known},
        source_refs=source_refs,
        confidence=float(props.get("confidence") or 1.0),
        extraction_method=props.get("extraction_method") or "deterministic",
        last_seen=props.get("last_seen") or "",
        freshness=float(props.get("freshness") or 1.0),
        status=props.get("status") or "active",
    )


def _falkor_edge_to_graph(value) -> GraphEdge | None:
    relation = getattr(value, "relation", None)
    properties = getattr(value, "properties", None)
    if not isinstance(properties, dict) or not isinstance(relation, str):
        return None
    stable_id = properties.get("stable_id")
    product_id = properties.get("product_id")
    from_id = properties.get("from_id")
    to_id = properties.get("to_id")
    if not all(isinstance(v, str) for v in (stable_id, product_id, from_id, to_id)):
        return None
    props = dict(properties)
    source_refs = _source_refs_from_props(props)
    known = {
        "product_id",
        "stable_id",
        "from_id",
        "to_id",
        "source_refs_js",
        "confidence",
        "extraction_method",
        "last_seen",
        "freshness",
        "status",
    }
    return GraphEdge(
        product_id=product_id,
        stable_id=stable_id,
        type=relation,
        from_id=from_id,
        to_id=to_id,
        properties={k: v for k, v in props.items() if k not in known},
        source_refs=source_refs,
        confidence=float(props.get("confidence") or 1.0),
        extraction_method=props.get("extraction_method") or "deterministic",
        last_seen=props.get("last_seen") or "",
        freshness=float(props.get("freshness") or 1.0),
        status=props.get("status") or "active",
    )


def _source_refs_from_props(props: dict[str, Any]) -> list[SourceRef]:
    raw = props.get("source_refs_js")
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    refs: list[SourceRef] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            refs.append(SourceRef(**item))
        except Exception:
            continue
    return refs


def _graph_name(prefix: str, product_id: str) -> str:
    return _ident(f"{prefix}_{product_id}".lower())


def _ident(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"g_{cleaned}"
    return cleaned[:120]


def _already_exists(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(part in text for part in ("already exists", "already indexed", "constraint already"))
