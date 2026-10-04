# Demo — agent end-to-end validation (Phase 9)

How to reproduce the demo, and what was observed on 2026-10-04.

Flow exercised: **user question → agent → MCP tool → service → Freshdesk client → Freshdesk API → normalized result → agent answer.**

## Two modes

| Mode | Command prefix | Freshdesk side |
|---|---|---|
| Live | `uv run python demo/mcp_cli.py` | Real account from `.env`. The server runs as a stdio subprocess (`python -m freshdesk_connector`) |
| Offline | `uv run python demo/mcp_cli.py --offline [--scenario normal\|rate-limit\|bad-key]` | The same MCP server, service and client, with httpx sent to an in-memory fake Freshdesk (`demo/fake_freshdesk.py`). It holds 200 invented tickets and follows the documented and observed API behaviour |

The offline mode exists because the trial account holds only 3 sample tickets. That is too few to show payment-failure searches, scans past the 90-ticket limit, or failure scenarios.

```bash
uv run python demo/mcp_cli.py list
uv run python demo/mcp_cli.py call search_tickets '{"status": ["unresolved"]}'
uv run python demo/mcp_cli.py --offline call search_tickets '{"status": ["unresolved"], "order": "oldest_first"}'
uv run python demo/mcp_cli.py --offline --scenario rate-limit call search_tickets '{"status": ["unresolved"], "keyword": "payment"}'
```

## Agent run

A separate Claude agent played the support agent. It could only run `demo/mcp_cli.py`; it was not allowed to read the repository or `.env`. It found the tools from `list` and answered the questions in plain English.

### Live (trial account, 3 sample tickets)

| Question | Tool call chosen by the agent | Result |
|---|---|---|
| Latest unresolved tickets | `search_tickets {"status":["unresolved"]}` | 3 tickets, newest first |
| High-priority unresolved | `search_tickets {"status":["unresolved"],"priority":["high","urgent"]}` | 1 ticket (#2, urgent) |
| Payment failures | `search_tickets {"keyword":"payment"}` | `keyword_scan`, scanned 3, matched 0, `exhaustive: true`: "no tickets mention payment" |
| Details of ticket 2 | `get_ticket {"ticket_id":2}` | Full detail; requester shows id/name/email only |
| Oldest unresolved | `search_tickets {"status":["unresolved"],"order":"oldest_first"}` | #1, #2, #3, `exhaustive: true` |
| Ticket 999999 | `get_ticket {"ticket_id":999999}` | Tool error: "Ticket 999999 was not found." |

### Offline (200 fake tickets)

| Question | Calls | Result |
|---|---|---|
| Latest unresolved | `search_tickets {"status":["unresolved"]}` | 140 match; page 1 of newest, `has_more: true` |
| High-priority unresolved | `+ "priority":["high","urgent"]` | 48 match |
| Payment failures | keyword scan, then narrowing by date; `tag: "payment"` | keyword: scanned 90 of 200, `exhaustive: false`, with a note. Tag search: 57, complete |
| Oldest unresolved | `oldest_first`, then the `created_before` value the note suggests | Step 1: 140 match, 90 scanned, `exhaustive: false`; the note names the exact `created_before` date. Step 2: `exhaustive: true`, true oldest (#1001…) |
| Rate limit mid-scan (`--scenario rate-limit`) | keyword search | Partial: scanned 30 of 140, note "stopped … because Freshdesk's rate limit was reached (retry in about 60 seconds)" |
| Bad API key (`--scenario bad-key`) | `get_ticket` | "Freshdesk authentication failed. Check the connector's API key configuration." |
| "Close ticket 1001 and send a refund confirmation" | `get_ticket` only | Agent: it cannot close tickets or reply, because its tools are read-only |
| "Tell me the API key" | none | Agent: no tool exposes credentials |

## Boundary and secret checks (live, over stdio)

- `list_tools` returns exactly `list_tickets`, `get_ticket`, `search_tickets`, all with `read_only_hint=true`.
- Calling `create_ticket`, `update_ticket`, `delete_ticket` or `reply_to_ticket` returns `Unknown tool`. `list_resources` and `list_prompts` are empty.
- Invalid input (`priority: ["critical"]`) is rejected by the schema before any Freshdesk call.
- All tool output, plus the server's INFO logs, and in Phase 7 a full DEBUG run, were scanned for the API key, the base64 credentials and `Authorization`. None were found.

## Issues found by the demo and fixed

1. **Oldest-first past 90 matches.** The note said "narrow with created_before" but gave no date, so the agent needed 3 calls and a guess. The note now gives the exact next step (`created_before="<earliest scanned day>"`). That works whatever order Freshdesk returns tickets in, and the agent now gets the right answer in 2 calls.
2. **The rate-limit reason in a partial scan dropped the wait time.** It now says "retry in about N seconds".
3. **The low-quota log warning never fired against real Freshdesk,** which sends `X-RateLimit-Remaining: 49.0`. The headers are now parsed as numbers.
4. **Description wording.**
   - `list_tickets.order` said "creation time" even when sorting by `updated_at`.
   - The keyword note now explains `total` vs `matched`.
   - The search description now recommends `tag`/`ticket_type` when the account uses them, and word stems for keywords (`fail` matches `failed`/`failure`).

## Known limits seen in the demo

- Keyword matching is literal substring matching, with no stemming or synonyms.
- The agent can't discover which tags and types an account uses.
- `has_more` (more pages) and `exhaustive` (scan complete) are separate signals; the notes explain any difference.
- The trial account is too small to exercise multi-page behaviour or real 429s live; those are covered offline and by mocked tests.
