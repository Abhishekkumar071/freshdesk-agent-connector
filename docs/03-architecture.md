# 03 — Architecture & Technical Design

Phase 3 deliverable. How the connector is structured, what each module is responsible for, the agent-facing tool contracts, and the decisions behind them. Builds on `01-requirements.md` and `02-freshdesk-api-notes.md`.

---

## 1. Shape

```
MCP client (Agent Studio / Claude / MCP Inspector)
        │  stdio (default) or Streamable HTTP
        ▼
server.py      MCPServer, 3 tools, ConnectorError → ToolError
        ▼
service.py     TicketService: validation, query building, pagination, keyword scan
   │  uses query.py (Freshdesk search syntax) and normalize.py (raw → models)
        ▼
client.py      FreshdeskClient: auth, httpx, timeouts, retries, HTTP status → ConnectorError
        ▼
Freshdesk REST API v2  (GET only)
```

Rule of dependencies: each layer imports only the layer below it plus `errors.py` / `models.py` / `config.py`. `server.py` never imports `httpx` or knows a Freshdesk URL. `client.py` never knows about MCP or normalized models.

## 2. Repository layout

```
freshdesk-connector/
├── src/freshdesk_connector/
│   ├── __init__.py
│   ├── __main__.py     # `python -m freshdesk_connector [--transport stdio|http]`
│   ├── config.py       # Settings from env
│   ├── errors.py       # ConnectorError hierarchy
│   ├── client.py       # FreshdeskClient
│   ├── query.py        # build Freshdesk search query strings from typed filters
│   ├── statuses.py     # status code ↔ label catalog, loaded from ticket_fields
│   ├── models.py       # agent-facing Pydantic models
│   ├── normalize.py    # Freshdesk dict → models
│   ├── service.py      # TicketService
│   └── server.py       # create_server(): MCPServer + tools
├── tests/
│   ├── fixtures/       # fake Freshdesk bodies (Phase 2)
│   ├── test_config.py
│   ├── test_client.py  # respx-mocked HTTP
│   ├── test_query.py
│   ├── test_normalize.py
│   ├── test_service.py # fake client, no HTTP
│   └── test_server.py  # in-memory MCP client
├── docs/  (01–03, CAPABILITIES.md, DEMO.md)
├── .env.example  .gitignore  pyproject.toml  uv.lock  README.md
```

`query.py` and `normalize.py` are split out of `service.py` because they are pure functions with many edge cases. Testing them alone is simpler than testing them through the service.

## 3. Modules

### 3.1 `config.py`

`Settings` (pydantic-settings, reads env and `.env`):

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `domain` | `FRESHDESK_DOMAIN` | required | `acme` or `acme.freshdesk.com`; normalized to `acme.freshdesk.com`. Anything else (paths, other hosts, schemes other than https) → startup error |
| `api_key` | `FRESHDESK_API_KEY` | required | `SecretStr`; never printed by `repr`/`str` |
| `timeout_seconds` | `FRESHDESK_TIMEOUT_SECONDS` | 10 | per attempt |
| `max_attempts` | `FRESHDESK_MAX_ATTEMPTS` | 3 | 1 = no retry |
| `max_retry_wait_seconds` | `FRESHDESK_MAX_RETRY_WAIT_SECONDS` | 10 | longer `Retry-After` → fail fast |
| `log_level` | `LOG_LEVEL` | `INFO` | |

Missing or invalid config fails at startup with a message naming the variable, never its value.

### 3.2 `errors.py`

```
ConnectorError(message: str)            # message is always safe to show the agent
├── InvalidInput                         # our validation (bad dates, bad tag chars, query too long)
├── InvalidRequest                       # Freshdesk 400
├── AuthenticationFailed                 # 401
├── PermissionDenied                     # 403
├── NotFound                             # 404
├── RateLimited(retry_after: int|None)   # 429 after retries / wait too long
├── ServiceUnavailable                   # 5xx after retries
├── RequestTimeout                       # timeout after retries
├── NetworkError                         # connect/DNS after retries
└── UnexpectedResponse                   # 405/406/409/415, bad JSON, wrong shape
```

Messages are written by us from the status code, never copied from the raw response. The one exception is 400, where Freshdesk's `errors[].field` / `message` are copied after truncation, because that is what lets the agent fix its input.

### 3.3 `client.py` — `FreshdeskClient`

