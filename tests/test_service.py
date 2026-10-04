"""Service tests run against a fake client: no HTTP, no Freshdesk account."""

from datetime import UTC, date, datetime, timedelta

import pytest

from freshdesk_connector.client import TicketListPage, TicketSearchPage
from freshdesk_connector.errors import (
    AuthenticationFailed,
    InvalidInput,
    InvalidRequest,
    NetworkError,
    NotFound,
    RateLimited,
    RequestTimeout,
    ServiceUnavailable,
)
from freshdesk_connector.service import (
    NOTE_BUILTIN_STATUSES,
    NOTE_LIST_30_DAYS,
    SCAN_MAX_TICKETS,
    TicketService,
    create_ticket_service,
)
from freshdesk_connector.statuses import StatusCatalog

pytestmark = pytest.mark.anyio

DOMAIN = "acme.freshdesk.com"
TODAY = date(2026, 10, 4)
STATUSES = StatusCatalog({2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed", 6: "Waiting on Customer"})
BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def raw_ticket(ticket_id: int, *, hours: int | None = None, status: int = 2, priority: int = 2,
               subject: str = "Subject", description_text: str = "") -> dict:
    """A minimal raw Freshdesk ticket; `hours` after BASE_TIME sets created_at (default: id)."""
    created = (BASE_TIME + timedelta(hours=ticket_id if hours is None else hours)).isoformat()
    return {
        "id": ticket_id, "subject": subject, "status": status, "priority": priority,
        "type": None, "tags": [], "created_at": created, "updated_at": created,
        "requester_id": 1, "responder_id": None, "group_id": None,
        "description": f"<p>{description_text}</p>", "description_text": description_text,
    }


class FakeClient:
    """Records calls. Search serves `search_results` 30 per page, in the given order."""

    def __init__(self, *, list_page=None, ticket=None, search_results=(), total=None,
                 fields=None, error=None, page_errors=None, on_search=None):
        self.list_page = list_page or TicketListPage(tickets=[], has_next=False)
        self.ticket = ticket
        self.search_results = list(search_results)
        self.total = len(self.search_results) if total is None else total
        self.fields = fields
        self.error = error
        self.page_errors = page_errors or {}  # search page number -> exception to raise
        self.on_search = on_search  # called before each search, e.g. to advance a fake clock
        self.calls: list[tuple] = []

    async def list_tickets(self, **kwargs):
        self.calls.append(("list", kwargs))
        return self.list_page

    async def get_ticket(self, ticket_id, include=()):
        self.calls.append(("get", ticket_id, tuple(include)))
        if self.error:
            raise self.error
        return self.ticket

    async def search_tickets(self, query, page=1):
        self.calls.append(("search", query, page))
        if self.on_search:
            self.on_search()
        if page in self.page_errors:
            raise self.page_errors[page]
        start = (page - 1) * 30
        return TicketSearchPage(tickets=self.search_results[start:start + 30], total=self.total)

    async def list_ticket_fields(self):
        self.calls.append(("fields",))
        if self.error:
            raise self.error
        return self.fields

    @property
    def search_calls(self):
        return [c for c in self.calls if c[0] == "search"]


def make_service(client: FakeClient) -> TicketService:
    return TicketService(client, DOMAIN, STATUSES, today=lambda: TODAY)


# --- startup ---------------------------------------------------------------------


async def test_create_service_loads_custom_statuses():
    fields = [{"name": "status", "choices": {"2": ["Open", "Open"], "6": ["Waiting on Customer", "x"]}}]
    service = await create_ticket_service(FakeClient(fields=fields), DOMAIN)
    assert service.statuses.labels == ["unresolved", "open", "waiting_on_customer"]


async def test_create_service_falls_back_when_statuses_unavailable():
    client = FakeClient(error=AuthenticationFailed("nope"))
    service = await create_ticket_service(client, DOMAIN)
    assert service.statuses.labels == ["unresolved", "open", "pending", "resolved", "closed"]


# --- list_tickets ---------------------------------------------------------------


async def test_list_maps_arguments_to_client():
    client = FakeClient(list_page=TicketListPage(tickets=[raw_ticket(1)], has_next=True))

    page = await make_service(client).list_tickets(
        order_by="updated_at", order="oldest_first", page=2, page_size=10
    )

    assert client.calls == [("list", {
        "page": 2, "per_page": 10, "order_by": "updated_at", "order_type": "asc", "updated_since": None,
    })]
    assert [t.id for t in page.tickets] == [1]
    assert page.has_more is True
    assert page.notes == [NOTE_LIST_30_DAYS]


async def test_list_updated_since_is_midnight_utc_and_drops_30_day_note():
    client = FakeClient()

    page = await make_service(client).list_tickets(updated_since=date(2025, 6, 1))

    assert client.calls[0][1]["updated_since"] == datetime(2025, 6, 1, tzinfo=UTC)
    assert client.calls[0][1]["order_type"] == "desc"
    assert page.notes == []


async def test_list_stops_at_page_cap():
    client = FakeClient(list_page=TicketListPage(tickets=[raw_ticket(1)], has_next=True))

    page = await make_service(client).list_tickets(page=10)

    assert page.has_more is False
    assert any("last page" in n for n in page.notes)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": 0}, {"page": 11}, {"page_size": 0}, {"page_size": 51}, {"page": True},
        {"order_by": "status"}, {"order": "random"},
    ],
)
async def test_list_rejects_bad_arguments_without_calling_client(kwargs):
    client = FakeClient()
    with pytest.raises(InvalidInput):
        await make_service(client).list_tickets(**kwargs)
    assert client.calls == []


