# 02 — Freshdesk API Notes

Phase 2 deliverable. What the Freshdesk REST API actually does for the three operations we need, where it falls short of the target agent questions, and how the connector will deal with that.

Sources are listed at the end. Each claim is tagged:

- **[Docs]** — stated in the official API reference (developers.freshdesk.com/api) or the official Freshdesk support article.
- **[Community]** — Freshworks community/staff answers; not in the reference. Treat as likely, not guaranteed.
- **[Check on trial]** — not settled by docs; verify with a real trial account before Phase 9 (checklist in §11).

Documentation checked on 2026-10-04.

---

## 1. Base URL and version

- Base: `https://<subdomain>.freshdesk.com/api/v2/...` **[Docs]**
- v1 is deprecated and restricted for new users; v2 is current. **[Docs]**
- Responses are JSON.

**Connector decision:** `FRESHDESK_DOMAIN` must match `<subdomain>.freshdesk.com` (or a bare subdomain we expand). Anything else is rejected at startup. Reason: the API key is sent in every request, so a mis-set domain must never send it to an arbitrary host. Custom helpdesk domains (e.g. `support.merchant.com`) are therefore not supported — documented limitation.

## 2. Authentication

- HTTP Basic auth. Username = API key, password = any dummy string (docs use `X`). **[Docs]**

  ```
  curl -u "$FRESHDESK_API_KEY:X" https://<subdomain>.freshdesk.com/api/v2/tickets/1
  ```
- The API key is personal to a Freshdesk agent and inherits that agent's permissions. **[Docs]**
- The v2 API reference documents no OAuth flow for the REST API. (OAuth in the Freshworks ecosystem is for Marketplace apps calling *third-party* services, not for external clients calling Freshdesk.) **[Docs — absence]**

**Conclusion for Phase 1's open item:** API-key auth is the only documented option for an external connector, and the assignment explicitly accepts "OAuth or API-key". **API key is confirmed as the approach.**

**Operational recommendation (documented, not enforced):** create a dedicated Freshdesk agent for the connector with a restricted role (read access to tickets, ideally scoped to relevant groups). Our code is read-only, but the key itself is not — Freshdesk keys cannot be scoped to read-only. That is a real limitation worth stating in `CAPABILITIES.md`.

## 3. Ticket model (fields we care about)

From the ticket attribute table **[Docs]**:

| Field | Type | Notes |
|---|---|---|
| `id` | number | |
| `subject` | string | |
| `description` | string | **HTML** |
| `description_text` | string | Plain text |
| `status` | number | See codes below |
| `priority` | number | See codes below |
| `source` | number | Channel |
| `type` | string | Merchant-defined category |
| `tags` | string[] | |
| `requester_id` | number | |
| `responder_id` | number | Assigned agent |
| `group_id` | number | |
| `company_id` | number | |
| `created_at`, `updated_at` | datetime (UTC, ISO 8601) | |
| `due_by`, `fr_due_by` | datetime | Resolution / first-response SLA |
| `custom_fields` | object | Merchant-specific |
| `spam`, `deleted` | boolean | |

Codes **[Docs]**:

| Status | | Priority | | Source | |
|---|---|---|---|---|---|
| 2 | Open | 1 | Low | 1 | Email |
| 3 | Pending | 2 | Medium | 2 | Portal |
| 4 | Resolved | 3 | High | 3 | Phone |
| 5 | Closed | 4 | Urgent | 7 | Chat |
| | | | | 9 | Feedback widget |
| | | | | 10 | Outbound email |

**Custom statuses:** merchants can add statuses; they get codes outside 2–5. Their names are only available from the ticket-fields endpoint (`GET /api/v2/ticket_fields`). **[Check on trial]** for exact shape.

**Connector decision:** map 2–5 to labels; any other status code is surfaced as `"custom_<code>"` and never silently dropped. Reading `ticket_fields` to resolve custom names is a future improvement, not in scope. "Unresolved" = `open` + `pending` (2, 3); tickets in custom statuses are **not** counted as unresolved — documented limitation.

