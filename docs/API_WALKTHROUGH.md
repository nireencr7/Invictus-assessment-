# API walkthrough: every request, step by step

This document follows each request in the Postman collection through the code, line by line. Read it with the collection open: every request's description links back to its section here.

- **Collection:** [`postman/Riverbend.postman_collection.json`](../postman/Riverbend.postman_collection.json): 43 requests in 12 folders, in the order a message flows through the system.
- **Environments:** [`Riverbend-local`](../postman/Riverbend-local.postman_environment.json) works with `make dev` as it is. [`Riverbend-GCP`](../postman/Riverbend-GCP.postman_environment.json) points at the live demo; you paste the credentials in yourself.

## Contents

- [Using the collection](#using-the-collection)
- [Part 1. What happens on every request](#part-1-what-happens-on-every-request)
- [Part 2. Each API](#part-2-each-api)
  - 0 Health: [`/healthz`](#healthz) · [`/readyz`](#readyz)
  - 1 Sign in: [`POST /v1/auth/login`](#login) · [`GET /v1/me`](#me) · [sign out](#logout) · [change password](#password)
  - 2 Intake: [`POST /v1/shops/{shop}/inbound-messages`](#inbound)
  - 3 Test tool: [`POST /v1/shops/{shop}/test-messages`](#test-messages)
  - 4 Inbox and conversations: [inbox](#inbox) · [one conversation](#conversation) · [reply](#reply) · [retry AI](#retry)
  - 5 Work orders: [list](#work-orders) · [one](#work-order) · [change status](#work-order-status)
  - 6 Customers: [list](#customers) · [one customer's history](#customer)
  - 7 Observability: [model calls](#llm-calls) · [activity](#activity) · [spend](#spend)
  - 8 Customer side: [`GET /v1/customer/{shop}/{token}`](#customer-view)
  - 9 Admin: [provider](#admin-provider) · [set the cap](#admin-budget) · [staff accounts](#admin-staff)
  - 10 Dev only: [dev routes](#dev)
  - 11 [Security checks](#security)
- [Part 3. After intake: the worker turns a message into an outcome](#part-3-after-intake-the-worker)
- [Part 4. Tables: who writes, who reads](#part-4-tables)
- [Part 5. Status codes](#part-5-status-codes)

---

## Using the collection

1. In Postman: **Import**, then select the three files in `postman/`.
2. Pick an environment in the top-right corner.
   - **Local:** run `make dev` first, which starts the API on `:8000` with a worker inside it. The environment already holds the dev credentials from [`config.py`](../src/riverbend/config.py#L35-L41) and a dev account (`downtown@riverbend.test`, password `riverbend-dev`, created at start-up by [`seed_dev_accounts`](../src/riverbend/staff_auth.py#L277)). They are deliberately obvious and work nowhere else.
   - **GCP:** paste the real credentials into the environment's secret fields. There are no staff accounts until an admin creates them: run **Create a staff account** in folder 9 (or the `curl` in [the staff accounts section](#admin-staff)), then put that email and password into `staffEmail` and `staffPassword`. The tokens are in Secret Manager:
     ```bash
     gcloud secrets versions access latest --secret=riverbend-staff-tokens --project riverbend-wo-ac52e3   # {"<token>": "<shop>"}: copy one token
     gcloud secrets versions access latest --secret=riverbend-admin-token  --project riverbend-wo-ac52e3
     gcloud secrets versions access latest --secret=riverbend-ingest-keys  --project riverbend-wo-ac52e3   # {"<key>": [shops]}: copy the key
     ```
3. Run the folders **top to bottom**. Requests save ids for later ones as collection variables: `shopId`, `conversationId`, `customerId`, `workOrderId`, `gatewayConversationId`. Every request also checks its own status code.
4. **Requests with a built-in pause.** Requests marked *waits* pause for `workerWaitMs` first, because the AI runs in the background (2 s locally, 8 s on GCP).
5. **Folder 8 needs the customer's reply token**, which no staff API returns. Locally, read it from the database:
   ```bash
   psql postgresql://postgres@localhost:5433/riverbend -c "SELECT reply_token FROM conversations WHERE id = '<gatewayConversationId>'"
   ```
   Then paste it into the `replyToken` variable.
6. **Differences on GCP:**
   - Folder 10 answers `404`, because dev routes don't exist in production.
   - **Set a shop's monthly AI cap** changes a live shop's cap.

The original 36 requests were run in Postman against `make dev` in this order, and all returned the expected status. The 7 sign-in and staff-account requests added later were checked against `make dev` with the same calls from a script, not yet from Postman itself.

---

## Part 1. What happens on every request

These steps are shared by all endpoints. The per-endpoint sections below don't repeat them.

### 1.1 The app is built once, at start-up

| Step | Code | What it does |
|---|---|---|
| 1 | [`create_app`](../src/riverbend/api.py#L203) | Builds the FastAPI app. Reads the staff tokens and ingest keys from settings once ([L226-L227](../src/riverbend/api.py#L226-L227)). CORS is off unless `CORS_ORIGINS` is set ([L230](../src/riverbend/api.py#L230)): the console reaches the API through its own proxy, on the same origin. |
| 2 | [`lifespan`](../src/riverbend/api.py#L206) → [`build_services`](../src/riverbend/services.py#L84) | Wires everything together once: the database pool ([`Database`](../src/riverbend/db.py#L28)), the service catalog, the budget, the model provider ([`build_provider`](../src/riverbend/services.py#L20) picks `offline`, `vertex`, `gemini` or `anthropic` from `LLM_PROVIDER`), the circuit breaker and the pipeline. |
| 3 | [L213-L217](../src/riverbend/api.py#L213-L217) | With `EMBEDDED_WORKER=true` (`make dev`), a worker thread runs inside the API process. On GCP the worker is a separate Cloud Run service, and **the API process never calls the model**. |

### 1.2 Request validation happens before your handler runs

Every request body is a pydantic model with `extra="forbid"`, for example [`InboundPayload`](../src/riverbend/intake.py#L27), [`TestMessage`](../src/riverbend/api.py#L88) and [`StaffReply`](../src/riverbend/api.py#L73). Unknown fields, wrong types, blank bodies and bad emails are all rejected with **422** before any handler code runs.

### 1.3 Authentication: three kinds of caller, three dependencies

| Caller | Credential | Checked by | Failure |
|---|---|---|---|
| Email gateway / web-form backend | `X-Ingest-Key` header | inside [`post_inbound`](../src/riverbend/api.py#L308-L312) | 401 bad key; 403 key not allowed for this shop |
| Shop staff (the console) | `Authorization: Bearer <token>`: a **personal session token** from [signing in](#login) with email and password, or a **shop's static token** (for scripts and tools) | [`staff_shop`](../src/riverbend/api.py#L266) dependency | 401 unknown, expired or revoked; **404** for a shop the credential doesn't open |
| Platform admin | `Authorization: Bearer <admin token>` | [`require_admin`](../src/riverbend/api.py#L284) dependency | 401 |
| Customer | the reply token in the URL | [`customer_view`](../src/riverbend/api.py#L825) | 404 |

How `staff_shop` works, step by step:

1. [`staff_credential`](../src/riverbend/api.py#L253) strips `Bearer ` from the header ([`_bearer`](../src/riverbend/api.py#L190)).
2. [`_match`](../src/riverbend/api.py#L194) compares it with each shop's static token using `hmac.compare_digest`, a constant-time comparison, so response timing reveals nothing about how close a guess was. A match opens that one shop.
3. Otherwise [`StaffAuth.account_for`](../src/riverbend/staff_auth.py#L163) looks the token up as a session: it hashes it with SHA-256 and finds a session that isn't revoked or expired, belonging to an account that isn't disabled ([L171](../src/riverbend/staff_auth.py#L171)). That opens the account's shops, read from the account **on every request**, so moving someone to another shop applies at once.
4. Neither: **401**.
5. If the shop in the URL isn't one the credential opens, it returns **404, not 403** ([L268-L269](../src/riverbend/api.py#L268-L269)). The API never confirms that another shop's data exists.
6. If the shop isn't in the catalog, it also returns **404** ([`known_shop`](../src/riverbend/api.py#L241)).
7. It records who is acting, the person's email or "<shop> staff token", for the events staff actions write ([L272](../src/riverbend/api.py#L272)). Then the handler receives the shop as `shop_id`.

### 1.4 Tenant isolation: the database enforces it, not just the code

Every handler that touches customer data opens [`db.tenant_tx(shop_id)`](../src/riverbend/db.py#L42):

1. It checks the shop id's format ([L43](../src/riverbend/db.py#L43)).
2. It takes a pooled connection and starts a transaction.
3. It runs `SELECT set_config('app.current_shop', <shop>, true)` ([L46](../src/riverbend/db.py#L46)). The `true` means the setting lasts **for this transaction only**, so a pooled connection can't leak one shop's setting into the next request.
4. Postgres row-level security does the rest. Every tenant table has the policy `shop_id = current_setting('app.current_shop')` for both reads and writes ([`002_rls.sql`](../migrations/002_rls.sql#L18-L30), and `004` for `customers`). The policy is `FORCE`d, so it applies even to the table owner.
5. The API connects as `riverbend_app`, a role with no superuser, no `BYPASSRLS` and no `DELETE` anywhere ([`ensure_app_role`](../src/riverbend/db.py#L101)).

That is why many queries below have **no `WHERE shop_id = …`**: the database adds it. A query with a bug still can't see another shop's rows. [`system_tx`](../src/riverbend/db.py#L50) is the only other kind of transaction. It is used just for the job queue, and under row-level security it sees zero customer rows.

### 1.5 Writes are idempotent

The system never assumes a request arrives exactly once:
- inbound messages are unique on `(shop_id, external_id)`;
- jobs are unique on `(shop_id, message_id)`;
- every message to a customer has a `dedupe_key`, unique on `(shop_id, dedupe_key)`, written with `ON CONFLICT DO NOTHING`;
- a work order is unique per conversation.

Retries from gateways, double-clicks and worker crashes are all safe.

---

## Part 2. Each API

<a id="healthz"></a>
### `GET /healthz`: liveness

**Who calls it:** Cloud Run and load balancers. **Auth:** none.

1. [`healthz`](../src/riverbend/api.py#L290) returns `{"ok": true}`. It doesn't touch the database, so a slow database never makes the platform restart a healthy process.

<a id="readyz"></a>
### `GET /readyz`: readiness

**Who calls it:** the console's connection check, deploy checks. **Auth:** none.

1. [`readyz`](../src/riverbend/api.py#L294) opens a `system_tx` and runs `SELECT 1` ([L296-L297](../src/riverbend/api.py#L296-L297)). If the database is unreachable, the request fails with a 500.
2. It returns `{"ok": true, "provider": "gemini", "model": "gemini-3.8-flash"}`, the provider this process was built with ([`build_provider`](../src/riverbend/services.py#L20)).

---

<a id="login"></a>
### `POST /v1/auth/login`: sign in with email and password

**Who calls it:** the console's sign-in screen. **Auth:** none; the body is the credential. **Request:** `{"email": "...", "password": "..."}` ([`LoginRequest`](../src/riverbend/api.py#L136)).

1. [`login`](../src/riverbend/api.py#L553) hands both to [`StaffAuth.login`](../src/riverbend/staff_auth.py#L117), which lower-cases the email and locks the account's row (`FOR UPDATE`, [L122](../src/riverbend/staff_auth.py#L122)), so two attempts at once can't both slip past the failure count.
2. **Unknown email:** it still checks the password against a dummy hash, so it takes as long as a wrong password, then answers **401** "Email or password is incorrect." Nobody can find out which emails have accounts by timing the answer.
3. **Locked** (too many failures recently): **429** with a `Retry-After` header, even if the password is right.
4. **Wrong password:** it adds one to `failed_logins`. On the fifth in a row, it locks the account for 15 minutes ([L135](../src/riverbend/staff_auth.py#L135)). The count is in the database, so it holds across every API instance. Same **401** as an unknown email. A **disabled** account gets that same 401.
5. **Right password:** it makes a random token (`rbs_` + 32 random bytes) and stores only its SHA-256, with an expiry 12 hours away ([L149](../src/riverbend/staff_auth.py#L149)). It resets the failure count.
6. It returns the token, its expiry, the account, the shops it opens, and the deployment's features.

Passwords are stored as scrypt hashes ([`hash_password`](../src/riverbend/staff_auth.py#L42)), about 32 MiB and tens of milliseconds each, so a stolen table is slow to crack. Postman saves the token into `{{sessionToken}}`.

<a id="me"></a>
### `GET /v1/me`: who is this credential?

**Who calls it:** the console, to read the deployment's features, and its "Other ways to sign in" token box. **Auth:** any staff credential, checked inline because the URL has no shop yet.

1. [`me`](../src/riverbend/api.py#L535) resolves the credential with [`staff_credential`](../src/riverbend/api.py#L253): a shop's static token or a session. Neither: **401**.
2. **A session:** it returns `account` (id, email, name) and `shops`, every shop the account opens.
3. **A static token:** it returns that one shop as `shops`, plus `shop_id` and `shop_name`, which tools have used since before accounts existed. `account` is `null`.
4. Both include `features.test_messages`. The console shows **Send a test message** only when the deployment allows it ([`test_messages_enabled`](../src/riverbend/config.py#L97)).

Postman saves `shop_id` into `{{shopId}}`.

<a id="logout"></a>
### `POST /v1/auth/logout`: sign out

**Auth:** the session token. [`logout`](../src/riverbend/api.py#L576) sets `revoked_at` on that session ([`StaffAuth.logout`](../src/riverbend/staff_auth.py#L177)), and from then on the token gets **401** everywhere. It always answers 200, so signing out twice, or with a token that has already expired, is harmless. There's no `DELETE`: the app role has none.

<a id="password"></a>
### `POST /v1/auth/password`: change your own password

**Auth:** the session token; a shop's static token has no password, so it gets **401**. **Request:** `{"current_password": "...", "new_password": "..."}`, where the new one must be at least 10 characters (**422** otherwise).

1. [`change_password`](../src/riverbend/api.py#L584) checks the current password ([`StaffAuth.change_password`](../src/riverbend/staff_auth.py#L185)). Wrong: **400**.
2. It stores the new hash, then **revokes every other session of that account**. If someone else had your password, they're signed out. The session you're using stays signed in.

---

<a id="inbound"></a>
### `POST /v1/shops/{shop}/inbound-messages`: a customer message arrives

This is the front door. Every customer email or web-form submission comes through here.

**Who calls it:** the email gateway or the web-form backend, never a browser. **Auth:** `X-Ingest-Key`.

**Request:**
```json
{ "message_id": "<the email's Message-ID>", "channel": "email", "received_at": "2026-11-02T09:15:00",
  "sender": "Sam Rivera <sam.rivera@example.com>", "body": "Hi, my rear brake squeals ...",
  "reply_token": "<only on a reply: the token from our reply-to address>" }
```

**Response:** `202 {"message_id", "conversation_id", "duplicate": false, "detail": "accepted"}`. The response comes back at once, because **the AI has not run yet**.

**Steps in the handler** ([`post_inbound`](../src/riverbend/api.py#L301)):

1. Pydantic validates the body against [`InboundPayload`](../src/riverbend/intake.py#L27): body of 1–10,000 characters and not blank, channel `email` or `web`, no extra fields.
2. It checks the ingest key with `_match` against the configured keys ([L308](../src/riverbend/api.py#L308)). Unknown key: **401**. Each key lists the shops it may post for. This shop isn't on the list: **403** ([L311-L312](../src/riverbend/api.py#L311-L312)).
3. It confirms the shop exists ([`known_shop`](../src/riverbend/api.py#L314)).
4. It calls [`ingest(db, shop_id, payload)`](../src/riverbend/intake.py#L119) and returns its status code and ids ([L315-L322](../src/riverbend/api.py#L315-L322)).

**Steps inside `ingest`.** Everything below runs in **one** tenant transaction ([`_ingest_once`](../src/riverbend/intake.py#L133)), so either all of it happens or none of it does:

1. **Hash the body** with SHA-256 ([L122](../src/riverbend/intake.py#L122)).
2. **Duplicate check** ([`_existing`](../src/riverbend/intake.py#L93)) looks for an inbound message with this `external_id` (the gateway's `message_id`):
   - **Same body:** a gateway retry. It adds 1 to `duplicate_deliveries`, writes a `duplicate_delivery` event and returns **200** with the **original** ids ([L107-L116](../src/riverbend/intake.py#L107-L116)). Nothing new is stored and no second acknowledgement is sent.
   - **Different body:** a gateway bug. It returns **409** ([L99-L106](../src/riverbend/intake.py#L99-L106)).
3. **Load the shop's name and timezone.** A `received_at` without a timezone is taken as shop-local time ([L140-L143](../src/riverbend/intake.py#L140-L143)).
4. **Customer identity** ([`_customer_id`](../src/riverbend/intake.py#L78)):
   - [`parse_sender`](../src/riverbend/intake.py#L67) turns `"Sam Rivera <Sam@Example.com>"` into `("sam@example.com", "Sam Rivera")`.
   - The customer row is upserted on `(shop_id, email)`, which updates `last_seen_at`.
   - With no usable address, there is no customer (the brief's samples have none).
5. **Find or open the conversation:**
   - With a `reply_token`, it looks up that conversation ([L147-L150](../src/riverbend/intake.py#L147-L150)). Row-level security means a token from another shop finds nothing.
   - Otherwise it inserts a new conversation with a fresh, unguessable `reply_token` from `secrets.token_urlsafe(16)` ([L152-L157](../src/riverbend/intake.py#L152-L157)). That token becomes the customer's link and reply-to address.
   - On a reply, the conversation gets a customer id if it didn't have one ([L158-L163](../src/riverbend/intake.py#L158-L163)).
6. **Store the message** ([L165-L179](../src/riverbend/intake.py#L165-L179)) with `ON CONFLICT (shop_id, external_id) DO NOTHING`. If another delivery of the same message committed in the same instant, nothing comes back. The code raises `_LostRace`, rolls back, and answers as a duplicate ([L125-L130](../src/riverbend/intake.py#L125-L130)).
7. **Acknowledge the customer**, new conversations only. It writes an `ack` row to `outbound_messages` (the outbox), worded by [`customer_copy.ack`](../src/riverbend/customer_copy.py#L14), with the dedupe key `ack:<message id>` ([L183-L188](../src/riverbend/intake.py#L183-L188)). The customer is told "we've got it" before the AI runs. If the AI is down, they still got that.
8. **A reply to our question** moves the conversation from `awaiting_customer` back to `processing` ([L189-L193](../src/riverbend/intake.py#L189-L193)).
9. **Queue the AI work** with [`jobqueue.enqueue`](../src/riverbend/jobqueue.py#L38), one `jobs` row per message, unique per message ([L194](../src/riverbend/intake.py#L194)).
10. **Timeline event `received`**, with the channel, whether it's a reply, and `via` when it came from the test tool ([L195-L210](../src/riverbend/intake.py#L195-L210)).
11. **Commit, then return 202.**

Because the message, acknowledgement, job and event commit **together**, a message can never be stored without being queued, or queued without being stored. What happens next, in the background, is [Part 3](#part-3-after-intake-the-worker).

**Postman requests in this folder:** new message (202), same again (200 duplicate), same `message_id` with a different body (409), wrong key (401). Folder 8 then sends a reply carrying the `reply_token`.

---

<a id="test-messages"></a>
### `POST /v1/shops/{shop}/test-messages`: staff send a message as a customer

**Who calls it:** the console's **Send a test message** and **Write as this customer** boxes. **Auth:** staff token; own shop only.

**Request:**
```json
{ "customer_email": "jamie.lee@example.com", "customer_name": "Jamie Lee", "body": "My front tyre keeps going flat...",
  "channel": "email", "reply_to": "<conversation id, only to reply in an existing conversation>" }
```

**Response:** `202 {"message_id", "external_id": "test-…", "conversation_id", "customer_id", "reply": false, "detail": "accepted"}`

**Steps** ([`send_test_message`](../src/riverbend/api.py#L734)):

1. Pydantic validates [`TestMessage`](../src/riverbend/api.py#L88):
   - `customer_email` must be a plain address. `"Jamie <jamie@…>"` is rejected with 422 ([L101-L107](../src/riverbend/api.py#L101-L107)).
   - Quotes and angle brackets are stripped from the name, so it can't break the `From` header built later ([L109-L113](../src/riverbend/api.py#L109-L113)).
2. `staff_shop` authenticates the token, as in [Part 1.3](#13-authentication-three-credentials-three-dependencies).
3. If the deployment hasn't switched the tool on, it returns **404** ([L739-L740](../src/riverbend/api.py#L739-L740)). The tool is on in dev mode, and on GCP only with `TEST_MESSAGES=true`; the demo has it on.
4. **Replies:** it looks up the conversation's `reply_token` by id, inside this shop's tenant transaction ([L741-L749](../src/riverbend/api.py#L741-L749)). Staff never see the token; the server fills it in. Another shop's conversation id isn't found: **404**.
5. It builds the same [`InboundPayload`](../src/riverbend/intake.py#L27) a gateway would send ([L750-L759](../src/riverbend/api.py#L750-L759)):
   - `message_id` is `test-<random>`;
   - `received_at` is now;
   - `sender` is `"Name" <email>`.
6. It calls the **same** [`ingest`](../src/riverbend/intake.py#L119) as the email gateway, with `via="staff test"` ([L760](../src/riverbend/api.py#L760)). Every step in the previous section happens, and the timeline marks the message as a test.
7. It reads back the conversation's `customer_id` so the console can open the customer ([L761-L764](../src/riverbend/api.py#L761-L764)), then returns 202.

The three Postman requests in folder 3 tell one story:
1. **The first message has no day.** The AI asks one follow-up question.
2. **"Saturday morning works for me."** The AI now has a problem and a day, so it creates a work order.
3. **"Is the price negotiable?"** The conversation already has an order, so the pipeline skips the model and hands it to staff with a "passed to the team" note ([pipeline.py L110](../src/riverbend/pipeline.py#L110)).

---

<a id="inbox"></a>
### `GET /v1/shops/{shop}/inbox`: what isn't a work order yet

**Who calls it:** the console's **Inbox**. **Auth:** staff token. **Postman:** *waits* first.

1. [`inbox`](../src/riverbend/api.py#L395) opens a tenant transaction.
2. One query ([L400-L408](../src/riverbend/api.py#L400-L408)) selects conversations in `processing`, `awaiting_customer`, `needs_staff` or `answered`. A `JOIN LATERAL` attaches each conversation's **latest** inbound message, which supplies its body, status and outcome. Newest activity comes first.
3. It adds `needs_action` to each row ([L416](../src/riverbend/api.py#L416)): true when a person must act, meaning the conversation is `needs_staff` or the message is held at the budget cap. That's the console's red badge.
4. It returns `{shop_id, count, items[]}`.

---

<a id="conversation"></a>
### `GET /v1/shops/{shop}/conversations/{id}`: everything about one conversation

**Who calls it:** the console's conversation panel. **Auth:** staff token.

1. [`conversation`](../src/riverbend/api.py#L422) loads the conversation by id ([L425-L429](../src/riverbend/api.py#L425-L429)). It isn't found, or belongs to another shop (row-level security hides it): **404**.
2. Four more queries run in the same transaction:
   - **inbound:** the customer's messages ([L430-L434](../src/riverbend/api.py#L430-L434));
   - **outbound:** everything we sent: ack, follow-up, summary, handoff, staff reply… ([L435-L439](../src/riverbend/api.py#L435-L439));
   - **events:** the timeline ([L440-L443](../src/riverbend/api.py#L440-L443));
   - **model calls**, with tokens, cost, latency and error ([L444-L449](../src/riverbend/api.py#L444-L449)).
3. It **drops `reply_token`** from the response ([L450](../src/riverbend/api.py#L450)). The token is the customer's key, and staff never need it.
4. It turns money into strings, so no float rounding creeps in ([L456-L458](../src/riverbend/api.py#L456-L458)).

---

<a id="reply"></a>
### `POST /v1/shops/{shop}/conversations/{id}/reply`: staff answer the customer

**Who calls it:** the conversation panel's **Reply to the customer** box. **Auth:** staff token.

**Request:** `{"body": "Hi Jamie, ...", "client_id": "<one id per reply, from the browser>"}`
**Response:** `{"sent": true, "duplicate": false, "state": "work_order_created", "resolved": true}`

**Steps** ([`staff_reply`](../src/riverbend/api.py#L483)):

1. Pydantic validates [`StaffReply`](../src/riverbend/api.py#L73): body of 1–5,000 characters, trimmed, and not blank. `client_id` is 8–64 characters from `A-Z a-z 0-9 -`.
2. It loads the conversation **with a row lock** (`FOR UPDATE OF c`), plus whether it has a work order ([L495-L500](../src/riverbend/api.py#L495-L500)). The lock means two staff replying at the same moment can't both hand the conversation back. Not found: **404**.
3. It writes the reply to the outbox as kind `staff_reply`, with the dedupe key `staff:<client_id>` and `ON CONFLICT DO NOTHING` ([L503-L508](../src/riverbend/api.py#L503-L508)). If that key already exists, this is a double-click or a retried request. It returns `duplicate: true` and changes nothing ([L509-L510](../src/riverbend/api.py#L509-L510)).
4. **If the conversation was waiting for a person** (`needs_staff`), the reply resolves it ([L511-L521](../src/riverbend/api.py#L511-L521)):
   - the state goes back to `work_order_created` if there's an order, otherwise to `answered` with the reason `staff_replied`;
   - the waiting inbound messages are marked `processed`;
   - the conversation leaves **Needs a person**.
5. Otherwise it only bumps `updated_at` ([L522-L523](../src/riverbend/api.py#L522-L523)). Staff can write at any time.
6. It writes a timeline event `staff_replied {resolved, state}` ([L524-L531](../src/riverbend/api.py#L524-L531)).

Like every message to a customer, the reply waits in the outbox; sending it out (SMTP or SMS) is not built yet. The customer sees it at once on their status page ([customer view](#customer-view)).

---

<a id="retry"></a>
### `POST /v1/shops/{shop}/conversations/{id}/retry`: try the AI again

**Who calls it:** the conversation panel's **Retry AI** button, after an outage or a fix. **Auth:** staff token.

1. [`retry_ai`](../src/riverbend/api.py#L461) finds the conversation's latest inbound message ([L465-L471](../src/riverbend/api.py#L465-L471)). None: **404**.
2. [`jobqueue.requeue`](../src/riverbend/jobqueue.py#L111) puts that message's job back in the queue, **only if** it is `dead` (the AI gave up) or `parked` (held at the cap). It also resets the attempts and **restarts the processing clock** (`sla_started_at`, [decision D16](DECISIONS.md#d16-the-processing-deadline-restarts-when-processing-restarts)). Anything else: **409**, nothing to retry ([L472-L473](../src/riverbend/api.py#L472-L473)). That's what Postman shows on a healthy conversation.
3. It sets the message back to `queued` and the conversation back to `processing` ([L474-L480](../src/riverbend/api.py#L474-L480)). The worker picks the job up within about half a second ([Part 3](#part-3-after-intake-the-worker)).

---

<a id="work-orders"></a>
### `GET /v1/shops/{shop}/work-orders?status=open|all`: the shop's work orders

**Who calls it:** the console's **Work orders** page. **Auth:** staff token.

1. `status` must be `open` or `all`; anything else is rejected with 422 ([L327](../src/riverbend/api.py#L327)). `open` means open or cancel-requested. `all` adds cancelled and completed ([L331-L335](../src/riverbend/api.py#L331-L335)).
2. It runs two queries: the orders, oldest first, then all their lines in one go with `= ANY(ids)`, which avoids a query per order ([L336-L343](../src/riverbend/api.py#L336-L343)).
3. It groups the lines by order and shapes each order with [`_work_order_json`](../src/riverbend/api.py#L979):
   - the estimate is **recomputed from the lines**, and a database trigger also guarantees the stored total equals the sum of the lines;
   - the requested window gets a human label, such as "Thu 05 Nov 2026, afternoon", from [`DateWindow`](../src/riverbend/extraction/dates.py#L32);
   - money is formatted from integer cents.
4. It adds `open_estimate_total`, the sum of open orders ([L348](../src/riverbend/api.py#L348)).

Postman saves the test customer's order id into `{{workOrderId}}`.

<a id="work-order"></a>
### `GET /v1/shops/{shop}/work-orders/{id}`: one work order

1. [`get_work_order`](../src/riverbend/api.py#L356) loads the order by id; another shop's order is invisible, so **404** ([L359-L361](../src/riverbend/api.py#L359-L361)). Then it loads its lines.
2. It shapes the order with [`_work_order_json`](../src/riverbend/api.py#L979), the same as the list.

<a id="work-order-status"></a>
### `POST /v1/shops/{shop}/work-orders/{id}/status`: staff change the status

**Request:** `{"status": "completed"}`, which can be `open`, `completed` or `cancelled`. Anything else is rejected with 422 ([`StatusChange`](../src/riverbend/api.py#L61)).

1. [`set_work_order_status`](../src/riverbend/api.py#L367) runs `UPDATE … RETURNING id, conversation_id` in one statement ([L376-L382](../src/riverbend/api.py#L376-L382)). No row means not found: **404**.
2. It writes a `work_order_status` event linked to the conversation, so it shows in that customer's history ([L383-L392](../src/riverbend/api.py#L383-L392)).

---

<a id="customers"></a>
### `GET /v1/shops/{shop}/customers`: everyone who wrote from an email address

1. [`list_customers`](../src/riverbend/api.py#L599) runs [`_CUSTOMER_SUMMARY`](../src/riverbend/api.py#L946): customers left-joined to their conversations and work orders, aggregated per customer. It counts conversations, awaiting reply, needs a person and open orders, sums the open estimate, and finds the last activity. It groups by the primary key `(shop_id, id)`, newest activity first ([L603-L605](../src/riverbend/api.py#L603-L605)).
2. It counts conversations with **no** customer (no email address) separately ([L606-L608](../src/riverbend/api.py#L606-L608)).
3. [`_customer_json`](../src/riverbend/api.py#L963) chooses the display name: the name in their `From` header, else the name the AI read in their message.

<a id="customer"></a>
### `GET /v1/shops/{shop}/customers/{id}`: one customer's whole history

1. [`customer_history`](../src/riverbend/api.py#L616) loads the customer summary ([L621-L625](../src/riverbend/api.py#L621-L625)). Not found: **404**.
2. It loads their conversations, each with its work order and first message ([L626-L635](../src/riverbend/api.py#L626-L635)).
3. It loads every inbound message, outbound message and event for those conversations ([L638-L655](../src/riverbend/api.py#L638-L655)). Older status-change events stored without a conversation id are matched through `detail.work_order_id`.
4. It totals the model calls and cost for this customer ([L656-L660](../src/riverbend/api.py#L656-L660)).
5. **It merges everything into one timeline** ([L665-L705](../src/riverbend/api.py#L665-L705)), sorted by time. Rows written in the same transaction share a timestamp, so ties go in this order: the customer's message, then what the system did, then what we sent back.

---

<a id="llm-calls"></a>
### `GET /v1/shops/{shop}/llm-calls?limit=N`: every model call

1. [`llm_calls`](../src/riverbend/api.py#L777) clamps `limit` to between 1 and 500 ([L787](../src/riverbend/api.py#L787)).
2. It joins each call to its message, so you can see which customer message caused it.
3. Each row comes from the budget ledger ([Part 3, step 6](#part-3-after-intake-the-worker)). It records what was **reserved** before the call and what was actually **charged** after it, plus tokens, latency, status (`ok`, `invalid_output`, `transient_error`, `refused`…) and the error text.

<a id="activity"></a>
### `GET /v1/shops/{shop}/activity?limit=N`: the shop's timeline

1. [`activity`](../src/riverbend/api.py#L802) returns the latest events, newest first, joined to the message's external id. The limit is clamped to between 1 and 500.
2. Event types include `received`, `llm_call`, `validation_failed`, `follow_up_asked`, `work_order_created`, `escalated`, `held_budget`, `staff_replied`, `work_order_status` and `budget_cap_changed`.

<a id="spend"></a>
### `GET /v1/shops/{shop}/spend`: this month's AI budget

1. [`spend`](../src/riverbend/api.py#L820) calls [`Budget.status`](../src/riverbend/budget.py#L186).
2. That creates this month's `shop_spend` row if it's missing ([L189](../src/riverbend/budget.py#L189)). It reads `cap_usd`, `spent_usd` and `reserved_usd` (money held for calls in flight), plus a count of calls and tokens.
3. It returns the remaining money and a state: `ok`, `warning` (past 80% of the cap) or `exhausted`.

---

<a id="customer-view"></a>
### `GET /v1/customer/{shop}/{reply_token}`: what the customer sees

**Who calls it:** the customer's status link. **Auth:** none; the unguessable token *is* the key. **Postman:** folder 8, and *waits* first.

1. [`customer_view`](../src/riverbend/api.py#L825) checks the shop exists ([L828](../src/riverbend/api.py#L828)).
2. It finds the conversation by `reply_token`, inside that shop's tenant transaction ([L829-L834](../src/riverbend/api.py#L829-L834)). A wrong token, or a token used under the wrong shop, returns **404**.
3. It loads **only the outbound messages**, the things we sent them ([L835-L839](../src/riverbend/api.py#L835-L839)). Internal notes, model calls, review flags and staff-only fields are never included.
4. It maps the conversation state to a plain sentence ([L840-L849](../src/riverbend/api.py#L840-L849)), for example `needs_staff` becomes "A team member is reviewing your request personally."

<a id="customer-reply"></a>
**The second request in folder 8**, "Customer replies by email", is the same [`inbound`](#inbound) endpoint with `reply_token` set. In `ingest`, the token finds the existing conversation (step 5), so the reply joins the same thread. This conversation already has a work order, so the worker hands it to staff without asking the model.

---

<a id="admin-provider"></a>
### `GET /v1/admin/provider`: model provider and circuit breaker

**Auth:** admin token, through [`require_admin`](../src/riverbend/api.py#L284), a constant-time comparison. A staff token gets **401**.

1. [`provider_status`](../src/riverbend/api.py#L868) returns the provider name, the model, and the breaker state for this process: `closed`, `open` or `half_open` ([`CircuitBreaker.state`](../src/riverbend/llm/resilience.py#L33)).
2. On GCP this answers from the **API** process. The worker that actually calls the model is a separate service with its own breaker.

<a id="admin-budget"></a>
### `PUT /v1/admin/shops/{shop}/budget`: set a shop's monthly AI cap

**Request:** `{"monthly_cap_usd": "5.00"}`, between 0 and 1000 ([`CapChange`](../src/riverbend/api.py#L65)).

1. [`set_budget`](../src/riverbend/api.py#L853) checks the shop exists.
2. [`Budget.set_cap`](../src/riverbend/budget.py#L211) updates the shop's cap and this month's `shop_spend` row. It clears the "exhausted" or "warning" flags if the new cap is above current spend, and records a `budget_cap_changed` event.
3. [`jobqueue.unpark(shop, "budget")`](../src/riverbend/jobqueue.py#L99) releases every job held at the cap back to `queued`, and restarts their processing clock ([L857](../src/riverbend/api.py#L857)).
4. If any were released, it sets those messages back to `queued` and clears the conversations' `held_budget` reason ([L858-L865](../src/riverbend/api.py#L858-L865)). The worker processes them next.
5. It returns the new budget status plus `released_held_messages`.

<a id="admin-staff"></a>
### `/v1/admin/staff-accounts`: who can sign in

**Auth:** admin token. A staff session, even a manager's, gets **401**: managing accounts isn't a staff permission. There is no self sign-up.

| Request | Code | What it does |
|---|---|---|
| `GET /v1/admin/staff-accounts` | [`list_staff_accounts`](../src/riverbend/api.py#L873) | Every account: email, name, shops, disabled, locked, open sessions, last sign-in. Never a password or a hash. |
| `POST /v1/admin/staff-accounts` | [`create_staff_account`](../src/riverbend/api.py#L877) | `{"email", "name", "password", "shops": [...]}` → **201**. Unknown shop or empty list: **422**. Password under 10 characters: **422**. Email already taken: **409**. |
| `PATCH /v1/admin/staff-accounts/{id}` | [`update_staff_account`](../src/riverbend/api.py#L886) | Changes only the fields given: `name`, `shops`, `password`, `disabled`. **A new password or `disabled: true` signs the person out everywhere** ([`StaffAuth.update_account`](../src/riverbend/staff_auth.py#L232)). A new password also clears a lock. New `shops` apply to open sessions at once. |

The tables aren't tenant tables: signing in must find the account by email before anyone knows the shop, and they hold no customer data ([`006_staff_accounts.sql`](../migrations/006_staff_accounts.sql)). Every shop-scoped request still runs under row-level security for the one shop in its URL.

Creating the first account on GCP (the admin token is in Secret Manager):

```bash
curl -X POST "$API/v1/admin/staff-accounts" -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"email": "sam@riverbendcycles.example", "name": "Sam", "password": "<at least 10 characters>", "shops": ["rb-downtown", "rb-northside", "rb-lakeshore"]}'
```

---

<a id="dev"></a>
### Dev-only routes (`make dev`; 404 on GCP)

These are registered only when `DEV_MODE=true` ([L898](../src/riverbend/api.py#L898)). In production the routes don't exist at all.

| Route | Code | What it does |
|---|---|---|
| `GET /` | [L900](../src/riverbend/api.py#L900) | The local dashboard page. |
| `GET /v1/dev/config` | [`dev_config`](../src/riverbend/api.py#L904) | Shops, dev staff tokens, the dev accounts and their password, the ingest key and the admin token, so the local dashboard can act as staff and as the gateway. Provider and breaker state. |
| `GET /v1/dev/samples` | [`dev_samples`](../src/riverbend/api.py#L920) | The 40 messages from the brief (`data/samples.json`). |
| `PUT /v1/dev/faults` | [`dev_faults`](../src/riverbend/api.py#L924) | Breaks the model provider on purpose. `{"spec": "outage"}`, `"flaky:0.5"`, `"slow:2"` or `"garbage:0.2"` wraps the real provider in [`FaultInjectingProvider`](../src/riverbend/llm/resilience.py#L108). `{"spec": ""}` restores it and closes the breaker ([L928-L933](../src/riverbend/api.py#L928-L933)). Use it to watch retries, the breaker and the hand-off to staff. |

---

<a id="security"></a>
### Folder 11: security checks (each must fail)

| Request | Why it fails | Code |
|---|---|---|
| Sign out, then the session is refused → **401** | the session is revoked; the request checks the `sessionToken` again afterwards | [`StaffAuth.logout`](../src/riverbend/staff_auth.py#L177) |
| No token → **401** | `staff_credential` finds neither a static token nor a session | [api.py L253-L264](../src/riverbend/api.py#L253-L264) |
| Another shop with this token → **404** | the token is bound to one shop; 404 doesn't reveal that the other shop's data exists | [L268-L269](../src/riverbend/api.py#L268-L269) |
| Admin route with a staff token → **401** | `require_admin` compares against the admin token only | [L284-L287](../src/riverbend/api.py#L284-L287) |
| Invalid input → **422** | `TestMessage` rejects `Name <email>` before any handler code runs | [L101-L107](../src/riverbend/api.py#L101-L107) |

Even if the 404 check had a bug, row-level security ([Part 1.4](#14-tenant-isolation-the-database-enforces-it-not-just-the-code)) would still return no rows for another shop.

---

<a id="part-3-after-intake-the-worker"></a>
## Part 3. After intake: the worker turns a message into an outcome

`POST inbound-messages` and `POST test-messages` stop after queuing a job. This is the other half: it runs in the worker (a separate Cloud Run service on GCP, a thread inside the API in `make dev`), usually within a second of the message arriving.

### 3.1 The worker loop

1. [`Worker.run_forever`](../src/riverbend/worker.py#L74) loops:
   - every 30 seconds it runs housekeeping;
   - otherwise it calls `run_once` and sleeps `poll_seconds` (0.5 s) whenever the queue is empty.
2. [`run_once`](../src/riverbend/worker.py#L43) → [`jobqueue.claim`](../src/riverbend/jobqueue.py#L45) atomically takes the oldest runnable job:
   - `FOR UPDATE SKIP LOCKED` means many workers never take the same job;
   - the job gets a **lease** (`locked_until`, 120 s) and a fresh `lease_token`;
   - a job whose worker died is re-claimed once its lease expires.
3. It calls [`pipeline.process(job)`](../src/riverbend/pipeline.py#L95). An unexpected crash, meaning a bug rather than a provider failure, is rescheduled with backoff. After `max_attempts` (6) the job is marked `dead` ([worker.py L50-L63](../src/riverbend/worker.py#L50-L63)).
4. **Housekeeping** ([L69](../src/riverbend/worker.py#L69)) books budget reservations orphaned by a crashed worker at their worst case ([`sweep_orphans`](../src/riverbend/budget.py#L164)).

### 3.2 Processing one message: `Pipeline._process`

[`pipeline.py L104`](../src/riverbend/pipeline.py#L104). The model call happens **outside** any database transaction, because it can take seconds. All the writes that matter commit together at the end.

1. **Load** ([`_load`](../src/riverbend/pipeline.py#L209)), in one tenant transaction:
   - the message, conversation and shop;
   - all customer messages so far, plus any follow-up question we asked, merged in time order into `turns`.
   - If the message was already processed, a re-delivery after a crash, it just completes the job ([L215-L217](../src/riverbend/pipeline.py#L215-L217)).
2. **Staff-owned conversations skip the model** ([L110-L117](../src/riverbend/pipeline.py#L110-L117)). If the conversation is already `work_order_created`, `cancellation_requested`, `redirected`, `needs_staff` or `closed`, and this is a later message, it goes to a person. Example: "Is the price negotiable?". So the model can never renegotiate a confirmed order.
3. **Processing deadline** ([L120-L128](../src/riverbend/pipeline.py#L120-L128)). If this cycle has been failing for longer than `processing_sla_seconds` (15 min), it stops retrying and hands the message to staff with the reason `ai_unavailable`.
4. **Circuit breaker** ([L130-L133](../src/riverbend/pipeline.py#L130-L133)). After repeated provider failures the breaker opens ([`allow`](../src/riverbend/llm/resilience.py#L44)). Jobs are pushed back without using up an attempt, instead of hammering a provider that's down.
5. **Build the request** ([L135-L153](../src/riverbend/pipeline.py#L135-L153)):
   - **System prompt** ([`system_prompt`](../src/riverbend/extraction/prompt.py#L19)): built once, identical on every call so it can be cached. It holds the shops, the service catalog and 11 rules, such as "never output prices" and "evidence must be a verbatim quote".
   - **User prompt** ([`user_prompt`](../src/riverbend/extraction/prompt.py#L83)): the conversation, with customer text **escaped and fenced** in `<customer_message>` tags ([`_escape`](../src/riverbend/extraction/prompt.py#L72)). A message can't close our tags or pose as an instruction.
   - **JSON schema** ([`api_json_schema`](../src/riverbend/extraction/schema.py#L184)): generated from the pydantic model [`Extraction`](../src/riverbend/extraction/schema.py#L127), with the catalog codes and shop ids as enums.
6. **Reserve budget, then call the model** ([`_call_model`](../src/riverbend/pipeline.py#L245)):
   1. **Worst-case cost:** [`worst_case_cost`](../src/riverbend/llm/pricing.py#L53) prices every input token at the dearest rate, plus `max_tokens` of output.
   2. **Reserve:** [`Budget.reserve`](../src/riverbend/budget.py#L59) runs one conditional `UPDATE … WHERE spent + reserved + amount <= cap`. It is atomic, so concurrent workers can't jointly overshoot the cap. It also inserts an `llm_calls` row with status `reserved`.
   3. **No room under the cap:** the message is **held** ([`_hold_for_budget`](../src/riverbend/pipeline.py#L346)). The job is parked, staff see it in the inbox, and raising the cap releases it.
   4. **Call the provider:** `self.provider.complete(request)`, which is [`GeminiProvider.complete`](../src/riverbend/llm/gemini_provider.py#L90) on the live site, [`AnthropicProvider.complete`](../src/riverbend/llm/anthropic_provider.py#L73) for Claude, or the [offline stand-in](../src/riverbend/llm/offline.py#L36). Each adapter turns provider errors into one of three kinds.
   5. **Transient error** (timeout, 429, 5xx): settle the reservation, record a breaker failure, then retry with backoff, which is exponential with jitter ([`backoff_seconds`](../src/riverbend/jobqueue.py#L122)). After `max_attempts` the message goes to staff ([`_retry_or_escalate`](../src/riverbend/pipeline.py#L331)). If the provider may have billed us, the full reservation is booked.
   6. **Refusal:** goes to staff (`model_refused`). **Permanent error** (bad configuration): goes to staff (`ai_configuration_error`).
   7. **Success:** [`cost_of`](../src/riverbend/llm/pricing.py#L42) computes the real cost from token usage, and [`Budget.settle`](../src/riverbend/budget.py#L119) books it: it releases the reservation and adds the actual cost.
7. **Validate** ([`parse_and_validate`](../src/riverbend/extraction/validate.py#L134)):
   - **Parse and shape:** the output must be JSON, and pydantic checks it against `Extraction`.
   - **Meaning** ([`semantic_errors`](../src/riverbend/extraction/validate.py#L72)):
     - codes must exist in the catalog;
     - per-wheel rules must make sense;
     - no brake bleed on rim brakes;
     - every `evidence` must be a **verbatim quote** from the customer ([`is_grounded`](../src/riverbend/extraction/validate.py#L38));
     - dates must be real dates;
     - a cancellation has no services.
   - **If it fails:** one **repair call**. The model gets its own answer plus the validator's errors and is asked to fix only those ([L158-L184](../src/riverbend/pipeline.py#L158-L184), [`repair_prompt`](../src/riverbend/extraction/prompt.py#L100)). If it fails twice, the message goes to staff (`invalid_model_output`). Rejected outputs are kept in `llm_calls.raw_output` for debugging.
8. **Code decides, not the model** ([`policy.decide`](../src/riverbend/policy.py#L114)). The decisions, in order:
   - `cancellation`: request a cancellation.
   - `price_inquiry`: send a quote, with prices from the catalog.
   - `other`: goes to staff.
   - **Wrong shop:** the message names another Riverbend shop. Ask once which shop the customer means, then redirect or continue; the message is never moved to the other shop automatically.
   - **Missing problem or day:** ask **one** follow-up question. The model's wording is used only when its reason agrees with the code's ([L156-L163](../src/riverbend/policy.py#L156-L163)).
   - **Otherwise, a work order:**
     - each line is priced by [`catalog.price_line`](../src/riverbend/catalog.py#L124), with per-wheel and per-bike quantities;
     - "Thursday afternoon" becomes a calendar window, resolved in the shop's timezone by [`dates.resolve`](../src/riverbend/extraction/dates.py#L64);
     - review flags are added, such as a provisional estimate, a possible prompt injection, or a non-English customer.
9. **Apply** ([`_apply`](../src/riverbend/pipeline.py#L404)), in **one** tenant transaction together with completing the job:

   | Decision | Writes | Message to the customer (outbox) |
   |---|---|---|
   | Create work order ([`_create_work_order`](../src/riverbend/pipeline.py#L510)) | `work_orders` (unique per conversation, so it's idempotent) + `work_order_lines`; conversation → `work_order_created` | `summary`: lines, estimate, requested day |
   | Ask a follow-up | conversation → `awaiting_customer`, `follow_ups_asked + 1` | `follow_up`: the question, plus "just reply to this message" |
   | Send a quote | conversation → `answered` | `price_quote` |
   | Request cancellation ([`_request_cancellation`](../src/riverbend/pipeline.py#L596)) | matches **one** open order by customer name, in this shop only, from the last 14 days → `cancel_requested`; no single match → goes to staff | `cancellation_ack` |
   | Redirect | conversation → `redirected` | `redirect`, with the other shop's contact |
   | Escalate ([`_mark_needs_staff`](../src/riverbend/pipeline.py#L372)) | conversation → `needs_staff`, with the reason | `handoff`: "a member of staff will reply personally", once per hand-off |

   - Every outbox row goes through [`_send`](../src/riverbend/pipeline.py#L641) with a dedupe key, so re-processing never sends twice.
   - Then [`jobqueue.complete`](../src/riverbend/jobqueue.py#L64) marks the job done **only if this worker still holds the lease**. If another worker took the job over meanwhile, `LeaseLost` rolls the whole transaction back. Its results count, not ours ([L503-L507](../src/riverbend/pipeline.py#L503-L507)).

The customer sees the result on their status page ([customer view](#customer-view)), and staff see it in the inbox, the work orders and the customer's history.

---

<a id="part-4-tables"></a>
## Part 4. Tables: who writes, who reads

Schema: [`migrations/001_schema.sql`](../migrations/001_schema.sql), plus `002` (row-level security), `003` (processing clock), `004` (customers), `005` (staff replies) and `006` (staff accounts). All tables except `shops`, `jobs`, `staff_accounts` and `staff_sessions` are tenant tables under row-level security.

| Table | Written by | Read by |
|---|---|---|
| `shops` | migrations (seed), admin cap | everything (name, timezone, cap) |
| `customers` | intake (`_customer_id`) | customers list / history |
| `conversations` | intake (new / reply), pipeline (state), staff reply, retry | inbox, conversation, customers, customer view |
| `inbound_messages` | intake | pipeline (`_load`), inbox, conversation, history |
| `outbound_messages` (outbox) | intake (`ack`), pipeline (`summary`, `follow_up`, `handoff`, …), staff reply | conversation, history, **customer view** |
| `jobs` | intake (`enqueue`), worker (claim / complete / reschedule / park), retry, admin cap (unpark) | worker |
| `work_orders`, `work_order_lines` | pipeline (`_create_work_order`, cancellation), status change | work-order routes, history |
| `llm_calls` | budget (`reserve` → `settle`) | llm-calls, conversation, history |
| `shop_spend` | budget (`reserve`, `settle`, `set_cap`) | spend, admin cap |
| `events` | intake, pipeline, staff reply, status change (with who did it), budget | activity, conversation, history |
| `staff_accounts` | admin staff-account routes, sign-in (failure count, lock), password change; dev accounts at start-up | sign-in, every session check |
| `staff_sessions` | sign-in (new session), sign-out / password change / disable (revoke) | every request with a session token |

The app role can't `DELETE` anything, and it can only `INSERT` into `outbound_messages`, `work_order_lines` and `events`, which are append-only ([`ensure_app_role`](../src/riverbend/db.py#L101)).

---

<a id="part-5-status-codes"></a>
## Part 5. Status codes

| Code | Meaning here | Where it comes from |
|---|---|---|
| 200 | OK; also an **ignored duplicate** delivery | most routes; `_existing` in intake |
| 201 | Staff account created | `create_staff_account` |
| 202 | Accepted: stored and queued, the AI runs later | inbound-messages, test-messages |
| 400 | Wrong current password when changing it | `change_password` |
| 401 | Missing, invalid, expired or revoked credential; wrong email or password | `post_inbound`, `staff_shop`, `me`, `login`, `require_admin` |
| 403 | The ingest key is valid but may not post for this shop | `post_inbound` |
| 404 | Not found, **or another shop's**, or a dev route in production, or the test tool switched off | `staff_shop`, every lookup, `send_test_message` |
| 409 | `message_id` reused with a different body; nothing to retry; staff email already taken | `_existing`; `retry_ai`; `create_staff_account` |
| 429 | Too many wrong passwords in a row: the account is locked for a while (`Retry-After`) | `login` |
| 422 | Request body or query failed validation | pydantic models |
| 500 | Unexpected; `/readyz` when the database is unreachable | — |
