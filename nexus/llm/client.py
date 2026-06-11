"""LLM client - thin OpenAI-compatible SDK wrapper with multi-provider routing.

Every council role goes through `ChatClient.from_role(config, role)`. The provider
field decides the base URL and auth header; the model field decides the request
body. DeepInfra council clients stream token deltas for prose/markdown calls while
still returning a complete response to callers. JSON-mode calls stay non-streamed
by default because they are machine-parsed control messages.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import AsyncOpenAI, OpenAIError

from nexus.config import ModelCfg
from nexus.llm.tracing import record_generation

log = logging.getLogger(__name__)

TokenSink = Callable[[dict[str, str]], Awaitable[None]]

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
    finish_reason: str = "stop"  # "stop" | "length" | "content_filter" | other
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """True when the response hit max_tokens before the model would have stopped."""
        return self.finish_reason == "length"


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
        timeout_s: float = 300.0,
        stream_chat: bool = False,
        token_sink: TokenSink | None = None,
        temperature: float = 0.0,
        top_p: float | None = None,
        trace_context: dict[str, Any] | None = None,
    ):
        """
        Initialize the client with provider/model configuration and create the underlying AsyncOpenAI and HTTP clients.

        Parameters:
            provider (str): Provider identifier (e.g., "deepinfra", "openai").
            model (str): Model name to use for requests.
            base_url (str): Provider base URL; any trailing slash is removed.
            api_key (str | None): API key to supply to the SDK; when None the SDK receives the sentinel "unused".
            role (str): Role label used in emitted token payloads and error messages.
            timeout_s (float): Timeout in seconds for the HTTP client and SDK.
            stream_chat (bool): When True, enable provider-specific streaming behavior by default.
            token_sink (TokenSink | None): Optional async callable invoked for each emitted token; ignored if None.
            temperature (float): Sampling temperature for generated completions.
            top_p (float | None): Nucleus sampling parameter to pass through to requests.
        """
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.role = role
        self._stream_chat = stream_chat
        self._token_sink = token_sink
        self.temperature = temperature
        self.top_p = top_p
        self.trace_context = trace_context or {}
        self._http_client = httpx.AsyncClient(timeout=timeout_s)
        # AsyncOpenAI sets Authorization from api_key for OpenAI-compatible providers.
        self._client = AsyncOpenAI(
            api_key=api_key or "unused",
            base_url=self.base_url,
            timeout=timeout_s,
            max_retries=0,
            http_client=self._http_client,
        )

    @classmethod
    def from_cfg(
        cls,
        cfg: ModelCfg,
        *,
        role: str,
        token_sink: TokenSink | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> ChatClient:
        """
        Create a ChatClient configured from the provided ModelCfg and role.

        Parameters:
            cfg (ModelCfg): Configuration containing provider, model, API key, and sampling settings.
            role (str): Role identifier used for logging and error messages.
            token_sink (TokenSink | None): Optional async callable to receive emitted token events.

        Returns:
            ChatClient: An instance configured with the resolved base URL, model, credentials, and streaming/sampling settings.

        Raises:
            LLMError: If no base URL can be resolved for the configured provider.
        """
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
            stream_chat=provider == "deepinfra",
            token_sink=token_sink,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            trace_context=trace_context,
        )

    async def aclose(self) -> None:
        """
        Close the underlying OpenAI SDK client and release associated network resources.

        This asynchronously closes the internal AsyncOpenAI client used by this ChatClient; the instance must not be used for further requests after calling this method.
        """
        await self._client.close()

    async def health(self) -> bool:
        """
        Return whether the provider's model-list endpoint is reachable.

        The check is intentionally broad because OpenAI-compatible providers vary
        in their exact model-list response shape.
        """
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int = 2048,
        json_mode: bool = False,
        stream: bool | None = None,
    ) -> ChatResponse:
        """
        Send a chat completion request and return the assembled assistant response.

        Builds an SDK-style request from the provided messages and sampling parameters, chooses streaming or non-streaming execution based on the `stream` argument and the client's streaming configuration, and retries once without streaming if a streaming attempt fails.

        Parameters:
            messages (list[dict[str, str]]): Conversation messages in OpenAI chat format (each item with 'role' and 'content').
            temperature (float | None): Sampling temperature to use for this call; falls back to the client's default when `None`.
            top_p (float | None): Nucleus sampling parameter to use for this call; omitted when `None`.
            max_tokens (int): Maximum tokens to generate for the completion.
            json_mode (bool): When True, requests the model to return a single JSON object (response_format={"type":"json_object"}).
            stream (bool | None): When True forces streaming; when False forces non-streaming; when None uses the client's streaming preference (suppressed for JSON mode unless explicitly allowed).

        Returns:
            ChatResponse: Assembled assistant content, token usage, model name, normalized finish reason, and raw response payload.

        Raises:
            LLMError: If the underlying chat call fails (after retry logic for streaming failures).
        """
        request_temperature = self.temperature if temperature is None else temperature
        request_top_p = self.top_p if top_p is None else top_p
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request_temperature,
            "max_tokens": max_tokens,
        }
        if request_top_p is not None:
            kwargs["top_p"] = request_top_p
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        should_stream = stream is True or (
            self._stream_chat and (stream if stream is not None else not json_mode)
        )
        start = time.perf_counter()
        try:
            if should_stream:
                try:
                    resp = await self._chat_stream(kwargs)
                except LLMError as e:
                    log.warning(
                        "%s: streaming chat failed; retrying without stream: %s",
                        self.role,
                        e,
                    )
                    resp = await self._chat_non_stream(kwargs)
            else:
                resp = await self._chat_non_stream(kwargs)
        except Exception as e:
            self._trace(messages, None, TokenUsage(), start, error=str(e))
            raise
        self._trace(messages, resp.content, resp.usage, start, finish_reason=resp.finish_reason)
        return resp

    def _trace(
        self,
        messages: list[dict[str, str]],
        output: str | None,
        usage: TokenUsage,
        start: float,
        *,
        finish_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        record_generation(
            name=self.role,
            model=self.model,
            provider=self.provider,
            messages=messages,
            output=output,
            usage={"prompt": usage.prompt, "completion": usage.completion},
            latency_ms=(time.perf_counter() - start) * 1000,
            finish_reason=finish_reason,
            error=error,
            metadata=self.trace_context,
        )

    async def _chat_non_stream(self, kwargs: dict[str, Any]) -> ChatResponse:
        """
        Convert a non-streaming SDK chat completion into a ChatResponse.

        Calls the OpenAI-compatible SDK's chat completions create method with the provided keyword arguments, extracts the first choice's message content, finish reason, and token usage, and returns a ChatResponse containing the assembled fields and the raw payload.

        Parameters:
            kwargs (dict[str, Any]): Keyword arguments forwarded to the SDK call (e.g., model, messages, temperature, max_tokens, response_format).

        Returns:
            ChatResponse: Assembled response with `content`, `usage` (prompt and completion token counts), `model`, `finish_reason`, and `raw` payload.

        Raises:
            LLMError: If the SDK call fails or the response contains no choices.
        """
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except OpenAIError as e:
            raise LLMError(f"{self.role}: chat call failed: {e}") from e
        payload = resp.model_dump(mode="json")
        choices = resp.choices
        if not choices:
            raise LLMError(f"{self.role}: empty choices in response")
        content = choices[0].message.content or ""
        finish_reason = str(choices[0].finish_reason or "stop").lower()
        usage = resp.usage
        return ChatResponse(
            content=content,
            usage=TokenUsage(
                prompt=int(usage.prompt_tokens if usage else 0),
                completion=int(usage.completion_tokens if usage else 0),
            ),
            model=self.model,
            finish_reason=finish_reason,
            raw=payload,
        )

    async def _chat_stream(self, kwargs: dict[str, Any]) -> ChatResponse:
        """
        Stream a chat completion from the configured provider, collect emitted text deltas, and forward each delta to the token sink.

        Parameters:
            kwargs (dict[str, Any]): Keyword arguments forwarded to the underlying chat completion call (e.g., model, messages, temperature, max_tokens, response_format).

        Returns:
            ChatResponse: Assembled response where `content` is the concatenation of all streamed text deltas, `usage` reflects the latest reported token counts, `model` is the model used, `finish_reason` is the normalized finish reason, and `raw` contains the collected stream chunks.
        """
        content_parts: list[str] = []
        finish_reason = "stop"
        usage = TokenUsage()
        raw_chunks: list[dict[str, Any]] = []
        try:
            stream = await self._client.chat.completions.create(
                **kwargs,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                payload = chunk.model_dump(mode="json")
                raw_chunks.append(payload)
                if chunk.usage:
                    usage = TokenUsage(
                        prompt=int(chunk.usage.prompt_tokens or usage.prompt),
                        completion=int(
                            chunk.usage.completion_tokens or usage.completion
                        ),
                    )
                for choice in chunk.choices or []:
                    choice_finish = choice.finish_reason
                    if choice_finish:
                        finish_reason = str(choice_finish).lower()
                    delta = choice.delta
                    text = delta.content or ""
                    if not text:
                        continue
                    content_parts.append(text)
                    await self._emit_token(text)
        except OpenAIError as e:
            raise LLMError(f"{self.role}: streaming chat call failed: {e}") from e

        return ChatResponse(
            content="".join(content_parts),
            usage=usage,
            model=self.model,
            finish_reason=finish_reason,
            raw={"stream": raw_chunks},
        )

    async def _emit_token(self, text: str) -> None:
        if self._token_sink is None:
            return
        try:
            await self._token_sink(
                {
                    "role": self.role,
                    "model": self.model,
                    "provider": self.provider,
                    "text": text,
                }
            )
        except Exception:
            log.warning("token sink failed for role=%s", self.role, exc_info=True)

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int = 2048,
        stream: bool = False,
    ) -> tuple[Any, TokenUsage]:
        """Convenience: ask for JSON, parse it. Falls back to extracting the first JSON
        object from the text if `response_format` isn't honoured by the provider."""
        resp = await self.chat(
            messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            json_mode=True,
            stream=stream,
        )
        try:
            return _parse_json_payload(resp.content), resp.usage
        except LLMError:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": resp.content},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid complete JSON. "
                        "Return only a valid JSON object that satisfies the requested "
                        "schema. Keep all string fields concise. Do not include Markdown, "
                        "fences, commentary, or repeated prompt text."
                    ),
                },
            ]
            repaired = await self.chat(
                repair_messages,
                temperature=0.0,
                top_p=top_p,
                max_tokens=max_tokens,
                json_mode=True,
                stream=False,
            )
            usage = TokenUsage(
                prompt=resp.usage.prompt + repaired.usage.prompt,
                completion=resp.usage.completion + repaired.usage.completion,
            )
            return _parse_json_payload(repaired.content), usage

    async def chat_markdown(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int = 2048,
        max_continuations: int = 2,
    ) -> ChatResponse:
        """Long-form markdown with auto-continuation on finish_reason=='length'.

        The aider/cursor pattern: when the API returns length-truncated, send the
        partial content back as an assistant message and ask the model to continue
        from exactly where it stopped. Combines the chunks into a single response.
        Token usage is summed across continuations.
        """
        resp = await self.chat(
            messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            json_mode=False,
        )
        if not resp.truncated:
            return resp

        combined = resp.content
        total_prompt = resp.usage.prompt
        total_completion = resp.usage.completion
        finish = resp.finish_reason

        for _ in range(max_continuations):
            continuation_messages = [
                *messages,
                {"role": "assistant", "content": combined},
                {
                    "role": "user",
                    "content": (
                        "Continue exactly where you stopped. Do not repeat any prior "
                        "text. Do not add any preamble. Resume mid-sentence if that's "
                        "where you stopped. End cleanly when the document is complete."
                    ),
                },
            ]
            next_resp = await self.chat(
                continuation_messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                json_mode=False,
            )
            combined += next_resp.content
            total_prompt += next_resp.usage.prompt
            total_completion += next_resp.usage.completion
            finish = next_resp.finish_reason
            if not next_resp.truncated:
                break

        return ChatResponse(
            content=combined,
            usage=TokenUsage(prompt=total_prompt, completion=total_completion),
            model=self.model,
            finish_reason=finish,
            raw=resp.raw,
        )


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
