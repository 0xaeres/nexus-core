"""Query complexity classifier.

Routes simple lookups (exact symbol / path / single token) to a cheap Stage-1-only
path; complex / cross-source queries to the full 5-stage pipeline + HyDE.

Pure heuristic by default. Optionally augment with a light LLM probe — but most
of the signal comes from cheap structural cues (quotes, dots, snake/camel case,
short length, file extensions), so the heuristic is the production path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Identifier-like tokens: snake_case, camelCase, dotted.module.path, std.file:42 anchors.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_FILE_RE = re.compile(r"^[\w./\-]+\.\w+(:\d+)?$")
_HAS_QUOTED = re.compile(r'"[^"]+"|`[^`]+`')


class Complexity(StrEnum):
    SIMPLE = "simple"
    COMPLEX = "complex"


@dataclass(frozen=True)
class ClassifierResult:
    complexity: Complexity
    confidence: float  # 0..1
    reason: str

    @property
    def is_simple(self) -> bool:
        return self.complexity is Complexity.SIMPLE


def classify(query: str, *, threshold: float = 0.8) -> ClassifierResult:
    """Heuristic classification — used by the retrieval orchestrator at Stage 0."""
    q = query.strip()
    if not q:
        return ClassifierResult(Complexity.COMPLEX, 1.0, "empty query")

    word_count = len(q.split())
    has_question_mark = "?" in q
    has_natural_words = any(w.lower() in _NATURAL_WORDS for w in q.split())
    has_quoted = bool(_HAS_QUOTED.search(q))

    # Strong simple signals
    if _IDENT_RE.match(q):
        return ClassifierResult(Complexity.SIMPLE, 0.95, "single identifier")
    if _FILE_RE.match(q):
        return ClassifierResult(Complexity.SIMPLE, 0.95, "file path anchor")
    if word_count == 1 and not has_question_mark:
        return ClassifierResult(Complexity.SIMPLE, 0.85, "single token")

    # Strong complex signals
    if has_question_mark or has_natural_words or word_count >= 6:
        return ClassifierResult(
            Complexity.COMPLEX, 0.9, "natural-language phrasing or long query"
        )

    # Medium: 2-5 word phrases without strong signals — slight complex lean.
    if word_count >= 2:
        return ClassifierResult(
            Complexity.COMPLEX, 0.7, "multi-word phrase, no strong simple signal"
        )

    # Quoted exact phrase counts as simple (precise lookup intent)
    if has_quoted:
        return ClassifierResult(Complexity.SIMPLE, 0.9, "quoted exact phrase")

    return ClassifierResult(Complexity.COMPLEX, 0.6, "fallback")


# Common English words — presence implies natural-language phrasing.
_NATURAL_WORDS = {
    "how",
    "what",
    "why",
    "when",
    "where",
    "who",
    "which",
    "should",
    "would",
    "could",
    "does",
    "do",
    "is",
    "are",
    "was",
    "were",
    "the",
    "a",
    "an",
    "for",
    "with",
    "without",
    "and",
    "or",
    "but",
    "not",
    "this",
    "that",
    "these",
    "those",
    "in",
    "on",
    "of",
    "to",
    "from",
    "explain",
    "describe",
    "find",
    "show",
}
