"""Turns raw model text into a trusted `Extraction`, or a list of reasons it isn't one.

This is the gate the brief asks for: "Output that fails validation is never saved as a
work order." Nothing downstream accepts anything but the `Extraction` returned here.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from riverbend.catalog import Catalog
from riverbend.extraction.schema import Extraction


@dataclass(frozen=True)
class ValidationContext:
    catalog: Catalog
    shop_id: str
    customer_texts: tuple[str, ...]  # everything the customer wrote in this conversation, verbatim


@dataclass(frozen=True)
class Invalid:
    errors: tuple[str, ...]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    return re.sub(r"[^\w]+", " ", text).strip()


def is_grounded(quote: str, sources: tuple[str, ...]) -> bool:
    """True if every fragment of `quote` appears in the customer's own words.

    Case, punctuation and whitespace are ignored; '...' separates fragments, so the model
    may elide words but not invent them.
    """
    haystack = " " + " ".join(_norm(s) for s in sources) + " "
    fragments = [f for f in re.split(r"\.\.\.|…", quote) if _norm(f)]
    return bool(fragments) and all(f" {_norm(f)} " in haystack for f in fragments)


def _parse_json(raw: str) -> dict | Invalid:
    text = raw.strip()
    # Constrained decoding should give bare JSON; tolerate a fenced block from providers without it.
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return Invalid((f"output is not valid JSON: {exc.msg} at char {exc.pos}",))
    if not isinstance(data, dict):
        return Invalid(("output must be a single JSON object",))
    return data


def _shape_errors(exc: ValidationError) -> tuple[str, ...]:
    out = []
    for err in exc.errors()[:12]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        out.append(f"{loc}: {err['msg']}")
    return tuple(out)


def semantic_errors(ex: Extraction, ctx: ValidationContext) -> list[str]:
    errors: list[str] = []
    catalog = ctx.catalog
    seen: set[tuple[str, str]] = set()
    codes = {line.code for line in ex.services}

    if ex.other_shop_mentioned != "none" and ex.other_shop_mentioned not in catalog.shops:
        errors.append(f"other_shop_mentioned: unknown shop id {ex.other_shop_mentioned!r}")

    for i, line in enumerate(ex.services):
        where = f"services[{i}]"
        svc = catalog.services.get(line.code)
        if svc is None:
            errors.append(f"{where}.code: {line.code!r} is not a catalog service code")
            continue
        key = (line.code, line.wheel)
        if key in seen:
            errors.append(
                f"{where}: duplicate line for {line.code}/{line.wheel}; use one line with the right wheel/bikes"
            )
        seen.add(key)
        if svc.per_wheel and line.wheel == "not_applicable":
            errors.append(f"{where}.wheel: {line.code} is priced per wheel; use front/rear/both/unspecified")
        if not svc.per_wheel and line.wheel != "not_applicable":
            errors.append(f"{where}.wheel: {line.code} is not a per-wheel service; use 'not_applicable'")
        if line.bikes > ex.bike_count:
            errors.append(f"{where}.bikes: {line.bikes} exceeds bike_count {ex.bike_count}")
        if line.code == "BRAKE_BLEED" and ex.brake_type in ("rim", "mechanical_disc"):
            errors.append(
                f"{where}: BRAKE_BLEED is only possible on hydraulic brakes (brake_type={ex.brake_type})"
            )
        if not is_grounded(line.evidence, ctx.customer_texts):
            errors.append(f"{where}.evidence: {line.evidence!r} is not a verbatim quote from the customer")

    for i, issue in enumerate(ex.reported_issues):
        if not is_grounded(issue.evidence, ctx.customer_texts):
            errors.append(
                f"reported_issues[{i}].evidence: {issue.evidence!r} is not a verbatim quote from the customer"
            )

    rd = ex.requested_date
    if rd.kind == "calendar_date":
        try:
            date(2024, rd.month, rd.day)  # leap year: accepts 29 Feb, rejects 31 Sep
        except ValueError:
            errors.append(f"requested_date: {rd.month}/{rd.day} is not a real calendar date")

    if {"TUNE_BASIC", "TUNE_FULL"} <= codes:
        errors.append("services: TUNE_FULL already includes a tune-up; do not list TUNE_BASIC as well")
    if (
        ex.intent == "repair_request"
        and not ex.services
        and not (ex.follow_up.needed and ex.follow_up.reason in ("missing_issue", "ambiguous_request"))
    ):
        errors.append(
            "services: a repair_request needs at least one service, or a follow-up asking what is wrong"
        )
    if ex.intent == "cancellation" and ex.services:
        errors.append("services: a cancellation must not list services")
    return errors


def parse_and_validate(raw: str, ctx: ValidationContext) -> Extraction | Invalid:
    data = _parse_json(raw)
    if isinstance(data, Invalid):
        return data
    try:
        ex = Extraction.model_validate(data)
    except ValidationError as exc:
        return Invalid(_shape_errors(exc))
    errors = semantic_errors(ex, ctx)
    if errors:
        return Invalid(tuple(errors))
    return ex
