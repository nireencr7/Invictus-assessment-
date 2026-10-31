"""Claude via the Anthropic API or Google Vertex AI.

Design choices worth knowing:
- SDK retries are OFF (max_retries=0). Retries happen in exactly one place — the job
  queue — where they are visible, back off, respect the circuit breaker and reserve budget.
- Structured outputs (`output_config.format`) constrain the shape; we still validate.
- Server-side refusal fallback is on for the first-party API and switches itself off if the
  account or endpoint rejects it, so a missing beta can never take the pipeline down.
"""

from __future__ import annotations

import logging
import time

import anthropic

from riverbend.llm.base import (
    LLMRequest,
    LLMResponse,
    PermanentProviderError,
    ProviderRefusal,
    TransientProviderError,
    Usage,
)

log = logging.getLogger(__name__)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        effort: str,
        timeout_seconds: float,
        api_key: str | None = None,
        vertex_project: str | None = None,
        vertex_region: str = "global",
        refusal_fallback: bool = True,
    ):
        self.model = model
        self.effort = effort
        if vertex_project:
            self.name = "vertex"
            # Auth is the service account (ADC). No API key exists in this deployment mode.
            self._client = anthropic.AnthropicVertex(
                project_id=vertex_project, region=vertex_region, timeout=timeout_seconds, max_retries=0
            )
            self._fallback = False  # server-side fallbacks are first-party only
        else:
            self.name = "anthropic"
            self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)
            self._fallback = refusal_fallback

    def _call(self, request: LLMRequest):
        kwargs = dict(
            model=self.model,
            max_tokens=request.max_tokens,
            system=[{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            messages=request.messages,
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": request.json_schema},
            },
        )
        if self._fallback:
            return self._client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
        return self._client.messages.create(**kwargs)

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            try:
                msg = self._call(request)
            except anthropic.BadRequestError as exc:
                if self._fallback and ("fallback" in str(exc).lower() or "beta" in str(exc).lower()):
                    log.warning("refusal fallback rejected by the API; continuing without it: %s", exc)
                    self._fallback = False
                    msg = self._call(request)
                else:
                    raise
        except anthropic.APITimeoutError as exc:
            # The request may have reached the model: we might be billed.
            raise TransientProviderError(f"timeout: {exc}", billed_unknown=True) from exc
        except anthropic.APIConnectionError as exc:
            raise TransientProviderError(f"connection: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise TransientProviderError(f"rate limited: {exc.message}", request_id=_rid(exc)) from exc
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ) as exc:
            raise PermanentProviderError(
                f"{type(exc).__name__}: {exc.message}", request_id=_rid(exc)
            ) from exc
        except anthropic.BadRequestError as exc:
            raise PermanentProviderError(f"bad request: {exc.message}", request_id=_rid(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in (408, 409):
                raise TransientProviderError(
                    f"HTTP {exc.status_code}: {exc.message}", request_id=_rid(exc)
                ) from exc
            raise PermanentProviderError(
                f"HTTP {exc.status_code}: {exc.message}", request_id=_rid(exc)
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        request_id = getattr(msg, "_request_id", None)
        if msg.stop_reason == "refusal":
            raise ProviderRefusal("model refused", request_id=request_id)
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        if msg.stop_reason == "max_tokens":
            # Truncated JSON. Let validation reject it (and the repair path / staff take over).
            log.warning("model output hit max_tokens (%s)", request.max_tokens)
        return LLMResponse(
            text=text,
            usage=usage,
            model=getattr(msg, "model", None) or self.model,
            stop_reason=msg.stop_reason or "",
            latency_ms=latency_ms,
            request_id=request_id,
        )


def _rid(exc: anthropic.APIStatusError) -> str | None:
    try:
        return exc.response.headers.get("request-id")
    except Exception:  # pragma: no cover - defensive
        return None
