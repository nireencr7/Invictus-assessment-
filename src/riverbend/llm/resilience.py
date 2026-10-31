"""Circuit breaker and fault injection.

The breaker stops a worker from hammering a provider that is clearly down: after N
consecutive transient failures it opens for a cooldown, during which jobs are deferred
(without spending an attempt) instead of called. One probe is let through when the
cooldown ends (half-open); success closes the breaker.

It is per worker process. With several workers each learns independently, which costs a
few extra failed calls per outage — acceptable at this scale, and noted in the docs.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from riverbend.llm.base import LLMRequest, LLMResponse, TransientProviderError


class CircuitBreaker:
    def __init__(self, failure_threshold: int, cooldown_seconds: float, clock=time.monotonic):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """May we call the provider now?"""
        with self._lock:
            state = self._state_locked()
            if state == "closed":
                return True
            if state == "half_open" and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

    def seconds_until_retry(self) -> float:
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self.cooldown_seconds - (self._clock() - self._opened_at))

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self) -> bool:
        """Returns True if this failure opened (or re-opened) the breaker."""
        with self._lock:
            self._failures += 1
            was_probe = self._probe_in_flight
            self._probe_in_flight = False
            if was_probe or self._failures >= self.failure_threshold:
                self._opened_at = self._clock()
                return True
            return False


@dataclass
class FaultPlan:
    flaky_rate: float = 0.0  # fraction of calls that fail with a transient error
    garbage_rate: float = 0.0  # fraction of calls that return non-JSON
    outage_calls: int = 0  # first N calls fail (-1 = every call)
    slow_seconds: float = 0.0  # extra latency per call

    @classmethod
    def parse(cls, spec: str) -> FaultPlan:
        plan = cls()
        for part in filter(None, (p.strip() for p in spec.split(","))):
            name, _, arg = part.partition(":")
            if name == "flaky":
                plan.flaky_rate = float(arg or 0.3)
            elif name == "garbage":
                plan.garbage_rate = float(arg or 0.2)
            elif name == "outage":
                plan.outage_calls = int(arg) if arg else -1
            elif name == "slow":
                plan.slow_seconds = float(arg or 2.0)
            else:
                raise ValueError(f"unknown fault {name!r}")
        return plan

    @property
    def active(self) -> bool:
        return bool(self.flaky_rate or self.garbage_rate or self.outage_calls or self.slow_seconds)


class FaultInjectingProvider:
    """Wraps a real provider and makes it misbehave on purpose. Faults fire *before* the
    inner call, so a chaos run never pays for a call it then throws away."""

    def __init__(self, inner, plan: FaultPlan, seed: int = 7):
        self.inner = inner
        self.plan = plan
        self.name = f"{inner.name}+faults"
        self.model = inner.model
        self._rng = random.Random(seed)
        self._calls = 0
        self._lock = threading.Lock()
        self.injected = {"outage": 0, "flaky": 0, "garbage": 0}

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self._calls += 1
            n = self._calls
            roll_fail, roll_garbage = self._rng.random(), self._rng.random()
        if self.plan.slow_seconds:
            time.sleep(self.plan.slow_seconds)
        if self.plan.outage_calls == -1 or n <= self.plan.outage_calls:
            self.injected["outage"] += 1
            raise TransientProviderError("injected outage: HTTP 529 overloaded")
        if roll_fail < self.plan.flaky_rate:
            self.injected["flaky"] += 1
            raise TransientProviderError("injected flaky failure: HTTP 503")
        response = self.inner.complete(request)
        if roll_garbage < self.plan.garbage_rate:
            self.injected["garbage"] += 1
            return LLMResponse(
                text=response.text[: len(response.text) // 2],  # truncated JSON, still billed
                usage=response.usage,
                model=response.model,
                stop_reason="max_tokens",
                latency_ms=response.latency_ms,
                request_id=response.request_id,
                simulated=response.simulated,
            )
        return response
