"""Gemini via Google Vertex AI.

This is the model the deployment runs while the project has no Claude quota on Vertex (every
Claude model answers 429 until Google approves an increase). It keeps the Claude adapter's contract:
- SDK retries are OFF (attempts=1; 0 would mean the SDK default of 5). Retries happen only in
  the job queue, where they back off, respect the circuit breaker and reserve budget.
- Output is constrained by the same JSON schema (`response_json_schema`); we still validate.
- Auth is the service account (ADC). No API key exists in this deployment mode.
- A safety block is a refusal: a person looks at the message, as with Claude's `refusal`.
Thinking tokens are billed as output, so they are counted as output here.
"""

from __future__ import annotations

import time

import httpx
from google import genai
from google.auth import exceptions as auth_exceptions
from google.genai import errors, types

from riverbend.llm.base import (
    LLMRequest,
    LLMResponse,
    PermanentProviderError,
    ProviderRefusal,
    TransientProviderError,
    Usage,
)

try:  # the SDK may run on either httpx generation
    import httpx2

    _TIMEOUTS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx2.TimeoutException)
    _NETWORK: tuple[type[Exception], ...] = (httpx.TransportError, httpx2.TransportError)
except ImportError:  # pragma: no cover - depends on the installed SDK
    _TIMEOUTS, _NETWORK = (httpx.TimeoutException,), (httpx.TransportError,)

_BLOCKED = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}
_THINKING = {
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        model: str,
        effort: str,
        timeout_seconds: float,
        vertex_project: str,
        vertex_region: str = "global",
        client: genai.Client | None = None,
    ):
        self.model = model
        self.effort = effort
        self._client = client or genai.Client(
            vertexai=True,
            project=vertex_project,
            location=vertex_region,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000), retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    def _config(self, request: LLMRequest) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=request.system,
            max_output_tokens=request.max_tokens,
            response_mime_type="application/json",
            response_json_schema=request.json_schema,
            thinking_config=types.ThinkingConfig(thinking_level=_THINKING[self.effort]),
        )

    @staticmethod
    def _contents(request: LLMRequest) -> list[types.Content]:
        return [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part(text=str(m["content"]))],
            )
            for m in request.messages
        ]

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=self._contents(request), config=self._config(request)
            )
        except _TIMEOUTS as exc:
            # The request may have reached the model: we might be billed.
            raise TransientProviderError(f"timeout: {exc}", billed_unknown=True) from exc
        except _NETWORK as exc:
            raise TransientProviderError(f"connection: {exc}") from exc
        except auth_exceptions.TransportError as exc:
            raise TransientProviderError(f"auth transport: {exc}") from exc
        except auth_exceptions.GoogleAuthError as exc:
            raise PermanentProviderError(f"credentials: {exc}") from exc
        except errors.APIError as exc:
            detail = f"HTTP {exc.code} {exc.status or ''}: {exc.message or ''}".strip()
            if exc.code == 429 or exc.code in (408, 409) or exc.code >= 500:
                raise TransientProviderError(detail) from exc
            raise PermanentProviderError(detail) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        u = resp.usage_metadata
        cached = (u.cached_content_token_count or 0) if u else 0
        usage = Usage(
            input_tokens=max(((u.prompt_token_count or 0) if u else 0) - cached, 0),
            output_tokens=((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0,
            cache_read_tokens=cached,
        )
        request_id = resp.response_id
        feedback = resp.prompt_feedback
        if feedback and feedback.block_reason:
            raise ProviderRefusal(f"prompt blocked: {_name(feedback.block_reason)}", request_id=request_id)
        candidate = resp.candidates[0] if resp.candidates else None
        if candidate is None:
            raise ProviderRefusal("no candidate returned", request_id=request_id)
        finish = _name(candidate.finish_reason)
        if finish in _BLOCKED:
            raise ProviderRefusal(f"response blocked: {finish}", request_id=request_id)
        parts = candidate.content.parts if candidate.content and candidate.content.parts else []
        # Thought summaries are never requested, but skip them if a model returns any.
        text = "".join(p.text for p in parts if p.text and not p.thought)
        return LLMResponse(
            text=text,
            usage=usage,
            model=resp.model_version or self.model,
            # MAX_TOKENS means truncated JSON: validation rejects it and the repair path / staff take over.
            stop_reason=finish.lower(),
            latency_ms=latency_ms,
            request_id=request_id,
        )


def _name(value) -> str:
    return getattr(value, "name", None) or str(value or "")
