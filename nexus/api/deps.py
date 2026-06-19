"""FastAPI dependency providers."""

from __future__ import annotations

import logging
from functools import lru_cache

from nexus.auth.store import AuthStore
from nexus.config import NexusConfig, get_config
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.setup import SetupKV
from nexus.skills.store import SkillStore

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_proposal_queue() -> ProposalQueue:
    config: NexusConfig = get_config()
    return ProposalQueue(config.storage.proposal_queue)


@lru_cache(maxsize=1)
def get_registry() -> Registry:
    config: NexusConfig = get_config()
    # Co-locate the registry alongside the proposal queue
    return Registry(config.storage.proposal_queue.parent / "registry.db")


@lru_cache(maxsize=1)
def get_setup_kv() -> SetupKV:
    config: NexusConfig = get_config()
    return SetupKV(config.storage.proposal_queue.parent / "registry.db")


@lru_cache(maxsize=1)
def get_auth_store() -> AuthStore:
    config: NexusConfig = get_config()
    return AuthStore(config.storage.proposal_queue.parent / "registry.db")


def resolve_skills_repo_url(
    config: NexusConfig | None = None, kv: SetupKV | None = None
) -> str:
    """Return the active skills_repo URL or '' if setup is still required.

    Resolution order: runtime KV (set by /setup/skills-repo) > nexus.yaml.
    """
    cfg = config or get_config()
    store = kv or get_setup_kv()
    return store.get("skills_repo") or cfg.skills_repo or ""


@lru_cache(maxsize=1)
def get_skill_store() -> SkillStore:
    from pathlib import Path

    config: NexusConfig = get_config()
    root = Path(config.hierarchy_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    return SkillStore(root)


def get_config_dep() -> NexusConfig:
    return get_config()
