from nexus.ingest.chunker import chunk_resource
from nexus.ingest.models import ChunkKind, ResourceRef


def _res(uri: str, mime: str) -> ResourceRef:
    return ResourceRef(source_id="local:test", uri=uri, mime=mime)


def test_python_chunks_at_function_and_class_boundaries() -> None:
    code = (
        "import os\n"
        "\n"
        "def hello(name: str) -> str:\n"
        '    """Greet someone."""\n'
        '    return f"Hello, {name}!"\n'
        "\n"
        "class Greeter:\n"
        "    def __init__(self, prefix: str):\n"
        "        self.prefix = prefix\n"
        "\n"
        "    def greet(self, name: str) -> str:\n"
        '        return f"{self.prefix} {name}!"\n'
    )
    chunks = chunk_resource("forge", _res("a.py", "text/x-python"), code)
    paths = sorted(c.context_path for c in chunks if c.context_path)
    assert "hello" in paths
    assert "Greeter" in paths
    assert "Greeter.__init__" in paths
    assert "Greeter.greet" in paths
    # All code chunks
    assert all(c.kind is ChunkKind.CODE for c in chunks)
    # Each anchor points at a real line
    for c in chunks:
        assert c.start_line >= 1
        assert c.end_line >= c.start_line


def test_markdown_chunks_carry_heading_path() -> None:
    md = (
        "# Project\n"
        "\nIntro text long enough.\n"
        "\n## Setup\n"
        "\nStep 1: install dependencies.\n"
        "Step 2: run the thing.\n"
        "\n## Usage\n"
        "\nRun with --help to see options.\n"
        "\n### Advanced\n"
        "\nPower-user features go here.\n"
    )
    chunks = chunk_resource("forge", _res("README.md", "text/markdown"), md)
    assert chunks, "markdown should produce chunks"
    paths = [c.context_path for c in chunks]
    assert any(p and "Setup" in p for p in paths)
    assert any(p and "Usage" in p for p in paths)
    assert any(p and "Advanced" in p and "Usage" in p for p in paths)
    assert all(c.kind is ChunkKind.DOC for c in chunks)


def test_chunk_id_is_deterministic_uuid() -> None:
    code = "def foo():\n    pass\n    return 1\n    return 2\n"
    a = chunk_resource("p", _res("x.py", "text/x-python"), code)
    b = chunk_resource("p", _res("x.py", "text/x-python"), code)
    assert [c.id for c in a] == [c.id for c in b]
    for c in a:
        # UUID format: 8-4-4-4-12
        parts = c.id.split("-")
        assert len(parts) == 5


def test_chunk_anchor_matches_start_line() -> None:
    code = (
        "x = 1\n"
        "\n"
        "def bar(value: int) -> int:\n"
        '    """Double a number with a long-enough body to clear min size."""\n'
        "    if value < 0:\n"
        "        return 0\n"
        "    return value * 2\n"
    )
    chunks = chunk_resource("p", _res("y.py", "text/x-python"), code)
    bar = next(c for c in chunks if c.context_path == "bar")
    assert bar.anchor.endswith(f":{bar.start_line}")


def test_text_for_embedding_prepends_context_summary() -> None:
    code = (
        "def foo(seed: int) -> list[int]:\n"
        '    """Produce a small list — bodied enough to be a chunk."""\n'
        "    result = []\n"
        "    for i in range(seed):\n"
        "        result.append(i * 2)\n"
        "    return result\n"
    )
    chunks = chunk_resource("p", _res("z.py", "text/x-python"), code)
    assert chunks, "expected at least one chunk for a real function body"
    c = chunks[0]
    assert c.text_for_embedding() == c.content
    c.context_summary = "Location: foo() in z.py"
    assert c.text_for_embedding().startswith("Location: foo() in z.py")
    assert c.content in c.text_for_embedding()
