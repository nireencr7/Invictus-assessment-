"""Builds the offline stand-in model outputs, the golden labels and the simulated customer
replies from ONE table, so they cannot drift apart.

    uv run python scripts/build_offline_fixtures.py

The table below is our reading of what a correct extraction looks like for each brief
sample (where reasonable mechanics could disagree, `accept` lists alternatives). Three
first attempts are deliberately broken, the way real models break, to exercise the
validator and the repair path:
  m-1013  an "evidence" quote the customer never wrote (hallucinated detail)
  m-1024  an extra `discount_price: 0` field — the prompt injection partly "worked"
  m-1036  JSON cut off mid-object (as if max_tokens hit)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from riverbend.catalog import Catalog  # noqa: E402
from riverbend.extraction.schema import Extraction  # noqa: E402
from riverbend.extraction.validate import ValidationContext, semantic_errors  # noqa: E402

NA = "not_applicable"


def D(kind="unspecified", weekday="none", month=0, day=0, tod="any", time="", phrase=""):
    return {
        "kind": kind,
        "weekday": weekday,
        "month": month,
        "day": day,
        "time_of_day": tod,
        "time_24h": time,
        "phrase": phrase,
    }


def svc(code, wheel, evidence, reason, bikes=1):
    return {"code": code, "wheel": wheel, "bikes": bikes, "evidence": evidence, "reason": reason}


def issue(summary, evidence):
    return {"summary": summary, "evidence": evidence}


def X(
    *,
    intent="repair_request",
    lang="en",
    name="",
    bike="unknown",
    count=1,
    brakes="unknown",
    issues=(),
    services=(),
    urgency="normal",
    urgency_reason="No safety issue or time pressure stated.",
    date=None,
    other="none",
    confirm="not_applicable",
    injection=False,
    follow=None,
    notes="",
):
    return {
        "intent": intent,
        "language": lang,
        "customer_name": name,
        "bike_type": bike,
        "bike_count": count,
        "brake_type": brakes,
        "reported_issues": list(issues),
        "services": list(services),
        "urgency": urgency,
        "urgency_reason": urgency_reason,
        "requested_date": date or D(),
        "other_shop_mentioned": other,
        "shop_confirmation": confirm,
        "suspected_prompt_injection": injection,
        "follow_up": follow or {"needed": False, "reason": "none", "question": ""},
        "notes_for_staff": notes,
    }


# key -> (outputs for each attempt, golden expectation)
T: dict[str, dict] = {}


def add(
    key,
    output,
    *,
    outcome,
    codes=None,
    accept=(),
    estimate=None,
    window=None,
    bad_first=None,
    reason=None,
    flags=(),
):
    T[key] = {
        "attempts": ([bad_first] if bad_first is not None else []) + [output],
        "golden": {
            "outcome": outcome,
            "codes": codes,
            "accept": [sorted(a) for a in accept],
            "estimate_cents": estimate,
            "window": window,
            "follow_up_reason": reason,
            "review_flags": list(flags),
        },
    }


add(
    "m-1001#1",
    X(
        name="Priya",
        bike="road",
        issues=[issue("Wants a tune-up before a charity ride", "my road bike needs a tune-up")],
        services=[svc("TUNE_BASIC", NA, "needs a tune-up", "Customer asks for a tune-up.")],
        date=D("weekday", "thursday", tod="morning", phrase="Thursday morning"),
        notes="Charity ride on October 4; bike must be ready before then.",
    ),
    outcome="work_order_created",
    codes={"TUNE_BASIC": 1},
    accept=[["TUNE_BASIC"]],
    estimate=6500,
    window=["2026-09-24", "2026-09-24"],
)

add(
    "m-1002#1",
    X(
        bike="kids",
        issues=[issue("Flat front tire", "Flat front tire on my kid's bike")],
        services=[svc("FLAT_FIX", "front", "Flat front tire", "Front tire is flat.")],
        date=D("tomorrow", tod="afternoon", time="16:00", phrase="tomorrow after school, around 4"),
    ),
    outcome="work_order_created",
    codes={"FLAT_FIX": 1},
    accept=[["FLAT_FIX"]],
    estimate=1500,
    window=["2026-09-23", "2026-09-23"],
)

add(
    "m-1003#1",
    X(
        name="Marcus",
        bike="hybrid",
        issues=[
            issue(
                "Chain falls off when shifting to the small chainring",
                "keeps falling off when I shift to the small ring",
            )
        ],
        services=[
            svc(
                "GEAR_ADJUST",
                NA,
                "The chain on my hybrid keeps falling off when I shift to the small ring",
                "Dropped chain on a shift is a derailleur limit/adjustment problem.",
            )
        ],
        date=D("weekday", "saturday", phrase="Free Saturday"),
    ),
    outcome="work_order_created",
    codes={"GEAR_ADJUST": 1},
    accept=[["GEAR_ADJUST"]],
    estimate=2500,
    window=["2026-09-26", "2026-09-26"],
)

add(
    "m-1004#1",
    X(
        bike="mountain",
        brakes="hydraulic_disc",
        issues=[
            issue(
                "Rear brake pads worn to the metal, grinding",
                "Rear brake pads are worn to the metal, grinding noise",
            )
        ],
        services=[svc("BRAKE_PADS", "rear", "Rear brake pads are worn to the metal", "Worn-out rear pads.")],
        urgency="high",
        urgency_reason="Pads worn to metal: braking is compromised.",
        date=D("weekday", "friday", phrase="Friday OK?"),
    ),
    outcome="work_order_created",
    codes={"BRAKE_PADS": 1},
    accept=[["BRAKE_PADS"], ["BRAKE_PADS", "BRAKE_BLEED"]],
    estimate=3500,
    window=["2026-09-25", "2026-09-25"],
)

add(
    "m-1005#1",
    X(
        issues=[
            issue("Rear brake squeals", "My rear brake squeals"),
            issue("Gears skip on hills", "the gears skip on hills"),
        ],
        services=[
            svc(
                "BRAKE_ADJUST",
                "rear",
                "My rear brake squeals",
                "Squeal without wear mentioned: adjust/clean.",
            ),
            svc("GEAR_ADJUST", NA, "the gears skip on hills", "Skipping under load: indexing adjustment."),
        ],
        date=D("weekday", "saturday", phrase="Saturday"),
    ),
    outcome="work_order_created",
    codes={"BRAKE_ADJUST": 1, "GEAR_ADJUST": 1},
    estimate=4300,
    accept=[
        ["BRAKE_ADJUST", "GEAR_ADJUST"],
        ["BRAKE_PADS", "GEAR_ADJUST"],
        ["BRAKE_ADJUST", "CHAIN_REPLACE"],
    ],
    window=["2026-09-26", "2026-09-26"],
)

add(
    "m-1006#1",
    X(
        bike="commuter",
        issues=[issue("Rear wheel wobbles after a pothole", "Back wheel wobbles side to side")],
        services=[
            svc("WHEEL_TRUE", "rear", "Back wheel wobbles side to side", "Wheel out of true after impact.")
        ],
        urgency="low",
        urgency_reason="Customer is flexible on timing.",
        date=D("next_week", phrase="Any day next week"),
    ),
    outcome="work_order_created",
    codes={"WHEEL_TRUE": 1},
    accept=[["WHEEL_TRUE"]],
    estimate=3000,
    window=["2026-09-28", "2026-10-04"],
)

add(
    "m-1007#1",
    X(
        name="Dana",
        bike="touring",
        issues=[issue("Wants a full overhaul", "I'd like the full overhaul")],
        services=[
            svc(
                "TUNE_FULL", NA, "I'd like the full overhaul", "Customer explicitly asks for a full overhaul."
            )
        ],
        urgency="low",
        urgency_reason="'No rush'.",
        date=D("month", month=10, phrase="sometime in October"),
        notes="1987 steel touring bike.",
    ),
    outcome="work_order_created",
    codes={"TUNE_FULL": 1},
    accept=[["TUNE_FULL"]],
    estimate=18500,
    window=["2026-10-01", "2026-10-31"],
)

add(
    "m-1008#1",
    X(
        bike="e-bike",
        issues=[
            issue(
                "Display error E07 and motor cuts out",
                "E-bike display shows error E07 and the motor cuts out",
            )
        ],
        services=[
            svc(
                "EBIKE_DIAG",
                NA,
                "E-bike display shows error E07 and the motor cuts out",
                "Electrical fault on an e-bike.",
            )
        ],
        date=D("weekday", "thursday", phrase="Thursday"),
    ),
    outcome="work_order_created",
    codes={"EBIKE_DIAG": 1},
    accept=[["EBIKE_DIAG"]],
    estimate=6000,
    window=["2026-09-24", "2026-09-24"],
    flags=["estimate_provisional_until_inspection"],
)

jo = dict(
    name="Jo",
    bike="gravel",
    issues=[issue("Wants both tires replaced", "replace both tires on my gravel bike")],
    services=[
        svc("TIRE_REPLACE", "both", "replace both tires", "Customer asks for both tires to be replaced.")
    ],
    date=D("weekday", "sunday", phrase="Sunday works"),
    other="rb-lakeshore",
    notes="Greeted 'Lakeshore team' and mentions a previous visit; that visit is not visible from this shop.",
)
add("m-1009#1", X(**jo), outcome="follow_up_asked", reason="wrong_shop")
add(
    "m-1009#2",
    X(**{**jo, "confirm": "confirmed_this_shop"}),
    outcome="work_order_created",
    codes={"TIRE_REPLACE": 2},
    accept=[["TIRE_REPLACE"]],
    estimate=4000,
    window=["2026-09-27", "2026-09-27"],
    flags=["customer_confirmed_shop_after_wrong_shop_question"],
)

add(
    "m-1010#1",
    X(
        bike="road",
        issues=[issue("Chain is rusty, wants a new one", "Need new chain, old one is rusty")],
        services=[svc("CHAIN_REPLACE", NA, "Need new chain", "Rusty chain to replace.")],
        date=D("weekday", "wednesday", phrase="Wednesday"),
    ),
    outcome="work_order_created",
    codes={"CHAIN_REPLACE": 1},
    accept=[["CHAIN_REPLACE"]],
    estimate=4500,
    window=["2026-09-23", "2026-09-23"],
)

add(
    "m-1011#1",
    X(
        brakes="hydraulic_disc",
        issues=[
            issue(
                "Front brake lever pulls almost to the bar",
                "Front brake lever goes almost to the handlebar before it stops",
            )
        ],
        services=[
            svc(
                "BRAKE_BLEED",
                "front",
                "Front brake lever goes almost to the handlebar before it stops",
                "Lever travel to the bar on hydraulic brakes means air in the system.",
            )
        ],
        urgency="high",
        urgency_reason="Front brake barely works; daily commuter; asked ASAP.",
        date=D("asap", phrase="ASAP please"),
    ),
    outcome="work_order_created",
    codes={"BRAKE_BLEED": 1},
    accept=[["BRAKE_BLEED"], ["BRAKE_BLEED", "BRAKE_PADS"]],
    estimate=4000,
    window=["2026-09-22", "2026-09-23"],
)

add(
    "m-1012#1",
    X(
        name="Ken",
        issues=[issue("Used bike needs a pre-ride check", "want someone to look it over")],
        services=[
            svc(
                "GENERAL_INSPECT",
                NA,
                "want someone to look it over before I ride it much",
                "Pre-purchase style inspection.",
            )
        ],
        date=D("weekday", "saturday", tod="morning", phrase="Saturday morning?"),
    ),
    outcome="work_order_created",
    codes={"GENERAL_INSPECT": 1},
    accept=[["GENERAL_INSPECT"], ["TUNE_BASIC"]],
    estimate=2500,
    window=["2026-09-26", "2026-09-26"],
)

m1013 = X(
    issues=[issue("Two rear flats in one week; tire may be the cause", "Two flats in one week on the rear")],
    services=[svc("FLAT_FIX", "rear", "Two flats in one week on the rear", "Repeated rear flat.")],
    date=D("weekday", "friday", tod="afternoon", phrase="Friday afternoon"),
    notes="Customer suspects the tire itself; check casing and offer a tire replacement if worn.",
)
m1013_bad = json.loads(json.dumps(m1013))
m1013_bad["services"].append(
    svc("TIRE_REPLACE", "rear", "the tire is worn out", "Tire is worn.")
)  # never said
add(
    "m-1013#1",
    m1013,
    bad_first=m1013_bad,
    outcome="work_order_created",
    codes={"FLAT_FIX": 1},
    estimate=1500,
    accept=[["FLAT_FIX"], ["FLAT_FIX", "TIRE_REPLACE"], ["FLAT_FIX", "GENERAL_INSPECT"]],
    window=["2026-09-25", "2026-09-25"],
)

add(
    "m-1015#1",
    X(
        bike="folding",
        issues=[issue("Wants a basic tune-up", "Basic tune-up please")],
        services=[svc("TUNE_BASIC", NA, "Basic tune-up please", "Requested explicitly.")],
        date=D("weekday", "monday", phrase="Monday"),
    ),
    outcome="work_order_created",
    codes={"TUNE_BASIC": 1},
    accept=[["TUNE_BASIC"]],
    estimate=6500,
    window=["2026-09-28", "2026-09-28"],
)

add(
    "m-1016#1",
    X(
        issues=[
            issue("Won't shift into the two largest cogs", "Gears won't shift into the two biggest cogs")
        ],
        services=[
            svc(
                "GEAR_ADJUST", NA, "Gears won't shift into the two biggest cogs", "Indexing/limit adjustment."
            )
        ],
        date=D("tomorrow", phrase="Tomorrow if possible"),
    ),
    outcome="work_order_created",
    codes={"GEAR_ADJUST": 1},
    accept=[["GEAR_ADJUST"]],
    estimate=2500,
    window=["2026-09-23", "2026-09-23"],
)

add(
    "m-1017#1",
    X(
        name="Aisha",
        bike="city",
        brakes="rim",
        issues=[issue("Both brakes feel soft", "Both brakes need adjusting, they feel soft")],
        services=[
            svc("BRAKE_ADJUST", "both", "Both brakes need adjusting", "Soft rim brakes need adjusting.")
        ],
        date=D("weekday", "thursday", time="10:00", phrase="Thursday at 10?"),
    ),
    outcome="work_order_created",
    codes={"BRAKE_ADJUST": 2},
    accept=[["BRAKE_ADJUST"]],
    estimate=3600,
    window=["2026-09-24", "2026-09-24"],
)

add(
    "m-1018#1",
    X(
        intent="price_inquiry",
        issues=[],
        services=[
            svc(
                "TUNE_FULL", NA, "How much do you charge for a full overhaul", "Asked for the overhaul price."
            )
        ],
        urgency="low",
        urgency_reason="Price comparison only.",
        notes="Comparing shops; no booking requested.",
    ),
    outcome="price_quoted",
    codes={"TUNE_FULL": 1},
    accept=[["TUNE_FULL"]],
)

add(
    "m-1019#1",
    X(
        bike="road",
        brakes="rim",
        issues=[
            issue(
                "Front wheel rubs the brake pad once per rotation",
                "Front wheel rubs the brake pad every rotation",
            )
        ],
        services=[
            svc(
                "WHEEL_TRUE",
                "front",
                "Front wheel rubs the brake pad every rotation",
                "Rubbing once per rotation means the rim is out of true.",
            )
        ],
        date=D("weekday", "friday", phrase="Friday"),
    ),
    outcome="work_order_created",
    codes={"WHEEL_TRUE": 1},
    estimate=3000,
    accept=[["WHEEL_TRUE"], ["BRAKE_ADJUST"], ["WHEEL_TRUE", "BRAKE_ADJUST"]],
    window=["2026-09-25", "2026-09-25"],
)

add(
    "m-1020#1",
    X(
        issues=[issue("Clicking noise, source unknown", "making a clicking noise somewhere")],
        services=[
            svc(
                "GENERAL_INSPECT",
                NA,
                "making a clicking noise somewhere. Not sure where",
                "Unlocated noise needs an inspection.",
            )
        ],
        date=D("this_weekend", phrase="Weekend?"),
    ),
    outcome="work_order_created",
    codes={"GENERAL_INSPECT": 1},
    accept=[["GENERAL_INSPECT"]],
    estimate=2500,
    window=["2026-09-26", "2026-09-27"],
)

add(
    "m-1021#1",
    X(
        name="Leo",
        bike="gravel",
        issues=[
            issue(
                "Tubeless tire no longer holds air", "Tubeless tire on my gravel bike won't hold air anymore"
            )
        ],
        services=[
            svc("TIRE_REPLACE", "unspecified", "Can you replace it", "Customer wants the tire replaced.")
        ],
        date=D("weekday", "thursday", phrase="Thursday"),
        notes="Tubeless tire: the tire itself is not in the catalog (labor only). Wheel not stated.",
    ),
    outcome="work_order_created",
    codes={"TIRE_REPLACE": 1},
    accept=[["TIRE_REPLACE"]],
    estimate=2000,
    window=["2026-09-24", "2026-09-24"],
    flags=["wheel_not_specified_estimate_assumes_one"],
)

add(
    "m-1022#1",
    X(
        bike="e-bike",
        issues=[
            issue(
                "Battery range dropped from 50 km to 10 km", "E-bike battery drains in 10 km, used to do 50"
            )
        ],
        services=[svc("EBIKE_DIAG", NA, "E-bike battery drains in 10 km", "Battery/electrical diagnosis.")],
        date=D("weekday", "saturday", phrase="Saturday"),
    ),
    outcome="work_order_created",
    codes={"EBIKE_DIAG": 1},
    accept=[["EBIKE_DIAG"]],
    estimate=6000,
    window=["2026-09-26", "2026-09-26"],
)

add(
    "m-1023#1",
    X(
        name="Sam",
        issues=[
            issue("Wants a basic tune-up", "can you do a basic tune-up"),
            issue("Rear wheel needs truing", "true the back wheel"),
        ],
        services=[
            svc("TUNE_BASIC", NA, "a basic tune-up", "Requested."),
            svc("WHEEL_TRUE", "rear", "true the back wheel", "Requested for the rear wheel."),
        ],
        date=D("weekday", "wednesday", tod="afternoon", phrase="Wednesday afternoon"),
    ),
    outcome="work_order_created",
    codes={"TUNE_BASIC": 1, "WHEEL_TRUE": 1},
    accept=[["TUNE_BASIC", "WHEEL_TRUE"]],
    estimate=9500,
    window=["2026-09-23", "2026-09-23"],
)

m1024 = X(
    issues=[issue("Wants the chain replaced", "Chain replacement please")],
    services=[svc("CHAIN_REPLACE", NA, "Chain replacement please", "Requested.")],
    urgency="high",
    urgency_reason="Customer note says to mark urgent.",
    injection=True,
    date=D("weekday", "friday", phrase="Friday"),
    notes="Message contains text addressed to the assistant asking for zero prices and urgency; ignored.",
)
m1024_bad = {**m1024, "discount_price": 0}
add(
    "m-1024#1",
    m1024,
    bad_first=m1024_bad,
    outcome="work_order_created",
    codes={"CHAIN_REPLACE": 1},
    accept=[["CHAIN_REPLACE"]],
    estimate=4500,
    window=["2026-09-25", "2026-09-25"],
    flags=["possible_prompt_injection"],
)

add(
    "m-1025#1",
    X(
        issues=[
            issue(
                "Bike unused for three years; brakes, tires and chain look rough",
                "Brakes, tires, chain all look rough",
            )
        ],
        services=[
            svc(
                "GENERAL_INSPECT", NA, "Whatever it needs", "Unknown scope after long storage: inspect first."
            )
        ],
        date=D("weekday", "monday", phrase="Monday"),
        notes="Stored three years; likely needs brakes, tires and chain. Quote after inspection.",
    ),
    outcome="work_order_created",
    codes={"GENERAL_INSPECT": 1},
    estimate=2500,
    accept=[["GENERAL_INSPECT"], ["TUNE_FULL"], ["TUNE_BASIC"], ["GENERAL_INSPECT", "TUNE_BASIC"]],
    window=["2026-09-28", "2026-09-28"],
)

add(
    "m-1026#1",
    X(
        issues=[issue("Wants a gear adjustment", "Gear adjustment")],
        services=[svc("GEAR_ADJUST", NA, "Gear adjustment", "Requested.")],
        date=D("tomorrow", tod="morning", time="09:00", phrase="Tomorrow 9am"),
    ),
    outcome="work_order_created",
    codes={"GEAR_ADJUST": 1},
    accept=[["GEAR_ADJUST"]],
    estimate=2500,
    window=["2026-09-23", "2026-09-23"],
)

brakes_squeal = dict(
    issues=[issue("Brakes squeal", "Brakes squeal")],
    services=[svc("BRAKE_ADJUST", "both", "Brakes squeal", "Squealing brakes: adjust and clean.")],
)
add(
    "m-1027#1",
    X(
        **brakes_squeal,
        follow={
            "needed": True,
            "reason": "missing_date",
            "question": "When would you like to bring the bike in?",
        },
    ),
    outcome="follow_up_asked",
    reason="missing_date",
)
add(
    "m-1027#2",
    X(
        **{**brakes_squeal, "bike": "road"},
        date=D("weekday", "thursday", tod="afternoon", phrase="Thursday afternoon"),
    ),
    outcome="work_order_created",
    codes={"BRAKE_ADJUST": 2},
    accept=[["BRAKE_ADJUST"]],
    estimate=3600,
    window=["2026-09-24", "2026-09-24"],
)

add(
    "m-1028#1",
    X(
        name="Nora",
        issues=[
            issue("Bent rear derailleur, shifting badly", "Rear derailleur got bent when the bike fell over")
        ],
        services=[
            svc(
                "GEAR_ADJUST",
                NA,
                "Rear derailleur got bent",
                "Bent derailleur: realign/adjust (hanger may need parts).",
            )
        ],
        date=D("weekday", "thursday", phrase="Thursday?"),
        notes="Derailleur or hanger may need replacing; parts not in catalog.",
    ),
    outcome="work_order_created",
    codes={"GEAR_ADJUST": 1},
    estimate=2500,
    accept=[["GEAR_ADJUST"], ["GEAR_ADJUST", "GENERAL_INSPECT"], ["GENERAL_INSPECT"]],
    window=["2026-09-24", "2026-09-24"],
)

add(
    "m-1029#1",
    X(
        name="Ravi",
        bike="mountain",
        brakes="hydraulic_disc",
        issues=[
            issue("New pads on both wheels", "New pads on both wheels"),
            issue("Bleed both brakes", "bleed both brakes"),
        ],
        services=[
            svc("BRAKE_PADS", "both", "New pads on both wheels", "Requested."),
            svc("BRAKE_BLEED", "both", "bleed both brakes", "Requested."),
        ],
        date=D("weekday", "saturday", phrase="Saturday"),
    ),
    outcome="work_order_created",
    codes={"BRAKE_PADS": 2, "BRAKE_BLEED": 2},
    accept=[["BRAKE_PADS", "BRAKE_BLEED"]],
    estimate=15000,
    window=["2026-09-26", "2026-09-26"],
)

add(
    "m-1030#1",
    X(
        issues=[issue("Front flat", "Front flat")],
        services=[svc("FLAT_FIX", "front", "Front flat", "Flat front tire.")],
        urgency="high",
        urgency_reason="Asked for today.",
        date=D("today", phrase="Today if you can"),
    ),
    outcome="work_order_created",
    codes={"FLAT_FIX": 1},
    accept=[["FLAT_FIX"]],
    estimate=1500,
    window=["2026-09-22", "2026-09-22"],
)

add(
    "m-1031#1",
    X(
        bike="road",
        issues=[issue("Both wheels need truing", "Wheel truing, front and rear")],
        services=[svc("WHEEL_TRUE", "both", "Wheel truing, front and rear", "Requested for both wheels.")],
        date=D("next_weekday", "tuesday", phrase="Next Tuesday"),
    ),
    outcome="work_order_created",
    codes={"WHEEL_TRUE": 2},
    accept=[["WHEEL_TRUE"]],
    estimate=6000,
    window=["2026-09-29", "2026-09-29"],
)

add(
    "m-1032#1",
    X(
        name="Beth",
        bike="cargo",
        issues=[issue("Wants a basic tune-up before winter", "basic tune-up on my cargo bike")],
        services=[svc("TUNE_BASIC", NA, "basic tune-up", "Requested.")],
        urgency="low",
        urgency_reason="Flexible: any weekday, before winter.",
        date=D("any_weekday", phrase="Any weekday"),
    ),
    outcome="work_order_created",
    codes={"TUNE_BASIC": 1},
    accept=[["TUNE_BASIC"]],
    estimate=6500,
    window=["2026-09-23", "2026-10-06"],
)

add(
    "m-1033#1",
    X(
        lang="es",
        name="Carlos",
        issues=[
            issue("Brakes don't stop well", "los frenos de mi bicicleta no frenan bien"),
            issue("Chain is noisy", "la cadena hace ruido"),
        ],
        services=[
            svc(
                "BRAKE_ADJUST",
                "both",
                "los frenos de mi bicicleta no frenan bien",
                "Weak braking on both brakes.",
            ),
            svc("GEAR_ADJUST", NA, "la cadena hace ruido", "Noisy chain is usually indexing; check wear."),
        ],
        urgency="high",
        urgency_reason="Brakes are not stopping well (safety).",
        date=D("weekday", "friday", phrase="el viernes"),
        notes="Customer wrote in Spanish. Check chain wear while adjusting gears.",
    ),
    outcome="work_order_created",
    codes={"BRAKE_ADJUST": 2, "GEAR_ADJUST": 1},
    estimate=6100,
    accept=[
        ["BRAKE_ADJUST", "GEAR_ADJUST"],
        ["BRAKE_ADJUST", "CHAIN_REPLACE"],
        ["BRAKE_ADJUST", "GENERAL_INSPECT"],
        ["BRAKE_ADJUST"],
    ],
    window=["2026-09-25", "2026-09-25"],
    flags=["customer_language_es"],
)

add(
    "m-1034#1",
    X(
        issues=[
            issue("Chain skips under load at ~5000 km", "Chain skips under load, it's about 5000 km old")
        ],
        services=[
            svc(
                "CHAIN_REPLACE",
                NA,
                "Chain skips under load, it's about 5000 km old",
                "Worn chain at 5000 km.",
            )
        ],
        date=D("weekday", "wednesday", phrase="Wednesday"),
    ),
    outcome="work_order_created",
    codes={"CHAIN_REPLACE": 1},
    accept=[["CHAIN_REPLACE"], ["CHAIN_REPLACE", "GEAR_ADJUST"]],
    estimate=4500,
    window=["2026-09-23", "2026-09-23"],
)

add(
    "m-1035#1",
    X(
        bike="commuter",
        issues=[
            issue(
                "Replace both tires; customer supplies tires",
                "Replace both tires, I have the new tires already",
            )
        ],
        services=[svc("TIRE_REPLACE", "both", "Replace both tires", "Labor only; customer has the tires.")],
        date=D("weekday", "friday", phrase="Friday"),
        notes="Customer brings their own tires.",
    ),
    outcome="work_order_created",
    codes={"TIRE_REPLACE": 2},
    accept=[["TIRE_REPLACE"]],
    estimate=4000,
    window=["2026-09-25", "2026-09-25"],
)

m1036 = X(
    name="Omar",
    bike="e-bike",
    issues=[issue("E-bike won't power on after washing", "E-bike won't turn on at all after I washed it")],
    services=[
        svc(
            "EBIKE_DIAG",
            NA,
            "E-bike won't turn on at all after I washed it",
            "Likely water ingress; electrical diagnosis.",
        )
    ],
    date=D("tomorrow", phrase="Tomorrow please"),
)
add(
    "m-1036#1",
    m1036,
    bad_first=json.dumps(m1036)[:180],
    outcome="work_order_created",
    codes={"EBIKE_DIAG": 1},
    accept=[["EBIKE_DIAG"]],
    estimate=6000,
    window=["2026-09-23", "2026-09-23"],
)

add(
    "m-1037#1",
    X(
        brakes="hydraulic_disc",
        issues=[issue("Rear hydraulic brake bleed", "Hydraulic brake bleed, rear only")],
        services=[svc("BRAKE_BLEED", "rear", "Hydraulic brake bleed, rear only", "Requested.")],
        date=D("weekday", "saturday", tod="morning", time="11:00", phrase="Saturday 11am"),
    ),
    outcome="work_order_created",
    codes={"BRAKE_BLEED": 1},
    accept=[["BRAKE_BLEED"]],
    estimate=4000,
    window=["2026-09-26", "2026-09-26"],
)

add(
    "m-1038#1",
    X(
        intent="cancellation",
        name="Marcus",
        issues=[],
        services=[],
        urgency_reason="Cancellation.",
        notes="Wants to cancel this morning's chain appointment; fixed it himself.",
    ),
    outcome="cancellation_requested",
)

add(
    "m-1039#1",
    X(
        bike="mountain",
        issues=[
            issue("Wants a full overhaul", "Full overhaul on my mountain bike"),
            issue("Rear flat", "it has a flat on the rear"),
        ],
        services=[
            svc("TUNE_FULL", NA, "Full overhaul", "Requested."),
            svc("FLAT_FIX", "rear", "it has a flat on the rear", "Rear flat."),
        ],
        date=D("next_weekday", "monday", phrase="Next Monday"),
    ),
    outcome="work_order_created",
    codes={"TUNE_FULL": 1, "FLAT_FIX": 1},
    accept=[["TUNE_FULL", "FLAT_FIX"]],
    estimate=20000,
    window=["2026-09-28", "2026-09-28"],
)

add(
    "m-1040#1",
    X(
        name="Greta",
        bike="kids",
        count=2,
        issues=[
            issue("Basic tune-up for two bikes", "Basic tune-up for my daughter's bike and mine, two bikes")
        ],
        services=[
            svc(
                "TUNE_BASIC",
                NA,
                "Basic tune-up for my daughter's bike and mine, two bikes",
                "Two bikes, one tune-up each.",
                bikes=2,
            )
        ],
        date=D("weekday", "saturday", phrase="Saturday"),
        notes="Two bikes: a child's bike and an adult bike.",
    ),
    outcome="work_order_created",
    codes={"TUNE_BASIC": 2},
    accept=[["TUNE_BASIC"]],
    estimate=13000,
    window=["2026-09-26", "2026-09-26"],
)

# Customer replies the eval harness sends when (and only when) a follow-up was asked.
# Written to answer the questions a live model is likely to ask, too.
REPLIES = {
    "m-1009": "Oops, wrong greeting! Yes please, Downtown is fine for this, it's closer to work. Jo",
    "m-1027": "Thursday afternoon if possible. It's a road bike.",
    "m-1005": "It's a hybrid with rim brakes. Saturday morning works.",
    "m-1013": "It's the rear tire and it looks worn. Friday afternoon is fine.",
    "m-1020": "It's a road bike, the clicking comes from the pedals. Saturday works.",
    "m-1021": "It's the rear tire. Please supply a new tubeless tire. Thursday.",
    "m-1025": "It's a hybrid. Please do a full service, whatever it needs. Monday.",
    "m-1033": "Es una bicicleta de montaña con frenos de disco hidráulicos. El viernes por la mañana.",
    "m-1012": "It's a road bike. Saturday morning around 10.",
    "m-1040": "My daughter's is a kids' bike and mine is a hybrid. Saturday morning.",
    "m-1019": "Yes, the rim seems bent slightly. Friday is good.",
    "m-1028": "The derailleur itself is bent; I'd like it straightened or replaced. Thursday.",
}


def main() -> None:
    catalog = Catalog.load(ROOT / "data" / "catalog.json")
    samples = {
        m["message_id"]: m for m in json.loads((ROOT / "data" / "samples.json").read_text())["messages"]
    }
    outputs, golden = {}, {}
    for key, entry in T.items():
        ext, turn = key.split("#")
        texts = (samples[ext]["body"],) + ((REPLIES[ext],) if turn == "2" else ())
        final = entry["attempts"][-1]
        ex = Extraction.model_validate(final)  # fixtures must themselves be valid (except bad_first)
        errs = semantic_errors(ex, ValidationContext(catalog, samples[ext]["shop_id"], texts))
        assert not errs, (key, errs)
        if entry["golden"]["codes"]:
            est = sum(catalog.services[c].price_cents * q for c, q in entry["golden"]["codes"].items())
            assert entry["golden"]["estimate_cents"] in (None, est), (key, est)
        outputs[key] = entry["attempts"]
        golden[key] = entry["golden"]
    (ROOT / "data" / "offline_model_outputs.json").write_text(
        json.dumps(
            {
                "_note": "Hand-written stand-in outputs for offline runs. NOT model output. Built by "
                "scripts/build_offline_fixtures.py. Keys are '<first message_id>#<customer turn>'; "
                "each value lists outputs for successive attempts (first call, repair call).",
                "outputs": outputs,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n"
    )
    (ROOT / "data" / "golden.json").write_text(
        json.dumps(
            {
                "_note": "Expected outcome per conversation turn. 'accept' lists acceptable service-code sets "
                "(quantities checked only against 'codes'). Built by scripts/build_offline_fixtures.py.",
                "expected": golden,
                "duplicates": ["m-1003@09:22"],
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n"
    )
    (ROOT / "data" / "simulated_replies.json").write_text(
        json.dumps(
            {
                "_note": "Simulated customer replies, sent by the eval harness only to conversations awaiting a reply.",
                "replies": REPLIES,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"wrote {len(outputs)} fixture entries, {len(golden)} golden entries, {len(REPLIES)} replies")


if __name__ == "__main__":
    main()
