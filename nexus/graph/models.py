"""Pydantic models for the derived product-system graph."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from nexus.retrieval.evidence import EvidenceCoverage, EvidenceMode, QueryPlan, RetrievalTrace

GraphStatus = Literal["active", "stale", "corrected", "deleted"]
ExtractionMethod = Literal["deterministic", "heuristic", "llm", "human"]


class SourceRef(BaseModel):
    product_id: str
    source_key: str
    source_id: str
    resource_uri: str
    anchor: str
    start_line: int | None = None
    end_line: int | None = None


class GraphNode(BaseModel):
    product_id: str
    stable_id: str
    labels: list[str]
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(default_factory=list)
    confidence: float = 1.0
    extraction_method: ExtractionMethod = "deterministic"
    last_seen: str
    freshness: float = 1.0
    status: GraphStatus = "active"

    @property
    def fact_id(self) -> str:
        return self.stable_id


class GraphEdge(BaseModel):
    product_id: str
    stable_id: str
    type: str
    from_id: str
    to_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(default_factory=list)
    confidence: float = 1.0
    extraction_method: ExtractionMethod = "deterministic"
    last_seen: str
    freshness: float = 1.0
    status: GraphStatus = "active"

    @property
    def fact_id(self) -> str:
        return self.stable_id


class GraphExtraction(BaseModel):
    product_id: str
    source_key: str
    resource_uri: str
    extraction_version: str
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    @property
    def fact_ids(self) -> list[str]:
        return [*[n.fact_id for n in self.nodes], *[e.fact_id for e in self.edges]]

    @property
    def node_ids(self) -> list[str]:
        return [n.stable_id for n in self.nodes]


class GraphQueryResult(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    paths: list[dict[str, Any]] = Field(default_factory=list)


class GraphContextEntity(BaseModel):
    stable_id: str
    labels: list[str] = Field(default_factory=list)
    name: str = ""
    resource_uri: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    confidence: float = 1.0


class GraphEvidenceHit(BaseModel):
    score: float
    source: str
    anchor: str
    context_path: str | None = None
    content: str | None = None
    graph_node_ids: list[str] = Field(default_factory=list)


class CodeContextPack(BaseModel):
    product_id: str
    query: str
    current_file: str | None = None
    resolved_entities: list[GraphContextEntity] = Field(default_factory=list)
    neighbor_entities: list[GraphContextEntity] = Field(default_factory=list)
    graph_edges: list[GraphEdge] = Field(default_factory=list)
    evidence: list[GraphEvidenceHit] = Field(default_factory=list)
    graph_used: bool = False
    graph_available: bool = False
    reranked: bool = False
    unknowns: list[str] = Field(default_factory=list)


class AffectedEntity(BaseModel):
    stable_id: str
    labels: list[str] = Field(default_factory=list)
    name: str = ""
    resource_uri: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    confidence: float = 1.0
    category: str = "other"


class ImpactAnalysis(BaseModel):
    product_id: str
    query: str
    changed_files: list[str] = Field(default_factory=list)
    seed_entities: list[AffectedEntity] = Field(default_factory=list)
    affected_entities: list[AffectedEntity] = Field(default_factory=list)
    graph_edges: list[GraphEdge] = Field(default_factory=list)
    evidence: list[GraphEvidenceHit] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    graph_available: bool = False
    graph_used: bool = False
    reranked: bool = False
    unknowns: list[str] = Field(default_factory=list)


class DependencyTrace(BaseModel):
    product_id: str
    query: str
    seed_entities: list[AffectedEntity] = Field(default_factory=list)
    upstream: list[AffectedEntity] = Field(default_factory=list)
    downstream: list[AffectedEntity] = Field(default_factory=list)
    graph_edges: list[GraphEdge] = Field(default_factory=list)
    evidence: list[GraphEvidenceHit] = Field(default_factory=list)
    confidence: float = 0.0
    graph_available: bool = False
    graph_used: bool = False
    reranked: bool = False
    unknowns: list[str] = Field(default_factory=list)


class GraphRAGCitation(BaseModel):
    id: str
    anchor: str
    source: str = ""
    context_path: str | None = None
    graph_node_ids: list[str] = Field(default_factory=list)
    excerpt: str = ""


class GraphRAGPath(BaseModel):
    seed_id: str
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0


class GraphRAGMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class GraphRAGQuery(BaseModel):
    query: str
    history: list[GraphRAGMessage] = Field(default_factory=list)
    current_file: str | None = None
    mode: EvidenceMode = "auto"
    max_depth: int = 3
    top_k: int = 8


class GraphRAGAnswer(BaseModel):
    product_id: str
    query: str
    session_id: str | None = None
    answer: str = ""
    citations: list[GraphRAGCitation] = Field(default_factory=list)
    graph_paths: list[GraphRAGPath] = Field(default_factory=list)
    resolved_entities: list[GraphContextEntity] = Field(default_factory=list)
    confidence: float = 0.0
    trace: list[RetrievalTrace] = Field(default_factory=list)
    coverage: EvidenceCoverage | None = None
    query_plan: QueryPlan | None = None
    graph_available: bool = False
    graph_used: bool = False
    reranked: bool = False
    needs_clarification: bool = False
    clarification_options: list[GraphContextEntity] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class GraphStore(Protocol):
    async def ensure_schema(self) -> None: ...

    async def upsert_resource_graph(
        self,
        extraction: GraphExtraction,
        *,
        previous_fact_ids: list[str] | None = None,
    ) -> list[str]: ...

    async def retire_resource_graph(
        self,
        *,
        product_id: str,
        fact_ids: list[str],
    ) -> int: ...

    async def delete_product(self, *, product_id: str) -> int: ...

    async def resolve_entity(
        self,
        *,
        product_id: str,
        mention: str,
        limit: int = 10,
    ) -> GraphQueryResult: ...

    async def traverse(
        self,
        *,
        product_id: str,
        seed_ids: list[str],
        edge_types: list[str] | None = None,
        max_depth: int = 2,
        limit: int = 50,
    ) -> GraphQueryResult: ...

    async def aclose(self) -> None: ...
