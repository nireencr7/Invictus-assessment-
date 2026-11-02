"""The Gemini adapter keeps the provider contract: same request in, same errors and usage out.

The SDK client is replaced by a stub, so these run without Google credentials or network.
"""

import json
from decimal import Decimal

import httpx
import pytest
from google.genai import errors, types

from riverbend.llm.base import LLMRequest, PermanentProviderError, ProviderRefusal, TransientProviderError
from riverbend.llm.gemini_provider import GeminiProvider
from riverbend.services import build_provider
from tests.conftest import load_samples, make_settings, offline_output, post_message


class StubClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, Exception):
            raise item
        return item


def response(text="{}", finish=types.FinishReason.STOP, *, parts=None, blocked=None, usage=None):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts or [types.Part(text=text)]),
                finish_reason=finish,
            )
        ],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=blocked)
        if blocked
        else None,
        usage_metadata=usage
        or types.GenerateContentResponseUsageMetadata(
            prompt_token_count=3000,
            cached_content_token_count=1000,
            candidates_token_count=400,
            thoughts_token_count=60,
        ),
        model_version="gemini-3.8-flash",
        response_id="resp-1",
    )


def provider(*results):
    stub = StubClient(*results)
    p = GeminiProvider(
        model="gemini-3.8-flash", effort="low", timeout_seconds=45, vertex_project="p", client=stub
    )
    return p, stub


REQUEST = LLMRequest(
    system="SYSTEM",
    messages=[
        {"role": "user", "content": "conversation"},
        {"role": "assistant", "content": '{"bad": true}'},
        {"role": "user", "content": "fix it"},
    ],
    json_schema={"type": "object", "properties": {}},
    max_tokens=4000,
)


def test_request_maps_onto_gemini():
    p, stub = provider(response('{"ok": 1}'))
    r = p.complete(REQUEST)
    call = stub.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert [c.role for c in call["contents"]] == ["user", "model", "user"]
    assert call["contents"][1].parts[0].text == '{"bad": true}'
    cfg = call["config"]
    assert cfg.system_instruction == "SYSTEM" and cfg.max_output_tokens == 4000
    assert cfg.response_mime_type == "application/json" and cfg.response_json_schema == REQUEST.json_schema
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert (r.text, r.model, r.stop_reason, r.request_id, r.simulated) == (
        '{"ok": 1}',
        "gemini-3.8-flash",
        "stop",
        "resp-1",
        False,
    )


def test_usage_counts_cached_input_and_thinking_as_output():
    p, _ = provider(response())
    u = p.complete(REQUEST).usage
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.cache_write_tokens) == (
        2000,
        1000,
        460,
        0,
    )


def test_thought_parts_are_not_part_of_the_answer():
    parts = [types.Part(text="thinking...", thought=True), types.Part(text='{"a": 1}')]
    p, _ = provider(response(parts=parts))
    assert p.complete(REQUEST).text == '{"a": 1}'


def test_truncated_output_is_returned_for_validation_to_reject():
    p, _ = provider(response('{"intent": "rep', types.FinishReason.MAX_TOKENS))
    r = p.complete(REQUEST)
    assert r.stop_reason == "max_tokens" and r.text == '{"intent": "rep'


@pytest.mark.parametrize(
    "resp",
    [
        response(finish=types.FinishReason.SAFETY),
        response(finish=types.FinishReason.PROHIBITED_CONTENT),
        response(blocked=types.BlockedReason.JAILBREAK),
        types.GenerateContentResponse(candidates=[]),
    ],
)
def test_blocked_answers_are_refusals(resp):
    p, _ = provider(resp)
    with pytest.raises(ProviderRefusal):
        p.complete(REQUEST)


def api_error(cls, code, status):
    return cls(code, {"error": {"code": code, "message": f"{status} happened", "status": status}})


@pytest.mark.parametrize(
    ("exc", "kind", "billed_unknown"),
    [
        (api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED"), TransientProviderError, False),
        (api_error(errors.ServerError, 503, "UNAVAILABLE"), TransientProviderError, False),
        (api_error(errors.ClientError, 408, "DEADLINE_EXCEEDED"), TransientProviderError, False),
        (httpx.ReadTimeout("slow"), TransientProviderError, True),
        (httpx.ConnectError("no route"), TransientProviderError, False),
        (api_error(errors.ClientError, 400, "INVALID_ARGUMENT"), PermanentProviderError, False),
        (api_error(errors.ClientError, 403, "PERMISSION_DENIED"), PermanentProviderError, False),
        (api_error(errors.ClientError, 404, "NOT_FOUND"), PermanentProviderError, False),
    ],
)
def test_errors_are_classified_once(exc, kind, billed_unknown):
    p, _ = provider(exc)
    with pytest.raises(kind) as caught:
        p.complete(REQUEST)
    assert type(caught.value) is kind and caught.value.billed_unknown is billed_unknown


def test_gemini_needs_a_project_and_a_gemini_model(db_urls):
    with pytest.raises(ValueError, match="VERTEX_PROJECT"):
        build_provider(make_settings(db_urls, llm_provider="gemini", llm_model="gemini-3.8-flash"))
    with pytest.raises(ValueError, match="Gemini LLM_MODEL"):
        build_provider(make_settings(db_urls, llm_provider="gemini", vertex_project="p"))


def test_pipeline_runs_on_gemini_and_books_its_price(make_services, admin):
    """End to end through the real pipeline: a Gemini answer becomes a priced work order."""
    p, _ = provider(response(json.dumps(offline_output("m-1001#1"))))
    s = make_services(provider=p)
    body = next(m["body"] for m in load_samples() if m["message_id"] == "m-1001")
    post_message(s, "rb-downtown", "m-1001", body)
    s.worker().drain()
    assert admin.execute("SELECT count(*) AS n FROM work_orders").fetchone()["n"] == 1
    call = admin.execute("SELECT provider, model, status, cost_usd FROM llm_calls").fetchone()
    # 2000 input x $1.50 + 1000 cached x $0.15 + 460 output x $7.50, per million tokens
    assert call == {
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "status": "ok",
        "cost_usd": Decimal("0.006600"),
    }