# --- get_ticket -----------------------------------------------------------------


async def test_get_ticket_requests_requester_and_stats():
    client = FakeClient(ticket=raw_ticket(7) | {"source": 1, "requester": {"id": 1, "name": "A", "email": "a@example.com"}})

    ticket = await make_service(client).get_ticket(7)

    assert client.calls == [("get", 7, ("requester", "stats"))]
    assert ticket.id == 7
    assert ticket.requester.email == "a@example.com"


@pytest.mark.parametrize("bad", [0, -1, True, "7"])
async def test_get_ticket_rejects_bad_ids(bad):
    client = FakeClient()
    with pytest.raises(InvalidInput):
        await make_service(client).get_ticket(bad)
    assert client.calls == []


async def test_get_ticket_propagates_not_found():
    client = FakeClient(error=NotFound("Ticket 7 was not found."))
    with pytest.raises(NotFound):
        await make_service(client).get_ticket(7)


# --- search: validation and query building ----------------------------------------


async def test_search_requires_a_filter_or_keyword():
    client = FakeClient()
    with pytest.raises(InvalidInput):
        await make_service(client).search_tickets()
    assert client.calls == []


async def test_search_unresolved_includes_custom_statuses():
    client = FakeClient(search_results=[raw_ticket(1)])

    await make_service(client).search_tickets(status=["unresolved"], priority=["high", "urgent"])

    assert client.search_calls[0][1] == (
        "(status:2 OR status:3 OR status:6) AND (priority:3 OR priority:4)"
    )


async def test_search_accepts_single_string_status():
    client = FakeClient(search_results=[raw_ticket(1)])
    await make_service(client).search_tickets(status="open")
    assert client.search_calls[0][1] == "status:2"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": ["escalated"]},
        {"priority": ["critical"]},
        {"tag": "x' OR tag:'y"},
        {"status": ["open"], "order": "random"},
        {"status": ["open"], "page": 11},
        {"keyword": "x"},
        {"keyword": "   "},
        {"keyword": "y" * 101},
        {"keyword": "refund", "page": 4},
        {"status": ["open"], "order": "oldest_first", "page": 4},
    ],
)
async def test_search_rejects_bad_arguments_without_calling_client(kwargs):
    client = FakeClient()
    with pytest.raises(InvalidInput):
        await make_service(client).search_tickets(**kwargs)
    assert client.calls == []


