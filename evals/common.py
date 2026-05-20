"""Shared helpers for the eval runners."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoldenItem:
    id: str
    query: str
    expected_answer: str
    expected_skill: str
    expected_files: list[str]
    complexity: str
    anti_answer: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> GoldenItem:
        return cls(
            id=d["id"],
            query=d["query"],
            expected_answer=d.get("expected_answer", ""),
            expected_skill=d.get("expected_skill", ""),
            expected_files=list(d.get("expected_files") or []),
            complexity=str(d.get("complexity", "complex")),
            anti_answer=d.get("anti_answer"),
        )


def load_golden(path: Path) -> list[GoldenItem]:
    items: list[GoldenItem] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            items.append(GoldenItem.from_dict(json.loads(line)))
    return items


def iter_passes(items: list[GoldenItem]) -> Iterator[GoldenItem]:
    yield from items
