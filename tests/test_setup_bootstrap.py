"""Tests for the skills-repo bootstrap flow.

Covers the SetupKV store, the GitHub client (mocked), and the bootstrap
orchestrator end-to-end against a local bare repo acting as the "GitHub remote."
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from git import Repo

from nexus.setup import SetupKV, bootstrap_skills_repo
from nexus.setup.bootstrap import _redact_token
from nexus.setup.github_api import GitHubAPIError, create_repo

# ---------- SetupKV ---------------------------------------------------------


def test_setup_kv_set_get_delete(tmp_path: Path) -> None:
    kv = SetupKV(tmp_path / "data.db")
    assert kv.get("skills_repo") is None
    kv.set("skills_repo", "https://github.com/me/x.git")
    assert kv.get("skills_repo") == "https://github.com/me/x.git"
    kv.set("skills_repo", "https://github.com/me/y.git")
    assert kv.get("skills_repo") == "https://github.com/me/y.git"
    kv.delete("skills_repo")
    assert kv.get("skills_repo") is None


def test_setup_kv_creates_db_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deeper" / "data.db"
    SetupKV(db)
    assert db.parent.is_dir()


# ---------- GitHub API client -----------------------------------------------


def test_create_repo_user_endpoint() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = request.read().decode()
        return httpx.Response(
            201, json={"clone_url": "https://github.com/u/nexus-skills.git"}
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    repo = asyncio.run(
        create_repo(token="tok", name="nexus-skills", client=client)
    )
    assert repo["clone_url"] == "https://github.com/u/nexus-skills.git"
    assert captured["url"].endswith("/user/repos")
    assert captured["headers"]["authorization"] == "Bearer tok"
    import json as _json
    assert _json.loads(captured["json"])["auto_init"] is True


def test_create_repo_org_endpoint() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(201, json={"clone_url": "https://x.git"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    asyncio.run(create_repo(token="t", name="nx", org="acme", client=client))
    assert "/orgs/acme/repos" in captured["url"]


def test_create_repo_raises_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"message":"name already exists"}')

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    with pytest.raises(GitHubAPIError) as exc:
        asyncio.run(create_repo(token="t", name="x", client=client))
    assert exc.value.status == 422


def test_bootstrap_redacts_token_from_clone_errors() -> None:
    text = "https://x-access-token:ghp_secret@github.com/acme/skills.git failed"
    assert "ghp_secret" not in _redact_token(text)


# ---------- bootstrap orchestrator end-to-end -------------------------------


def _init_local_remote(path: Path) -> str:
    bare = path / "remote.git"
    Repo.init(bare, bare=True, initial_branch="main")
    seed = path / "seed"
    work = Repo.clone_from(str(bare), str(seed))
    work.git.checkout("-b", "main")
    (seed / "README.md").write_text("# Skills\n")
    work.git.add(A=True)
    work.index.commit("initial")
    work.remotes.origin.push("main")
    return str(bare)


def test_bootstrap_existing_repo_clones_successfully(tmp_path: Path) -> None:
    remote_url = _init_local_remote(tmp_path)
    result = asyncio.run(
        bootstrap_skills_repo(mode="existing", existing_repo_url=remote_url)
    )
    assert result.created_repo is False
    assert result.files_seeded == 0  # no starter skill — empty bootstrap
    assert result.commit_sha is None
    assert result.skills_repo_url == remote_url


def test_bootstrap_create_mode_calls_github(tmp_path: Path) -> None:
    remote_url = _init_local_remote(tmp_path)
    fake_repo = {"clone_url": remote_url}

    with patch(
        "nexus.setup.bootstrap.create_repo", new=AsyncMock(return_value=fake_repo)
    ) as mock_create:
        result = asyncio.run(
            bootstrap_skills_repo(
                mode="create",
                github_token="tok",
                github_org="acme",
                repo_name="nexus-skills",
            )
        )
    mock_create.assert_awaited_once()
    kwargs = mock_create.await_args.kwargs
    assert kwargs["org"] == "acme"
    assert kwargs["name"] == "nexus-skills"
    assert result.created_repo is True


def test_bootstrap_create_requires_token() -> None:
    with pytest.raises(Exception) as exc:
        asyncio.run(bootstrap_skills_repo(mode="create"))
    assert "github_token" in str(exc.value)


def test_bootstrap_existing_requires_url() -> None:
    with pytest.raises(Exception) as exc:
        asyncio.run(bootstrap_skills_repo(mode="existing"))
    assert "existing_repo_url" in str(exc.value)


def test_bootstrap_unknown_mode_rejected() -> None:
    with pytest.raises(Exception) as exc:
        asyncio.run(bootstrap_skills_repo(mode="garbage"))
    assert "unknown mode" in str(exc.value)