# --- search: native newest-first ----------------------------------------------------


async def test_native_search_pages_and_has_more():
    tickets = [raw_ticket(i) for i in range(100, 0, -1)]  # 100 tickets, newest first
    client = FakeClient(search_results=tickets)
    service = make_service(client)

    first = await service.search_tickets(status=["open"])
    last = await service.search_tickets(status=["open"], page=4)

    assert first.search_mode == "native"
    assert [t.id for t in first.tickets][:3] == [100, 99, 98]
    assert len(first.tickets) == 30 and first.has_more is True
    assert len(last.tickets) == 10 and last.has_more is False
    assert first.total == 100
    assert first.scanned is None and first.exhaustive is None and first.notes == []


async def test_native_search_sorts_page_newest_first():
    client = FakeClient(search_results=[raw_ticket(1), raw_ticket(3), raw_ticket(2)])
    result = await make_service(client).search_tickets(status=["open"])
    assert [t.id for t in result.tickets] == [3, 2, 1]


async def test_native_search_notes_300_result_cap():
    client = FakeClient(search_results=[raw_ticket(i) for i in range(1, 31)], total=450)

    result = await make_service(client).search_tickets(status=["open"], page=10)

    assert result.has_more is False
    assert "450 tickets match" in result.notes[0]


# --- search: oldest-first (connector-side sort, bounded) ---------------------------


async def test_oldest_first_is_exact_when_all_matches_fit():
    # Server order deliberately scrambled: the result must not depend on it.
    ids = [5, 1, 4, 2, 3]
    client = FakeClient(search_results=[raw_ticket(i) for i in ids])

    result = await make_service(client).search_tickets(status=["unresolved"], order="oldest_first")

    assert [t.id for t in result.tickets] == [1, 2, 3, 4, 5]
    assert result.exhaustive is True
    assert result.scanned == 5 and result.max_scan == SCAN_MAX_TICKETS
    assert result.notes == []
    assert len(client.search_calls) == 1


async def test_oldest_first_scans_multiple_pages_and_paginates_locally():
    client = FakeClient(search_results=[raw_ticket(i) for i in range(75, 0, -1)])
    service = make_service(client)

    page1 = await service.search_tickets(status=["open"], order="oldest_first")
    page3 = await service.search_tickets(status=["open"], order="oldest_first", page=3)

    assert [c[2] for c in client.search_calls] == [1, 2, 3, 1, 2, 3]
    assert [t.id for t in page1.tickets][:2] == [1, 2] and page1.has_more is True
    assert [t.id for t in page3.tickets] == list(range(61, 76)) and page3.has_more is False
    assert page1.exhaustive is True


async def test_oldest_first_is_flagged_non_exhaustive_beyond_scan_limit():
    client = FakeClient(search_results=[raw_ticket(i) for i in range(1, 201)], total=200)

    result = await make_service(client).search_tickets(status=["open"], order="oldest_first")

    assert len(client.search_calls) == 3  # never more than the scan limit
    assert result.scanned == 90 and result.total == 200
    assert result.exhaustive is False
    assert result.notes[0].startswith("Not exhaustive")


async def test_oldest_first_with_no_matches():
    client = FakeClient(search_results=[])
    result = await make_service(client).search_tickets(status=["open"], order="oldest_first")
    assert result.tickets == [] and result.exhaustive is True and result.has_more is False


# --- search: keyword scan -------------------------------------------------------------


async def test_keyword_matches_subject_and_description_case_insensitively():
    client = FakeClient(search_results=[
        raw_ticket(1, subject="Payment FAILED at checkout"),
        raw_ticket(2, subject="Refund", description_text="card payment failed twice"),
        raw_ticket(3, subject="Payment received", description_text="all good"),
        raw_ticket(4, subject="Login issue"),
    ])

    result = await make_service(client).search_tickets(status=["open"], keyword="payment failed")

    assert result.search_mode == "keyword_scan"
    assert [t.id for t in result.tickets] == [2, 1]
    assert result.keyword == "payment failed"
    assert (result.scanned, result.matched, result.total) == (4, 2, 4)
    assert result.exhaustive is True
    assert not any(n.startswith("Not exhaustive") for n in result.notes)
    assert client.search_calls[0][1] == "status:2"


