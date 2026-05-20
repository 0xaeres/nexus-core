from nexus.ingest.models import Chunk, ChunkKind, ResourceRef
from nexus.ingest.relation_extractor import _entity_id, _parse


def _chunk() -> Chunk:
    return Chunk(
        product_id="forge",
        resource=ResourceRef(
            source_id="local:test", uri="ADR-014.md", mime="text/markdown"
        ),
        content="dummy",
        start_line=1,
        end_line=1,
        kind=ChunkKind.DOC,
    )


def test_entity_id_is_stable_across_case() -> None:
    a = _entity_id("forge", "ticket", "ENG-123")
    b = _entity_id("forge", "ticket", "eng-123")
    assert a == b


def test_parse_drops_unknown_entity_types() -> None:
    payload = {
        "entities": [
            {"name": "ENG-1", "type": "ticket"},
            {"name": "bogus", "type": "alien"},
        ],
        "relations": [],
    }
    ents, rels = _parse(_chunk(), payload)
    names = [e.name for e in ents]
    assert "ENG-1" in names
    assert "bogus" not in names
    assert rels == []


def test_parse_drops_relations_referencing_unknown_entities() -> None:
    payload = {
        "entities": [
            {"name": "ENG-1", "type": "ticket"},
            {"name": "ADR-14", "type": "adr"},
        ],
        "relations": [
            {"src": "ADR-14", "dst": "ENG-1", "type": "closes"},
            {"src": "ADR-14", "dst": "ghost", "type": "references"},
        ],
    }
    _, rels = _parse(_chunk(), payload)
    assert len(rels) == 1
    assert rels[0].type == "closes"


def test_parse_drops_unknown_relation_types() -> None:
    payload = {
        "entities": [
            {"name": "A", "type": "service"},
            {"name": "B", "type": "service"},
        ],
        "relations": [
            {"src": "A", "dst": "B", "type": "implements"},
            {"src": "A", "dst": "B", "type": "deletes"},
        ],
    }
    _, rels = _parse(_chunk(), payload)
    assert len(rels) == 1
    assert rels[0].type == "implements"


def test_parse_caps_entity_count() -> None:
    payload = {
        "entities": [{"name": f"E{i}", "type": "service"} for i in range(20)],
        "relations": [],
    }
    ents, _ = _parse(_chunk(), payload)
    assert len(ents) == 6
