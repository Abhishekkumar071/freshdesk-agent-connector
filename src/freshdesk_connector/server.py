"""MCP server exposing three read-only Freshdesk ticket tools.

Tools only validate arguments (via their schemas), call the TicketService and
translate ConnectorError into ToolError. There is no Freshdesk or HTTP logic here.
"""

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date
from typing import Annotated, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .errors import ConnectorError
from .models import SearchResult, TicketDetail, TicketPage
from .service import (
    KEYWORD_MAX_CHARS,
    KEYWORD_MIN_CHARS,
    LIST_MAX_PAGE,
    LIST_MAX_PAGE_SIZE,
    SCAN_MAX_TICKETS,
    SEARCH_MAX_PAGE,
    TicketService,
)

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[], AbstractAsyncContextManager[TicketService]]
T = TypeVar("T")

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

INSTRUCTIONS = (
    "Read-only access to this merchant's Freshdesk support tickets. "
    "You can list, look up and search tickets; you cannot create, update, reply to or close them. "
    "Always check a result's `notes`, and when `exhaustive` is false, tell the user the answer may be incomplete."
)

# Tool descriptions are what the agent reads to pick a tool, so they live here as
# plain text rather than as indented docstrings.

LIST_TICKETS_DESCRIPTION = """List recent Freshdesk tickets, newest first by default.

Use for "latest" or "recent" tickets when no status or priority filter is
needed. It cannot filter by status or priority; use search_tickets for that.
Freshdesk only lists tickets created in the last 30 days unless
updated_since is given. Returns ticket summaries; use get_ticket for details."""

GET_TICKET_DESCRIPTION = """Get full details of one Freshdesk ticket by its ID.

Use when the user names a specific ticket or you need more than a summary.
Includes the plain-text description (truncated at 2,000 characters),
requester name and email, and first-response / resolved / closed times.
Does not include the conversation thread or attachments."""

SEARCH_TICKETS_DESCRIPTION = f"""Search Freshdesk tickets by status, priority, tag, type and creation date.

Use for questions like "unresolved tickets", "high-priority open tickets" or
"oldest unresolved tickets". Give at least one filter or a keyword. Pages hold 30 tickets.

Freshdesk cannot search text, so `keyword` is matched by this connector over at
most {SCAN_MAX_TICKETS} tickets that match the other filters (search_mode
"keyword_scan"). order="oldest_first" also sorts at most {SCAN_MAX_TICKETS}
tickets in the connector. With a keyword or oldest_first, only pages 1-3 exist.
In both cases `scanned` and `exhaustive` are set: if `exhaustive` is false, more
matching tickets may exist, so tell the user, or follow the note's suggestion
(usually created_before) to continue the search.

If the account tags or types its tickets (for example tag "payment"), filtering by
`tag` or `ticket_type` is complete and cheaper than a keyword scan."""


def create_server(service_factory: ServiceFactory) -> MCPServer:
    """Build the MCP server. `service_factory` opens the TicketService for the
    server's lifetime (real Freshdesk in production, a fake in tests)."""

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[TicketService]:
        async with service_factory() as service:
            yield service

    mcp = MCPServer("freshdesk-tickets", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool(annotations=READ_ONLY, description=LIST_TICKETS_DESCRIPTION)
    async def list_tickets(
        ctx: Context[TicketService],
        order_by: Annotated[
            Literal["created_at", "updated_at"],
            Field(description="Timestamp to sort by."),
        ] = "created_at",
        order: Annotated[
            Literal["newest_first", "oldest_first"],
            Field(description="Sort direction."),
        ] = "newest_first",
        page: Annotated[int, Field(ge=1, le=LIST_MAX_PAGE)] = 1,
        page_size: Annotated[int, Field(ge=1, le=LIST_MAX_PAGE_SIZE)] = 20,
        updated_since: Annotated[
            date | None,
            Field(description="Only tickets updated on or after this date (YYYY-MM-DD). "
                              "Also lifts Freshdesk's 30-day default window."),
        ] = None,
    ) -> TicketPage:
        return await _call(ctx, "list_tickets", lambda s: s.list_tickets(
            order_by=order_by, order=order, page=page, page_size=page_size,
            updated_since=updated_since,
        ))

    @mcp.tool(annotations=READ_ONLY, description=GET_TICKET_DESCRIPTION)
    async def get_ticket(
        ctx: Context[TicketService],
        ticket_id: Annotated[int, Field(ge=1, description="Numeric Freshdesk ticket ID, e.g. 12345.")],
    ) -> TicketDetail:
        return await _call(ctx, "get_ticket", lambda s: s.get_ticket(ticket_id))

    @mcp.tool(annotations=READ_ONLY, description=SEARCH_TICKETS_DESCRIPTION)
    async def search_tickets(
        ctx: Context[TicketService],
        status: Annotated[
            list[str] | None,
            Field(description='Status labels, e.g. ["unresolved"] (every status except resolved and '
                              'closed, including custom ones), ["open"], ["pending"], ["resolved"], '
                              '["closed"]. Accounts may have custom statuses such as '
                              '"waiting_on_customer"; an unknown value returns the list of valid ones.'),
        ] = None,
        priority: Annotated[
            list[Literal["low", "medium", "high", "urgent"]] | None,
            Field(description='e.g. ["high", "urgent"].'),
        ] = None,
        tag: Annotated[str | None, Field(max_length=64, description="Exact ticket tag.")] = None,
        ticket_type: Annotated[
            str | None, Field(max_length=64, description='Exact ticket type, e.g. "Refund".')
        ] = None,
        created_after: Annotated[date | None, Field(description="Created on or after (YYYY-MM-DD).")] = None,
        created_before: Annotated[date | None, Field(description="Created on or before (YYYY-MM-DD).")] = None,
        keyword: Annotated[
            str | None,
            Field(min_length=KEYWORD_MIN_CHARS, max_length=KEYWORD_MAX_CHARS,
                  description="Words that must all appear in the subject or description (case-insensitive "
                              "substring match, no stemming: use short stems, e.g. \"fail\" matches "
                              "\"failed\" and \"failure\")."),
        ] = None,
        order: Annotated[
            Literal["newest_first", "oldest_first"],
            Field(description="Sort by ticket creation time."),
        ] = "newest_first",
        page: Annotated[int, Field(ge=1, le=SEARCH_MAX_PAGE)] = 1,
    ) -> SearchResult:
        return await _call(ctx, "search_tickets", lambda s: s.search_tickets(
            status=status or (), priority=priority or (), tag=tag, ticket_type=ticket_type,
            created_after=created_after, created_before=created_before,
            keyword=keyword, order=order, page=page,
        ))

    return mcp


async def _call(
    ctx: Context[TicketService], tool: str, operation: Callable[[TicketService], Awaitable[T]]
) -> T:
    """Run a service operation; turn expected failures into agent-readable tool errors.

    Logs one line per call with the outcome and duration. Arguments and results are
    not logged: they can contain customer data.
    """
    service = ctx.request_context.lifespan_context
    started = time.monotonic()
    outcome = "unexpected_error"  # overwritten unless an unexpected exception escapes
    try:
        result = await operation(service)
        outcome = "ok"
        return result
    except ConnectorError as exc:
        outcome = type(exc).__name__
        raise ToolError(exc.message) from exc
    finally:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        logger.info("tool=%s outcome=%s ms=%d", tool, outcome, elapsed_ms)
