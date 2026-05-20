"""FastAPI dependency providers."""

from __future__ import annotations

from functools import lru_cache

from nexus.config import NexusConfig, get_config
from nexus.council.queue import ProposalQueue
from nexus.registry import Registry
from nexus.skills.store import SkillStore


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
def get_skill_store() -> SkillStore:
    from pathlib import Path

    config: NexusConfig = get_config()
    root = Path(config.hierarchy_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    return SkillStore(root)


def get_config_dep() -> NexusConfig:
    return get_config()
