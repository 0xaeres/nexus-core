"""LLM client - thin OpenAI-compatible HTTP wrapper with multi-provider routing.

Every council role goes through `ChatClient.from_role(config, role)`. The provider
field decides the base URL and auth header; the model field decides the request
body. Streaming and structured-output are deliberately omitted for the MVP — we
parse JSON from the model's text response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from nexus.config import ModelCfg

# Provider → base URL. Override with model.base_url / model.url in nexus.yaml.
_PROVIDER_BASES: dict[str, str] = {
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "ollama": "http://localhost:11434/v1",
}


@dataclass(frozen=True)
class TokenUsage:
    prompt: int = 0
    completion: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion


@dataclass
class ChatResponse:
    content: str
    usage: TokenUsage
    model: str
    raw: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class ChatClient:
    """Async chat client. Construct one per role for clean cost attribution."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        role: str,
        timeout_s: float = 120.0,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.role = role
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout_s)

    @classmethod
    def from_cfg(cls, cfg: ModelCfg, *, role: str) -> ChatClient:
        provider = cfg.provider.lower()
        base = cfg.base_url or cfg.url or _PROVIDER_BASES.get(provider)
        if not base:
            raise LLMError(f"no base URL known for provider={provider}")
        return cls(
            provider=provider,
            model=cfg.model,
            base_url=base,
            api_key=cfg.api_key,
            role=role,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> ChatResponse:
        """OpenAI-compatible /chat/completions. Returns the assistant content."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"{self.role}: chat call failed: {e}") from e
        if resp.status_code != 200:
            raise LLMError(
                f"{self.role}: chat returned {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        choices = payload.get("choices", [])
        if not choices:
            raise LLMError(f"{self.role}: empty choices in response")
        content = choices[0].get("message", {}).get("content", "")
        usage_obj = payload.get("usage", {}) or {}
        return ChatResponse(
            content=content or "",
            usage=TokenUsage(
                prompt=int(usage_obj.get("prompt_tokens", 0)),
                completion=int(usage_obj.get("completion_tokens", 0)),
            ),
            model=self.model,
            raw=payload,
        )

    async def chat_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2, max_tokens: int = 2048
    ) -> tuple[Any, TokenUsage]:
        """Convenience: ask for JSON, parse it. Falls back to extracting the first JSON
        object from the text if `response_format` isn't honoured by the provider."""
        resp = await self.chat(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        return _parse_json_payload(resp.content), resp.usage


def _parse_json_payload(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to extract a JSON object from a fenced or noisy response
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    raise LLMError(f"failed to parse JSON from model output: {text[:200]!r}")
