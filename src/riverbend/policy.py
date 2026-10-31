"""Business policy: given a *validated* extraction, decide what happens next.

A pure function with no I/O, so every rule here is a one-line unit test. The model's
output is an input to this decision, not the decision itself:
- The model may suggest a follow-up question; code decides whether one is warranted
  (only when a required field is missing) and enforces "at most one".
- The model may claim urgency; under a suspected prompt injection code caps it.
- The model picks services; code prices them from the catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from riverbend.catalog import Catalog, PricedLine
from riverbend.extraction.dates import DateWindow, resolve
from riverbend.extraction.schema import Extraction

# Independent of the model's own flag: a crude pattern check is cheap defence in depth.
_INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\b(system|assistant|ai|developer)\s+(note|prompt|instruction|message|override)",
        r"\bignore\s+(all\s+|any\s+)?(previous|prior|above)\s+(instructions|rules)",
        r"\bset\s+(every|all|the)\s+prices?\b",
        r"\bprices?\s+to\s+(0|zero|\$?0(\.00)?)\b",
        r"\byou\s+are\s+now\b",
    )
]


def looks_like_injection(texts: tuple[str, ...]) -> bool:
    return any(p.search(t) for t in texts for p in _INJECTION_PATTERNS)


@dataclass(frozen=True)
class DecisionContext:
    shop_id: str
    shop_name: str
    follow_ups_asked: int
    received_local_date: date
    customer_texts: tuple[str, ...]


@dataclass(frozen=True)
class CreateWorkOrder:
    lines: tuple[PricedLine, ...]
    estimate_cents: int
    window: DateWindow | None
    urgency: str
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class AskFollowUp:
    reason: str
    question: str
    question_source: str  # "model" | "template"


@dataclass(frozen=True)
class SendQuote:
    lines: tuple[PricedLine, ...]


@dataclass(frozen=True)
class RequestCancellation:
    customer_name: str


@dataclass(frozen=True)
class Redirect:
    target_shop_id: str


@dataclass(frozen=True)
class Escalate:
    reason: str
    detail: str = ""


Decision = CreateWorkOrder | AskFollowUp | SendQuote | RequestCancellation | Redirect | Escalate

FOLLOW_UP_TEMPLATES = {
    "missing_issue": "Could you tell us a little more about what's wrong with the bike, so we can book the right repair?",
    "missing_date": "When would you like to bring the bike in? A day (and morning or afternoon) is perfect.",
    "ambiguous_request": "Could you tell us a little more about what you'd like us to do, so we can book the right repair?",
}


def wrong_shop_question(this_shop: str, other_shop: str) -> str:
    return (
        f"Your message reached {this_shop}, but it mentions {other_shop}. "
        f"Would you like {this_shop} to handle this request? Reply 'yes' to continue here, "
        f"or let us know if you'd rather contact {other_shop} directly."
    )


@dataclass
class _Review:
    reasons: list[str] = field(default_factory=list)

    def add(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


def price_lines(ex: Extraction, catalog: Catalog) -> tuple[PricedLine, ...]:
    return tuple(catalog.price_line(s.code, s.wheel, s.bikes) for s in ex.services)


def decide(ex: Extraction, ctx: DecisionContext, catalog: Catalog) -> Decision:
    if ex.intent == "cancellation":
        return RequestCancellation(customer_name=ex.customer_name)
    if ex.intent == "price_inquiry":
        if ex.services:
            return SendQuote(lines=price_lines(ex, catalog))
        return Escalate(
            "price_question_unclear", "Customer asked about prices but no catalog service was identified."
        )
    if ex.intent == "other":
        return Escalate("not_a_repair_request", ex.notes_for_staff)

    review = _Review()

    # Wrong-shop: never move a customer's message to another tenant automatically.
    other = ex.other_shop_mentioned
    if other not in ("none", ctx.shop_id):
        if ex.shop_confirmation == "wants_other_shop":
            return Redirect(target_shop_id=other)
        if ex.shop_confirmation != "confirmed_this_shop":
            if ctx.follow_ups_asked == 0:
                return AskFollowUp(
                    "wrong_shop",
                    wrong_shop_question(ctx.shop_name, catalog.shops[other].name),
                    "template",
                )
            return Escalate(
                "shop_unclear_after_follow_up", f"Message mentions {other}; customer did not confirm."
            )
        review.add("customer_confirmed_shop_after_wrong_shop_question")

    window = resolve(ex.requested_date, ctx.received_local_date)

    # Required fields: something to fix, and when. Bike type is useful but not required —
    # the shop sees the bike at drop-off, and asking for it would double the follow-ups.
    missing = None
    if not ex.services:
        missing = "missing_issue"
    elif window is None:
        missing = "missing_date"

    if missing:
        if ctx.follow_ups_asked == 0:
            model_reason = ex.follow_up.reason if ex.follow_up.needed else "none"
            agrees = model_reason == missing or (
                missing == "missing_issue" and model_reason == "ambiguous_request"
            )
            if agrees and ex.follow_up.question.strip():
                return AskFollowUp(model_reason, ex.follow_up.question.strip(), "model")
            return AskFollowUp(missing, FOLLOW_UP_TEMPLATES[missing], "template")
        if missing == "missing_issue":
            return Escalate(
                "no_service_after_follow_up", "Still no identifiable repair after one follow-up question."
            )
        review.add("no_date_after_follow_up")

    lines = price_lines(ex, catalog)
    urgency = ex.urgency
    if ex.suspected_prompt_injection or looks_like_injection(ctx.customer_texts):
        review.add("possible_prompt_injection")
        if urgency == "high":
            urgency = "normal"
    if any(line.requires_inspection for line in lines):
        review.add("estimate_provisional_until_inspection")
    if any(line.wheel == "unspecified" for line in lines):
        review.add("wheel_not_specified_estimate_assumes_one")
    if ex.language != "en":
        review.add(f"customer_language_{ex.language}")

    return CreateWorkOrder(
        lines=lines,
        estimate_cents=catalog.total_cents(list(lines)),
        window=window,
        urgency=urgency,
        review_reasons=tuple(review.reasons),
    )
