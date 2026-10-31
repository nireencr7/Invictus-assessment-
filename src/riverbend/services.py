"""Composition root: the one place that wires concrete implementations together."""

from __future__ import annotations

from dataclasses import dataclass

from riverbend.budget import Budget
from riverbend.catalog import Catalog
from riverbend.config import Settings
from riverbend.db import Database
from riverbend.llm.anthropic_provider import AnthropicProvider
from riverbend.llm.base import LLMProvider
from riverbend.llm.offline import OfflineProvider
from riverbend.llm.resilience import CircuitBreaker, FaultInjectingProvider, FaultPlan
from riverbend.pipeline import Pipeline
from riverbend.worker import Worker


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "offline":
        provider: LLMProvider = OfflineProvider(settings.offline_outputs_path, model=settings.llm_model)
    elif settings.llm_provider == "vertex":
        if not settings.vertex_project:
            raise ValueError("LLM_PROVIDER=vertex requires VERTEX_PROJECT")
        provider = AnthropicProvider(
            model=settings.llm_model,
            effort=settings.llm_effort,
            timeout_seconds=settings.llm_timeout_seconds,
            vertex_project=settings.vertex_project,
            vertex_region=settings.vertex_region,
        )
    else:
        if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
            raise ValueError(
                "LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY (set it in .env, never in git)"
            )
        provider = AnthropicProvider(
            model=settings.llm_model,
            effort=settings.llm_effort,
            timeout_seconds=settings.llm_timeout_seconds,
            api_key=settings.anthropic_api_key.get_secret_value(),
            refusal_fallback=settings.llm_refusal_fallback,
        )
    plan = FaultPlan.parse(settings.llm_faults)
    return FaultInjectingProvider(provider, plan, settings.llm_faults_seed) if plan.active else provider


@dataclass
class Services:
    settings: Settings
    db: Database
    catalog: Catalog
    budget: Budget
    provider: LLMProvider
    breaker: CircuitBreaker
    pipeline: Pipeline

    def worker(self) -> Worker:
        return Worker(
            self.db,
            self.pipeline,
            self.budget,
            lease_seconds=self.settings.job_lease_seconds,
            poll_seconds=self.settings.worker_poll_seconds,
        )

    def close(self) -> None:
        self.db.close()


def build_services(
    settings: Settings, *, provider: LLMProvider | None = None, db: Database | None = None
) -> Services:
    db = db or Database(settings.database_url.get_secret_value())
    catalog = Catalog.load(settings.catalog_path)
    budget = Budget(db, settings.budget_warn_ratio)
    provider = provider or build_provider(settings)
    breaker = CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_seconds)
    pipeline = Pipeline(
        db=db, catalog=catalog, provider=provider, budget=budget, breaker=breaker, settings=settings
    )
    return Services(settings, db, catalog, budget, provider, breaker, pipeline)
