"""Connector logic between the agent-facing tools and the Freshdesk client.

Validates input, builds Freshdesk queries, applies the pagination and scan
limits, and normalizes results. Has no HTTP or MCP code of its own.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from .client import FreshdeskClient
from .errors import ConnectorError, InvalidInput
from .models import SearchResult, TicketDetail, TicketPage, TicketSummary
from .normalize import PRIORITY_CODES, plain_text, to_detail, to_summary
from .query import build_query
from .statuses import StatusCatalog

logger = logging.getLogger(__name__)

Order = Literal["newest_first", "oldest_first"]

LIST_MAX_PAGE = 10
LIST_MAX_PAGE_SIZE = 50
SEARCH_PAGE_SIZE = 30  # fixed by Freshdesk
SEARCH_MAX_PAGE = 10  # Freshdesk rejects page > 10
SEARCH_MAX_REACHABLE = SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE  # 300
SCAN_MAX_PAGES = 3  # bounded connector-side scans: keyword and oldest-first
SCAN_MAX_TICKETS = SCAN_MAX_PAGES * SEARCH_PAGE_SIZE  # 90
KEYWORD_MIN_CHARS = 2
KEYWORD_MAX_CHARS = 100
KEYWORD_DEFAULT_LOOKBACK_DAYS = 365

NOTE_LIST_30_DAYS = (
    "Freshdesk only lists tickets created in the last 30 days unless updated_since is set."
)


async def create_ticket_service(client: FreshdeskClient, domain: str) -> "TicketService":
    """Load the account's status names, then build the service. If the statuses
    can't be loaded, fall back to the four built-in ones rather than fail startup."""
    try:
        statuses = StatusCatalog.from_ticket_fields(await client.list_ticket_fields())
    except ConnectorError as exc:
        logger.warning("Could not load ticket statuses (%s); using built-in statuses", type(exc).__name__)
        statuses = StatusCatalog.default()
    return TicketService(client, domain, statuses)


