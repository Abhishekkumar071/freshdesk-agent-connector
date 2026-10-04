# What the agent can and cannot do

This connector gives an AI agent **read-only** access to one merchant's Freshdesk tickets through three MCP tools: `list_tickets`, `get_ticket` and `search_tickets`. Nothing else is exposed.

## The agent can

**List recent tickets** (`list_tickets`)
- Newest or oldest first, by creation or last-update time.
- Up to 50 per page, 10 pages.
- Only tickets created in the last 30 days, unless `updated_since` is given (a Freshdesk default).

**Read one ticket** (`get_ticket`)
- Subject, status, priority, type, tags, channel and timestamps.
- Plain-text description, cut at 2,000 characters.
- Requester name and email.
- First-response, resolved and closed times.
- A link to the ticket in Freshdesk.

**Search and filter tickets** (`search_tickets`)
- By status, including custom statuses and the shortcut `unresolved` (everything except resolved and closed).
- By priority, tag, ticket type, and creation date range (both ends inclusive).
- By keyword: every word must appear in the subject or description.
- Newest or oldest first.

**Tell when an answer is incomplete**
- Every result has `notes` in plain language.
- Keyword searches and oldest-first sorting read at most 90 tickets. Their results include `scanned`, `max_scan` and `exhaustive`.
- When `exhaustive` is `false`, the note says why: the 90-ticket limit, a rate limit, a timeout, or data changing while paging. It also says what to try next, usually a `created_before` date.
- Freshdesk search returns at most 300 results per query. A note says so when more match.

**Get understandable errors**
- "Ticket 99999 was not found."
- "Freshdesk rate limit reached. Try again in about 45 seconds."
- "Freshdesk authentication failed. Check the connector's API key configuration."
- An unknown status value returns the list of valid ones.

## The agent cannot

| Action | Why it's impossible |
|---|---|
| Create tickets | No such tool, and the HTTP client can only send GET requests |
| Update tickets (status, priority, assignee, fields) | Same |
| Reply to customers or add notes | Same |
| Close, resolve, merge or delete tickets | Same |
| See or use the API key | The key stays in the server process; it never appears in tool output, error messages or logs |
| Change which Freshdesk account it talks to | The domain comes from server configuration, not from tool arguments |
| Read conversations, attachments, custom fields or CC addresses | Not exposed; they add cost and can contain sensitive data |
| See requester IP addresses or activity times | Freshdesk returns them; the connector drops them |
| Search text across all tickets at once | Freshdesk has no text search; keyword matching covers at most 90 tickets per call |
| See contacts, companies, agents, or other Freshdesk objects | Out of scope |

These limits are enforced in code, not by asking the model to behave. An MCP client that calls `create_ticket` gets `Unknown tool`.

## Things to know when relying on the answers

- **"Latest" and "oldest" mean by creation time** in `search_tickets`. `list_tickets` can also sort by update time.
- **Search lags a little:** Freshdesk takes a few minutes to index new or changed tickets.
- **Keyword matching is literal.** "fail" matches "failed" and "failure", but "refund" won't match "money back". If the merchant tags tickets (e.g. `payment`), searching by tag is complete and cheaper.
- **New statuses need a restart.** A status added in Freshdesk while the server is running is shown as `status_<code>` until the server restarts.

## For the person deploying it

- Use an API key from a Freshdesk agent with a **restricted role**. The connector only reads, but a Freshdesk key itself can't be limited to read-only.
- The default stdio transport runs locally for one client. The HTTP transport (`--transport http`) has no authentication of its own and binds to `127.0.0.1`. Put an authenticating proxy in front before exposing it.
