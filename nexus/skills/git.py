"""Git operations for the skills hierarchy.

Best-effort: if the hierarchy_root is not a git repo (e.g. seed directory), the
add/commit/push are no-ops and we log a warning. Real prod path uses an
ephemeral clone of the skills_repo configured in nexus.yaml.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

try:
    from git import Repo  # type: ignore[import-not-found]
    from git.exc import (  # type: ignore[import-not-found]
        GitError,
        InvalidGitRepositoryError,
        NoSuchPathError,
    )
except Exception:  # pragma: no cover
    Repo = None  # type: ignore[assignment]
    GitError = Exception  # type: ignore[misc,assignment]
    InvalidGitRepositoryError = Exception  # type: ignore[misc,assignment]
    NoSuchPathError = Exception  # type: ignore[misc,assignment]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitPublishResult:
    committed: bool
    pushed: bool
    commit_hexsha: str = ""
    error: str = ""


def ensure_checkout(root: Path, repo_url: str, *, token: str | None = None) -> None:
    """Ensure `root` is a Git checkout of the configured skills repo."""
    if Repo is None:
        raise GitError("gitpython unavailable")
    try:
        repo = Repo(root, search_parent_directories=False)
        origin_urls = set(repo.remotes.origin.urls) if repo.remotes else set()
        if not origin_urls or _repo_url_matches(repo_url, origin_urls):
            return
        raise GitError(f"skills root {root} is not a checkout of the configured skills repo")
    except (InvalidGitRepositoryError, NoSuchPathError):
        pass

    if not repo_url:
        raise GitError("skills_repo is not configured")
    if root.exists() and any(root.rglob("*")):
        if any(path.is_file() or path.is_symlink() for path in root.rglob("*")):
            raise GitError(f"skills root {root} exists but is not a git repo")
        shutil.rmtree(root)

    root.parent.mkdir(parents=True, exist_ok=True)
    if root.exists():
        root.rmdir()
    try:
        repo = Repo.clone_from(_authenticated_clone_url(repo_url, token), str(root))
        if token and repo.remotes:
            repo.remotes.origin.set_url(repo_url)
    except Exception as e:
        raise GitError(f"clone failed: {_redact_token(str(e))}") from e


def commit_and_push(root: Path, message: str, *, push: bool = True) -> GitPublishResult:
    """Stage everything in `root`, commit, and (optionally) push origin/main.

    Returns explicit local commit and remote push state.
    """
    if Repo is None:
        log.warning("gitpython unavailable; skipping commit")
        return GitPublishResult(committed=False, pushed=False, error="gitpython unavailable")
    try:
        repo = Repo(root, search_parent_directories=False)
    except InvalidGitRepositoryError:
        log.info("skills root %s is not a git repo; skipping commit", root)
        return GitPublishResult(committed=False, pushed=False, error="not a git repo")
    except GitError as e:  # pragma: no cover
        log.warning("git open failed: %s", e)
        return GitPublishResult(committed=False, pushed=False, error=str(e))

    repo.git.add(A=True)
    if not repo.is_dirty(untracked_files=True):
        log.info("no changes to commit")
        return GitPublishResult(committed=False, pushed=False, error="no changes to commit")
    commit = repo.index.commit(message)

    if push and repo.remotes:
        try:
            origin = repo.remotes.origin
            infos = origin.push()
            if any(info.flags & info.ERROR for info in infos):
                error = "; ".join(info.summary for info in infos)
                log.warning("git push failed: %s", error)
                return _rollback_commit(repo, commit, error)
        except GitError as e:
            log.warning("git push failed (commit retained locally): %s", e)
            return _rollback_commit(repo, commit, str(e))
    return GitPublishResult(
        committed=True,
        pushed=True,
        commit_hexsha=commit.hexsha,
    )


def _rollback_commit(repo, commit, error: str) -> GitPublishResult:
    commit_hexsha = commit.hexsha
    try:
        if commit.parents:
            repo.git.reset("--mixed", commit.parents[0].hexsha)
        else:
            repo.git.update_ref("-d", "HEAD")
            repo.git.read_tree("--empty")
        return GitPublishResult(
            committed=False,
            pushed=False,
            commit_hexsha=commit_hexsha,
            error=error,
        )
    except GitError as e:  # pragma: no cover - rare, but preserve truth if rollback fails.
        return GitPublishResult(
            committed=True,
            pushed=False,
            commit_hexsha=commit_hexsha,
            error=f"{error}; rollback failed: {e}",
        )


def _authenticated_clone_url(url: str, token: str | None) -> str:
    if not token:
        return url
    if not url.startswith("https://"):
        return url
    return url.replace("https://", f"https://x-access-token:{token}@", 1)


def _redact_token(text: str) -> str:
    return re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text)


def _repo_url_matches(expected: str, actual_urls: set[str]) -> bool:
    if not expected:
        return False
    normalized_expected = _normalize_repo_url(expected)
    return any(_normalize_repo_url(url) == normalized_expected for url in actual_urls)


def _normalize_repo_url(url: str) -> str:
    if url.startswith("git@github.com:"):
        return "https://github.com/" + url.removeprefix("git@github.com:").removesuffix(".git")
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        host = parsed.hostname or ""
        path = parsed.path.removesuffix(".git").rstrip("/")
        return urlunparse(("https", host.lower(), path, "", "", ""))
    return url.removesuffix(".git").rstrip("/")