Requester details are **not** in the ticket body, only `requester_id`. Getting name/email costs an `include=requester` embed (see §7).

## 4. List tickets — `GET /api/v2/tickets`

Parameters **[Docs]**:

| Param | Values |
|---|---|
| `filter` | `new_and_my_open`, `watching`, `spam`, `deleted` (predefined only) |
| `requester_id`, `email`, `unique_external_id`, `company_id` | filter by requester / company |
| `updated_since` | ISO datetime; tickets modified after it |
| `order_by` | `created_at`, `due_by`, `updated_at`, `status` |
| `order_type` | `asc`, `desc` (default `desc`) |
| `page` | starts at 1 |
| `per_page` | default 30, max 100 |
| `include` | `stats`, `requester`, `description` (extra credits each) |

Behavior **[Docs]**:

- **Only tickets created in the last 30 days are returned by default.** Use `updated_since` to reach older tickets.
- Max 300 pages (30,000 tickets) reachable.
- Next page signalled by a `Link` header with `rel="next"`; absent on the last page.
- For accounts created after 2018-11-30, `description` is **not** returned unless `include=description`.

**What list cannot do:** filter by status or priority. `new_and_my_open` is relative to the API key's agent, not "all unresolved" — staff confirm no API exists for custom views like "All unresolved tickets". **[Community]**

**Connector use:** "recent tickets" listing with ordering (`created_at` / `updated_at`, asc/desc) and optional `updated_since`. Not used for status/priority questions — that goes to search.

## 5. View a ticket — `GET /api/v2/tickets/{id}`

- Returns the full ticket including `description` and `description_text`. **[Docs]**
- `include` options: `conversations` (up to 10, costs extra), `requester` (id, name, email, mobile, phone), `company` (id, name), `stats` (`resolved_at`, `closed_at`, `first_responded_at`). **[Docs]**
- 404 when the ID doesn't exist. **[Docs]** Behavior for deleted / spam / archived tickets **[Check on trial]**. Archived tickets have a separate endpoint in Freshdesk; we don't support them (limitation).

**Connector use:** `get_ticket` calls with `include=requester,stats` so the agent can answer "who raised it" and "when was it resolved". Cost: 1 base + embeds (§7). Conversations are excluded — they add cost, contain customer PII, and aren't needed for the target questions.

## 6. Filter (search) tickets — `GET /api/v2/search/tickets?query="..."`

Query rules **[Docs]**:

- The whole query is wrapped in double quotes, URL-encoded, **max 512 characters**.
- `AND`, `OR`, parentheses for grouping.
- `:>` and `:<` mean **≥** and **≤** (inclusive) for numeric/date fields.
- `null` to match empty fields.
- Strings in single quotes: `type:'Refund'`.
- Field names are case-sensitive.
- Dates as `'YYYY-MM-DD'` (UTC). Full timestamps are not accepted. **[Docs + Community]**

Supported fields **[Docs]**: `agent_id`, `group_id`, `priority`, `status`, `tag`, `type`, `due_by`, `fr_due_by`, `created_at`, `updated_at`, `closed_at`, and custom fields (text, number, checkbox, dropdown; date in beta).

Examples **[Docs]**: `"priority:3"`, `"status:3 OR status:4"`, `"priority:>3 AND group_id:11 AND status:2"`.

Response and paging **[Docs]**:

- `{ "results": [...], "total": N }`
- **Fixed 30 results per page** (no `per_page`).
- **`page` must be 1–10** → at most 300 results reachable per query.
- Result objects include `description` (HTML) in the documented example. Whether `description_text` is also present **[Check on trial]**.

Limitations:

- **No free-text / keyword search** on subject or description. Not documented anywhere in the v2 reference. **[Docs — absence]**
- **No sort control.** Results come back newest `created_at` first. **[Community]**
- Archived tickets are excluded. **[Docs]**
- New/updated tickets take "a few minutes" to be indexed. **[Docs]**
- Not searchable: `requester_id`, `company_id`, `subject`, `description`.

