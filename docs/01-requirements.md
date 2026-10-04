# 01 — Requirements & Scope

Phase 1 deliverable for the Freshdesk private connector (Razorpay FDE, Agent Studio — Assignment 3).

Status legend used throughout: **[Verify P2]** = belief to be confirmed against official Freshdesk docs in Phase 2.

---

## 1. Assignment (source of truth)

> Build a connector that enables an Agent Studio agent to read tickets from Freshdesk. Include a working OAuth or API-key authentication flow, suitable list/get/search primitives, rate-limit handling, an MCP tool specification or equivalent, and a short document describing what the agent can and cannot do. Include setup and run instructions, plus any assumptions or limitations. Do not include real customer data, passwords, API keys, or other credentials. Deadline: 48 hours.

### Requirement mapping

| # | Requirement | How we meet it | Deliverable |
|---|---|---|---|
| R1 | Agent can read Freshdesk tickets | Three read-only MCP tools | `server.py` |
| R2 | Working auth flow (OAuth or API key) | Planned: Freshdesk API-key auth from env, isolated in client (confirm in Phase 2) | `config.py`, `client.py` |
| R3 | list / get / search primitives | `list_tickets`, `get_ticket`, `search_tickets` | `server.py`, `service.py` |
| R4 | Rate-limit handling | 429 detection, `Retry-After`, bounded backoff retries | `client.py` |
| R5 | MCP tool specification | MCP server via official Python SDK; schemas + descriptions | `server.py`, README |
| R6 | Doc: what agent can / cannot do | Dedicated capability document | `docs/CAPABILITIES.md` |
| R7 | Setup, run, assumptions, limitations | README | `README.md` |
| R8 | No secrets / real data | `.env.example`, `.gitignore`, fake fixtures only | repo hygiene |

---

## 2. Business problem

A merchant runs customer support on Freshdesk. Support leads want an AI agent to answer operational questions ("what's urgent?", "what's been stuck longest?", "status of ticket 123?") without clicking through the Freshdesk UI.

The underlying need is **safe, structured, read-only access to support data for an agent**: tools that cannot be misused, outputs the model can reason over, and predictable failure behavior.

## 3. Users

| User | Need |
|---|---|
| Support lead / ops manager | Asks questions in natural language |
| AI agent (direct consumer) | Narrow, well-described tools; validated inputs; compact normalized outputs |
| Merchant admin / FDE (operator) | Configures domain + key once; key never leaves the server |
| Reviewer | Sound judgment, security, honest limitations |

## 4. Target agent questions

| Question | Expected tool path |
|---|---|
| Show me the latest unresolved tickets | `list_tickets` / `search_tickets` (status open+pending), newest first |
| Find high-priority open tickets | `search_tickets` (priority high/urgent, status open) |
| Find tickets related to payment failures | `search_tickets` (native filters, e.g. tag) + bounded keyword match — see §8 |
| Give me the details of ticket 12345 | `get_ticket` |
| Which unresolved tickets are the oldest? | Unresolved filter + oldest-first ordering — feasibility **[Verify P2]** |
| Search for tickets containing a specific issue | `search_tickets` keyword (bounded) |

---

## 5. Scope

### In scope
- `list_tickets` — bounded, paginated listing with ordering and supported filters.
- `get_ticket` — single ticket by ID, normalized.
- `search_tickets` — Freshdesk-native field search (status, priority, tag, etc. **[Verify P2]**), plus optional bounded connector-side keyword match.
- Authentication via environment variables. API-key auth is the planned approach, pending confirmation against the official Freshdesk docs and the assignment wording in Phase 2 (which allows "OAuth or API-key"). If Phase 2 shows OAuth is needed or clearly better for this use case, we revisit.
- Timeouts, typed error mapping, bounded retries with backoff, `Retry-After` support.
- Normalized, agent-friendly responses (labels such as `"open"` / `"urgent"` instead of numeric codes; plain-text description, truncated).
- Explicit truncation signal (`has_more`) so the agent never assumes it saw everything.
- MCP server using the official Python SDK, without FastAPI.

### Non-goals
- Any write operation (create, update, delete, close, reply, note, priority/status change).
- Contacts / companies / agents / conversations endpoints beyond what a ticket response embeds.
- Separate REST API (FastAPI dropped by decision).
- Databases, caches, queues, UI, multi-tenant credential management.
- Unbounded page fetching of any kind.

## 6. Read-only boundary

Enforced in code, not by prompt:

1. The MCP server registers exactly three tools, all read-only.
2. The Freshdesk client exposes only GET requests; it has no method capable of POST/PUT/DELETE.
3. A test asserts the tool list and the client's HTTP-method surface.
4. Recommended operational control: the API key should belong to a Freshdesk agent with a restricted role, so even a leaked key has limited power (documented, not enforced by us).

## 7. Credential handling

- `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY` from environment (`.env` locally, git-ignored).
- Key held as a secret type; never logged, never in error messages, never in tool output.
- `.env.example` with placeholders only.
- Tests assert the key does not appear in exception text or captured logs.

---

## 8. Search strategy (decision: option c)

1. **Native first.** Use Freshdesk's Search/Filter API for supported fields (status, priority, tag, type, dates, custom fields — exact list **[Verify P2]**).
2. **Bounded keyword fallback.** If the request includes free-text keywords that Freshdesk cannot search natively, the connector matches keywords locally over a **hard-capped** set of candidate tickets (cap defined in Phase 3, small page count). Which field(s) to match on — subject, description, or something else — is not decided yet. It depends on what Freshdesk already searches natively and what fields the list/search responses return without extra API cost; Phase 2 answers both.
3. **Honesty in output.** Results from keyword matching indicate how many tickets were scanned and that the match is not exhaustive, so the agent can tell the user.
4. **No unbounded fetches** to make local search "complete".

