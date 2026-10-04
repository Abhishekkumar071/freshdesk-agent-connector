# Freshdesk API notes

What the Freshdesk v2 API actually does for the three things this connector needs (list, view, search), and where it falls short. The design in [architecture.md](architecture.md) follows from these constraints.

Each claim is marked with where it comes from:

- **docs**: the official API reference or the Freshdesk support site
- **community**: Freshworks community or staff answers, so likely but not guaranteed
- **observed**: checked on a Freshdesk trial account on 2026-10-04

## Authentication

- HTTP Basic auth: the API key is the username and the password can be anything (`X`). *(docs, observed)*
- The key belongs to a Freshdesk agent and has that agent's permissions. **It can't be limited to read-only.** *(docs)*
- The v2 reference doesn't describe an OAuth flow for external clients, so API-key auth is the only option. The assignment allows either. *(docs)*
- **The Free plan has no API access** (0 calls/min). A trial account works. *(docs)*

## Ticket fields and codes

The connector uses `id`, `subject`, `description` (HTML), `description_text`, `status`, `priority`, `source`, `type`, `tags`, `requester_id`, `responder_id`, `group_id`, `created_at`, `updated_at` and `due_by`. Everything else, such as `custom_fields`, CC addresses and `spam`, is ignored. *(docs)*

| Status | | Priority | | Source | |
|---|---|---|---|---|---|
| 2 | Open | 1 | Low | 1 | Email |
| 3 | Pending | 2 | Medium | 2 | Portal |
| 4 | Resolved | 3 | High | 3 | Phone |
| 5 | Closed | 4 | Urgent | 7 | Chat |
| | | | | 9 | Feedback widget |
| | | | | 10 | Outbound email |

**Custom statuses are common.** A brand-new trial already had `6 Waiting on Customer`, `7 Waiting on Third Party` and `9000 Assigned to AI Agent`. Their names are only available from `GET /ticket_fields`, where they look like `"6": ["Waiting on Customer", "Awaiting your Reply"]` (agent label first). *(observed)*

## List — `GET /api/v2/tickets`

- Supports `order_by` (`created_at`, `updated_at`, `due_by`, `status`), `order_type` (`asc`/`desc`), `page`, `per_page` (max 100), `updated_since`, and requester/company filters. *(docs)*
- **By default it only returns tickets created in the last 30 days.** Use `updated_since` to go further back. *(docs; not checkable on a new account)*
- **It can't filter by status or priority.** The predefined `filter` values are things like `new_and_my_open`, which is relative to the key's agent. *(docs, community)*
- The next page is signalled by a `Link: <...>; rel="next"` header. *(docs, observed)*
- `description` is only included with `include=description`, which costs extra. *(docs, observed)*

## View — `GET /api/v2/tickets/{id}`

- Returns `description` and `description_text`. A missing ID returns 404. *(docs, observed)*
- `include=requester,stats` adds requester details and resolution timestamps, and costs **2 credits instead of 1**. *(observed)*
- The requester embed contains more than the docs list, including `ip_address`, `first_seen` and `last_seen`. The connector keeps only id, name and email. *(observed)*

## Search — `GET /api/v2/search/tickets?query="..."`

- The query goes inside double quotes, max 512 characters. Supported fields: `status`, `priority`, `tag`, `type`, `agent_id`, `group_id`, dates and custom fields, combined with `AND`/`OR`/parentheses. Strings go in single quotes. *(docs)*
- Dates are `'YYYY-MM-DD'`. `created_at:<'D'` and `created_at:>'D'` both **include the whole day D**. *(docs, observed)*
- **Fixed 30 results per page, and `page` must be 1–10**, so at most 300 results per query. Page 11 returns 400; a page past the end returns an empty list. *(docs, observed)*
- The response is `{"results": [...], "total": N}`, and results include `description_text` at no extra cost. *(observed)*
- **No text search.** `subject:'payment'` returns 400 "Unexpected/invalid field". *(docs, observed)*
- **No sort control.** Results reportedly come back newest `created_at` first. The trial was consistent with that, but with only 3 tickets it couldn't rule out other orders. *(community, observed)*
- New or updated tickets take a few minutes to become searchable, and archived tickets are excluded. *(docs)*

## Rate limits

- Per minute, across the whole account: Trial 50, Growth 100, Pro 400, Enterprise 700. Some plans also cap the list endpoint separately (Growth: 40/min). *(docs, support article updated 2026-08-06)*
- Responses carry `X-RateLimit-Total`, `X-RateLimit-Remaining` and `X-RateLimit-Used-CurrentRequest`. **Values may be decimals such as `49.0`.** *(docs, observed)*
- Over the limit returns **429 with `Retry-After` in seconds**. *(docs)*
- **Failed requests count too**, including 401s, so 4xx errors shouldn't be retried. *(docs)*

## Errors

| Status | Meaning | Connector behaviour |
|---|---|---|
| 400 | Validation failed; the body has `errors[].field/message` | `InvalidRequest`, passing on the field and message only; not retried |
| 401 | Bad or missing key | `AuthenticationFailed`; not retried |
| 403 | No permission, feature missing, or account suspended | `PermissionDenied`; not retried |
| 404 | Not found | `NotFound` ("Ticket N was not found."); not retried |
| 429 | Rate limited | retried after `Retry-After`; fails fast if the wait exceeds 10 s |
| 500/502/503/504 | Server side | retried with backoff, up to 3 attempts |
| other | 405, 409, redirects, bad JSON, ... | `UnexpectedResponse`; not retried |

## Sources

- API reference: https://developers.freshdesk.com/api/
- Rate limits: https://support.freshdesk.com/support/solutions/articles/225439
- Filtering unresolved tickets (community): https://community.freshworks.dev/t/how-to-filter-tickets-based-on-predefined-filters-like-all-unresolved-tickets/5897
- Sorting search results (community): https://community.freshworks.com/ask-the-freshdesk-community-11036/api-possible-to-sort-tickets-11045
- Date format in queries (community): https://community.freshworks.dev/t/how-to-filter-tickets-by-date-with-time-in-query-in-created-at/5749
