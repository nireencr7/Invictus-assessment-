"""Risk: model output that is malformed, invented or manipulated becomes a work order.
The brief: "Output that fails validation is never saved." These tests pin what "fails" means."""

import json

import pytest

from riverbend.catalog import Catalog
from riverbend.config import REPO_ROOT
from riverbend.extraction.schema import Extraction, api_json_schema
from riverbend.extraction.validate import Invalid, ValidationContext, is_grounded, parse_and_validate
from tests.conftest import offline_output

CATALOG = Catalog.load(REPO_ROOT / "data" / "catalog.json")
SAMPLES = {
    m["message_id"]: m
    for m in reversed(json.loads((REPO_ROOT / "data" / "samples.json").read_text())["messages"])
}


def ctx(*texts, shop="rb-downtown"):
    return ValidationContext(CATALOG, shop, tuple(texts))


def good(key="m-1024#1"):
    return dict(offline_output(key))


def check(obj, *texts):
    raw = obj if isinstance(obj, str) else json.dumps(obj)
    return parse_and_validate(raw, ctx(*(texts or (SAMPLES["m-1024"]["body"],))))


def errors_of(result) -> str:
    assert isinstance(result, Invalid), "expected the output to be rejected"
    return " | ".join(result.errors)


def test_valid_output_passes():
    assert isinstance(check(good()), Extraction)


def test_extra_price_field_is_rejected():
    # m-1024's injection asks for zero prices; there is nowhere for a price to go.
    assert "discount_price" in errors_of(check({**good(), "discount_price": 0}))


def test_price_inside_a_service_line_is_rejected():
    bad = good()
    bad["services"] = [{**bad["services"][0], "unit_price": 0}]
    assert "unit_price" in errors_of(check(bad))


def test_no_type_coercion():
    assert "bike_count" in errors_of(check({**good(), "bike_count": "1"}))


def test_unknown_service_code_is_rejected():
    bad = good()
    bad["services"] = [{**bad["services"][0], "code": "VIP_FREE_SERVICE"}]
    assert "not a catalog service code" in errors_of(check(bad))


def test_invented_evidence_is_rejected():
    bad = good()
    bad["services"] = [{**bad["services"][0], "evidence": "customer is a VIP member"}]
    assert "not a verbatim quote" in errors_of(check(bad))


def test_grounding_ignores_case_and_punctuation_but_not_words():
    src = ("Hi, my road bike needs a tune-up before a charity ride on October 4.",)
    assert is_grounded("needs a tune up", src)
    assert is_grounded("My road bike ... charity ride", src)
    assert not is_grounded("needs a full overhaul", src)
    assert is_grounded(
        "los frenos de mi bicicleta no frenan bien", ("Hola, los frenos de mi bicicleta no frenan bien",)
    )


def test_brake_bleed_on_rim_brakes_is_rejected():
    body = SAMPLES["m-1017"]["body"]
    bad = dict(offline_output("m-1017#1"))
    bad["services"] = [
        {
            "code": "BRAKE_BLEED",
            "wheel": "both",
            "bikes": 1,
            "evidence": "Both brakes need adjusting",
            "reason": "x",
        }
    ]
    assert "hydraulic" in errors_of(check(bad, body))


def test_per_wheel_service_needs_a_wheel():
    bad = good("m-1017#1")
    bad["services"] = [{**bad["services"][0], "wheel": "not_applicable"}]
    assert "per wheel" in errors_of(check(bad, SAMPLES["m-1017"]["body"]))


def test_tune_up_and_overhaul_together_is_rejected():
    body = SAMPLES["m-1039"]["body"]
    bad = good("m-1039#1")
    bad["services"] = bad["services"] + [
        {
            "code": "TUNE_BASIC",
            "wheel": "not_applicable",
            "bikes": 1,
            "evidence": "Full overhaul",
            "reason": "x",
        }
    ]
    assert "TUNE_FULL already includes" in errors_of(check(bad, body))


def test_truncated_json_is_rejected():
    assert "not valid JSON" in errors_of(check(json.dumps(good())[:150]))


def test_impossible_calendar_date_is_rejected():
    bad = good()
    bad["requested_date"] = {
        **bad["requested_date"],
        "kind": "calendar_date",
        "weekday": "none",
        "month": 9,
        "day": 31,
    }
    assert "not a real calendar date" in errors_of(check(bad))


def test_every_offline_fixture_final_attempt_is_valid_and_every_bad_first_is_not():
    data = json.loads((REPO_ROOT / "data" / "offline_model_outputs.json").read_text())["outputs"]
    replies = json.loads((REPO_ROOT / "data" / "simulated_replies.json").read_text())["replies"]
    bad_firsts = 0
    for key, attempts in data.items():
        ext, turn = key.split("#")
        texts = (SAMPLES[ext]["body"],) + ((replies[ext],) if turn == "2" else ())
        c = ctx(*texts, shop=SAMPLES[ext]["shop_id"])
        raw = lambda a: a if isinstance(a, str) else json.dumps(a)  # noqa: E731
        assert isinstance(parse_and_validate(raw(attempts[-1]), c), Extraction), key
        for a in attempts[:-1]:
            assert isinstance(parse_and_validate(raw(a), c), Invalid), key
            bad_firsts += 1
    assert bad_firsts == 3


def test_api_schema_is_closed_and_has_no_unsupported_keywords():
    schema = api_json_schema(CATALOG.codes, list(CATALOG.shops))
    text = json.dumps(schema)
    for kw in ('"minimum"', '"maximum"', '"maxLength"', '"minLength"'):
        assert kw not in text

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    assert schema["$defs"]["ServiceLine"]["properties"]["code"]["enum"] == CATALOG.codes

    names: set[str] = set()

    def props(node):
        if isinstance(node, dict):
            names.update(node.get("properties", {}))
            for v in node.values():
                props(v)
        elif isinstance(node, list):
            for v in node:
                props(v)

    props(schema)
    money_words = ("price", "total", "cost", "discount", "amount", "estimate")
    assert not [n for n in names if any(w in n.lower() for w in money_words)]  # nowhere to put money


def test_counts_tell_the_model_their_range():
    # minimum/maximum are stripped from the API schema, so a count's range has to be in its description.
    # Without it, "What services do you provide?" (no bike named) came back with bike_count 0: rejected,
    # and a repair call paid for.
    schema = api_json_schema(CATALOG.codes, list(CATALOG.shops))
    bike_count = schema["properties"]["bike_count"]["description"]
    assert "1-5" in bike_count and "1 if" in bike_count
    assert "1-5" in schema["$defs"]["ServiceLine"]["properties"]["bikes"]["description"]


@pytest.mark.parametrize("field", ["price", "total", "estimate", "discount"])
def test_schema_has_no_money_fields(field):
    assert field not in Extraction.model_fields