Exact feasibility, caps, and limitations to be confirmed in Phase 2 before any implementation.

---

## 9. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| K1 | Native search may not support free text **[Verify P2]** | "payment failures" query weak | Option (c) + documented limitation |
| K2 | List endpoint may default to recent tickets only (~30 days) **[Verify P2]** | Agent states wrong totals | Document in tool description; expose date params if supported |
| K3 | Search API may cap results and not allow custom sort **[Verify P2]** | "Oldest unresolved" hard | Bounded fetch + connector-side sort, or list endpoint ordering; decide in P2/P3 |
| K4 | Custom statuses (merchant-defined) | "Unresolved" mapping incomplete | Default Open+Pending; document; unknown codes surfaced as `"custom:<id>"` |
| K5 | Plan-dependent rate limits; embeds cost extra credits **[Verify P2]** | 429s during demo | Bounded retries, `Retry-After`, minimal embeds |
| K6 | Trial account availability for demo | Phase 9 blocked | Create trial early; mocked demo fallback |
| K7 | 48-hour deadline vs 10 gated phases | Incomplete submission | Short docs-phases; protect time for phases 4–9 |

## 10. Assumptions

- One merchant / one Freshdesk domain per deployment.
- API-key auth satisfies "working authentication flow" (to be confirmed in Phase 2).
- "Unresolved" = Open + Pending by default.
- Consumer is an MCP-compatible client (Agent Studio, Claude Desktop, MCP Inspector for demo).
- Where a ticket description is returned to the agent (at least in `get_ticket`), it is plain text, truncated to a safe length. Which endpoints return it, and at what API cost, is checked in Phase 2.
- Automated tests never call real Freshdesk; only the Phase 9 demo does.

---

## 11. Technology decisions (Phase 1 level)

| Area | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | Chosen implementation language (not an assignment requirement); mature MCP SDK and async HTTP tooling |
| MCP | Official `mcp` Python SDK (API verified before Phase 6) | Standard; supports stdio and Streamable HTTP |
| Web framework | **None** (FastAPI dropped) | MCP SDK provides transport; extra layer adds no value |
| HTTP | `httpx` (async) | Timeouts built in; mockable with `respx` |
| Models / config | Pydantic v2, `pydantic-settings` | Validation + secret types |
| Tests | pytest, pytest-asyncio, respx | Mocked HTTP, no real API |
| Packaging | `uv` | Fast, lockfile, single tool |
| Retries | Decided in Phase 3 (custom vs library) | — |

Architecture:

```
AI Agent → MCP Tools → Service Layer → Freshdesk Client → Freshdesk API
```

## 12. Proposed repository structure

```
freshdesk-connector/
├── src/freshdesk_connector/
│   ├── config.py      # env settings, secret key
│   ├── errors.py      # typed error hierarchy
│   ├── client.py      # Freshdesk HTTP client (GET only, auth, retry)
│   ├── models.py      # normalized ticket models
│   ├── service.py     # validation, mapping, pagination, normalization
│   └── server.py      # MCP tools
├── tests/
├── docs/
│   ├── 01-requirements.md
│   ├── 02-freshdesk-api-notes.md
│   ├── 03-architecture.md
│   ├── CAPABILITIES.md
│   └── DEMO.md
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

## 13. Development plan

| # | Phase | Output |
|---|---|---|
| 1 | Requirements | This document |
| 2 | Freshdesk API study | `02-freshdesk-api-notes.md` (verified endpoints, search limits, pagination, rate limits, errors) |
| 3 | Architecture | `03-architecture.md` (module contracts, error/retry/pagination design, alternatives) |
| 4 | Freshdesk client | config, errors, client + mocked-HTTP tests |
| 5 | Service layer | models, service + unit tests |
| 6 | MCP tools | server + tool tests |
| 7 | Reliability hardening | retry/backoff/429 review, logging, redaction |
| 8 | Comprehensive testing | full suite, coverage, gaps |
| 9 | Agent demo | real trial account via MCP client, `DEMO.md`, failure case |
| 10 | Final review | README, `CAPABILITIES.md`, assignment mapping |

Each phase ends with tests run, a summary, a suggested commit message, and a stop for approval.

## 14. Testing strategy

- **Client:** respx-mocked 200 / 400 / 401 / 403 / 404 / 429 (with and without `Retry-After`) / 5xx / timeout / connection error; key-not-leaked assertions.
- **Service:** fake client injected; normalization, label↔code mapping, validation, pagination contract, keyword-cap behavior.
- **MCP:** exactly three read-only tools; schemas; invalid input handling; tools delegate to the service.
- **Rule:** zero real network calls in the automated suite.

## 15. Phase 1 acceptance criteria

- [x] Assignment requirements mapped (§1)
- [x] Business problem, users, target questions defined (§2–4)
- [x] Scope, non-goals, read-only boundary defined (§5–6)
- [x] Credential handling rules defined (§7)
- [x] Search strategy decided, pending Phase 2 verification (§8)
- [x] Risks and assumptions listed (§9–10)
- [x] Stack decisions recorded: no FastAPI, `uv`, official MCP SDK (§11)
- [x] Repository structure and phase plan agreed (§12–13)
