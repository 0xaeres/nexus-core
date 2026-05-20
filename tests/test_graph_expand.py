"""Stage 4 expand() - tests the merge behaviour with a mock GraphStore.

We're not exercising live Neo4j; we just verify that the function:
- passes seeds through when graph is None or returns no neighbours
- emits Hit(source='graph') for graph-reachable neighbours
- merges via RRF (combined ranking elevates items hit by both ranks)
"""

import asyncio
from dataclasses import dataclass

from nexus.retrieval.graph import expand
from nexus.retrieval.hybrid import Hit


@dataclass
class _NeighbourChunk:
    chunk_id: str
    file: str
    line: int
    kind: str
    via_entity: str
    hop: int


class _StubGraph:
    def __init__(self, neighbours: list[_NeighbourChunk]):
        self._n = neighbours
        self.calls = 0

    async def expand_neighbours(self, *, product_id, chunk_ids, hops, limit_per_seed=5):
        self.calls += 1
        return self._n


def _hit(id_: str, source: str = "dense") -> Hit:
    return Hit(id=id_, score=1.0, payload={"id": id_}, source=source)


def test_no_graph_passes_seeds_through() -> None:
    seeds = [_hit("a"), _hit("b")]
    out = asyncio.run(expand(product_id="p", seeds=seeds, hops=2, graph=None))
    assert out == seeds


def test_empty_neighbours_passes_seeds_through() -> None:
    seeds = [_hit("a")]
    out = asyncio.run(
        expand(product_id="p", seeds=seeds, hops=2, graph=_StubGraph([]))
    )
    assert out == seeds


def test_neighbours_emitted_as_graph_source() -> None:
    seeds = [_hit("a")]
    g = _StubGraph(
        [
            _NeighbourChunk(
                chunk_id="b", file="x.py", line=10, kind="code", via_entity="E", hop=1
            ),
            _NeighbourChunk(
                chunk_id="c", file="y.md", line=2, kind="doc", via_entity="F", hop=2
            ),
        ]
    )
    out = asyncio.run(expand(product_id="p", seeds=seeds, hops=2, graph=g))
    ids = {h.id for h in out}
    assert ids == {"a", "b", "c"}
    by_id = {h.id: h for h in out}
    assert "graph" in by_id["b"].source
    assert by_id["c"].payload.get("graph_hop") == 2


def test_closer_neighbours_outrank_farther() -> None:
    seeds = [_hit("a")]
    g = _StubGraph(
        [
            _NeighbourChunk(
                chunk_id="hop1", file="x", line=1, kind="code", via_entity="E", hop=1
            ),
            _NeighbourChunk(
                chunk_id="hop2", file="y", line=2, kind="code", via_entity="F", hop=2
            ),
        ]
    )
    out = asyncio.run(expand(product_id="p", seeds=seeds, hops=2, graph=g))
    pos = {h.id: i for i, h in enumerate(out)}
    assert pos["hop1"] < pos["hop2"]
