"""First-run setup routes — org-wide skills_repo bootstrap.

See `nexus/setup/` for the actual orchestration. The UI calls `GET /setup/status`
on app load; if `configured=false` it routes the user through the wizard which
posts to `POST /setup/skills-repo`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from git.exc import GitError

from nexus.api.authz import require_admin
from nexus.api.deps import get_config_dep, get_setup_kv, resolve_skills_repo_url
from nexus.config import NexusConfig
from nexus.setup import BootstrapError, SetupKV, bootstrap_skills_repo
from nexus.skills.git import ensure_checkout

log = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])


@router.get("/status")
async def setup_status(kv: SetupKV = Depends(get_setup_kv)) -> dict:
    url = resolve_skills_repo_url(kv=kv)
    source = None
    if kv.get("skills_repo"):
        source = "runtime"
    elif url:
        source = "config"
    return {
        "configured": bool(url),
        "skills_repo_url": url or None,
        "source": source,
    }


@router.post("/skills-repo")
async def setup_skills_repo(
    request: Request,
    mode: Literal["create", "existing"] = Body(..., embed=True),
    github_org: str | None = Body(None, embed=True),
    repo_name: str = Body("nexus-skills", embed=True),
    existing_repo_url: str | None = Body(None, embed=True),
    kv: SetupKV = Depends(get_setup_kv),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    require_admin(request)
    create_token = os.environ.get("NEXUS_SKILLS_REPO_TOKEN") or ""
    checkout_token = _skills_repo_token()
    if mode == "create" and not create_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "NEXUS_SKILLS_REPO_TOKEN env var is required to create a skills repo; "
                "this is separate from product source GitHub tokens"
            ),
        )
    try:
        result = await bootstrap_skills_repo(
            mode=mode,
            github_token=(create_token if mode == "create" else checkout_token) or None,
            github_org=github_org,
            repo_name=repo_name,
            existing_repo_url=existing_repo_url,
        )
    except BootstrapError as e:
        log.warning("skills-repo bootstrap failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        await asyncio.to_thread(
            ensure_checkout,
            config.hierarchy_root,
            result.skills_repo_url,
            token=checkout_token or None,
        )
    except GitError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    kv.set("skills_repo", result.skills_repo_url)
    log.info(
        "skills-repo bootstrap: mode=%s url=%s seeded=%d created=%s",
        mode,
        result.skills_repo_url,
        result.files_seeded,
        result.created_repo,
    )
    return {
        "skills_repo_url": result.skills_repo_url,
        "files_seeded": result.files_seeded,
        "commit_sha": result.commit_sha,
        "created_repo": result.created_repo,
    }


def _skills_repo_token() -> str:
    return os.environ.get("NEXUS_SKILLS_REPO_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
