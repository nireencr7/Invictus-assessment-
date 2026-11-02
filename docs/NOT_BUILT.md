# What I did not build

Finishing everything was not the goal. These are the cuts, stated plainly.

## Cut on purpose

| Not built | What exists instead | Why it was cut |
|---|---|---|
| **A live-model results table** | `docs/RESULTS.md` comes from the offline stand-in; `make eval-live` regenerates it against Claude | No API key was available while building. The harness, golden labels and scoring are ready; only the run is missing. |
| **Sending email / web replies** | Every customer message is written to `outbound_messages` (an outbox with dedupe keys) and visible via the customer view | Delivery is plumbing (SendGrid / Gmail API + bounce handling), not architecture |
| **The email gateway** | Intake assumes a gateway gives `message_id`, body, sender, channel and a reply token (from `Reply-To: reply+<token>@…`). See "How an email reaches the API" in [DEPLOYMENT_GCP.md](DEPLOYMENT_GCP.md) | A small adapter behind an inbound-email service; no product decisions in it |
| **Password reset by email, MFA, SSO** | Staff sign in with email and password ([D20](DECISIONS.md#d20-staff-sign-in-with-email-and-password-the-session-token-is-used-like-a-shop-token)): personal 12-hour sessions, sign-out, password change, lockout after 5 wrong passwords. An admin creates accounts and sets new passwords through the API; there is no admin screen in the console yet | Reset links need the email sender (not built); MFA and Google sign-in are what Identity Platform / IAP would add. The tenant check is in one place (`staff_shop`), so swapping the identity provider changes one function |
| **Customer identity / CRM** | Customers are identified by email address within one shop (migration `004`), with a full per-customer history in the console. Conversations are still threaded by reply token, and cancellations are still matched by name inside one shop (last 14 days, exactly one match, otherwise staff) | Phone numbers, merging two addresses into one person, and matching cancellations by customer instead of name. The brief's samples carry no sender, so they have no customer |
| **Scheduling** | A resolved requested *window* (earliest/latest, time of day) on each order | No calendar or capacity model in the brief |
| **Parts and inventory** | Parts not in the catalog (tubeless tire m-1021, derailleur hanger m-1028) go to `notes_for_staff` | The catalog is services only |
| **Per-shop catalogs** | One shared catalog, version-stamped on every order (`catalog_version`) | The brief says the shops share one |
| **Localised templates** | Only the follow-up question is in the customer's language; acks and summaries are English (orders are flagged `customer_language_es`) | One Spanish sample; a translation layer is easy to add later |
| **Editing orders in the UI** | Staff can change status (`open` / `cancelled` / `completed`) via the API | — |
| **Data retention** | Message text and rejected model output (`llm_calls.raw_output`) are kept indefinitely, under RLS | Needs a policy decision from Riverbend |
| **Content-level dedupe** | Dedupe is by `message_id` | The same text under a new id from the same sender is a second request — or a resend. That needs sender identity to tell apart. |
| **Prompt-cache tuning, Batches API** | `cache_control` is set; cache hits are not verified. Budget-held backlog could go through Batches at 50% cost. | Needs live traffic to measure |
| **Browser tests for the console** | Unit tests on its data layer; checked by hand in live and snapshot modes, at desktop and phone widths, light and dark | Playwright against `make up` is the next step |
| **Load tests** | Concurrency tests (8 duplicate posts, 30 racing reservations, lease stealing) | Volume is tiny |
| **Identity-Aware Proxy and a custom domain on the live demo** | The demo is deployed with public Cloud Run URLs; staff sign in with email and password (or a shop token) | Needs an OAuth consent screen and a domain; first thing to add before real staff use it |
| **Structured logs / tracing** | Plain logs + an `events` timeline per conversation + log-based alerts | OpenTelemetry is the next step for ops |

## What I'd build next, in order

1. **Run `make eval-live` on Opus 5.5 *and* Haiku 5.5.** Pick the default model from golden-label agreement versus cost per message. This is a business decision about the spend cap, and it should be made with data.
2. **Staff identity via Identity Platform / IAP** (password reset, MFA, Google sign-in) with the shops as a claim, an account admin screen in the console, and per-route roles (staff vs. manager vs. HQ). Email-and-password sign-in and per-person sessions exist already.
3. **Outbox sender** with delivery status; reply-to tokens in the address.
4. **Fair queueing across tenants**: claim round-robin by shop, cap concurrency per shop, rate-limit intake per shop.
5. **Customer identity** (email/phone) for threading, cancellations and history.
6. **Retention + PII redaction** for raw model output; per-shop export/delete.
7. **OpenTelemetry + dashboards**: p50/p95 time-to-work-order, cost per message, escalation rate per shop.

## What breaks first at 100 shops

Assume ~30 messages per shop per day: ~3,000/day, peaks of perhaps 30/minute. **Throughput is not the problem** — Postgres-as-queue handles orders of magnitude more. In the order I expect them to hurt:

1. **Staff onboarding and support.** Hundreds of people with accounts that an admin creates by API call, and no self-service password reset, means the admin becomes a help desk. Needs a managed identity provider (reset, MFA, SSO) and an admin screen. Per-person sessions and lockout already exist.
2. **Noisy neighbours.** The queue is a single global FIFO and there is one provider quota. A spam burst or reply storm at one shop delays all 100. The per-shop caps protect *money*, not *latency*. Fix: fair claim per tenant, intake rate limits, Cloud Armor.
3. **Database connections.** Every Cloud Run instance opens a pool of up to 10. An autoscaled API plus workers will pass Cloud SQL's `max_connections`. Fix: smaller pools, managed connection pooling or PgBouncer, then AlloyDB.
4. **The shop list in the prompt.** Wrong-shop detection lists every shop in the system prompt. At 100 shops that's a long prompt and a model-side lookup. Move shop matching into code (name/alias index), and make catalogs tenant-scoped (orders are already version-stamped).
5. **Per-process circuit breakers.** Each worker learns about an outage separately: cheap at 2 workers, wasteful at 20. Move breaker state to the database.
6. **Name-based cancellation matching.** "Marcus" collides more often as volume grows; it routes to staff more and more. Needs customer identity (see 5 above).
7. **The human queue.** Hundreds of shops' `needs_staff` items with no SLA tracking or paging.
8. **Spend becomes a real line item.** At ~$0.02/message on Opus that's ~$1,800/month across 100 shops. Model choice (Haiku is ~40× cheaper) and caching stop being optional.