async def test_keyword_falls_back_to_html_description():
    ticket = raw_ticket(1) | {"description_text": None, "description": "<b>UPI</b> timeout"}
    client = FakeClient(search_results=[ticket])
    result = await make_service(client).search_tickets(status=["open"], keyword="upi timeout")
    assert result.matched == 1


async def test_keyword_only_uses_365_day_window():
    client = FakeClient(search_results=[raw_ticket(1, subject="refund")])

    result = await make_service(client).search_tickets(keyword="refund")

    assert client.search_calls[0][1] == "created_at:>'2025-10-04'"
    assert "last 365 days" in result.notes[0]


async def test_keyword_scan_is_bounded_and_flagged_non_exhaustive():
    tickets = [raw_ticket(i, subject="refund" if i % 2 else "other") for i in range(1, 301)]
    client = FakeClient(search_results=tickets, total=1200)

    result = await make_service(client).search_tickets(status=["open"], keyword="refund")

    assert len(client.search_calls) == 3
    assert result.scanned == 90 and result.matched == 45 and result.total == 1200
    assert result.exhaustive is False
    assert any(n.startswith("Not exhaustive") for n in result.notes)
    assert len(result.tickets) == 30 and result.has_more is True


async def test_keyword_oldest_first_order():
    client = FakeClient(search_results=[raw_ticket(i, subject="refund") for i in (3, 1, 2)])
    result = await make_service(client).search_tickets(status=["open"], keyword="refund", order="oldest_first")
    assert [t.id for t in result.tickets] == [1, 2, 3]


async def test_keyword_no_matches():
    client = FakeClient(search_results=[raw_ticket(1, subject="hello")])
    result = await make_service(client).search_tickets(status=["open"], keyword="refund")
    assert result.tickets == [] and result.matched == 0 and result.exhaustive is True


# --- reliability: partial scans, time budget, duplicates --------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RateLimited("limit", retry_after=30), "rate limit was reached"),
        (ServiceUnavailable("down"), "temporarily unavailable"),
        (RequestTimeout("slow"), "timed out"),
        (NetworkError("unreachable"), "could not be reached"),
    ],
)
async def test_scan_keeps_earlier_pages_when_a_later_page_fails(error, reason):
    client = FakeClient(search_results=[raw_ticket(i) for i in range(1, 91)], page_errors={2: error})

    result = await make_service(client).search_tickets(status=["open"], order="oldest_first")

    assert result.scanned == 30 and result.total == 90
    assert [t.id for t in result.tickets][:2] == [1, 2]
    assert result.exhaustive is False
    assert result.notes[0].startswith("Not exhaustive: the scan stopped after 30 of 90 tickets")
    assert reason in result.notes[0]


async def test_keyword_scan_also_returns_partial_results():
    tickets = [raw_ticket(i, subject="refund") for i in range(1, 91)]
    client = FakeClient(search_results=tickets, page_errors={3: RateLimited("limit")})

    result = await make_service(client).search_tickets(status=["open"], keyword="refund")

    assert (result.scanned, result.matched, result.exhaustive) == (60, 60, False)
    assert "rate limit" in result.notes[0]


async def test_scan_raises_when_first_page_fails():
    client = FakeClient(search_results=[raw_ticket(1)], page_errors={1: RateLimited("limit")})
    with pytest.raises(RateLimited):
        await make_service(client).search_tickets(status=["open"], order="oldest_first")


async def test_scan_raises_permanent_errors_on_later_pages():
    client = FakeClient(search_results=[raw_ticket(i) for i in range(1, 91)],
                        page_errors={2: InvalidRequest("bad")})
    with pytest.raises(InvalidRequest):
        await make_service(client).search_tickets(status=["open"], keyword="subject")


