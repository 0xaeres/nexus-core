"""Prompt-injection guard - regex scan retrieved chunks before agent injection.

The corpus we retrieve from is partially untrusted (PRs, issues, Slack threads
mirror in via MCP). An adversarial commit or comment can carry classic prompt
injection patterns:

  ignore previous instructions
  system: you are now ...
  <|im_start|>system
  ```
  SYSTEM: ...
  ```

We scan retrieved chunk content for a small allow-list of high-precision
patterns. Flagged chunks are redacted (content replaced with a notice) and
the event is logged so Langfuse / OTel pipeline traces show the redaction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore-previous",
        re.compile(
            r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role-spoof",
        re.compile(
            r"^\s*(?:system|assistant|user)\s*[:>]\s*you\s+are\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "chat-template-token",
        re.compile(r"<\|(?:im_start|im_end|system|assistant|user)\|>", re.IGNORECASE),
    ),
    (
        "fenced-system-prompt",
        re.compile(
            r"```[a-zA-Z]*\s*\n\s*(?:system|assistant)\s*:",
            re.IGNORECASE,
        ),
    ),
    (
        "override-rules",
        re.compile(
            r"\b(?:disregard|forget)\s+(?:everything|all|previous)\s+(?:above|before|instructions)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tool-spoof",
        re.compile(
            r"\b(?:call|invoke)\s+(?:the\s+)?tool\s+[`'\"]?(?:exec|shell|run_command|sh)\b",
            re.IGNORECASE,
        ),
    ),
)


_REDACTED = "[REDACTED: prompt-injection pattern detected]"


@dataclass(frozen=True)
class GuardHit:
    pattern: str
    span: tuple[int, int]
    excerpt: str


@dataclass
class GuardResult:
    hits: list[GuardHit]
    redacted_content: str
    is_clean: bool


def scan(content: str) -> GuardResult:
    if not content:
        return GuardResult(hits=[], redacted_content=content, is_clean=True)
    hits: list[GuardHit] = []
    for name, pat in _PATTERNS:
        for m in pat.finditer(content):
            hits.append(
                GuardHit(
                    pattern=name,
                    span=(m.start(), m.end()),
                    excerpt=content[max(0, m.start() - 30) : m.end() + 30],
                )
            )
    redacted = _REDACTED if hits else content
    return GuardResult(hits=hits, redacted_content=redacted, is_clean=not hits)


def scan_payloads(payloads: list[dict]) -> tuple[list[dict], list[GuardHit]]:
    """Returns (safe_payloads, hits). Payloads keep their structure; chunk content
    is replaced wholesale when any pattern fires (cheaper than per-span redaction
    and keeps citations intact)."""
    safe: list[dict] = []
    all_hits: list[GuardHit] = []
    for p in payloads:
        content = p.get("content", "") or ""
        result = scan(content)
        if result.is_clean:
            safe.append(p)
            continue
        all_hits.extend(result.hits)
        log.warning(
            "guard: redacted %s:%s (%d hits)",
            p.get("resource_uri", "?"),
            p.get("start_line", "?"),
            len(result.hits),
        )
        safe.append({**p, "content": result.redacted_content, "guard_redacted": True})
    return safe, all_hits
