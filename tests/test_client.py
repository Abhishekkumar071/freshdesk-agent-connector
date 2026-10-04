import base64
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from conftest import load_fixture
from freshdesk_connector.client import FreshdeskClient, build_http_client
from freshdesk_connector.config import Settings
from freshdesk_connector.errors import (
    AuthenticationFailed,
    ConnectorError,
    InvalidInput,
    InvalidRequest,
    NetworkError,
    NotFound,
    PermissionDenied,
    RateLimited,
    RequestTimeout,
    ServiceUnavailable,
    UnexpectedResponse,
)

pytestmark = pytest.mark.anyio

FAKE_KEY = "fake-key-should-never-leak-123"
BASE_URL = "https://acme.freshdesk.com/api/v2"


class RecordingSleep:
    """Stands in for asyncio.sleep so retry tests run instantly."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, domain="acme", api_key=FAKE_KEY)


@pytest.fixture
def sleep() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
async def client(settings, sleep):
    async with build_http_client(settings) as http:
        yield FreshdeskClient(http, max_attempts=3, max_retry_wait_seconds=10, sleep=sleep)


@pytest.fixture
def api():
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


def query_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


# --- successful calls --------------------------------------------------------


async def test_list_tickets_sends_params_and_parses_page(api, client):
    route = api.get("/tickets").respond(200, json=load_fixture("tickets_list.json"))

    page = await client.list_tickets(page=2, per_page=50, order_by="updated_at", order_type="asc")

    assert [t["id"] for t in page.tickets] == [12346, 12345]
    assert page.has_next is False
    assert query_of(route.calls.last.request) == {
        "page": ["2"],
        "per_page": ["50"],
        "order_by": ["updated_at"],
        "order_type": ["asc"],
    }


async def test_list_tickets_detects_next_page_from_link_header(api, client):
    api.get("/tickets").respond(
        200,
        json=load_fixture("tickets_list.json"),
        headers={"Link": f'<{BASE_URL}/tickets?page=2>; rel="next"'},
    )

    page = await client.list_tickets()

    assert page.has_next is True


async def test_list_tickets_formats_updated_since_as_utc(api, client):
    route = api.get("/tickets").respond(200, json=[])
    ist = timezone(timedelta(hours=5, minutes=30))

    await client.list_tickets(updated_since=datetime(2026, 1, 1, 5, 30, tzinfo=ist))

    assert query_of(route.calls.last.request)["updated_since"] == ["2026-01-01T00:00:00Z"]


async def test_list_tickets_treats_naive_updated_since_as_utc(api, client):
    route = api.get("/tickets").respond(200, json=[])

    await client.list_tickets(updated_since=datetime(2026, 1, 1))

    assert query_of(route.calls.last.request)["updated_since"] == ["2026-01-01T00:00:00Z"]


async def test_get_ticket_with_includes(api, client):
    route = api.get("/tickets/12345").respond(200, json=load_fixture("ticket_view.json"))

    ticket = await client.get_ticket(12345, include=["requester", "stats"])

    assert ticket["id"] == 12345
    assert ticket["requester"]["email"] == "customer@example.com"
    assert query_of(route.calls.last.request) == {"include": ["requester,stats"]}


async def test_search_tickets_quotes_query_and_parses_total(api, client):
    route = api.get("/search/tickets").respond(200, json=load_fixture("search_tickets.json"))

    page = await client.search_tickets("status:2 AND priority:3", page=2)

    assert page.total == 1
    assert page.tickets[0]["id"] == 12345
    params = query_of(route.calls.last.request)
    assert params["query"] == ['"status:2 AND priority:3"']
    assert params["page"] == ["2"]


async def test_requests_use_basic_auth_with_api_key(api, client):
    route = api.get("/tickets/1").respond(200, json={"id": 1})

    await client.get_ticket(1)

    expected = "Basic " + base64.b64encode(f"{FAKE_KEY}:X".encode()).decode()
    assert route.calls.last.request.headers["Authorization"] == expected


# --- input guard ---------------------------------------------------------------


@pytest.mark.parametrize("bad_id", [0, -5, True, "12345", "1/../../contacts"])
async def test_get_ticket_rejects_bad_ids_without_calling_freshdesk(api, client, bad_id):
    with pytest.raises(InvalidInput):
        await client.get_ticket(bad_id)
    assert len(api.calls) == 0


# --- permanent errors: mapped, never retried ------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationFailed),
        (403, PermissionDenied),
        (404, NotFound),
        (405, UnexpectedResponse),
        (409, UnexpectedResponse),
        (302, UnexpectedResponse),
    ],
)
async def test_permanent_errors_are_mapped_and_not_retried(api, client, sleep, status, error_type):
    route = api.get("/tickets").respond(status, json={"code": "x", "message": "y"})

    with pytest.raises(error_type):
        await client.list_tickets()

    assert route.call_count == 1
    assert sleep.calls == []


async def test_400_reports_field_level_details(api, client):
    route = api.get("/search/tickets").respond(400, json=load_fixture("error_400_invalid_value.json"))

    with pytest.raises(InvalidRequest) as excinfo:
        await client.search_tickets("status:9")

    assert excinfo.value.message == (
        "Freshdesk rejected the request: status: It should be one of these values: '2,3,4,5'"
    )
    assert route.call_count == 1


async def test_400_without_json_body_still_maps(api, client):
    api.get("/tickets").respond(400, text="<html>bad</html>")

    with pytest.raises(InvalidRequest) as excinfo:
        await client.list_tickets()

    assert excinfo.value.message == "Freshdesk rejected the request: invalid request."


async def test_404_on_get_ticket_names_the_ticket(api, client):
    api.get("/tickets/99999").respond(404)

    with pytest.raises(NotFound) as excinfo:
        await client.get_ticket(99999)

    assert excinfo.value.message == "Ticket 99999 was not found."


# --- rate limiting ------------------------------------------------------------


async def test_429_waits_retry_after_then_succeeds(api, client, sleep):
    route = api.get("/tickets").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json=[]),
        ]
    )

    page = await client.list_tickets()

    assert page.tickets == []
    assert route.call_count == 2
    assert sleep.calls == [2.0]


async def test_429_without_retry_after_uses_backoff(api, client, sleep):
    api.get("/tickets").mock(side_effect=[httpx.Response(429), httpx.Response(200, json=[])])

    await client.list_tickets()

    assert len(sleep.calls) == 1
    assert 0.375 <= sleep.calls[0] <= 0.625  # 0.5s +/- 25% jitter


async def test_429_with_long_retry_after_fails_fast(api, client, sleep):
    route = api.get("/tickets").respond(429, headers={"Retry-After": "45"})

    with pytest.raises(RateLimited) as excinfo:
        await client.list_tickets()

    assert route.call_count == 1
    assert sleep.calls == []
    assert excinfo.value.retry_after == 45
    assert "about 45 seconds" in excinfo.value.message


async def test_429_every_attempt_raises_rate_limited(api, client, sleep):
    route = api.get("/tickets").respond(429, headers={"Retry-After": "1"})

    with pytest.raises(RateLimited) as excinfo:
        await client.list_tickets()

    assert route.call_count == 3
    assert sleep.calls == [1.0, 1.0]
    assert excinfo.value.retry_after == 1


# --- server errors, timeouts, network --------------------------------------------


async def test_5xx_then_success_recovers(api, client, sleep):
    route = api.get("/tickets/1").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json={"id": 1})]
    )

    ticket = await client.get_ticket(1)

    assert ticket == {"id": 1}
    assert route.call_count == 2
    assert len(sleep.calls) == 1


async def test_5xx_on_every_attempt_raises_service_unavailable(api, client, sleep):
    route = api.get("/tickets").respond(503)

    with pytest.raises(ServiceUnavailable):
        await client.list_tickets()

    assert route.call_count == 3
    assert len(sleep.calls) == 2
    assert sleep.calls[1] > sleep.calls[0] * 1.1  # exponential: ~0.5s then ~1s


async def test_501_is_not_retried(api, client):
    route = api.get("/tickets").respond(501)

    with pytest.raises(UnexpectedResponse):
        await client.list_tickets()

    assert route.call_count == 1


async def test_timeout_then_success_recovers(api, client):
    route = api.get("/tickets").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[])]
    )

    await client.list_tickets()

    assert route.call_count == 2


async def test_timeout_every_attempt_raises_request_timeout(api, client):
    route = api.get("/tickets").mock(side_effect=httpx.ConnectTimeout("slow"))

    with pytest.raises(RequestTimeout):
        await client.list_tickets()

    assert route.call_count == 3


async def test_connection_error_raises_network_error(api, client):
    route = api.get("/tickets").mock(side_effect=httpx.ConnectError("name resolution failed"))

    with pytest.raises(NetworkError) as excinfo:
        await client.list_tickets()

    assert route.call_count == 3
    assert excinfo.value.message == "Could not reach Freshdesk."


async def test_single_attempt_configuration_does_not_retry(api, settings, sleep):
    route = api.get("/tickets").respond(503)
    async with build_http_client(settings) as http:
        client = FreshdeskClient(http, max_attempts=1, sleep=sleep)
        with pytest.raises(ServiceUnavailable):
            await client.list_tickets()

    assert route.call_count == 1
    assert sleep.calls == []


# --- malformed responses ---------------------------------------------------------


async def test_invalid_json_raises_unexpected_response(api, client):
    api.get("/tickets").respond(200, text="not json")

    with pytest.raises(UnexpectedResponse):
        await client.list_tickets()


@pytest.mark.parametrize(
    ("path", "body", "call"),
    [
        ("/tickets", {"not": "a list"}, lambda c: c.list_tickets()),
        ("/tickets/1", [1, 2], lambda c: c.get_ticket(1)),
        ("/search/tickets", {"results": []}, lambda c: c.search_tickets("status:2")),
        ("/search/tickets", [], lambda c: c.search_tickets("status:2")),
    ],
)
async def test_wrong_shape_raises_unexpected_response(api, client, path, body, call):
    api.get(path).respond(200, json=body)

    with pytest.raises(UnexpectedResponse):
        await call(client)


# --- logging ---------------------------------------------------------------------


async def test_warns_when_rate_limit_nearly_used(api, client, caplog):
    api.get("/tickets").respond(
        200, json=[], headers={"X-RateLimit-Total": "50", "X-RateLimit-Remaining": "3"}
    )

    with caplog.at_level(logging.INFO, logger="freshdesk_connector"):
        await client.list_tickets()

    assert any("ratelimit_remaining=3" in r.getMessage() for r in caplog.records)
    assert any("nearly used" in r.getMessage() for r in caplog.records)


# --- security --------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"errors": [{"field": "x", "message": "bad"}]}),
        httpx.Response(401),
        httpx.Response(403),
        httpx.Response(404),
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(503),
        httpx.Response(200, text="not json"),
    ],
)
async def test_api_key_never_in_errors_or_logs(api, client, caplog, response):
    api.get("/tickets").mock(return_value=response)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConnectorError) as excinfo:
            await client.list_tickets()

    encoded = base64.b64encode(f"{FAKE_KEY}:X".encode()).decode()
    for secret in (FAKE_KEY, encoded):
        assert secret not in str(excinfo.value)
        assert secret not in repr(excinfo.value)
        assert secret not in caplog.text


def test_client_exposes_only_read_operations():
    public = {name for name in dir(FreshdeskClient) if not name.startswith("_")}
    assert public == {"list_tickets", "get_ticket", "search_tickets"}


async def test_all_requests_are_get(api, client):
    api.get("/tickets").respond(200, json=[])
    api.get("/tickets/1").respond(200, json={"id": 1})
    api.get("/search/tickets").respond(200, json={"results": [], "total": 0})
    api.route().respond(500)  # any non-GET would land here and fail the calls below

    await client.list_tickets()
    await client.get_ticket(1)
    await client.search_tickets("status:2")

    assert {call.request.method for call in api.calls} == {"GET"}
