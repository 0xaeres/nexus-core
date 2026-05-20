"""Relation extractor - light LLM mines entities and relations from chunks.

Output schema (per ENGINEERING.md §4):
  entities  type in: service | module | symbol | ticket | ADR | RFC | incident | person
  relations type in: implements | references | supersedes | owned_by | closes | caused_by

For Slice 6 we only extract from DOC chunks by default — they yield richer
cross-source links (ADRs reference tickets, post-mortems blame services).
Code chunks fall back to tree-sitter symbols, captured at chunk-time. Toggle
via `ingestion.extract_relations.{docs,code}`.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from nexus.graph.store import EntityRef, RelationRef
from nexus.ingest.models import Chunk, ChunkKind

log = logging.getLogger(__name__)


_ENTITY_TYPES = {
    "service",
    "module",
    "symbol",
    "ticket",
    "adr",
    "rfc",
    "incident",
    "person",
}
_RELATION_TYPES = {
    "implements",
    "references",
    "supersedes",
    "owned_by",
    "closes",
    "caused_by",
}


@dataclass(frozen=True)
class ExtractionResult:
    entities: list[EntityRef]
    relations: list[RelationRef]


_SYSTEM = (
    "You extract structured knowledge for a code-and-docs RAG system. "
    "Read the excerpt and return ONLY entities and relations that are "
    "EXPLICITLY mentioned. Never invent. Never paraphrase. If unsure, omit."
)


_USER_TEMPLATE = """File: {file}
Kind: {kind}
Context: {context}

EXCERPT:
```
{snippet}
```

Output JSON only:

{{
  "entities": [
    {{"name": "...", "type": "service|module|symbol|ticket|adr|rfc|incident|person"}}
  ],
  "relations": [
    {{"src": "entity name", "dst": "entity name",
      "type": "implements|references|supersedes|owned_by|closes|caused_by"}}
  ]
}}

Rules:
- Tickets: JIRA-style keys like ENG-123, FORGE-77.
- ADRs / RFCs: by number ("ADR-014", "RFC-7") or title.
- Services / modules: only if named in the text (don't infer from filename alone).
- People: full names or clear handles only.
- Max 6 entities and 6 relations per excerpt.
- If nothing definite is present, return empty arrays.
"""


class RelationExtractor:
    """Pulls entities and relations out of chunks via the light LLM."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:3b",
        extract_docs: bool = True,
        extract_code: bool = False,
        timeout_s: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extract_docs = extract_docs
        self.extract_code = extract_code
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _should_extract(self, chunk: Chunk) -> bool:
        if chunk.kind is ChunkKind.CODE:
            return self.extract_code
        return self.extract_docs

    async def extract(self, chunks: Iterable[Chunk]) -> dict[str, ExtractionResult]:
        """Returns {chunk_id: ExtractionResult} for chunks we extracted from."""
        out: dict[str, ExtractionResult] = {}
        for chunk in chunks:
            if not self._should_extract(chunk):
                continue
            try:
                payload = await self._call(chunk)
            except Exception as e:
                log.debug("relation_extractor: %s -> %s", chunk.id, e)
                continue
            entities, relations = _parse(chunk, payload)
            if entities or relations:
                out[chunk.id] = ExtractionResult(entities=entities, relations=relations)
        return out

    async def _call(self, chunk: Chunk) -> dict:
        body = _USER_TEMPLATE.format(
            file=chunk.resource.uri,
            kind=chunk.kind.value,
            context=chunk.context_path or "<top-level>",
            snippet=chunk.content[:1500],
        )
        try:
            resp = await self._client.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": body},
                    ],
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 400},
                },
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"ollama call failed: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(f"ollama returned {resp.status_code}: {resp.text[:200]}")
        message = resp.json().get("message", {}) or {}
        content = message.get("content", "") or "{}"
        import json as _json

        try:
            return _json.loads(content)
        except _json.JSONDecodeError:
            return {}


# ---------------------------------------------------------------- parse


def _parse(chunk: Chunk, payload: dict) -> tuple[list[EntityRef], list[RelationRef]]:
    entities_raw = payload.get("entities") or []
    relations_raw = payload.get("relations") or []

    entities: list[EntityRef] = []
    name_to_id: dict[str, str] = {}
    for item in entities_raw[:6]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        type_ = str(item.get("type", "")).strip().lower()
        if not name or type_ not in _ENTITY_TYPES:
            continue
        eid = _entity_id(chunk.product_id, type_, name)
        if name in name_to_id:
            continue
        name_to_id[name] = eid
        entities.append(EntityRef(id=eid, type=type_, name=name))

    relations: list[RelationRef] = []
    for item in relations_raw[:6]:
        if not isinstance(item, dict):
            continue
        rtype = str(item.get("type", "")).strip().lower()
        src = str(item.get("src", "")).strip()
        dst = str(item.get("dst", "")).strip()
        if rtype not in _RELATION_TYPES or not src or not dst:
            continue
        if src not in name_to_id or dst not in name_to_id:
            continue
        relations.append(
            RelationRef(src_id=name_to_id[src], dst_id=name_to_id[dst], type=rtype)
        )

    return entities, relations


def _entity_id(product_id: str, type_: str, name: str) -> str:
    key = f"{product_id}|{type_}|{name.lower()}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]
