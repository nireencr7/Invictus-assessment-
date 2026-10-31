"""The provider port. Everything model-specific lives behind this interface.

Errors are classified once, here, into the three things the pipeline cares about:
- TransientProviderError: try again later (timeouts, 429, 5xx, overloaded, network).
- PermanentProviderError: retrying will not help (bad request, auth, unknown model).
- ProviderRefusal: the model declined; a human should look.
`billed_unknown` marks failures where the provider may still have charged us (a timeout
after the request was sent); the budget books those at their reserved worst case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMRequest:
    system: str
    messages: list[dict]
    json_schema: dict
    max_tokens: int
    # Correlation only (logging, offline replay). Never sent to the provider.
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: Usage
    model: str
    stop_reason: str
    latency_ms: int
    request_id: str | None = None
    simulated: bool = False


class ProviderError(Exception):
    billed_unknown: bool = False

    def __init__(self, message: str, *, billed_unknown: bool = False, request_id: str | None = None):
        super().__init__(message)
        self.billed_unknown = billed_unknown
        self.request_id = request_id


class TransientProviderError(ProviderError):
    pass


class PermanentProviderError(ProviderError):
    pass


class ProviderRefusal(ProviderError):
    pass


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
