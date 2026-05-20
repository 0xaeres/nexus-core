import asyncio
from pathlib import Path

import pytest

from nexus.connectors.local_fs import LocalFsConfig, LocalFsSource


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "src" / "lib.ts").write_text("export const x = 1;\n")
    (tmp_path / "README.md").write_text("# Hello\n\nWorld.\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("noise")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("not code")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return tmp_path


async def _list(src: LocalFsSource) -> list[str]:
    out: list[str] = []
    async for r in src.list_resources():
        out.append(r.uri)
    return out


def test_includes_code_and_md_excludes_vendored(sample_tree: Path) -> None:
    src = LocalFsSource(LocalFsConfig(root=sample_tree))
    uris = asyncio.run(_list(src))
    bases = sorted(Path(u).name for u in uris)
    assert "app.py" in bases
    assert "lib.ts" in bases
    assert "README.md" in bases
    assert "junk.js" not in bases  # node_modules excluded
    assert "config" not in bases  # .git excluded
    assert "image.png" not in bases


def test_read_resource_returns_content(sample_tree: Path) -> None:
    src = LocalFsSource(LocalFsConfig(root=sample_tree))

    async def go() -> str:
        async for r in src.list_resources():
            if r.uri.endswith("app.py"):
                return await src.read_resource(r)
        raise AssertionError("app.py not yielded")

    assert "def main()" in asyncio.run(go())
