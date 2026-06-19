"""Product-scoped derived graph extraction and storage."""

from __future__ import annotations

from nexus.graph.context import build_code_context_pack
from nexus.graph.extractor import extract_resource_graph, graph_extraction_version
from nexus.graph.impact import build_change_impact, build_dependency_trace
from nexus.graph.rag import answer_graph_rag
from nexus.graph.store import create_graph_store

__all__ = [
    "answer_graph_rag",
    "build_change_impact",
    "build_code_context_pack",
    "build_dependency_trace",
    "create_graph_store",
    "extract_resource_graph",
    "graph_extraction_version",
]
