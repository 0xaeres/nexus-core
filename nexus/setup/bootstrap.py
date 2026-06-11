"""Skills-repo bootstrap orchestrator.

Either creates a new GitHub repo or attaches to an existing one. The repo
is left empty after creation; per-product skill files are written as the
council approves them.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from git import Repo

from nexus.setup.github_api import create_repo

log = logging.getLogger(__name__)


class BootstrapError(RuntimeError):
    """Raised when the bootstrap flow cannot complete."""


@dataclass
class BootstrapResult:
    skills_repo_url: str
    files_seeded: int  # kept for response-shape compatibility; always 0 now
    commit_sha: str | None
    created_repo: bool


async def bootstrap_skills_repo(
    *,
    mode: str,
    github_token: str | None = None,
    github_org: str | None = None,
    repo_name: str = "nexus-skills",
    existing_repo_url: str | None = None,
) -> BootstrapResult:
    """Run the skills_repo bootstrap.

    Args:
        mode: "create" to mint a new repo via GitHub API; "existing" to attach
              to a repo the user already owns.
        github_token: PAT with `repo` scope. Required for `mode="create"` and
                      to push to a private existing repo.
        github_org: When set, the new repo is created under this org;
                    otherwise under the authenticated user.
        repo_name: Name of the repo to create (mode="create" only).
        existing_repo_url: Clone URL of an existing repo (mode="existing").
    """
    if mode not in {"create", "existing"}:
        raise BootstrapError(f"unknown mode: {mode!r}")

    created_repo = False
    if mode == "create":
        if not github_token:
            raise BootstrapError("github_token is required when mode='create'")
        repo_obj = await create_repo(
            token=github_token, name=repo_name, org=github_org
        )
        clone_url = _authenticated_clone_url(repo_obj["clone_url"], github_token)
        canonical_url = repo_obj["clone_url"]
        created_repo = True
    else:
        if not existing_repo_url:
            raise BootstrapError(
                "existing_repo_url is required when mode='existing'"
            )
        clone_url = _authenticated_clone_url(existing_repo_url, github_token)
        canonical_url = existing_repo_url

    # Verify we can clone — that's the only real bootstrap step now. Skills
    # land as the council approves them.
    with tempfile.TemporaryDirectory(prefix="nexus-bootstrap-") as tmp:
        workdir = Path(tmp) / "skills"
        try:
            Repo.clone_from(clone_url, str(workdir))
        except Exception as e:
            raise BootstrapError(f"clone failed: {_redact_token(str(e))}") from e

    return BootstrapResult(
        skills_repo_url=canonical_url,
        files_seeded=0,
        commit_sha=None,
        created_repo=created_repo,
    )


def _authenticated_clone_url(url: str, token: str | None) -> str:
    """Inline a PAT into the HTTPS clone URL so push doesn't prompt for creds."""
    if not token:
        return url
    if not url.startswith("https://"):
        return url
    return url.replace("https://", f"https://x-access-token:{token}@", 1)


def _redact_token(text: str) -> str:
    return re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text)