## 7. Rate limits

Per-minute, **account-wide** (not per agent or IP) **[Docs, support article updated 2026-08-06]**:

| Plan | Calls/min | Tickets List cap |
|---|---|---|
| Free | **0 (no API access)** | — |
| Trial | 50 | — |
| Growth | 100 | 40 |
| Pro | 400 | 100 |
| Enterprise | 700 | 200 |

(The API reference still shows older per-hour numbers for legacy plans; per-minute limits are "being rolled out in batches".)

- Headers on responses **[Docs]**: `X-RateLimit-Total`, `X-RateLimit-Remaining`, `X-RateLimit-Used-CurrentRequest`.
- When exceeded: **HTTP 429** with `Retry-After` (seconds). **[Docs]**
- **Invalid requests count too**, including 401s. **[Docs]** → never retry 4xx except 429.
- **Embeds cost extra credits.** The reference says "an additional API call credit per embedded resource"; the list section says 2 per embed. **[Docs — inconsistent]** → treat each embed as ≥1 extra call; read `X-RateLimit-Used-CurrentRequest` on trial **[Check on trial]**.

**Impact on demo:** a trial account (50/min) is required; a free account will fail every call. Five demo questions use roughly 10–20 credits — comfortable.

## 8. Errors

Status codes **[Docs]**: 400 validation, 401 authentication, 403 access denied, 404 not found, 405 method not allowed, 406 bad Accept header, 409 conflicting state, 415 bad Content-Type, 429 rate limit, 500 server error. 502/503/504 aren't listed but can come from any HTTP stack in front of the API.

Body **[Docs]**:

```json
{
  "description": "Validation failed",
  "errors": [
    { "field": "status", "message": "It should be one of these values: '2,3,4,5'", "code": "invalid_value" }
  ]
}
```

Relevant `code` values: `invalid_value`, `invalid_field`, `datatype_mismatch`, `invalid_credentials`, `access_denied`, `require_feature`, `account_suspended`, `ssl_required`.

### Error mapping (connector)

| Condition | Connector error | Retry? | Agent-facing message (example) |
|---|---|---|---|
| 400 | `InvalidRequest` | No | "Freshdesk rejected the request: <field>: <message>" (field/message only, sanitized) |
| 401 | `AuthenticationFailed` | No | "Freshdesk authentication failed. The connector's API key is invalid or missing." |
| 403 | `PermissionDenied` | No | "The connector's Freshdesk account lacks permission for this operation." (`account_suspended` / `require_feature` noted in logs) |
| 404 | `NotFound` | No | "Ticket 12345 was not found." |
| 405/406/409/415 | `UnexpectedResponse` | No | "Unexpected response from Freshdesk." (these mean a connector bug, not a user error) |
| 429 | `RateLimited` | Yes, bounded | After retries: "Freshdesk rate limit reached; retry after ~N seconds." |
| 500/502/503/504 | `ServiceUnavailable` | Yes, bounded | "Freshdesk is temporarily unavailable." |
| Timeout | `Timeout` | Yes, bounded | "The request to Freshdesk timed out." |
| Connection/DNS error | `NetworkError` | Yes, bounded | "Could not reach Freshdesk." |
| Non-JSON / unparseable 2xx | `UnexpectedResponse` | No | "Unexpected response from Freshdesk." |

Messages never include the API key, the `Authorization` header, raw response bodies, or stack traces.

## 9. Retry and rate-limit strategy

Every connector call is a GET, so retrying is safe (idempotent).

- **Retry on:** 429, 500, 502, 503, 504, timeouts, connection errors.
- **Never retry:** any other 4xx (they also burn quota).
- **Max attempts:** 3 total (1 + 2 retries). Exact numbers fixed in Phase 3.
- **429 delay:** use `Retry-After` when present. If it exceeds a cap (e.g. 30 s), **don't wait** — fail fast with `RateLimited` and include the wait time, so the agent can tell the user instead of hanging a tool call.
- **Other delays:** exponential backoff with jitter (e.g. 0.5 s, 1 s), capped.
- **Logging:** log `X-RateLimit-Remaining` at debug level, and warn when it drops low. No proactive throttling — not worth the complexity for a single-tenant connector.

