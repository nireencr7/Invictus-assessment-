"""The 40 brief samples, end to end through HTTP + worker + Postgres, as a regression test.

Uses the offline stand-in (hand-written outputs, three deliberately broken), so this checks
the *system* — idempotency, validation, follow-ups, pricing, dates, tenancy — not a model.
The same harness runs against the live model with `make eval-live`.
"""

import pytest

from riverbend import evaluation
from tests.conftest import make_settings


@pytest.fixture
def settings_for_eval(db_urls):
    return make_settings(db_urls)


def test_baseline_samples(settings_for_eval):
    r = evaluation.scenario_baseline(settings_for_eval)
    assert r["messages_received"] == 40 and r["duplicates"] == 1 and r["conversations"] == 39
    assert r["work_orders"] == 37
    assert r["events"]["follow_up_asked"] == 2
    assert r["events"]["price_quoted"] == 1
    assert r["events"]["cancellation_requested"] == 1
    assert r["calls_by_status"]["invalid_output"] == 3  # the three planted bad outputs, all repaired
    assert not [x for x in r["rows"] if x["state"] == "needs_staff"]
    for key in ("ok_outcome", "ok_services", "ok_quantities", "ok_estimate", "ok_window", "ok_follow_up"):
        failures = [x["first_id"] for x in r["rows"] if x.get(key) is False]
        assert not failures, (key, failures)
    injected = next(x for x in r["rows"] if x["first_id"] == "m-1024")
    assert injected["estimate_cents"] == 4500 and injected["urgency"] == "normal"
    assert "possible_prompt_injection" in injected["review"]


def test_chaos_has_no_duplicates_and_loses_nothing(settings_for_eval):
    r = evaluation.scenario_chaos(settings_for_eval)
    assert r["retries"] > 0
    # Every conversation ends somewhere visible: a work order, a question, a quote, a cancel, or staff.
    assert {x["state"] for x in r["rows"]} <= {
        "work_order_created",
        "awaiting_customer",
        "answered",
        "cancellation_requested",
        "needs_staff",
    }
    assert r["work_orders"] <= 37