```python
class FreshdeskClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, sleep=asyncio.sleep): ...
    async def list_tickets(self, *, order_by, order_type, page, per_page, updated_since=None) -> ListResult  # (items: list[dict], has_next: bool)
    async def get_ticket(self, ticket_id: int, include: list[str]) -> dict
    async def search_tickets(self, query: str, page: int) -> SearchResult  # (results: list[dict], total: int)
    async def _get(self, path: str, params: dict) -> httpx.Response   # the only method that does I/O
```

- **GET only.** There is no generic `request(method, ...)`. A test asserts that no other HTTP verb is reachable.
- Auth: `httpx.BasicAuth(api_key, "X")` is set on the `AsyncClient` that `build_http_client(settings)` creates. Auth lives in this one place.
- `http` and `sleep` are injected. Tests pass a respx-mocked client and a fake `sleep` that records waits, so retry tests run instantly.
- Returns raw Freshdesk JSON (dicts) plus pagination metadata. It does not normalize.
- `has_next` comes from the `Link: <...>; rel="next"` header. `total` comes from the search body.

**Retry loop in `_get`** (custom code, about 40 lines):

```
for attempt in 1..max_attempts:
    try: response = await http.get(...)
    except TimeoutException  → retryable(RequestTimeout)
    except TransportError    → retryable(NetworkError)
    else:
        2xx                 → return
        429                 → wait = Retry-After or backoff
                              if wait > max_retry_wait: raise RateLimited(retry_after)   # fail fast
                              retryable(RateLimited)
        500/502/503/504     → retryable(ServiceUnavailable)
        other               → raise mapped error immediately (no retry)
    if last attempt: raise the retryable error
    await sleep(wait or backoff(attempt))      # backoff: 0.5s, 1s, 2s … capped, ±25% jitter
```

Worst case for one tool call: 3 × 10 s timeouts + 2 × ≤10 s waits ≈ 50 s. Typical case: one round trip.

### 3.4 `query.py` and `statuses.py`

`query.py` turns typed filters into a Freshdesk query string:

```python
build_query(status_codes, priority_codes, tag, ticket_type, created_after, created_before) -> str
# e.g. "(status:2 OR status:3) AND (priority:3 OR priority:4) AND tag:'payment'"
```

- The agent never writes Freshdesk query syntax. It passes typed fields; we build the string.
- `tag` / `ticket_type`: 1–64 characters, letters, digits, space, `-`, `_`, `.` only. This rejects quotes and parentheses, so input can't break out of `'...'` or change the query's logic.
- Dates are formatted as `'YYYY-MM-DD'` and are inclusive. `created_after` ≤ `created_before` is checked.
- If the result is over 512 characters → `InvalidInput`.

`statuses.py` holds `StatusCatalog`, which is **loaded once at startup** from `GET /ticket_fields` (decision B, after the trial showed custom statuses 6, 7 and 9000 on a fresh account):

- Labels are slugs of the agent-facing names: `open`, `pending`, `resolved`, `closed`, `waiting_on_customer`, ...
- `unresolved` is a shortcut for **every status except Resolved (4) and Closed (5)**, so custom statuses are included.
- Unknown labels → `InvalidInput` listing the valid ones. Unknown codes in a ticket → `status_<code>`.
- If `ticket_fields` can't be loaded (any `ConnectorError`), the catalog falls back to the four built-in statuses with a warning, instead of failing startup.

### 3.5 `models.py` — agent-facing output

```python
class TicketSummary(BaseModel):
    id: int
    subject: str
    status: str                     # catalog label, e.g. "open", "waiting_on_customer"
    priority: str                   # "low" | "medium" | "high" | "urgent"
    type: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    due_by: datetime | None
    requester_id: int | None
    assigned_agent_id: int | None   # Freshdesk responder_id
    group_id: int | None
    url: str                        # https://<domain>/a/tickets/<id> for humans

class Requester(BaseModel):
    id: int | None; name: str | None; email: str | None

class TicketDetail(TicketSummary):
    source: str                     # "email", "portal", ...
    description: str                # plain text, truncated to 2,000 chars
    description_truncated: bool
    requester: Requester | None
    first_responded_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None

class TicketPage(BaseModel):        # list_tickets
    tickets: list[TicketSummary]
    page: int
    has_more: bool
    notes: list[str]                # plain-language caveats for the agent

class SearchResult(BaseModel):     # search_tickets, every mode
    search_mode: Literal["native", "keyword_scan"]
    tickets: list[TicketSummary]
    page: int
    has_more: bool
    total: int                      # Freshdesk total for the native filters
    notes: list[str]
    keyword: str | None             # keyword_scan only
    max_scan: int | None            # set when the connector scanned a bounded set (keyword or oldest_first)
    scanned: int | None             #   "
    matched: int | None             # keyword_scan only
    exhaustive: bool | None         #   set with max_scan — False → matches may exist beyond the scan
```

