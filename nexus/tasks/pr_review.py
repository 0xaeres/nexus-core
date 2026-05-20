"""PR Review task - GitHub `pull_request` webhook -> structured review comment.

Slice 5 MVP: holistic review (one issue comment) rather than per-line comments.
Per-line review can land later; the demo gate is `[skill: ...]` citations + a
verdict within 30 seconds, both of which fit comfortably in an issue comment.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from nexus.config import NexusConfig
from nexus.llm.client import ChatClient
from nexus.mcp_server.tools import ToolState, find_skills

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


_SYSTEM = (
    "You are the Nexus PR Reviewer. You receive a diff and a small set of "
    "relevant skills (curated guidance). Produce a SHORT, opinionated review: "
    "highest-impact findings first, each cited `[skill: name]` where guidance "
    "applies. Cite `[file: path:line]` for any code claim. Two sections only: "
    "## Findings (max 5) and ## Verdict (approve | request_changes | comment)."
)


async def run_pr_review(*, payload: dict, config: NexusConfig) -> None:
    pr = payload.get("pull_request", {}) or {}
    repo = payload.get("repository", {}) or {}
    owner = (repo.get("owner") or {}).get("login")
    repo_name = repo.get("name")
    number = pr.get("number")
    if not (owner and repo_name and number):
        log.warning("pr_review: malformed payload (missing owner/repo/number)")
        return

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("pr_review: GITHUB_TOKEN not set; aborting")
        return

    product_id = _product_for_repo(payload, default="forge")

    async with httpx.AsyncClient(timeout=60.0) as gh:
        gh.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        files = await _fetch_files(gh, owner, repo_name, number)
        if not files:
            log.info("pr_review: no files in PR #%d", number)
            return

        # Build a compact diff bundle (cap to keep prompt cost bounded)
        diff_blocks: list[str] = []
        for f in files[:20]:
            patch = f.get("patch") or ""
            if not patch:
                continue
            diff_blocks.append(f"### `{f.get('filename')}`\n```diff\n{patch[:4000]}\n```")
        diff_text = "\n\n".join(diff_blocks) or "(no inline patches)"

        # Curated guidance: query skills with the file list as context
        file_list = ", ".join(f.get("filename", "") for f in files[:10])
        tool_state = ToolState(product=product_id, config=config)
        skills_payload = await find_skills(
            tool_state,
            query=f"changes touching {file_list}",
            context="code-review",
        )
        skill_summaries = skills_payload.get("skills", [])[:5]

        chat = ChatClient.from_cfg(config.models.pr_review, role="pr_review")
        try:
            user = _render_user(diff_text, skill_summaries)
            resp = await chat.chat(
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                temperature=0.2,
                max_tokens=2000,
            )
            body = resp.content.strip()
            if not body:
                log.warning("pr_review: empty LLM response")
                return
        finally:
            await chat.aclose()

        comment = _wrap_comment(body, skill_summaries)
        await _post_comment(gh, owner, repo_name, number, comment)
        log.info("pr_review: posted comment on %s/%s#%d", owner, repo_name, number)


# ---------------------------------------------------------------- helpers


async def _fetch_files(gh: httpx.AsyncClient, owner: str, repo: str, number: int) -> list[dict]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/files"
    r = await gh.get(url, params={"per_page": 100})
    r.raise_for_status()
    return r.json() if isinstance(r.json(), list) else []


async def _post_comment(
    gh: httpx.AsyncClient, owner: str, repo: str, number: int, body: str
) -> None:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments"
    r = await gh.post(url, json={"body": body})
    r.raise_for_status()


def _render_user(diff_text: str, skills: list[dict]) -> str:
    skill_block = "\n".join(
        f"- `[skill: {s['name']}]` ({s.get('kind','?')}, confidence={s.get('confidence',0):.2f})\n"
        f"  {s.get('summary','')[:300]}"
        for s in skills
    ) or "_(no curated skills matched this change)_"
    return (
        "Relevant skills (curated):\n\n"
        f"{skill_block}\n\n"
        "Diff under review:\n\n"
        f"{diff_text}\n"
    )


def _wrap_comment(body: str, skills: list[dict]) -> str:
    header = "🤖 **Nexus PR Review**"
    skill_chip = ", ".join(f"`[skill: {s['name']}]`" for s in skills)
    if skill_chip:
        header += f"\n_skills consulted: {skill_chip}_"
    footer = "\n\n---\n_Generated by Nexus. Skills are human-validated guidance, " \
             "not absolute rules - judgment required._"
    return f"{header}\n\n{body}{footer}"


def _product_for_repo(payload: dict, *, default: str) -> str:
    """Map a repo to a Nexus product. For Slice 5 we use a single default;
    real mapping arrives with multi-product onboarding."""
    repo_topics = (payload.get("repository") or {}).get("topics") or []
    for t in repo_topics:
        if t.startswith("nexus-product:"):
            return t.split(":", 1)[1]
    return default


def _safe_get_files_count(payload: Any) -> int:
    pr = payload.get("pull_request") if isinstance(payload, dict) else None
    return int((pr or {}).get("changed_files") or 0)
