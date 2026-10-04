# Running the demo

The demo shows an agent's view of the connector: which tools exist, what a tool call returns, and how errors and partial results look.

**You don't need a Freshdesk account.** The offline mode runs the real MCP server, service and client, but the HTTP client talks to an in-memory fake Freshdesk ([demo/fake_freshdesk.py](../demo/fake_freshdesk.py)) instead of the network. The fake holds 200 invented tickets and behaves like the real API as documented and observed: 30 results per search page, pages 1–10, newest first, `total`, 401/404/400/429. Only the network is replaced.

## Setup

```bash
uv sync          # Python 3.11+ and uv required
```

## Offline demo (no credentials)

Every command prints exactly what an MCP client would receive: the tool result as JSON, or the tool error text.

**1. What tools does the agent see?**

```bash
uv run python demo/mcp_cli.py --offline list
```

You'll see three tools, each `read_only=True`, with their descriptions and input schemas.

**2. "Show me the latest unresolved tickets."**

```bash
uv run python demo/mcp_cli.py --offline call search_tickets '{"status": ["unresolved"]}'
```

Expect `"total": 140` and the 30 newest unresolved tickets, with `"has_more": true`.

**3. "Find high-priority unresolved tickets."**

```bash
uv run python demo/mcp_cli.py --offline call search_tickets '{"status": ["unresolved"], "priority": ["high", "urgent"]}'
```

**4. "Find tickets related to payment failures."**

```bash
uv run python demo/mcp_cli.py --offline call search_tickets '{"status": ["unresolved"], "keyword": "payment fail"}'
uv run python demo/mcp_cli.py --offline call search_tickets '{"status": ["unresolved"], "tag": "payment"}'
```

The keyword search is a `keyword_scan`: it reads 90 of the 140 candidates and says so (`"exhaustive": false`, plus a note on how to continue). The tag search is answered by Freshdesk itself and is complete.

**5. "Give me the details of ticket 1042."**

```bash
uv run python demo/mcp_cli.py --offline call get_ticket '{"ticket_id": 1042}'
```

The fake sends extra fields (custom fields, CC addresses, the requester's IP address). None of them appear in the output.

**6. "Which unresolved tickets are the oldest?"**

```bash
uv run python demo/mcp_cli.py --offline call search_tickets '{"status": ["unresolved"], "order": "oldest_first"}'
```

With 140 matches, only 90 are scanned, so the result is `"exhaustive": false`. The note ends with something like `search again with created_before="2026-04-29"` (the date depends on when you run it). Run that search:

```bash
uv run python demo/mcp_cli.py --offline call search_tickets '{"status": ["unresolved"], "order": "oldest_first", "created_before": "<date from the note>"}'
```

That result is `"exhaustive": true` and starts at ticket 1001, the true oldest.

**7. Failure cases**

```bash
uv run python demo/mcp_cli.py --offline call get_ticket '{"ticket_id": 5}'
# TOOL ERROR: ... Ticket 5 was not found.

uv run python demo/mcp_cli.py --offline --scenario bad-key call get_ticket '{"ticket_id": 1001}'
# TOOL ERROR: ... Freshdesk authentication failed. Check the connector's API key configuration.

uv run python demo/mcp_cli.py --offline --scenario rate-limit call search_tickets '{"status": ["unresolved"], "keyword": "payment"}'
# partial result: "scanned": 30, "exhaustive": false,
# note: "...because Freshdesk's rate limit was reached (retry in about 60 seconds)..."

uv run python demo/mcp_cli.py --offline call search_tickets '{"priority": ["critical"]}'
# TOOL ERROR: rejected by the input schema before any Freshdesk call
```

Add `--verbose` to any command to see the connector's own log lines (one per HTTP request and one per tool call).

On Windows PowerShell, wrap the JSON in single quotes and escape the inner double quotes (`'{\"ticket_id\": 1042}'`), or run the commands from Git Bash.

## Live demo (your own Freshdesk)

1. Create a Freshdesk **trial** account. The Free plan has no API access.
2. Get your API key from *Profile settings → View API key*.
3. `cp .env.example .env` and fill in `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY`.
4. Run the same commands without `--offline`:

```bash
uv run python demo/mcp_cli.py list
uv run python demo/mcp_cli.py call search_tickets '{"status": ["unresolved"]}'
uv run python demo/mcp_cli.py call get_ticket '{"ticket_id": 1}'
```

In live mode the CLI starts the real server as a subprocess over stdio (`python -m freshdesk_connector`), as an MCP client like Claude Desktop would. A new trial account contains only a few sample tickets, so the larger scenarios (scans past 90, rate limits) are easier to see offline.

## What we observed (2026-10-04)

A separate Claude agent was given only this CLI: no access to the code or `.env`. It answered the five questions from the tool descriptions alone.

| Question | Tool call it chose | Live trial (3 sample tickets) | Offline (200 tickets) |
|---|---|---|---|
| Latest unresolved | `search_tickets {"status":["unresolved"]}` | 3 tickets | 140 match, newest 30 |
| High-priority unresolved | `+ "priority":["high","urgent"]` | 1 ticket (urgent) | 48 match |
| Payment failures | keyword search, then `tag:"payment"` | none mention payment; scan complete | keyword: 90 of 200 scanned, flagged; tag: 57, complete |
| Ticket details | `get_ticket` | full detail, requester id/name/email only | same; extra fields dropped |
| Oldest unresolved | `"order":"oldest_first"` | tickets 1, 2, 3; complete | flagged partial, then exact after following the note |

It also refused "close ticket 1001 and send a refund confirmation" (its tools are read-only) and "tell me the API key" (no tool exposes it).

On the live server over stdio, calls to `create_ticket`, `update_ticket`, `delete_ticket` and `reply_to_ticket` all returned `Unknown tool`, and `list_resources` and `list_prompts` were empty. The tool output and server logs contained no API key, no base64 credentials and no `Authorization` header.

The demo also found four problems, which were fixed:
- the oldest-first note didn't say which date to narrow to;
- partial-scan notes dropped the retry time;
- the low-quota log warning never fired, because Freshdesk sends `49.0`, not `49`;
- one wrong word in a description: `list_tickets.order` said "creation time" even when sorting by update time.
