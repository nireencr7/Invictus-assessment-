"""Offline stand-in for the model, so the whole system runs with no API key.

THIS IS NOT A MODEL and its outputs are not model results. It exists so that reviewers
can run the pipeline, the tests and the demo for free and deterministically:

1. Replay: for the 40 brief samples, `data/offline_model_outputs.json` holds hand-written
   outputs in the exact shape the real model returns — including a few deliberately bad
   first attempts (an injected `price` field, an ungrounded quote, truncated JSON) so the
   validator and repair path are exercised exactly as they would be live.
2. Heuristic: for anything else (e.g. a message typed into the demo dashboard) a crude
   keyword extractor produces a plausible object. It is easy to fool, by design obvious.

Token usage is simulated from text length so the spend cap can be demonstrated offline;
every response is marked `simulated=True` and reports label the spend as simulated.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from riverbend.llm.base import LLMRequest, LLMResponse, Usage


class OfflineProvider:
    name = "offline"

    def __init__(self, outputs_path: Path, model: str = "claude-opus-5-5"):
        self.model = model  # priced as if it were this model, so the budget logic is realistic
        self._outputs: dict[str, list] = {}
        if outputs_path.exists():
            self._outputs = json.loads(outputs_path.read_text())["outputs"]

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        key = f"{request.metadata.get('conversation_key')}#{request.metadata.get('turn', 1)}"
        attempt = (len(request.messages) - 1) // 2  # 0 = first call, 1 = repair call
        attempts = self._outputs.get(key)
        if attempts:
            item = attempts[min(attempt, len(attempts) - 1)]
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        else:
            text = json.dumps(heuristic_extract(_customer_texts(request)), ensure_ascii=False)
        prompt_chars = len(request.system) + sum(len(str(m["content"])) for m in request.messages)
        usage = Usage(input_tokens=prompt_chars // 4 + 900, output_tokens=len(text) // 3 + 120)
        return LLMResponse(
            text=text,
            usage=usage,
            model=self.model,
            stop_reason="end_turn",
            latency_ms=int((time.monotonic() - started) * 1000) + 5,
            simulated=True,
        )


def _customer_texts(request: LLMRequest) -> list[str]:
    user = str(request.messages[0]["content"])
    found = re.findall(r"<customer_(?:message|reply)>\n(.*?)\n</customer_", user, flags=re.S)
    unescape = lambda s: s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")  # noqa: E731
    return [unescape(t) for t in found]


# --- heuristic extractor (demo only) ------------------------------------------------

_SERVICE_RULES: list[tuple[str, str]] = [
    (r"full overhaul|overhaul", "TUNE_FULL"),
    (r"tune[- ]?up", "TUNE_BASIC"),
    (r"bleed|lever goes|spongy", "BRAKE_BLEED"),
    (r"pads?\b[^.]*\b(worn|metal|grind)|new pads", "BRAKE_PADS"),
    (r"brakes?[^.]*\b(squeal|soft|adjust|rub)", "BRAKE_ADJUST"),
    (r"e-?bike|motor|battery|display", "EBIKE_DIAG"),
    (r"chain[^.]*\b(rust|new|replace|skip|worn)|new chain", "CHAIN_REPLACE"),
    (r"gears?|shift|derailleur|cogs?", "GEAR_ADJUST"),
    (r"\bflat\b|puncture", "FLAT_FIX"),
    (r"replace[^.]*tires?|new tires?", "TIRE_REPLACE"),
    (r"wobbl|true\b|truing|out of true", "WHEEL_TRUE"),
    (r"noise|look it over|inspect|whatever it needs|clicking", "GENERAL_INSPECT"),
]
_PER_WHEEL = {"BRAKE_ADJUST", "BRAKE_PADS", "BRAKE_BLEED", "FLAT_FIX", "TIRE_REPLACE", "WHEEL_TRUE"}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_BIKES = ["road", "mountain", "hybrid", "gravel", "commuter", "city", "touring", "folding", "cargo"]


def _sentence_with(text: str, match: re.Match) -> str:
    start = text.rfind(".", 0, match.start()) + 1
    end_candidates = [i for i in (text.find(".", match.end()), text.find("?", match.end())) if i != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end].strip()


def heuristic_extract(texts: list[str]) -> dict:
    text = " ".join(texts)
    low = text.lower()
    services, issues, used = [], [], set()
    for pattern, code in _SERVICE_RULES:
        m = re.search(pattern, low)
        if not m or code in used:
            continue
        if code == "TUNE_BASIC" and "TUNE_FULL" in used:
            continue
        used.add(code)
        evidence = _sentence_with(text, m)[:200] or text[m.start() : m.end()]
        wheel = "not_applicable"
        if code in _PER_WHEEL:
            ctx = evidence.lower()
            wheel = (
                "both"
                if "both" in ctx
                else "front"
                if "front" in ctx
                else "rear"
                if ("rear" in ctx or "back" in ctx)
                else "unspecified"
            )
        services.append(
            {
                "code": code,
                "wheel": wheel,
                "bikes": 1,
                "evidence": evidence,
                "reason": f"keyword match ({code})",
            }
        )
        issues.append({"summary": f"Customer described: {evidence[:120]}", "evidence": evidence})

    date = {
        "kind": "unspecified",
        "weekday": "none",
        "month": 0,
        "day": 0,
        "time_of_day": "any",
        "time_24h": "",
        "phrase": "",
    }
    if re.search(r"\basap\b|as soon as", low):
        date |= {"kind": "asap", "phrase": "ASAP"}
    elif "today" in low:
        date |= {"kind": "today", "phrase": "today"}
    elif "tomorrow" in low:
        date |= {"kind": "tomorrow", "phrase": "tomorrow"}
    elif "next week" in low:
        date |= {"kind": "next_week", "phrase": "next week"}
    elif "weekend" in low:
        date |= {"kind": "this_weekend", "phrase": "weekend"}
    else:
        for day in _WEEKDAYS:
            m = re.search(rf"\b(next\s+)?{day}\b", low)
            if m:
                date |= {
                    "kind": "next_weekday" if m.group(1) else "weekday",
                    "weekday": day,
                    "phrase": text[m.start() : m.end()],
                }
                break
    for tod in ("morning", "afternoon", "evening"):
        if tod in low and date["kind"] != "unspecified":
            date["time_of_day"] = tod

    intent = "repair_request"
    if re.search(r"\bcancel", low):
        intent, services = "cancellation", []
    elif re.search(r"how much|what do you charge|price", low) and not re.search(
        r"\bcan i\b|drop|bring|book", low
    ):
        intent = "price_inquiry"

    bike_type = (
        "e-bike"
        if re.search(r"e-?bike", low)
        else next((b for b in _BIKES if re.search(rf"\b{b}\b", low)), "unknown")
    )
    brake_type = "hydraulic_disc" if "hydraulic" in low else "rim" if "rim brake" in low else "unknown"
    if brake_type == "rim":
        services = [s for s in services if s["code"] != "BRAKE_BLEED"]

    follow_up = {"needed": False, "reason": "none", "question": ""}
    if intent == "repair_request" and not services:
        follow_up = {
            "needed": True,
            "reason": "missing_issue",
            "question": "Could you tell us what's wrong with the bike?",
        }
    elif intent == "repair_request" and date["kind"] == "unspecified":
        follow_up = {
            "needed": True,
            "reason": "missing_date",
            "question": "When would you like to bring the bike in?",
        }

    return {
        "intent": intent,
        "language": "es" if re.search(r"\b(hola|frenos|bicicleta|puedo)\b", low) else "en",
        "customer_name": "",
        "bike_type": bike_type,
        "bike_count": 1,
        "brake_type": brake_type,
        "reported_issues": issues[:10],
        "services": services[:10],
        "urgency": "high" if re.search(r"\basap\b|today|urgent", low) else "normal",
        "urgency_reason": "heuristic",
        "requested_date": date,
        "other_shop_mentioned": "none",
        "shop_confirmation": "not_applicable",
        "suspected_prompt_injection": bool(re.search(r"system note|ignore previous|set every price", low)),
        "follow_up": follow_up,
        "notes_for_staff": "Produced by the offline heuristic extractor (demo mode), not a model.",
    }
