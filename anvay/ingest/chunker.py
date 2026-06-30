"""Chunker — tree-sitter for code, heading-based splitter for markdown, char-split fallback.

Returns `Chunk` objects with 1-indexed `start_line`/`end_line` anchors. Per ADR / spec §4:
preserve function/class boundaries for code; preserve heading hierarchy for markdown.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from dataclasses import dataclass

import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_kotlin
import tree_sitter_python
import tree_sitter_rust
import tree_sitter_solidity
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from anvay.ingest.models import Chunk, ChunkKind, ResourceRef

# ---------------------------------------------------------------- constants

MAX_CHUNK_CHARS = 1200    # ~300-400 tokens; leaves headroom for HQE prefix under llama.cpp --ubatch-size 512
MIN_CHUNK_CHARS = 64  # for code — filters trivial 1-liner defs
MIN_DOC_CHUNK_CHARS = 20  # docs: keep short sections (headings + a sentence)
CHAR_SPLIT_TARGET = 700   # target for oversized chunks; ~175-250 tokens
CHAR_SPLIT_OVERLAP = 70



# ---------------------------------------------------------------- per-language config


@dataclass(frozen=True)
class _LangCfg:
    language: Language
    # Tree-sitter node types we consider "chunk boundaries" — these are the spans
    # we emit as chunks. Anything outside them is captured in a "module"-level chunk.
    boundary_nodes: tuple[str, ...]
    # Node types whose `name` child should be used as the context path component.
    name_field_nodes: tuple[str, ...] = ()
    # Node types that are doc-comments (block/line) living as prev-siblings of a
    # declaration.  When set, the chunker extends the chunk span back to include the
    # leading comment so it is NOT emitted as an orphaned <module> chunk.
    doc_comment_nodes: tuple[str, ...] = ()
    # Optional text-prefix filter for doc_comment_nodes. When non-empty, a comment
    # node is only attached if its text starts with one of these prefixes. Use this
    # for languages (e.g. Rust) where the same tree-sitter node type covers both
    # outer-doc comments (``///``, ``/**``) and plain comments (``//``, ``/*``).
    doc_comment_prefix: tuple[str, ...] = ()


def _language(raw) -> Language:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="int argument support is deprecated",
            category=DeprecationWarning,
        )
        return Language(raw)


_LANGS: dict[str, _LangCfg] = {
    "python": _LangCfg(
        language=_language(tree_sitter_python.language()),
        boundary_nodes=("function_definition", "class_definition", "decorated_definition"),
        name_field_nodes=("function_definition", "class_definition"),
    ),
    "typescript": _LangCfg(
        language=_language(tree_sitter_typescript.language_typescript()),
        boundary_nodes=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "interface_declaration",
            "type_alias_declaration",
            "export_statement",
            "variable_declarator",
        ),
        name_field_nodes=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "interface_declaration",
            "type_alias_declaration",
            "variable_declarator",
        ),
        doc_comment_nodes=("comment",),
    ),
    "tsx": _LangCfg(
        language=_language(tree_sitter_typescript.language_tsx()),
        boundary_nodes=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "interface_declaration",
            "type_alias_declaration",
            "export_statement",
            "variable_declarator",
        ),
        name_field_nodes=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "interface_declaration",
            "type_alias_declaration",
            "variable_declarator",
        ),
        doc_comment_nodes=("comment",),
    ),
    "javascript": _LangCfg(
        language=_language(tree_sitter_javascript.language()),
        boundary_nodes=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "export_statement",
            "variable_declarator",
        ),
        name_field_nodes=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "variable_declarator",
        ),
        doc_comment_nodes=("comment",),
    ),
    "rust": _LangCfg(
        language=_language(tree_sitter_rust.language()),
        boundary_nodes=("function_item", "impl_item", "struct_item", "trait_item", "enum_item"),
        name_field_nodes=("function_item", "impl_item", "struct_item", "trait_item", "enum_item"),
        # tree-sitter-rust uses `line_comment` for both `///` outer-doc and plain `//`,
        # and `block_comment` for both `/** */` outer-doc and plain `/* */`. Restrict
        # attachment to outer-doc markers only so `//` and `//!` inner-module docs
        # don't get pulled into unrelated declaration chunks.
        doc_comment_nodes=("line_comment", "block_comment"),
        doc_comment_prefix=("///", "/**"),
    ),
    "go": _LangCfg(
        language=_language(tree_sitter_go.language()),
        boundary_nodes=("function_declaration", "method_declaration", "type_declaration"),
        name_field_nodes=("function_declaration", "method_declaration"),
        doc_comment_nodes=("comment",),
    ),
    "java": _LangCfg(
        language=_language(tree_sitter_java.language()),
        boundary_nodes=(
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "constructor_declaration",
            "method_declaration",
        ),
        name_field_nodes=(
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "constructor_declaration",
            "method_declaration",
        ),
        doc_comment_nodes=("block_comment", "line_comment"),
    ),
    "cpp": _LangCfg(
        language=_language(tree_sitter_cpp.language()),
        boundary_nodes=(
            "namespace_definition",
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
            "template_declaration",
            "function_definition",
            "declaration",
        ),
        name_field_nodes=(
            "namespace_definition",
            "class_specifier",
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
            "function_definition",
            "declaration",
        ),
    ),
    "kotlin": _LangCfg(
        language=_language(tree_sitter_kotlin.language()),
        boundary_nodes=(
            "class_declaration",
            "object_declaration",
            "function_declaration",
            "property_declaration",
            "type_alias",
        ),
        name_field_nodes=(
            "class_declaration",
            "object_declaration",
            "function_declaration",
            "property_declaration",
            "type_alias",
        ),
    ),
    "solidity": _LangCfg(
        language=_language(tree_sitter_solidity.language()),
        boundary_nodes=(
            "contract_declaration",
            "interface_declaration",
            "library_declaration",
            "function_definition",
            "modifier_definition",
            "struct_declaration",
            "enum_declaration",
            "event_definition",
            "error_declaration",
        ),
        name_field_nodes=(
            "contract_declaration",
            "interface_declaration",
            "library_declaration",
            "function_definition",
            "modifier_definition",
            "struct_declaration",
            "enum_declaration",
            "event_definition",
            "error_declaration",
        ),
    ),
}


def _lang_for(uri: str) -> str | None:
    lower = uri.lower()
    if lower.endswith(".py"):
        return "python"
    if lower.endswith((".ts",)):
        return "typescript"
    if lower.endswith((".tsx",)):
        return "tsx"
    if lower.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "javascript"
    if lower.endswith(".rs"):
        return "rust"
    if lower.endswith(".go"):
        return "go"
    if lower.endswith(".java"):
        return "java"
    if lower.endswith((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h")):
        return "cpp"
    if lower.endswith((".kt", ".kts")):
        return "kotlin"
    if lower.endswith(".sol"):
        return "solidity"
    return None


# ---------------------------------------------------------------- entry point


def chunk_resource(
    product_id: str, resource: ResourceRef, content: str
) -> list[Chunk]:
    """Route to the right chunker based on resource type/extension."""
    if not content.strip():
        return []
    lang = _lang_for(resource.uri)
    if lang:
        return list(_chunk_code(product_id, resource, content, lang))
    if resource.mime == "text/markdown" or resource.uri.lower().endswith((".md", ".mdx")):
        return list(_chunk_markdown(product_id, resource, content))
    if resource.kind is ChunkKind.CODE:
        return list(_chunk_plain_text(product_id, resource, content, kind=ChunkKind.CODE))
    return list(_chunk_plain_text(product_id, resource, content))


# ---------------------------------------------------------------- code (tree-sitter)


def _chunk_code(
    product_id: str, resource: ResourceRef, content: str, lang: str
) -> Iterator[Chunk]:
    cfg = _LANGS[lang]
    parser = Parser(cfg.language)
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node
    lines = content.splitlines()

    emitted_ranges: list[tuple[int, int]] = []

    for node, ctx_path in _walk_boundaries(root, cfg, parent_ctx=""):
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1

        # Attach immediately-preceding doc-comment(s) (e.g. Java javadoc, JSDoc, rustdoc)
        # so they land in the same chunk as the declaration rather than as orphaned
        # <module> chunks with no useful context_path.  Walk backwards through a run of
        # consecutive adjacent comment siblings (handles both /** block */ and // lines).
        if cfg.doc_comment_nodes:
            earliest_comment_start: int | None = None
            probe = node.prev_named_sibling
            ref_line = node.start_point[0]  # 0-indexed
            while probe is not None and probe.type in cfg.doc_comment_nodes:
                if cfg.doc_comment_prefix:
                    text = (probe.text or b"").decode("utf-8", errors="replace")
                    if not any(text.startswith(p) for p in cfg.doc_comment_prefix):
                        break
                gap = ref_line - probe.end_point[0]
                # Allow up to one blank line between comment and declaration.
                # gap==1 means adjacent (no blank line); gap==2 means one blank line.
                if gap > 2:
                    break
                earliest_comment_start = probe.start_point[0] + 1
                ref_line = probe.start_point[0]
                probe = probe.prev_named_sibling
            if earliest_comment_start is not None:
                start = earliest_comment_start

        span = "\n".join(lines[start - 1 : end])
        if len(span) < MIN_CHUNK_CHARS:
            continue
        if len(span) > MAX_CHUNK_CHARS:
            for sub in _split_oversized(span, start):
                sub_start, sub_text = sub
                sub_end = sub_start + sub_text.count("\n")
                yield Chunk(
                    product_id=product_id,
                    resource=resource,
                    content=sub_text,
                    start_line=sub_start,
                    end_line=sub_end,
                    kind=ChunkKind.CODE,
                    context_path=ctx_path,
                )
                emitted_ranges.append((sub_start, sub_end))
        else:
            yield Chunk(
                product_id=product_id,
                resource=resource,
                content=span,
                start_line=start,
                end_line=end,
                kind=ChunkKind.CODE,
                context_path=ctx_path,
            )
            emitted_ranges.append((start, end))

    # Capture top-level statements / imports that fall outside any boundary node.
    yield from _emit_uncovered(product_id, resource, lines, emitted_ranges, ChunkKind.CODE)


def _walk_boundaries(
    node: Node, cfg: _LangCfg, parent_ctx: str
) -> Iterator[tuple[Node, str]]:
    for child in node.children:
        if child.type in cfg.boundary_nodes:
            name = _identifier_of(child) if child.type in cfg.name_field_nodes else None
            ctx = f"{parent_ctx}.{name}" if parent_ctx and name else (name or parent_ctx)
            yield child, ctx
            yield from _walk_boundaries(child, cfg, ctx)
        else:
            yield from _walk_boundaries(child, cfg, parent_ctx)


def _identifier_of(node: Node) -> str | None:
    """Find a child named 'name' or first 'identifier' descendant."""
    name = node.child_by_field_name("name")
    if name is not None:
        return name.text.decode("utf-8", errors="replace")
    type_name = node.child_by_field_name("type")
    if type_name is not None and type_name.type in ("type_identifier", "namespace_identifier"):
        return type_name.text.decode("utf-8", errors="replace")
    for child in node.children:
        if child.type in (
            "identifier",
            "field_identifier",
            "namespace_identifier",
            "type_identifier",
            "property_identifier",
        ):
            return child.text.decode("utf-8", errors="replace")
        nested = _identifier_of(child)
        if nested:
            return nested
    return None


# ---------------------------------------------------------------- markdown


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _chunk_markdown(
    product_id: str, resource: ResourceRef, content: str
) -> Iterator[Chunk]:
    lines = content.splitlines()
    sections: list[tuple[int, int, str, str]] = []  # (start_line, end_line, heading_path, body)
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    current_start = 1
    current_path = ""
    buf: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        if len(body) >= MIN_DOC_CHUNK_CHARS:
            sections.append((current_start, end_line, current_path, body))
        buf = []

    for i, raw in enumerate(lines, start=1):
        m = _HEADING_RE.match(raw)
        if m:
            flush(i - 1 if i > 1 else 1)
            level = len(m.group(1))
            title = m.group(2)
            heading_stack = [(lv, t) for lv, t in heading_stack if lv < level]
            heading_stack.append((level, title))
            current_path = " / ".join(t for _, t in heading_stack)
            current_start = i
            buf.append(raw)
        else:
            buf.append(raw)
    flush(len(lines))

    for start, end, path, body in sections:
        if len(body) > MAX_CHUNK_CHARS:
            for sub_start, sub_text in _split_oversized(body, start):
                sub_end = sub_start + sub_text.count("\n")
                yield Chunk(
                    product_id=product_id,
                    resource=resource,
                    content=sub_text,
                    start_line=sub_start,
                    end_line=sub_end,
                    kind=ChunkKind.DOC,
                    context_path=path or None,
                )
        else:
            yield Chunk(
                product_id=product_id,
                resource=resource,
                content=body,
                start_line=start,
                end_line=end,
                kind=ChunkKind.DOC,
                context_path=path or None,
            )


# ---------------------------------------------------------------- plain text


def _chunk_plain_text(
    product_id: str,
    resource: ResourceRef,
    content: str,
    *,
    kind: ChunkKind = ChunkKind.DOC,
) -> Iterator[Chunk]:
    for start, text in _split_oversized(content, start_line=1):
        end = start + text.count("\n")
        if len(text) < MIN_CHUNK_CHARS:
            continue
        yield Chunk(
            product_id=product_id,
            resource=resource,
            content=text,
            start_line=start,
            end_line=end,
            kind=kind,
            context_path="<module>" if kind is ChunkKind.CODE else None,
        )


# ---------------------------------------------------------------- helpers


def _split_oversized(text: str, start_line: int) -> Iterator[tuple[int, str]]:
    """Recursive char split with line-tracking; yields (start_line, sub_text)."""
    if len(text) <= MAX_CHUNK_CHARS:
        yield start_line, text
        return
    lines = text.splitlines(keepends=True)
    buf: list[str] = []
    buf_start = start_line
    buf_len = 0
    cursor = start_line
    for line in lines:
        if buf_len + len(line) > CHAR_SPLIT_TARGET and buf:
            yield buf_start, "".join(buf).rstrip("\n")
            # overlap: keep last few lines as context for the next chunk
            overlap_lines: list[str] = []
            overlap_chars = 0
            for prev in reversed(buf):
                overlap_chars += len(prev)
                overlap_lines.insert(0, prev)
                if overlap_chars >= CHAR_SPLIT_OVERLAP:
                    break
            buf = list(overlap_lines)
            buf_start = cursor - len(overlap_lines) + 1
            buf_len = sum(len(b) for b in buf)
        buf.append(line)
        buf_len += len(line)
        cursor += 1
    if buf:
        yield buf_start, "".join(buf).rstrip("\n")


def _emit_uncovered(
    product_id: str,
    resource: ResourceRef,
    lines: list[str],
    emitted: list[tuple[int, int]],
    kind: ChunkKind,
) -> Iterator[Chunk]:
    """Emit chunks for line ranges not already covered by boundary chunks (e.g. imports)."""
    covered = sorted(emitted)
    cursor = 1
    for start, end in covered:
        if cursor < start:
            gap = "\n".join(lines[cursor - 1 : start - 1]).strip()
            if len(gap) >= MIN_CHUNK_CHARS:
                yield Chunk(
                    product_id=product_id,
                    resource=resource,
                    content=gap,
                    start_line=cursor,
                    end_line=start - 1,
                    kind=kind,
                    context_path="<module>",
                )
        cursor = max(cursor, end + 1)
    if cursor <= len(lines):
        tail = "\n".join(lines[cursor - 1 :]).strip()
        if len(tail) >= MIN_CHUNK_CHARS:
            yield Chunk(
                product_id=product_id,
                resource=resource,
                content=tail,
                start_line=cursor,
                end_line=len(lines),
                kind=kind,
                context_path="<module>",
            )


__all__ = ["MAX_CHUNK_CHARS", "MIN_CHUNK_CHARS", "chunk_resource"]
