from nexus.council.state import (
    CodePatterns,
    DomainContext,
    EvidenceChunk,
    initial_state,
)


def test_initial_state_has_empty_streams() -> None:
    s = initial_state(
        session_id="cs_1",
        product_id="forge",
        topic="overview",
        skill_kind="master",
        config_path="nexus.yaml",
    )
    assert s["deliberation"] == []
    assert s["costs"] == []
    assert s["code_patterns"] is None
    assert s["domain_context"] is None


def test_code_patterns_roundtrip_via_pydantic() -> None:
    cp = CodePatterns(
        patterns=[
            {
                "name": "owner-check",
                "description": "always verify account owner",
                "evidence": [
                    EvidenceChunk(
                        chunk_id="c1",
                        file="lib.rs",
                        line=42,
                        score=0.9,
                        excerpt="check owner",
                    )
                ],
            }
        ],
        notes="ok",
    )
    dumped = cp.model_dump_json()
    restored = CodePatterns.model_validate_json(dumped)
    assert restored.patterns[0].name == "owner-check"
    assert restored.patterns[0].evidence[0].file == "lib.rs"


def test_domain_context_caps_inputs() -> None:
    ctx = DomainContext(
        vocabulary=["a"] * 30,
        entity_relationships=["x owns y"] * 10,
        summary="hi",
    )
    # No internal cap on the model itself — caps are applied by the agent.
    assert len(ctx.vocabulary) == 30
