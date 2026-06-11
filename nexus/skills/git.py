"""Git operations for the skills hierarchy.

Best-effort: if the hierarchy_root is not a git repo (e.g. seed directory), the
add/commit/push are no-ops and we log a warning. Real prod path uses an
ephemeral clone of the skills_repo configured in nexus.yaml.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

try:
    from git import Repo  # type: ignore[import-not-found]
    from git.exc import GitError, InvalidGitRepositoryError  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    Repo = None  # type: ignore[assignment]
    GitError = Exception  # type: ignore[misc,assignment]
    InvalidGitRepositoryError = Exception  # type: ignore[misc,assignment]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitPublishResult:
    committed: bool
    pushed: bool
    commit_hexsha: str = ""
    error: str = ""


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
