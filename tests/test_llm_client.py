import pytest

from nexus.config import ModelCfg
from nexus.llm.client import ChatClient, LLMError, _parse_json_payload


def test_provider_routing_deepinfra() -> None:
    cfg = ModelCfg(provider="deepinfra", model="m", api_key="k")
    c = ChatClient.from_cfg(cfg, role="x")
    assert c.base_url.startswith("https://api.deepinfra.com")
    assert c.role == "x"


def test_provider_routing_ollama_no_key_needed() -> None:
    cfg = ModelCfg(provider="ollama", model="qwen2.5:3b")
    c = ChatClient.from_cfg(cfg, role="light")
    assert c.base_url.startswith("http://localhost:11434")


def test_explicit_base_url_overrides_provider_default() -> None:
    cfg = ModelCfg(provider="ollama", model="m", base_url="http://other:9999")
    c = ChatClient.from_cfg(cfg, role="x")
    assert c.base_url == "http://other:9999"


def test_unknown_provider_raises() -> None:
    cfg = ModelCfg(provider="weird-cloud", model="m")
    with pytest.raises(LLMError):
        ChatClient.from_cfg(cfg, role="x")


def test_parse_json_payload_strict() -> None:
    assert _parse_json_payload('{"a": 1}') == {"a": 1}


def test_parse_json_payload_extracts_from_noisy_text() -> None:
    noisy = 'Sure, here is the JSON:\n```json\n{"name": "x", "n": 2}\n```\n'
    out = _parse_json_payload(noisy)
    assert out == {"name": "x", "n": 2}


def test_parse_json_payload_empty_returns_empty_dict() -> None:
    assert _parse_json_payload("") == {}


def test_parse_json_payload_unparseable_raises() -> None:
    with pytest.raises(LLMError):
        _parse_json_payload("not json at all")
