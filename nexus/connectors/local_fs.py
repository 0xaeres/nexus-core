"""Local filesystem source — walks a directory and yields resources.

Stand-in for a real MCP connector during Slice 1. The protocol surface is small
and intentionally identical to what we'll expose through the MCP client wrapper
in the slice-1 follow-on (`list_resources` + `read_resource`), so swapping in a
real MCP source later is purely a configuration change.
"""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from nexus.ingest.models import ResourceRef, guess_mime

# Reasonable defaults — exclude vendored / generated dirs and large binaries.
_DEFAULT_EXCLUDE = (
    ".git/*",
    ".venv/*",
    "venv/*",
    "node_modules/*",
    "__pycache__/*",
    ".pytest_cache/*",
    ".mypy_cache/*",
    ".ruff_cache/*",
    "dist/*",
    "build/*",
    "*.egg-info/*",
    "models/*",
    "skills/*",
    "*.lock",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.pdf",
    "*.zip",
    "*.tar.gz",
)

_DEFAULT_INCLUDE = (
    "*.py",
    "*.ts",
    "*.tsx",
    "*.js",
    "*.jsx",
    "*.mjs",
    "*.rs",
    "*.go",
    "*.md",
    "*.mdx",
    "*.txt",
    "*.rst",
    "*.yaml",
    "*.yml",
    "*.toml",
)

# Hard cap to avoid ingesting huge generated files (lockfiles, minified bundles).
_MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class LocalFsConfig:
    root: Path
    include: tuple[str, ...] = _DEFAULT_INCLUDE
    exclude: tuple[str, ...] = _DEFAULT_EXCLUDE
    max_file_bytes: int = _MAX_FILE_BYTES


class LocalFsSource:
    """Pull files from a local directory and present them as `ResourceRef`s."""

    def __init__(self, cfg: LocalFsConfig):
        self.cfg = cfg
        self.source_id = f"local:{cfg.root.resolve()}"

    async def list_resources(self) -> AsyncIterator[ResourceRef]:
        root = self.cfg.root.resolve()
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root))
            if any(fnmatch.fnmatch(rel, pat) for pat in self.cfg.exclude):
                continue
            if not any(fnmatch.fnmatch(rel, pat) for pat in self.cfg.include):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > self.cfg.max_file_bytes:
                continue
            yield ResourceRef(
                source_id=self.source_id,
                uri=str(path.resolve()),
                mime=guess_mime(rel),
                size_bytes=size,
            )

    async def read_resource(self, resource: ResourceRef) -> str:
        path = Path(resource.uri)
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as err:
            # binary or non-utf8 — skip by raising; caller swallows
            raise OSError(f"non-utf8: {resource.uri}") from err
