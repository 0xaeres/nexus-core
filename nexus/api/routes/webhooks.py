"""GitHub webhooks - HMAC-verified entry point for PR review + changelog tasks.

GitHub signs each delivery with `X-Hub-Signature-256: sha256=<hex>` derived from
the raw request body and the secret configured in nexus.yaml (`server.webhook_secret`).
We compute the same digest and constant-time-compare; mismatches return 401.

Routing:
  - X-GitHub-Event: pull_request,  action=opened|reopened|synchronize
       -> nexus.tasks.pr_review.run_pr_review (background task)
  - X-GitHub-Event: release, action=created
       -> nexus.tasks.changelog.run_changelog (background task)
  - X-GitHub-Event: ping
       -> 200 OK (handshake)
Everything else: 200 OK with `ignored: true` body so GitHub keeps the delivery.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from nexus.api.deps import get_config_dep, get_proposal_queue
from nexus.config import NexusConfig
from nexus.council.queue import ProposalQueue

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Anchor background tasks so the GC can't drop them mid-flight.
_RUNNING: set[asyncio.Task] = set()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time HMAC-SHA256 check. Empty secret is treated as 'disabled'
    (dev-only); production should always set `server.webhook_secret`."""
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
    config: NexusConfig = Depends(get_config_dep),
    queue: ProposalQueue = Depends(get_proposal_queue),
) -> dict:
    raw = await request.body()
    if not verify_signature(config.server.webhook_secret, raw, x_hub_signature_256):
        log.warning("webhook: HMAC mismatch (event=%s)", x_github_event)
        raise HTTPException(status_code=401, detail="invalid signature")

    payload: dict[str, Any]
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="invalid JSON") from e

    event = (x_github_event or "").lower()

    if event == "ping":
        return {"ok": True, "pong": payload.get("zen", "")}

    if event == "pull_request":
        action = payload.get("action", "")
        if action in ("opened", "reopened", "synchronize"):
            _spawn(_pr_review_task(payload=payload, config=config))
            return {"ok": True, "queued": "pr_review"}

    if event == "release":
        action = payload.get("action", "")
        if action == "created":
            _spawn(_changelog_task(payload=payload, config=config))
            return {"ok": True, "queued": "changelog"}

    return {"ok": True, "ignored": True, "event": event}


# ---------------------------------------------------------------- task wrappers


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def _pr_review_task(*, payload: dict, config: NexusConfig) -> None:
    from nexus.tasks.pr_review import run_pr_review

    try:
        await run_pr_review(payload=payload, config=config)
    except Exception:
        log.exception("pr_review task failed")


async def _changelog_task(*, payload: dict, config: NexusConfig) -> None:
    from nexus.tasks.changelog import run_changelog

    try:
        await run_changelog(payload=payload, config=config)
    except Exception:
        log.exception("changelog task failed")