async def test_scan_stops_at_time_budget():
    fake_clock = FakeClock()

    def slow_page():
        fake_clock.now += 10  # each page "takes" 10 seconds

    client = FakeClient(search_results=[raw_ticket(i) for i in range(1, 91)], on_search=slow_page)
    service = TicketService(client, DOMAIN, STATUSES, today=lambda: TODAY, monotonic=fake_clock)

    result = await service.search_tickets(status=["open"], order="oldest_first")

    assert len(client.search_calls) == 2  # page 3 not started: 20s used > 15s budget
    assert result.scanned == 60 and result.exhaustive is False
    assert "time limit" in result.notes[0]


async def test_scan_short_page_below_total_is_reported_as_changed_results():
    # Freshdesk says 40 match but page 2 holds only 5 (e.g. tickets changed while paging).
    client = FakeClient(search_results=[raw_ticket(i) for i in range(1, 36)], total=40)

    result = await make_service(client).search_tickets(status=["open"], order="oldest_first")

    assert result.scanned == 35 and result.exhaustive is False
    assert "results changed while paging" in result.notes[0]
    assert "limit" not in result.notes[0]


async def test_scan_deduplicates_tickets_that_shift_between_pages():
    page1 = [raw_ticket(i) for i in range(60, 30, -1)]  # ids 60..31
    page2 = [raw_ticket(31)] + [raw_ticket(i) for i in range(30, 1, -1)]  # 31 again, then 30..2
    client = FakeClient(search_results=page1 + page2 + [raw_ticket(1)], total=60)
    service = make_service(client)

    first = await service.search_tickets(status=["open"], order="oldest_first")
    second = await service.search_tickets(status=["open"], order="oldest_first", page=2)

    ids = [t.id for t in first.tickets + second.tickets]
    assert len(ids) == len(set(ids)) == 60
    assert first.scanned == 60 and first.exhaustive is True


# --- reliability: status catalog recovery --------------------------------------------------

CUSTOM_FIELDS = [{"name": "status", "choices": {"2": ["Open", "Open"], "3": ["Pending", "Pending"],
                                                "6": ["Waiting on Customer", "x"]}}]


def fields_calls(client: FakeClient) -> int:
    return sum(c[0] == "fields" for c in client.calls)


async def test_failed_status_load_is_retried_later_and_noted_meanwhile():
    fake_clock = FakeClock()
    client = FakeClient(search_results=[raw_ticket(1, status=6)], fields=CUSTOM_FIELDS,
                        error=ServiceUnavailable("down"))
    service = TicketService(client, DOMAIN, StatusCatalog.default(), today=lambda: TODAY,
                            statuses_loaded=False, monotonic=fake_clock)
    await service.load_statuses()  # startup attempt fails

    degraded = await service.search_tickets(status=["unresolved"])
    assert degraded.notes[0] == NOTE_BUILTIN_STATUSES
    assert degraded.tickets[0].status == "status_6"
    assert client.search_calls[-1][1] == "(status:2 OR status:3)"
    assert fields_calls(client) == 1  # too soon to retry

    client.error = None  # Freshdesk recovers
    fake_clock.now += 61
    recovered = await service.search_tickets(status=["unresolved"])

    assert fields_calls(client) == 2
    assert recovered.notes == []
    assert recovered.tickets[0].status == "waiting_on_customer"
    assert client.search_calls[-1][1] == "(status:2 OR status:3 OR status:6)"

    fake_clock.now += 600
    await service.search_tickets(status=["open"])
    assert fields_calls(client) == 2  # loaded once; never reloaded


async def test_list_and_get_load_statuses_on_first_use():
    client = FakeClient(ticket=raw_ticket(1, status=6) | {"source": 1}, fields=CUSTOM_FIELDS)
    service = TicketService(client, DOMAIN, StatusCatalog.default(), statuses_loaded=False)

    page = await service.list_tickets()
    ticket = await service.get_ticket(1)

    assert page.notes == [NOTE_LIST_30_DAYS]
    assert ticket.status == "waiting_on_customer"
    assert fields_calls(client) == 1
