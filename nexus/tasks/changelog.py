"""Changelog task - GitHub `release.created` -> populated release notes.

Walks commits between the previous tag and the just-created tag, asks the
changelog LLM to categorise each into feat / fix / breaking / other, and
PATCHes the release body.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from nexus.config import NexusConfig
from nexus.llm.client import ChatClient

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


_SYSTEM = (
    "You are the Nexus Changelog generator. Given a list of commits, classify "
    "each one into one of: feat, fix, breaking, other. Group them and produce "
    "release notes in markdown."
)


async def run_changelog(*, payload: dict, config: NexusConfig) -> None:
    release = payload.get("release", {}) or {}
    repo = payload.get("repository", {}) or {}
    owner = (repo.get("owner") or {}).get("login")
    repo_name = repo.get("name")
    release_id = release.get("id")
    tag = release.get("tag_name")
    if not (owner and repo_name and release_id and tag):
        log.warning("changelog: malformed payload")
        return

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("changelog: GITHUB_TOKEN not set; aborting")
        return

    async with httpx.AsyncClient(timeout=60.0) as gh:
        gh.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        prev_tag = await _previous_tag(gh, owner, repo_name, tag)
        commits = await _commits_between(gh, owner, repo_name, prev_tag, tag)
        if not commits:
            log.info("changelog: no commits between %s..%s", prev_tag, tag)
            return

        body = await _generate_changelog(config, prev_tag, tag, commits)
        await _patch_release(gh, owner, repo_name, release_id, body)
        log.info(
            "changelog: updated release %s for %s/%s (%d commits)",
            tag,
            owner,
            repo_name,
            len(commits),
        )


# ---------------------------------------------------------------- GitHub helpers


async def _previous_tag(
    gh: httpx.AsyncClient, owner: str, repo: str, current: str
) -> str | None:
    """Look up the tag chronologically before `current`. None means initial release."""
    r = await gh.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/tags", params={"per_page": 100}
    )
    if r.status_code != 200:
        return None
    tags = r.json() or []
    names = [t.get("name") for t in tags]
    try:
        idx = names.index(current)
    except ValueError:
        return None
    return names[idx + 1] if idx + 1 < len(names) else None


async def _commits_between(
    gh: httpx.AsyncClient, owner: str, repo: str, base: str | None, head: str
) -> list[dict]:
    if not base:
        # First release: list recent commits up to head
        r = await gh.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits",
            params={"sha": head, "per_page": 100},
        )
        r.raise_for_status()
        return r.json() or []
    r = await gh.get(f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base}...{head}")
    r.raise_for_status()
    return (r.json() or {}).get("commits", []) or []


async def _patch_release(
    gh: httpx.AsyncClient, owner: str, repo: str, release_id: int, body: str
) -> None:
    r = await gh.patch(
        f"{GITHUB_API}/repos/{owner}/{repo}/releases/{release_id}",
        json={"body": body},
    )
    r.raise_for_status()


# ---------------------------------------------------------------- LLM


async def _generate_changelog(
    config: NexusConfig, prev: str | None, tag: str, commits: list[dict]
) -> str:
    summary = []
    for c in commits[:200]:
        sha = (c.get("sha") or "")[:7]
        msg = (c.get("commit") or {}).get("message", "").splitlines()[0] if c.get("commit") else ""
        author = (c.get("commit") or {}).get("author", {}).get("name") or "unknown"
        summary.append(f"- {sha} {msg} ({author})")
    commit_block = "\n".join(summary)

    chat = ChatClient.from_cfg(config.models.changelog, role="changelog")
    try:
        user = (
            f"Previous tag: {prev or '(initial)'}\n"
            f"Current tag: {tag}\n\n"
            f"Commits:\n{commit_block}\n\n"
            "Output JSON in this schema:\n"
            '{\n'
            '  "feat": ["short bullet", ...],\n'
            '  "fix": [...],\n'
            '  "breaking": [...],\n'
            '  "other": [...]\n'
            '}\n'
            "Each bullet should be one terse line referencing the commit "
            "subject (no shas). Skip categories with no entries."
        )
        payload, _ = await chat.chat_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=1500,
        )
    finally:
        await chat.aclose()

    return _format_release_body(prev=prev, tag=tag, sections=payload)


def _format_release_body(*, prev: str | None, tag: str, sections: dict) -> str:
    parts: list[str] = [f"## {tag}"]
    if prev:
        parts.append(f"_Changes since {prev}_")
    for label, key in (
        ("Breaking changes", "breaking"),
        ("Features", "feat"),
        ("Fixes", "fix"),
        ("Other", "other"),
    ):
        items = sections.get(key) or []
        if not items:
            continue
        parts.append(f"\n### {label}")
        for it in items:
            parts.append(f"- {it}")
    parts.append("\n---\n_Generated by Nexus._")
    return "\n".join(parts)


# silence unused-import warning if json is dropped during refactors
_ = json
