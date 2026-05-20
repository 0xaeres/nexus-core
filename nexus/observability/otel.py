"""OpenTelemetry tracer + span helpers for the retrieval pipeline.

Phase-7 polish: we wrap each retrieval stage with a span so per-stage latency
and attributes (result_count, vector_name, ...) show up in any OTel-compatible
collector. Langfuse consumes OTel natively (per ENGINEERING.md §13).

We do not export by default - in offline dev there's no collector. Production
deployments set OTEL_EXPORTER_OTLP_ENDPOINT and the SDK picks it up.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _provider() -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "nexus"}),
    )
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            log.info("otel: exporting to %s", endpoint)
        except Exception as e:  # pragma: no cover - exporter is optional
            log.debug("otel exporter unavailable: %s", e)
    trace.set_tracer_provider(provider)
    return provider


def tracer():
    _provider()
    return trace.get_tracer("nexus.retrieval")


@asynccontextmanager
async def span(name: str, **attrs: object) -> AsyncIterator[Span]:
    """Async context manager for a single span. Records attributes + handles
    exceptions so caller sites stay tidy."""
    sp = tracer().start_span(name)
    for k, v in attrs.items():
        try:
            sp.set_attribute(k, v)  # type: ignore[arg-type]
        except Exception:
            sp.set_attribute(k, str(v))
    try:
        yield sp
    except Exception as e:
        sp.record_exception(e)
        sp.set_status(Status(StatusCode.ERROR, str(e)))
        raise
    finally:
        sp.end()


def record(span_: Span, **attrs: object) -> None:
    """Add attributes to an already-started span. Tolerates None."""
    if span_ is None:
        return
    for k, v in attrs.items():
        try:
            span_.set_attribute(k, v)  # type: ignore[arg-type]
        except Exception:
            span_.set_attribute(k, str(v))