`list_tickets` returns `TicketPage`; `search_tickets` always returns `SearchResult`. The scan fields are filled whenever the result came from a bounded connector-side scan (keyword search, or oldest-first ordering) and are null for plain Freshdesk paging. When `exhaustive` is `False`, `notes` also says so in a plain sentence.

**Deliberately left out:** HTML description, `custom_fields` (merchant-defined, may contain anything, and their schema is unknown to us), CC/BCC emails, conversations, attachments, `spam`/`deleted` flags. Requester name/email is in `get_ticket` only; the trial showed the requester embed also carries `ip_address`, `first_seen` and `last_seen`, which are dropped. List and search results carry `requester_id` only.

### 3.6 `normalize.py`

`to_summary(raw, statuses, domain)` and `to_detail(raw, statuses, domain)`. Code → label maps. Description: prefer `description_text`, otherwise strip tags from `description` with `html.parser`. Missing optional fields become `None`. A missing `id` / `status` / `created_at` / `updated_at`, or a bad timestamp, raises `UnexpectedResponse`.

### 3.7 `service.py` — `TicketService`

```python
async def create_ticket_service(client, domain) -> TicketService   # loads StatusCatalog

class TicketService:
    async def list_tickets(self, *, order_by, order, page, page_size, updated_since) -> TicketPage
    async def get_ticket(self, ticket_id) -> TicketDetail
    async def search_tickets(self, *, status, priority, tag, ticket_type,
                             created_after, created_before, keyword, order, page) -> SearchResult
```

Search routes to one of three paths:

| Path | When | Calls | Ordering |
|---|---|---|---|
| Native page | no keyword, `newest_first` | 1 | Freshdesk page `n` (30 per page, pages 1–10), re-sorted newest first within the page |
| Oldest-first scan | no keyword, `oldest_first` | ≤ 3 | scan ≤ 90 matches, sort by `created_at` ascending **in the connector**, page locally (pages 1–3) |
| Keyword scan | `keyword` given | ≤ 3 | scan ≤ 90 candidates, match locally, sort per `order`, page locally (pages 1–3) |

**Oldest-first (decision A).** The trial could not show which order Freshdesk search uses (docs/02 §11.1), so the connector does not depend on it. It reads up to 3 pages and sorts them itself. If `total` ≤ 90 the answer is exact (`exhaustive=True`). Otherwise `exhaustive=False` and a note tells the agent to narrow with `created_before`/`created_after`. The earlier "fetch the last page" idea is dropped.

**Keyword scan.** Base query = the native filters, or `created_at:>'<today − 365 days>'` when none are given (confirmed accepted on the trial). Match = every word (case-insensitive) appears in subject + plain-text description (the trial confirmed search results include `description_text`). `exhaustive = scanned >= total`. Keyword length is 2–100 characters. There is no stemming or fuzzy matching.

Limits are module constants: `LIST_MAX_PAGE=10`, `LIST_MAX_PAGE_SIZE=50`, `SCAN_MAX_PAGES=3` (→ `SCAN_MAX_TICKETS=90`), `KEYWORD_DEFAULT_LOOKBACK_DAYS=365`.

### 3.8 `server.py`

```python
def create_server(service_factory: Callable[[], AsyncContextManager[TicketService]]) -> MCPServer
```

- A lifespan opens the httpx client, builds `FreshdeskClient` and `TicketService`, and closes the client on shutdown.
- Each tool: validated args (Pydantic via `Annotated[..., Field(...)]`) → service call → returns a model (structured output plus text).
- `except ConnectorError as e: raise ToolError(e.message)`. The agent sees a categorized, safe message with `is_error=True`.
- Any other exception is left to the SDK, which returns a generic "Error executing tool" and logs the traceback on the server only.
- All tools carry `ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)`.
- Tests call `create_server` with a factory that yields a fake service. Production `__main__` passes the real factory.

## 4. Tool contracts

Descriptions below are drafts. Final wording comes in Phase 6.