## 10. How the target questions map onto the API

| Question | Plan | API calls | Caveat |
|---|---|---|---|
| Latest unresolved tickets | search `"status:2 OR status:3"`, page 1 | 1 | "Latest" = most recently **created**; search can't order by `updated_at` |
| High-priority open tickets | search `"(priority:3 OR priority:4) AND status:2"` | 1 | — |
| Tickets about payment failures | (a) native: `tag:'payment'` / `type:'...'` if the merchant uses them; (b) bounded keyword match, see below | 1–3 | Keyword match is not exhaustive |
| Details of ticket 12345 | `GET /tickets/12345?include=requester,stats` | 1 + embeds | — |
| Oldest unresolved tickets | search unresolved, read `total`, fetch the **last** page (results are newest-first) | 2 | Only works when `total` ≤ 300; above that, narrow with `created_at:<'date'` or report the limit. Relies on the **[Community]** sort order |
| Search for a specific issue | same as payment failures | 1–3 | same |

### Keyword search: feasibility (resolves Phase 1 §8)

- **Native free-text search does not exist** in the v2 API. Confirmed by its absence from the reference.
- **Candidate pool:** the connector runs a native filter search (e.g. unresolved, or a date window), fetching **at most a few pages** (proposed: 3 pages = 90 tickets = 3 calls), then matches keywords locally.
- **Fields matched:** `subject` (always present in search results) and the description text **if** search results include it at no extra cost. The docs example shows `description` (HTML) in search results, so the plan is: match `subject` + description converted to plain text. **[Check on trial]**: if the description is missing in search results, we fall back to subject-only and document it. We will **not** use `include=description` on list just to search, because it costs extra credits per call.
- **Output must state the bounds:** `scanned` count, `matched` count, and a note that older or unscanned tickets were not checked.

## 11. Trial-account checks (before Phase 9)

These run manually with a trial account. The key comes from the environment and is never written into files.

```bash
# 1. Auth works; see rate-limit headers
curl -s -D - -o /dev/null -u "$FRESHDESK_API_KEY:X" "https://$FRESHDESK_DOMAIN/api/v2/tickets?per_page=1"

# 2. Search result shape: description / description_text present?
curl -s -u "$FRESHDESK_API_KEY:X" "https://$FRESHDESK_DOMAIN/api/v2/search/tickets?query=%22status:2%22"

# 3. Search sort order (expect created_at desc)
# 4. Credits used by include=requester,stats (X-RateLimit-Used-CurrentRequest)
# 5. 404 body for a missing ticket; behaviour for deleted ticket
# 6. 401 body with a wrong key
# 7. Custom status codes, if any are configured
```

If any check contradicts these notes, this document gets updated before the code changes.

## 12. Fixtures

Sanitized, fake response bodies for Phase 4+ tests live in `tests/fixtures/`. Shapes follow the documented examples; all names, emails and IDs are invented.

---

## Sources

- Freshdesk API v2 reference: https://developers.freshdesk.com/api/ (Authentication, Rate Limit, Pagination, Error Handling, Tickets: List / View / Filter)
- Freshdesk support, "What are the rate limits for the API calls to Freshdesk?" (updated 2026-08-06): https://support.freshdesk.com/support/solutions/articles/225439
- Freshworks Developer Community, filtering unresolved tickets: https://community.freshworks.dev/t/how-to-filter-tickets-based-on-predefined-filters-like-all-unresolved-tickets/5897
- Freshworks community, sorting search results: https://community.freshworks.com/ask-the-freshdesk-community-11036/api-possible-to-sort-tickets-11045
- Freshworks Developer Community, date format in `created_at` queries: https://community.freshworks.dev/t/how-to-filter-tickets-by-date-with-time-in-query-in-created-at/5749
