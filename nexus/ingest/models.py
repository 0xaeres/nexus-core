"""Ingestion data model.

A `ResourceRef` identifies one file/page/document inside a source.
A `Chunk` is a span of that resource carrying a `file:line` anchor.
An `EmbeddedChunk` is a chunk plus its dense vector (named in Qdrant).
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, computed_field

_NEXUS_NS = uuid.UUID("8c4f4d7e-2c1b-4a6a-9a3e-1f5b8d2c9e10")


class ChunkKind(StrEnum):
    CODE = "code"
    DOC = "doc"


class ResourceRef(BaseModel):
    """Pointer to a single addressable resource within a source."""

    source_id: str  # e.g. "github:myorg/repo" or "local:/abs/path"
    uri: str  # canonical URI for the resource (path or URL)
    mime: str
    size_bytes: int | None = None
    last_modified: str | None = None  # ISO-8601 when known

    @computed_field
    @property
    def kind(self) -> ChunkKind:
        if _is_code_mime(self.mime) or _is_code_path(self.uri):
            return ChunkKind.CODE
        return ChunkKind.DOC


class Chunk(BaseModel):
    """A retrievable span of a resource."""

    product_id: str
    resource: ResourceRef
    content: str
    start_line: int  # 1-indexed (first line of content)
    end_line: int  # 1-indexed, inclusive
    kind: ChunkKind
    # Structural context discovered by the chunker (function/class/heading name)
    context_path: str | None = None
    # Filled by the contextual enricher (ADR-010); falsy = no enrichment
    context_summary: str | None = None

    @computed_field
    @property
    def id(self) -> str:
        """Deterministic content-addressable UUID (valid Qdrant point ID)."""
        key = f"{self.product_id}|{self.resource.uri}|{self.start_line}-{self.end_line}"
        return str(uuid.uuid5(_NEXUS_NS, key))

    @property
    def anchor(self) -> str:
        """The `file:line` anchor used in citations."""
        return f"{self.resource.uri}:{self.start_line}"

    def text_for_embedding(self) -> str:
        """Return the string actually fed to the embedder (with context prepended)."""
        if self.context_summary:
            return f"{self.context_summary}\n\n{self.content}"
        return self.content


class EmbeddedChunk(BaseModel):
    """A chunk with its computed vector for the appropriate Qdrant named vector."""

    chunk: Chunk
    vector: list[float]
    vector_name: Literal["dense_code", "dense_text"]


# ---------------------------------------------------------------- helpers


_CODE_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".cs",
}

_CODE_MIMES = {
    "text/x-python",
    "text/x-typescript",
    "text/x-javascript",
    "application/javascript",
    "text/x-rust",
    "text/x-go",
}


def _is_code_path(uri: str) -> bool:
    return any(uri.endswith(ext) for ext in _CODE_EXTS)


def _is_code_mime(mime: str) -> bool:
    return mime in _CODE_MIMES or mime.startswith("text/x-")


def guess_mime(path: str) -> str:
    """Lightweight mime guess based on extension; used by sources that don't carry mimes."""
    lower = path.lower()
    if lower.endswith(".py"):
        return "text/x-python"
    if lower.endswith((".ts", ".tsx")):
        return "text/x-typescript"
    if lower.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "application/javascript"
    if lower.endswith(".rs"):
        return "text/x-rust"
    if lower.endswith(".go"):
        return "text/x-go"
    if lower.endswith((".md", ".mdx")):
        return "text/markdown"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".txt", ".rst")):
        return "text/plain"
    return "application/octet-stream"