### `list_tickets`
> List recent Freshdesk tickets, newest first by default. Use for "latest/recent tickets" when no status or priority filter is needed. Only tickets created in the last 30 days are included unless `updated_since` is given. Cannot filter by status or priority — use `search_tickets` for that.

| Param | Type | Default |
|---|---|---|
| `order_by` | `"created_at" \| "updated_at"` | `created_at` |
| `order` | `"newest_first" \| "oldest_first"` | `newest_first` |
| `page` | int 1–10 | 1 |
| `page_size` | int 1–50 | 20 |
| `updated_since` | date, optional | — |

Returns `TicketPage`.

### `get_ticket`
> Get full details of one Freshdesk ticket by its numeric ID, including plain-text description, requester name/email, and response/resolution timestamps.

| Param | Type |
|---|---|
| `ticket_id` | int ≥ 1 |

Returns `TicketDetail`. A missing ticket returns a tool error: "Ticket N was not found."

### `search_tickets`
> Find tickets by status, priority, tag, type, and creation date. For "unresolved" use `status=["unresolved"]` (all statuses except resolved/closed, including custom ones). Optional `keyword` does a bounded text match on subject/description over at most 90 tickets matching the other filters — results say how many were scanned and whether the scan was complete. Results are capped at the 300 most recent matches.

| Param | Type | Default |
|---|---|---|
| `status` | list of catalog labels, or `unresolved` | — |
| `priority` | list of `low\|medium\|high\|urgent` | — |
| `tag` | str, restricted chars | — |
| `ticket_type` | str, restricted chars | — |
| `created_after`, `created_before` | date | — |
| `keyword` | str 2–100 | — |
| `order` | `newest_first \| oldest_first` | `newest_first` |
| `page` | int 1–10 | 1 (ignored with `keyword`) |

At least one filter or `keyword` is required. Always returns `SearchResult` (§3.5): `search_mode="native"` without `keyword`, `"keyword_scan"` with it.

Mapping of the target questions:

| Question | Call |
|---|---|
| Latest unresolved tickets | `search_tickets(status=["unresolved"])` |
| High-priority open tickets | `search_tickets(status=["open"], priority=["high","urgent"])` |
| Tickets about payment failures | `search_tickets(keyword="payment")` or `search_tickets(tag="payment")` |
| Ticket 12345 | `get_ticket(12345)` |
| Oldest unresolved | `search_tickets(status=["unresolved"], order="oldest_first")` |

## 5. Error flow example

```
agent → get_ticket(99999)
server → service.get_ticket(99999) → client._get("/tickets/99999")
Freshdesk → 404
client → raise NotFound("Ticket 99999 was not found.")       # no retry
server → raise ToolError("Ticket 99999 was not found.")
agent ← is_error=True, content="Ticket 99999 was not found."
log   ← INFO GET /api/v2/tickets/99999 status=404 attempt=1 ms=180
```

## 6. Logging

- Standard `logging`, to **stderr**. With the stdio transport, stdout carries the protocol, so writing logs there would corrupt it.
- One line per HTTP attempt: method, path, status, attempt, duration, `X-RateLimit-Remaining`. WARNING on retry and when remaining quota drops below 10% of `X-RateLimit-Total`.
- Never logged: the API key, the `Authorization` header, response bodies, ticket descriptions, requester email.
- A test captures logs during a failing call and asserts the key string is absent.

## 7. Security summary

| Threat | Control |
|---|---|
| Agent performs a write | No write tools; client has no non-GET path; test-enforced |
| Key leaks to the LLM | Key only in `Settings` → `BasicAuth`; errors are built from status codes; SDK masks unexpected exceptions |
| Key leaks to logs | Never logged; `SecretStr`; log-capture test |
| Key sent to the wrong host | Domain must be `*.freshdesk.com`, https only |
| Query injection through tag/type | Character allow-list + server-side query building |
| Large data pulls | Page ≤ 10, page_size ≤ 50, keyword scan ≤ 3 pages |
| PII in bulk results | Requester details only in `get_ticket`; no CC emails or custom fields |
| Unauthenticated HTTP transport | Defaults to stdio. HTTP binds to `127.0.0.1`. Exposing it beyond localhost needs auth in front (reverse proxy or gateway). Documented as a limitation, not built |
| Key itself can write | Freshdesk keys can't be scoped. Recommend a restricted-role agent (documented) |

