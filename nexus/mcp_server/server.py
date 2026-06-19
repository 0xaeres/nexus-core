"""Nexus MCP server (stdio transport).

Launched by an MCP client (e.g. Claude Desktop) as a subprocess. Each request
is served against a single product configured at launch:

  uv run python -m nexus.mcp_server.server --product <your-product-id>

Exposes the skill + corpus tools from ENGINEERING.md §8.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Template
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool

from nexus.config import NexusConfig
from nexus.mcp_server import tools as nx_tools

log = logging.getLogger("nexus.mcp_server")


def _build_server(*, product: str, config: NexusConfig) -> Server:
    server: Server = Server("nexus")
    state = nx_tools.ToolState(product=product, config=config)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="find_skills",
                description=(
                    "Rank curated skills relevant to a query+context. Returns skill IDs, "
                    "names, kinds, confidence, and one-line summaries. Always call this "
                    "first when starting a new task. Pass `current_file` and/or "
                    "`context` to hard-filter by skill applicability and keep the "
                    "response tight."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "context": {"type": "string", "default": "general"},
                        "current_file": {"type": "string"},
                        "top_k": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_skill",
                description=(
                    "Return the full Markdown body and parsed frontmatter for a named skill."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ),
            Tool(
                name="report_outcome",
                description="Tell Nexus whether a skill helped. Feeds staleness tracking.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string"},
                        "succeeded": {"type": "boolean"},
                        "notes": {"type": "string", "default": ""},
                    },
                    "required": ["skill_name", "succeeded"],
                },
            ),
            Tool(
                name="query_code_context",
                description=(
                    "Locate code chunks by symbol or pattern. Cheap, fast — prefer this "
                    "when you know an identifier name and want all usages."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "file_glob": {"type": "string", "default": "**/*"},
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="hybrid_search_corpus",
                description=(
                    "Hybrid retrieval (dense + BM25 + rerank) against the raw corpus. "
                    "Use when symbol lookup is too narrow or you need cross-source context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="grep_corpus",
                description=(
                    "Exact indexed chunk grep over the product corpus. Use for symbols, "
                    "constants, file names, routes, and literal terms that semantic search may miss."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "default": 8},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="evidence_search_corpus",
                description=(
                    "EvidenceGraphRAG retrieval across hybrid search, exact grep, repo map, "
                    "graph-local context, and approved skills. Prefer this for product-system "
                    "questions that need accurate, cited context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "current_file": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "local", "global", "drift_lite"],
                            "default": "auto",
                        },
                        "top_k": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="ask_product_graph",
                description=(
                    "Generic product-system GraphRAG for arbitrary multi-hop questions. "
                    "Resolves entities, traverses the product graph, retrieves cited corpus "
                    "evidence, and returns an evidence-backed answer with confidence and unknowns."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "history": {"type": "array", "default": []},
                        "current_file": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "local", "global", "drift_lite"],
                            "default": "auto",
                        },
                        "max_depth": {"type": "integer", "default": 3},
                        "top_k": {"type": "integer", "default": 8},
                        "synthesize": {"type": "boolean", "default": True},
                    },
                    "required": ["query"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        args = arguments or {}
        try:
            if name == "find_skills":
                payload = await nx_tools.find_skills(state, **args)
            elif name == "get_skill":
                payload = await nx_tools.get_skill(state, **args)
            elif name == "report_outcome":
                payload = await nx_tools.report_outcome(state, **args)
            elif name == "query_code_context":
                payload = await nx_tools.query_code_context(state, **args)
            elif name == "hybrid_search_corpus":
                payload = await nx_tools.hybrid_search_corpus(state, **args)
            elif name == "grep_corpus":
                payload = await nx_tools.grep_corpus(state, **args)
            elif name == "evidence_search_corpus":
                payload = await nx_tools.evidence_search_corpus(state, **args)
            elif name == "ask_product_graph":
                payload = await nx_tools.ask_product_graph(state, **args)
            else:
                payload = {"error": f"unknown tool: {name}"}
        except Exception as e:
            log.exception("tool %s failed", name)
            payload = {"error": str(e)}
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(
                uri="nexus://meta-skill",
                name="How to Use Nexus",
                description="Bootstraps any MCP client on first connect.",
                mimeType="text/markdown",
            ),
            Resource(
                uri="nexus://hierarchy",
                name="Skill Hierarchy",
                description="Full product skill tree as JSON.",
                mimeType="application/json",
            ),
            Resource(
                uri=f"nexus://corpus/{product}",
                name=f"Corpus summary — {product}",
                description="Source/chunk counts and last indexed.",
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def read_resource(uri: str) -> str:
        if uri == "nexus://meta-skill":
            return await _render_meta_skill(state)
        if uri == "nexus://hierarchy":
            tree = await nx_tools.skill_hierarchy(state)
            return json.dumps(tree, indent=2)
        if uri.startswith("nexus://skills/"):
            name = uri.removeprefix("nexus://skills/")
            return await nx_tools.skill_markdown(state, name=name)
        if uri.startswith("nexus://corpus/"):
            requested = uri.removeprefix("nexus://corpus/")
            summary = await nx_tools.corpus_summary(state, product_id=requested)
            return json.dumps(summary, indent=2)
        raise ValueError(f"unknown resource: {uri}")

    return server


async def _render_meta_skill(state: nx_tools.ToolState) -> str:
    template_path = Path(__file__).parent / "meta_skill.md.j2"
    template = Template(template_path.read_text(encoding="utf-8"))
    hierarchy = await nx_tools.skill_hierarchy(state)
    corpus = await nx_tools.corpus_summary(state, product_id=state.product)
    summary_lines = []
    for s in hierarchy.get("skills", []):
        summary_lines.append(
            f"- **{s['name']}** ({s['tier']}, confidence={s['confidence']:.2f}): "
            f"{s.get('description') or 'No description.'}"
        )
    return template.render(
        now_iso=datetime.now(UTC).isoformat(),
        product_name=state.product,
        skill_hierarchy_summary="\n".join(summary_lines) or "_(no skills indexed yet)_",
        corpus_summary=json.dumps(corpus, indent=2),
        chunk_count=corpus.get("chunk_count", 0),
        source_count=corpus.get("source_count", 0),
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Nexus MCP server (stdio).")
    p.add_argument(
        "--product",
        default=os.environ.get("NEXUS_PRODUCT"),
        required=not os.environ.get("NEXUS_PRODUCT"),
        help="Product ID to serve. Set via --product or NEXUS_PRODUCT env var.",
    )
    p.add_argument(
        "--config",
        default=os.environ.get("NEXUS_CONFIG", "nexus.yaml"),
        help="Path to nexus.yaml.",
    )
    return p.parse_args()


async def amain() -> None:
    args = _parse_args()
    config = NexusConfig.load(args.config)
    server = _build_server(product=args.product, config=config)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(amain())


if __name__ == "__main__":  # pragma: no cover
    main()
