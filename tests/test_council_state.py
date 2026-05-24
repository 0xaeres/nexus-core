from nexus.council.state import EvidenceChunk, initial_state


def test_initial_state_has_empty_streams() -> None:
    s = initial_state(
        session_id="cs_1",
        product_id="forge",
        topic="overview",
        config_path="nexus.yaml",
    )
    assert s["deliberation"] == []
    assert s["costs"] == []
    assert s["evidence"] == []
    assert s["proposal"] is None
    assert s["critique"] is None
    assert s["revision_count"] == 0


def test_evidence_chunk_roundtrip() -> None:
    e = EvidenceChunk(chunk_id="c1", file="lib.rs", line=42, score=0.9, excerpt="check owner")
    dumped = e.model_dump_json()
    restored = EvidenceChunk.model_validate_json(dumped)
    assert restored.file == "lib.rs"
    assert restored.line == 42