class TicketService:
    def __init__(
        self,
        client: FreshdeskClient,
        domain: str,
        statuses: StatusCatalog,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._client = client
        self._domain = domain
        self._statuses = statuses
        self._today = today

    @property
    def statuses(self) -> StatusCatalog:
        return self._statuses

    async def list_tickets(
        self,
        *,
        order_by: Literal["created_at", "updated_at"] = "created_at",
        order: Order = "newest_first",
        page: int = 1,
        page_size: int = 20,
        updated_since: date | None = None,
    ) -> TicketPage:
        _require_choice("order_by", order_by, ("created_at", "updated_at"))
        _require_choice("order", order, ("newest_first", "oldest_first"))
        _require_range("page", page, 1, LIST_MAX_PAGE)
        _require_range("page_size", page_size, 1, LIST_MAX_PAGE_SIZE)

        since = datetime.combine(updated_since, time.min, UTC) if updated_since else None
        result = await self._client.list_tickets(
            page=page,
            per_page=page_size,
            order_by=order_by,
            order_type="asc" if order == "oldest_first" else "desc",
            updated_since=since,
        )

        notes = [] if updated_since else [NOTE_LIST_30_DAYS]
        if result.has_next and page == LIST_MAX_PAGE:
            notes.append(
                f"This is the last page the connector returns (page {LIST_MAX_PAGE}). "
                "Use search_tickets with filters to narrow the results."
            )
        return TicketPage(
            tickets=self._summaries(result.tickets),
            page=page,
            has_more=result.has_next and page < LIST_MAX_PAGE,
            notes=notes,
        )

    async def get_ticket(self, ticket_id: int) -> TicketDetail:
        if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id < 1:
            raise InvalidInput("ticket_id must be a positive integer.")
        raw = await self._client.get_ticket(ticket_id, include=("requester", "stats"))
        return to_detail(raw, self._statuses, self._domain)

    async def search_tickets(
        self,
        *,
        status: Sequence[str] = (),
        priority: Sequence[str] = (),
        tag: str | None = None,
        ticket_type: str | None = None,
        created_after: date | None = None,
        created_before: date | None = None,
        keyword: str | None = None,
        order: Order = "newest_first",
        page: int = 1,
    ) -> SearchResult:
        _require_choice("order", order, ("newest_first", "oldest_first"))
        # A bare string would otherwise be iterated character by character.
        status = [status] if isinstance(status, str) else status
        priority = [priority] if isinstance(priority, str) else priority
        query = build_query(
            status_codes=self._statuses.codes_for(status),
            priority_codes=_priority_codes(priority),
            tag=tag,
            ticket_type=ticket_type,
            created_after=created_after,
            created_before=created_before,
        )

        if keyword is not None:
            return await self._keyword_scan(query, keyword, order, page)
        if not query:
            raise InvalidInput(
                "Give at least one filter (status, priority, tag, ticket_type, "
                "created_after, created_before) or a keyword."
            )
        if order == "oldest_first":
            return await self._oldest_first(query, page)
        return await self._native_page(query, page)

    async def _native_page(self, query: str, page: int) -> SearchResult:
        """Newest first, paged by Freshdesk (30 per page, at most 10 pages)."""
        _require_range("page", page, 1, SEARCH_MAX_PAGE)
        result = await self._client.search_tickets(query, page=page)

        notes = []
        if result.total > SEARCH_MAX_REACHABLE:
            notes.append(
                f"{result.total} tickets match, but Freshdesk search only returns the first "
                f"{SEARCH_MAX_REACHABLE}. Add filters such as created_after/created_before to see the rest."
            )
        return SearchResult(
            search_mode="native",
            tickets=_newest_first(self._summaries(result.tickets)),
            page=page,
            has_more=page * SEARCH_PAGE_SIZE < min(result.total, SEARCH_MAX_REACHABLE),
            total=result.total,
            notes=notes,
        )

    async def _oldest_first(self, query: str, page: int) -> SearchResult:
        """Freshdesk search can't sort, so scan up to SCAN_MAX_TICKETS and sort here."""
        _require_range("page", page, 1, SCAN_MAX_PAGES, when="when order is oldest_first")
        raw, total = await self._scan(query)
        tickets = sorted(self._summaries(raw), key=lambda t: (t.created_at, t.id))
        exhaustive = len(raw) >= total

        notes = []
        if not exhaustive:
            notes.append(
                f"Not exhaustive: {total} tickets match but only {len(raw)} were scanned "
                f"(limit {SCAN_MAX_TICKETS}), so older matching tickets may exist. "
                "Narrow with created_before/created_after for an exact answer."
            )
        return SearchResult(
            search_mode="native",
            tickets=_page_of(tickets, page),
            page=page,
            has_more=page * SEARCH_PAGE_SIZE < len(tickets),
            total=total,
            notes=notes,
            max_scan=SCAN_MAX_TICKETS,
            scanned=len(raw),
            exhaustive=exhaustive,
        )

    async def _keyword_scan(self, query: str, keyword: str, order: Order, page: int) -> SearchResult:
        """Match keyword terms in subject + description over a bounded set of candidates."""
        keyword = keyword.strip()
        if not KEYWORD_MIN_CHARS <= len(keyword) <= KEYWORD_MAX_CHARS:
            raise InvalidInput(
                f"keyword must be {KEYWORD_MIN_CHARS}-{KEYWORD_MAX_CHARS} characters."
            )
        _require_range("page", page, 1, SCAN_MAX_PAGES, when="when a keyword is given")

        notes = []
        if not query:
            since = self._today() - timedelta(days=KEYWORD_DEFAULT_LOOKBACK_DAYS)
            query = build_query(created_after=since)
            notes.append(
                f"No filters were given, so candidates are tickets created in the last "
                f"{KEYWORD_DEFAULT_LOOKBACK_DAYS} days."
            )

        raw, total = await self._scan(query)
        terms = keyword.lower().split()
        matches = [t for t in raw if _matches(t, terms)]
        summaries = self._summaries(matches)
        if order == "oldest_first":
            summaries.sort(key=lambda t: (t.created_at, t.id))
        else:
            summaries = _newest_first(summaries)

        exhaustive = len(raw) >= total
        if not exhaustive:
            notes.append(
                f"Not exhaustive: only {len(raw)} of {total} candidate tickets were scanned "
                f"(limit {SCAN_MAX_TICKETS}), so more matching tickets may exist. "
                "Add filters such as status or a date range to narrow the candidates."
            )
        notes.append(
            "Keyword matching is a case-insensitive text match on subject and description; "
            "every word must appear."
        )
        return SearchResult(
            search_mode="keyword_scan",
            tickets=_page_of(summaries, page),
            page=page,
            has_more=page * SEARCH_PAGE_SIZE < len(summaries),
            total=total,
            notes=notes,
            keyword=keyword,
            max_scan=SCAN_MAX_TICKETS,
            scanned=len(raw),
            matched=len(matches),
            exhaustive=exhaustive,
        )

    async def _scan(self, query: str) -> tuple[list[dict[str, Any]], int]:
        """Fetch search pages until all matches are read or SCAN_MAX_PAGES is reached."""
        collected: list[dict[str, Any]] = []
        total = 0
        for page in range(1, SCAN_MAX_PAGES + 1):
            result = await self._client.search_tickets(query, page=page)
            collected.extend(result.tickets)
            total = result.total
            if len(collected) >= total or len(result.tickets) < SEARCH_PAGE_SIZE:
                break
        return collected, total

    def _summaries(self, raw_tickets: list[dict[str, Any]]) -> list[TicketSummary]:
        return [to_summary(raw, self._statuses, self._domain) for raw in raw_tickets]


def _priority_codes(labels: Sequence[str]) -> list[int]:
    codes = []
    for raw in labels:
        label = raw.strip().lower()
        if label not in PRIORITY_CODES:
            raise InvalidInput(
                f"Unknown priority {raw!r}. Valid values: {', '.join(PRIORITY_CODES)}."
            )
        codes.append(PRIORITY_CODES[label])
    return codes


def _matches(raw: dict[str, Any], terms: list[str]) -> bool:
    text = f"{raw.get('subject') or ''} {plain_text(raw)}".lower()
    return all(term in text for term in terms)


def _newest_first(tickets: list[TicketSummary]) -> list[TicketSummary]:
    return sorted(tickets, key=lambda t: (t.created_at, t.id), reverse=True)


def _page_of(tickets: list[TicketSummary], page: int) -> list[TicketSummary]:
    start = (page - 1) * SEARCH_PAGE_SIZE
    return tickets[start : start + SEARCH_PAGE_SIZE]


def _require_range(name: str, value: int, low: int, high: int, when: str = "") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        suffix = f" {when}" if when else ""
        raise InvalidInput(f"{name} must be an integer from {low} to {high}{suffix}.")


def _require_choice(name: str, value: str, allowed: Sequence[str]) -> None:
    if value not in allowed:
        raise InvalidInput(f"{name} must be one of: {', '.join(allowed)}.")
