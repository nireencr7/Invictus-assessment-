"""Risk: a wrong price is a broken promise to a customer. Money is integer cents from the
catalog; quantities follow the catalog's per-wheel / per-bike units."""

import json

import pytest

from riverbend.catalog import PER_WHEEL_CODES, Catalog, CatalogError, format_money
from riverbend.config import REPO_ROOT


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load(REPO_ROOT / "data" / "catalog.json")


def test_prices_are_integer_cents_from_the_catalog(catalog):
    assert catalog.services["BRAKE_ADJUST"].price_cents == 1800
    assert catalog.services["TUNE_FULL"].price_cents == 18500
    assert all(isinstance(s.price_cents, int) for s in catalog.services.values())


@pytest.mark.parametrize(
    "code,wheel,bikes,qty,total",
    [
        ("BRAKE_ADJUST", "both", 1, 2, 3600),  # m-1017 both brakes
        ("BRAKE_ADJUST", "rear", 1, 1, 1800),  # m-1005 rear brake
        ("WHEEL_TRUE", "both", 1, 2, 6000),  # m-1031 front and rear
        ("TIRE_REPLACE", "unspecified", 1, 1, 2000),  # m-1021: wheel not said -> assume one, flagged
        ("TUNE_BASIC", "not_applicable", 2, 2, 13000),  # m-1040 two bikes
        ("BRAKE_PADS", "both", 2, 4, 14000),
    ],
)
def test_quantity_and_line_total(catalog, code, wheel, bikes, qty, total):
    line = catalog.price_line(code, wheel, bikes)
    assert (line.quantity, line.line_total_cents) == (qty, total)


def test_total_is_sum_of_lines(catalog):
    lines = [catalog.price_line("BRAKE_PADS", "both", 1), catalog.price_line("BRAKE_BLEED", "both", 1)]
    assert Catalog.total_cents(lines) == 15000  # m-1029: $150.00


def test_float_prices_do_not_drift(tmp_path):
    raw = json.loads((REPO_ROOT / "data" / "catalog.json").read_text())
    raw["services"][0]["price"] = 0.1 + 0.2  # 0.30000000000000004
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    assert Catalog.load(p).services["TUNE_BASIC"].price_cents == 30


def test_catalog_unit_change_fails_loudly(tmp_path):
    raw = json.loads((REPO_ROOT / "data" / "catalog.json").read_text())
    for s in raw["services"]:
        if s["code"] == "WHEEL_TRUE":
            s["name"] = "Wheel truing"  # no longer says per wheel -> our unit table is now wrong
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(CatalogError, match="per-wheel"):
        Catalog.load(p)


def test_per_wheel_table_matches_catalog_names(catalog):
    assert {c for c, s in catalog.services.items() if "per wheel" in s.name.lower()} == PER_WHEEL_CODES


def test_format_money():
    assert format_money(6500) == "$65.00"
    assert format_money(123456) == "$1,234.56"
    assert format_money(5) == "$0.05"
