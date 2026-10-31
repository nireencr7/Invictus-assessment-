"""The service catalog and all money arithmetic.

Boundary rule: the model may *choose* service codes and say which wheel(s); it never
sees a price and never produces a number that becomes money. Quantities and totals are
computed here, in integer cents, from the catalog file the client gave us.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# Services priced "per wheel". Kept explicit (not parsed from names at runtime) and
# cross-checked against the catalog names on load, so a catalog edit that changes the
# unit fails loudly at startup instead of silently mispricing.
PER_WHEEL_CODES = frozenset(
    {"BRAKE_ADJUST", "BRAKE_PADS", "BRAKE_BLEED", "FLAT_FIX", "TIRE_REPLACE", "WHEEL_TRUE"}
)

# One-line hints that help the model map symptoms to codes. They describe *when* to use a
# service, never what it costs.
USAGE_HINTS = {
    "TUNE_BASIC": "General tune-up / service when the customer asks for a tune-up or seasonal check.",
    "TUNE_FULL": "Full overhaul; only when explicitly requested or clearly a complete rebuild. Includes a tune-up.",
    "BRAKE_ADJUST": "Squealing, soft, rubbing or poorly-adjusted brakes that do not obviously need parts. Per wheel.",
    "BRAKE_PADS": "Pads worn out / grinding / metal-on-metal / customer asks for new pads. Per wheel.",
    "BRAKE_BLEED": "Hydraulic brakes only: spongy lever, lever pulls to the bar, customer asks for a bleed. Per wheel.",
    "GEAR_ADJUST": "Shifting problems: skipping, not reaching gears, chain falling off when shifting, bent derailleur.",
    "CHAIN_REPLACE": "Worn, rusty or stretched chain; chain skipping under load on a high-mileage chain.",
    "FLAT_FIX": "Flat tire / puncture; new tube. Per wheel.",
    "TIRE_REPLACE": "Replace a tire (labor only; tire supplied separately). Per wheel.",
    "WHEEL_TRUE": "Wheel wobbles, is out of true, or rubs the brake pad once per rotation. Per wheel.",
    "EBIKE_DIAG": "Any e-bike electrical problem: error codes, motor cut-outs, battery range, won't power on.",
    "GENERAL_INSPECT": "Vague symptoms (unknown noise), pre-purchase/used-bike checks, 'whatever it needs'.",
}


@dataclass(frozen=True)
class Service:
    code: str
    name: str
    price_cents: int
    minutes: int
    requires_inspection: bool

    @property
    def per_wheel(self) -> bool:
        return self.code in PER_WHEEL_CODES


@dataclass(frozen=True)
class Shop:
    id: str
    name: str


@dataclass(frozen=True)
class PricedLine:
    code: str
    name: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int
    requires_inspection: bool
    wheel: str


class CatalogError(ValueError):
    pass


def _to_cents(price: float | int | str) -> int:
    # Go through str() so 18.00 never becomes 17.999999 cents.
    cents = (Decimal(str(price)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


class Catalog:
    def __init__(self, raw: dict, version: str):
        self.version = version
        self.currency: str = raw["currency"]
        self.shops: dict[str, Shop] = {s["id"]: Shop(s["id"], s["name"]) for s in raw["shops"]}
        self.services: dict[str, Service] = {}
        for s in raw["services"]:
            if s["code"] in self.services:
                raise CatalogError(f"duplicate service code {s['code']}")
            if s["price"] < 0:
                raise CatalogError(f"negative price for {s['code']}")
            self.services[s["code"]] = Service(
                code=s["code"],
                name=s["name"],
                price_cents=_to_cents(s["price"]),
                minutes=int(s["minutes"]),
                requires_inspection=bool(s["requires_inspection"]),
            )
        named_per_wheel = {c for c, s in self.services.items() if "per wheel" in s.name.lower()}
        if named_per_wheel != set(PER_WHEEL_CODES):
            raise CatalogError(
                f"per-wheel services in catalog {sorted(named_per_wheel)} != PER_WHEEL_CODES {sorted(PER_WHEEL_CODES)}"
            )
        missing_hints = set(self.services) - set(USAGE_HINTS)
        if missing_hints:
            raise CatalogError(f"no usage hint for {sorted(missing_hints)}")

    @classmethod
    def load(cls, path: Path) -> Catalog:
        data = path.read_bytes()
        return cls(json.loads(data), version=hashlib.sha256(data).hexdigest()[:12])

    @property
    def codes(self) -> list[str]:
        return list(self.services)

    def quantity_for(self, code: str, wheel: str, bikes: int) -> int:
        """Units to bill. Per-wheel services: 2 per bike for 'both', else 1 per bike."""
        svc = self.services[code]
        if svc.per_wheel:
            return bikes * (2 if wheel == "both" else 1)
        return bikes

    def price_line(self, code: str, wheel: str, bikes: int) -> PricedLine:
        svc = self.services[code]  # KeyError here is a programming error: codes are validated upstream
        qty = self.quantity_for(code, wheel, bikes)
        return PricedLine(
            code=code,
            name=svc.name,
            quantity=qty,
            unit_price_cents=svc.price_cents,
            line_total_cents=svc.price_cents * qty,
            requires_inspection=svc.requires_inspection,
            wheel=wheel,
        )

    @staticmethod
    def total_cents(lines: list[PricedLine]) -> int:
        return sum(line.line_total_cents for line in lines)


def format_money(cents: int, currency: str = "USD") -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{sign}{symbol}{cents // 100:,}.{cents % 100:02d}"
