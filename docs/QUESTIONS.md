# Clarifying questions, and the assumptions made meanwhile

The brief invites questions. These are the ones whose answers would change the build. Each one lists what the code currently assumes, so a different answer means a small, local change.

| # | Question for Riverbend | Assumed for now | Where it lives |
|---|---|---|---|
| 1 | Which fields must a request have before you'll book it? Is bike type required? | **What's wrong + some timing.** Bike type is extracted but optional: the shop sees the bike at drop-off, and requiring it roughly triples follow-up questions on the samples. | `policy.decide` |
| 2 | When a message reaches the wrong shop (m-1009 says "Hi Lakeshore team" to Downtown), may we forward it to the other shop? | **No automatic cross-shop transfer.** Ask the customer; if they want the other shop, point them there. | `policy`, D9 |
| 3 | May the system cancel an appointment by itself (m-1038)? | **No.** Mark the matching order `cancel_requested` for staff to confirm. Ambiguous or no match → staff. | `pipeline._request_cancellation` |
| 4 | Is it fine to answer price questions automatically with catalog prices (m-1018)? | **Yes.** Code formats the quote from the catalog; no work order is created. | `customer_copy.price_quote` |
| 5 | When a per-wheel service doesn't say which wheel (m-1021), quote one wheel or two? | **One, flagged for staff.** | `catalog`, review flags |
| 6 | At the monthly LLM cap: stop, or keep going on a cheaper model? | **Stop calling the model and hold messages** (visible to staff, already acknowledged); alert; raise the cap to release them. No silent quality change. | `budget`, D7 |
| 7 | What should the caps be, per shop? | $5.00/month each (configurable per shop at runtime). | `shops.monthly_llm_cap_usd` |
| 8 | Are `message_id`s unique per shop or globally? How do customer replies arrive? | Unique **per shop**; replies carry a token from our reply-to address. | `inbound_messages`, `reply_token` |
| 9 | Should automatic replies be in the customer's language (m-1033 is Spanish)? | Only the follow-up question; summaries are English and the order is flagged. | `customer_copy` |
| 10 | Which timezone are the shops in? | America/Chicago for all three. | `shops.timezone` |
| 11 | How long may you keep message text and raw model output? | Indefinitely (not built: needs your policy). | NOT_BUILT |
| 12 | Does "open work orders" include ones with a pending cancellation? | Yes, shown with a `cancel_requested` flag until staff confirm. | `GET /work-orders` |
