from nexus.retrieval.classifier import Complexity, classify


def test_single_identifier_is_simple() -> None:
    assert classify("find_program_address").is_simple
    assert classify("SkillStore").is_simple


def test_dotted_path_is_simple() -> None:
    assert classify("nexus.retrieval.pipeline").is_simple


def test_file_path_anchor_is_simple() -> None:
    assert classify("programs/swap/src/lib.rs:42").is_simple
    assert classify("nexus/ingest/indexer.py").is_simple


def test_natural_language_question_is_complex() -> None:
    r = classify("How does the swap fee math handle overflow?")
    assert r.complexity is Complexity.COMPLEX


def test_long_phrase_is_complex() -> None:
    r = classify("rounding direction protocol fee swap output amount calculation")
    assert r.complexity is Complexity.COMPLEX


def test_two_word_phrase_is_complex() -> None:
    # ambiguous case, classifier leans complex
    r = classify("PDA validation")
    assert r.complexity is Complexity.COMPLEX


def test_empty_query_handled() -> None:
    r = classify("")
    assert r.complexity is Complexity.COMPLEX
