"""Runtime configuration.

Every setting comes from the environment (or a local, git-ignored `.env` file in dev).
On GCP the same variables are injected by Cloud Run, with secrets mounted from
Secret Manager, so the code never knows where a value came from.

Secrets are typed as SecretStr so they never appear in logs, reprs or tracebacks.
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- database -------------------------------------------------------------
    # The app connects as a NON-superuser role so Postgres row-level security applies.
    database_url: SecretStr = SecretStr("postgresql://riverbend_app:riverbend_app@localhost:5433/riverbend")
    # Owner/admin connection: migrations, seeding and the eval harness only. Never used by request handlers.
    database_admin_url: SecretStr = SecretStr("postgresql://postgres@localhost:5433/riverbend")
    app_db_password: SecretStr = SecretStr("riverbend_app")

    # --- auth -------------------------------------------------------------------
    # JSON maps. Dev defaults are deliberately obvious; production values come from Secret Manager.
    ingest_keys: SecretStr = SecretStr('{"dev-ingest-key": ["rb-downtown", "rb-northside", "rb-lakeshore"]}')
    staff_tokens: SecretStr = SecretStr(
        '{"dev-downtown-token": "rb-downtown", "dev-northside-token": "rb-northside",'
        ' "dev-lakeshore-token": "rb-lakeshore"}'
    )
    admin_token: SecretStr = SecretStr("dev-admin-token")
    # Staff sign-in with email and password (see staff_auth.py). A session lasts about a shift.
    staff_session_hours: float = Field(default=12.0, gt=0, le=24 * 7)
    login_max_failures: int = Field(default=5, ge=1)
    login_lockout_minutes: float = Field(default=15.0, gt=0)

    # --- LLM ----------------------------------------------------------------------
    # vertex = Claude on Vertex AI, gemini = Gemini on Vertex AI, anthropic = Claude API (key in .env).
    llm_provider: Literal["anthropic", "vertex", "gemini", "offline"] = "offline"
    llm_model: str = "claude-opus-5-5"  # with gemini, a Gemini id such as gemini-3.8-flash
    llm_effort: Literal["low", "medium", "high"] = "low"
    llm_max_tokens: int = 4000
    llm_timeout_seconds: float = 45.0
    llm_refusal_fallback: bool = True
    anthropic_api_key: SecretStr | None = None
    vertex_project: str | None = None
    vertex_region: str = "global"
    # Fault injection for demos and chaos runs, e.g. "flaky:0.3", "outage", "slow:2.0", "garbage:0.2".
    llm_faults: str = ""
    llm_faults_seed: int = 7

    # --- pipeline policy ------------------------------------------------------------
    max_attempts: int = 6
    retry_base_seconds: float = 20.0
    retry_max_seconds: float = 600.0
    # If the AI has not produced a result this long after the message arrived, hand it to staff.
    processing_sla_seconds: float = 900.0
    job_lease_seconds: float = 120.0
    breaker_failure_threshold: int = 5
    breaker_cooldown_seconds: float = 30.0
    default_monthly_cap_usd: Decimal = Decimal("5.00")
    budget_warn_ratio: Decimal = Decimal("0.80")

    # --- runtime ----------------------------------------------------------------------
    embedded_worker: bool = False
    worker_poll_seconds: float = 0.5
    dev_mode: bool = True
    # Lets signed-in staff send a message *as a customer* of their own shop from the console, to
    # try the workflow end to end. Always on in dev mode; off in production unless switched on.
    test_messages: bool = False
    # Comma-separated origins allowed to call the API from a browser (only needed when the staff
    # console is hosted on another origin without its reverse proxy). Empty = CORS off.
    cors_origins: str = ""
    catalog_path: Path = Field(default=REPO_ROOT / "data" / "catalog.json")
    offline_outputs_path: Path = Field(default=REPO_ROOT / "data" / "offline_model_outputs.json")
    shop_timezone: str = "America/Chicago"
    log_level: str = "INFO"

    @field_validator("llm_max_tokens")
    @classmethod
    def _max_tokens_sane(cls, v: int) -> int:
        if not 256 <= v <= 16000:
            raise ValueError("llm_max_tokens must be between 256 and 16000")
        return v

    @property
    def test_messages_enabled(self) -> bool:
        return self.test_messages or self.dev_mode

    def ingest_key_map(self) -> dict[str, set[str]]:
        return {k: set(v) for k, v in json.loads(self.ingest_keys.get_secret_value()).items()}

    def staff_token_map(self) -> dict[str, str]:
        return dict(json.loads(self.staff_tokens.get_secret_value()))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
