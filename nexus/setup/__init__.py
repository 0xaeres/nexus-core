"""First-run setup — org-wide skills_repo bootstrap.

The skills_repo is one Git repo per org. This package owns the one-time
bootstrap: create a new repo via the GitHub API or attach to an existing one.
Per-product skill files land later, one commit per council approval.
"""

from __future__ import annotations

from nexus.setup.bootstrap import (
    BootstrapError,
    BootstrapResult,
    bootstrap_skills_repo,
)
from nexus.setup.kv import SetupKV

__all__ = [
    "BootstrapError",
    "BootstrapResult",
    "SetupKV",
    "bootstrap_skills_repo",
]
