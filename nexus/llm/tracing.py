"""Best-effort Langfuse tracing for LLM calls."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _client():
    if not (
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    ):
        return None
    try:
        from langfuse import Langfuse
    except Exception:
        return None
    try:
        return Langfuse(host=os.getenv("LANGFUSE_HOST") or None)
    except Exception:
        return None


def record_generation(
    *,
    name: str,
    model: str,
    provider: str,
    messages: list[dict[str, str]],
    output: str | None,
    usage: dict[str, int],
    latency_ms: float,
    finish_reason: str | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    client = _client()
    if client is None:
        return
    trace_content = os.getenv("NEXUS_TRACE_CONTENT", "false").lower() == "true"
    safe_input: Any = messages if trace_content else [{"role": m.get("role")} for m in messages]
    safe_output = output if trace_content else None
    meta = {
        "provider": provider,
        "latency_ms": round(latency_ms, 1),
        "finish_reason": finish_reason,
        **(metadata or {}),
    }
    try:
        generation = client.generation(
            name=name,
            model=model,
            input=safe_input,
            output=safe_output,
            usage=usage,
            metadata=meta,
            level="ERROR" if error else "DEFAULT",
            status_message=error,
        )
        generation.end()
    except Exception:
        return
