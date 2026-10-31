# Judgement with AI: where it was trusted, where it wasn't, how we knew

AI showed up in two places: **the agent inside the product** (Claude extracting work orders) and **the coding assistant that helped build it** (Claude Code). The same rule governed both: *trust it to propose; verify with something that can't be talked into agreeing.*

## 1. The agent at runtime

| Trusted with | Not trusted with | How that's enforced and checked |
|---|---|---|
| Reading messy, multilingual text: intent, issues, which catalog services fit, which wheel, how many bikes, the customer's timing words | **Money.** No prices in the prompt, no money fields in the schema | An extra `discount_price` field is rejected (`test_validation.py`); the injection sample still quotes $45.00 |
| Suggesting a follow-up question and its wording | **Whether to ask.** Code decides from required fields; at most one question, enforced by the database | `test_policy.py::test_model_asking_unnecessary_question_is_overruled` |
| Flagging suspected prompt injection | **Urgency under injection**, and being the only detector | Code caps urgency and runs its own pattern check (`test_code_detects_injection_even_if_the_model_does_not`) |
| Classifying "next Tuesday" | **Calendar arithmetic** | `dates.resolve` + 21 date tests |
| Choosing services | **Inventing them** | Every service must quote the customer verbatim; ungrounded lines are rejected and repaired |
| — | **Tenancy, cancellations, cross-shop routing** | The model sees one conversation; matching happens in SQL under RLS |

**How we'd know it's drifting in production:** `llm_calls` records every rejected output with the validator's reasons. An alert fires on `invalid_model_output` escalations. The golden-label eval (`make eval-live`) is the regression gate for prompt or model changes.

## 2. The coding assistant during the build

The assistant wrote most of the first drafts. These are the places where its output was checked rather than taken on trust, and what each check found.

| What the assistant produced | Trusted? | How it was checked | What happened |
|---|---|---|---|
| The Anthropic SDK integration | **No.** Its reference material described SDK 1.x | Introspected the installed package (`anthropic 2.3.0`) before writing the adapter | Confirmed `output_config`, the error classes and the `AnthropicVertex` constructor; no `temperature` parameter exists. Adapter written against the real surface. |
| A PL/pgSQL trigger enforcing "estimate = Σ lines" | Not until it ran | Tests and eval run against **real Postgres**, never mocks | The first eval run crashed every work-order write (`record "new" has no field "work_order_id"`): PL/pgSQL resolves both arms of a `CASE`. A mock database would have passed. |
| **The fix for that trigger: an edit to `001_schema.sql`** | Yes, at first — tests and eval went green | A manual check of the running dashboard | **Wrong.** The existing dev database had already applied 001, so it kept the broken trigger. The dashboard showed spend accruing and *zero* work orders. Fresh test databases could never reveal this. Overridden: applied migrations are now immutable, with a checksum guard and a test. **This is the Loom.** |
| A test asserting `"price"` never appears in the schema | Questioned when it failed | Read the failure instead of loosening the assertion | The schema was fine (`price_inquiry` is an intent). The test was naive; rewritten to check property names. A red test isn't automatically right. |
| FastAPI request models defined inside a function | — | The budget test returned 422 | `from __future__ import annotations` made the type a string FastAPI couldn't resolve; moved to module level. |
| **Default model = Claude Opus 5.5** | **Not accepted as settled** | — | The assistant's own guidance said "always default to the most capable model". For a service with a per-shop spend cap, the default model is a cost decision. Left configurable; the decision is gated on `make eval-live` comparing Opus 5.5 with Haiku 5.5 (~40× cheaper). |
| The SLA hand-off rule ("staff after N minutes without a result") | Yes; it was tested | **Using the staff console** | Pressing *Retry AI* on an old escalated message sent it straight back to staff after one transient failure: the deadline was measured from the message's arrival, not from the retry. Every existing test used fresh messages, so none could see it. Fixed with a per-cycle clock (`003_job_sla_clock.sql`) and a regression test. |
| Terraform, Dockerfiles, CI | **Partially** | HCL parsed; nothing applied; no Docker locally | Stated as unverified in [DEPLOYMENT_GCP.md](DEPLOYMENT_GCP.md). CI builds the image on first push. |

The pattern: the assistant was fastest and most reliable at **shape**: boilerplate, DDL, route wiring, Terraform resource blocks. It was least reliable at **state it couldn't see**: an SDK version it hadn't read, how PL/pgSQL evaluates expressions at runtime, a database that already existed. So the checks went where hidden state lives: a real database, the real installed package, and the running app. Building the console counted as one of those checks: the first time the system was *operated*, rather than tested, it surfaced a bug.