## 8. Decisions and alternatives

| Decision | Options | Chosen | Why / trade-off |
|---|---|---|---|
| Layering | MCP tool calls Freshdesk directly · client/service/server layers | **Layers** | Tools stay thin and testable without HTTP; Freshdesk quirks (30-day list, 300-result cap, codes) stay out of the tool layer. Costs a few more files |
| MCP SDK | `mcp` v1 `FastMCP` · `mcp` v2 `MCPServer` · third-party `fastmcp` | **`mcp>=2.3,<3`** (`MCPServer`) | v2 is the current stable line (2.3.0, 2026-10-02). It is the official SDK, has typed tool schemas, structured output, `ToolError`, and an in-memory test `Client`. Most online examples use v1 `FastMCP`; we follow the v2 docs |
| Transport | stdio · Streamable HTTP | **Both; stdio default** | stdio for local clients and the demo; HTTP for a remote Agent Studio connection. One flag on `__main__` |
| Sync vs async | `requests` sync · `httpx` async | **async httpx** | MCP handlers are async. One shared connection pool. Keyword scan pages *could* run concurrently, but we keep them sequential to stay gentle on rate limits |
| Retries | `tenacity` · custom loop | **Custom** | Needs: Retry-After parsing, fail-fast above a cap, status-specific decisions, injectable sleep. That's about 40 readable lines; `tenacity` would need custom predicates and wait hooks anyway |
| Data types | raw dicts end-to-end · Pydantic models | **Raw dicts inside client, Pydantic at the service boundary** | Modelling all ~30 Freshdesk fields adds no value. The output models are the contract that matters |
| Search input | Agent writes raw Freshdesk query · typed filters | **Typed filters** | LLMs get the quoting, case-sensitivity and numeric codes wrong, and a raw string is an injection surface. Cost: fewer combinations (e.g. no OR across different fields) |
| Free text | none · fetch everything · bounded scan | **Bounded scan (≤90)** | Phase 1 decision. Honest `scanned`/`exhaustive` output |
| Unresolved | Open+Pending · read custom statuses from `ticket_fields` | **Read `ticket_fields` at startup** (changed after trial check) | A fresh trial already has 3 custom statuses; Open+Pending would miss them. Costs 1 call per server start |
| Test async plugin | pytest-asyncio · anyio pytest plugin | **anyio plugin** | anyio is already installed with `mcp`/`httpx`, and the MCP testing docs use it. One fewer dependency, same capability |
| Storage / cache | none · cache tickets | **None** | Data must be current; volume is small; a cache adds invalidation bugs and holds PII |

## 9. Decision: keyword search placement (resolved)

Options were (A) an optional `keyword` on `search_tickets`, or (B) a fourth tool `keyword_search_tickets`. **Chosen: A.** That keeps the tool surface at the three primitives the assignment asks for. To avoid two unrelated output shapes, both modes return one `SearchResult` envelope with `search_mode`, and the scan fields (`max_scan`, `scanned`, `matched`, `exhaustive`) are filled in keyword mode. Trade-off: the bounded-scan caveat lives in one tool description, next to native search, so the description and `notes` have to state it clearly.

## 10. Testing plan by phase

| Phase | File | Covers |
|---|---|---|
| 4 | `test_config.py` | domain normalization/rejection, missing vars, key not in repr |
| 4 | `test_client.py` | 200 list/get/search, Link header, 400 (field message), 401, 403, 404, 405, 429 with/without Retry-After, Retry-After over cap → fail fast, 500→200 recovery, 503 exhausted, timeout, connect error, bad JSON, no retry on 4xx, auth header present, key absent from errors/logs, GET-only |
| 5 | `test_query.py` | each filter, combos, quoting, rejected chars, date order, 512 limit |
| 5 | `test_normalize.py` | fixtures → models, label maps, custom status, HTML stripping, truncation, missing fields |
| 5 | `test_service.py` | pagination math, oldest-first exact vs. bounded (scrambled server order), keyword scan bounds and `exhaustive`, notes, status catalog fallback |
| 6 | `test_server.py` | exactly the expected read-only tools and annotations, schemas, invalid args rejected, ConnectorError → `is_error` with a safe message, unexpected error masked |

Dependencies: runtime `mcp>=2.3,<3`, `httpx`, `pydantic`, `pydantic-settings`. Dev: `pytest`, `respx`, `pytest-cov`. Managed with `uv`.
