"""Runtime UI performance metric routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

router = APIRouter(prefix="/metrics", tags=["metrics"])

MetricName = Literal[
    "FCP",
    "LCP",
    "CLS",
    "FID",
    "INP",
    "TTFB",
    "Next.js-hydration",
    "Next.js-route-change-to-render",
    "Next.js-render",
]


class WebVitalsMetric(BaseModel):
    name: MetricName
    value: float = Field(..., ge=0, le=600_000)
    rating: Literal["good", "needs-improvement", "poor"] | None = None
    id: str = Field(..., min_length=1, max_length=128)
    route: str = Field(..., min_length=1, max_length=256)
    product_id: str | None = Field(None, max_length=128)
    navigation_type: str | None = Field(None, max_length=64)

    @field_validator("route")
    @classmethod
    def _route(cls, value: str) -> str:
        parsed = urlsplit(value)
        route = parsed.path if parsed.scheme or parsed.netloc else value.split("?", 1)[0]
        route = route.split("#", 1)[0].strip()
        if not route.startswith("/"):
            route = f"/{route}"
        return route[:256]


class WebVitalsResponse(BaseModel):
    ok: bool


@router.post("/web-vitals", response_model=WebVitalsResponse)
async def web_vitals(request: Request, metric: WebVitalsMetric) -> WebVitalsResponse:
    user = getattr(request.state, "user", {}) or {}
    log.info(
        "web_vital user_id=%s name=%s value=%.3f rating=%s route=%s product_id=%s navigation_type=%s ts=%s",
        user.get("id", "anonymous"),
        metric.name,
        metric.value,
        metric.rating or "",
        metric.route,
        metric.product_id or "",
        metric.navigation_type or "",
        datetime.now(UTC).isoformat(),
    )
    return WebVitalsResponse(ok=True)
