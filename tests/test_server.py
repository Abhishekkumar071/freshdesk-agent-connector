"""MCP layer tests through the SDK's in-memory client (no subprocess, no network).

Most tests use a fake service to check the tool contract. The last section wires
the real service and client against mocked HTTP, end to end.
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
import respx
from mcp import Client

from conftest import load_fixture
from freshdesk_connector.__main__ import configure_logging, freshdesk_service_factory, main
from freshdesk_connector.config import Settings
from freshdesk_connector.errors import NotFound, RateLimited
from freshdesk_connector.models import SearchResult, TicketDetail, TicketPage, TicketSummary
from freshdesk_connector.server import create_server

pytestmark = pytest.mark.anyio

FAKE_KEY = "fake-key-should-never-leak-123"
NOW = datetime(2026, 10, 1, tzinfo=UTC)
SUMMARY = TicketSummary(
    id=1, subject="Payment failed", status="open", priority="high", type=None, tags=[],
    created_at=NOW, updated_at=NOW, due_by=None, requester_id=9, assigned_agent_id=None,
    group_id=None, url="https://acme.freshdesk.com/a/tickets/1",
)


class FakeService:
    """Records calls; returns canned models or raises `error`."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def _record(self, name, result, **kwargs):
        self.calls.append((name, kwargs))
        if self.error:
            raise self.error
        return result

    async def list_tickets(self, **kwargs):
        return await self._record("list_tickets", TicketPage(tickets=[SUMMARY], page=1, has_more=False, notes=[]), **kwargs)

    async def get_ticket(self, ticket_id):
        detail = TicketDetail(**SUMMARY.model_dump(), source="email", description="text",
                              description_truncated=False, requester=None,
                              first_responded_at=None, resolved_at=None, closed_at=None)
        return await self._record("get_ticket", detail, ticket_id=ticket_id)

    async def search_tickets(self, **kwargs):
        result = SearchResult(search_mode="native", tickets=[SUMMARY], page=1, has_more=False, total=1, notes=[])
        return await self._record("search_tickets", result, **kwargs)


def factory_for(service, events: list[str] | None = None):
    @asynccontextmanager
    async def factory():
        if events is not None:
            events.append("open")
        yield service
        if events is not None:
            events.append("close")

    return factory


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
async def client(service):
    async with Client(create_server(factory_for(service))) as c:
        yield c


def text_of(result) -> str:
    return result.content[0].text


# --- tool surface -----------------------------------------------------------------------


async def test_exactly_three_read_only_tools(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}

    assert set(tools) == {"list_tickets", "get_ticket", "search_tickets"}
    for tool in tools.values():
        assert tool.description and len(tool.description) > 80
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.output_schema is not None


async def test_input_schemas_carry_bounds_and_enums(client):
    tools = {t.name: t.input_schema for t in (await client.list_tools()).tools}

    get_props = tools["get_ticket"]["properties"]
    assert tools["get_ticket"]["required"] == ["ticket_id"]
    assert get_props["ticket_id"]["minimum"] == 1

    list_props = tools["list_tickets"]["properties"]
    assert list_props["page"]["maximum"] == 10
    assert list_props["page_size"]["maximum"] == 50
    assert list_props["order"]["enum"] == ["newest_first", "oldest_first"]
    assert "required" not in tools["list_tickets"]

    search = json.dumps(tools["search_tickets"])
    for expected in ('"low"', '"urgent"', '"maxLength": 100', '"minLength": 2', "unresolved"):
        assert expected in search
    assert "ctx" not in search


async def test_search_description_states_scan_limit(client):
    tool = next(t for t in (await client.list_tools()).tools if t.name == "search_tickets")
    description = " ".join(tool.description.split())
    assert "at most 90 tickets" in description
    assert "exhaustive" in description


# --- tools call the service ---------------------------------------------------------------


async def test_list_tickets_defaults(client, service):
    result = await client.call_tool("list_tickets", {})

    assert result.is_error is False
    assert service.calls == [("list_tickets", {
        "order_by": "created_at", "order": "newest_first", "page": 1, "page_size": 20, "updated_since": None,
    })]
    assert result.structured_content["tickets"][0]["id"] == 1
    assert json.loads(text_of(result))["tickets"][0]["subject"] == "Payment failed"


async def test_list_tickets_parses_date(client, service):
    await client.call_tool("list_tickets", {"updated_since": "2026-01-15", "order": "oldest_first"})
    kwargs = service.calls[0][1]
    assert kwargs["updated_since"] == date(2026, 1, 15)
    assert kwargs["order"] == "oldest_first"


async def test_get_ticket(client, service):
    result = await client.call_tool("get_ticket", {"ticket_id": 12345})

    assert result.is_error is False
    assert service.calls == [("get_ticket", {"ticket_id": 12345})]
    assert result.structured_content["description"] == "text"


async def test_search_tickets_passes_typed_arguments(client, service):
    result = await client.call_tool("search_tickets", {
        "status": ["unresolved"], "priority": ["high", "urgent"], "created_after": "2026-09-01",
        "keyword": "payment failed", "order": "oldest_first",
    })

    assert result.is_error is False
    assert service.calls == [("search_tickets", {
        "status": ["unresolved"], "priority": ["high", "urgent"], "tag": None, "ticket_type": None,
        "created_after": date(2026, 9, 1), "created_before": None,
        "keyword": "payment failed", "order": "oldest_first", "page": 1,
    })]
    assert result.structured_content["search_mode"] == "native"


async def test_search_without_filters_still_reaches_service_validation(client, service):
    await client.call_tool("search_tickets", {})
    assert service.calls[0][1]["status"] == () and service.calls[0][1]["priority"] == ()


# --- invalid input is rejected before the service ----------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_ticket", {}),
        ("get_ticket", {"ticket_id": 0}),
        ("get_ticket", {"ticket_id": "abc"}),
        ("list_tickets", {"page": 11}),
        ("list_tickets", {"page_size": 500}),
        ("list_tickets", {"order_by": "status"}),
        ("list_tickets", {"updated_since": "last week"}),
        ("search_tickets", {"priority": ["critical"]}),
        ("search_tickets", {"keyword": "x"}),
        ("search_tickets", {"tag": "t" * 65}),
        ("search_tickets", {"page": 0}),
    ],
)
async def test_invalid_arguments_return_tool_error(client, service, tool, args):
    result = await client.call_tool(tool, args)

    assert result.is_error is True
    assert service.calls == []


# --- errors -------------------------------------------------------------------------------


async def test_connector_error_becomes_readable_tool_error():
    service = FakeService(error=NotFound("Ticket 99999 was not found."))
    async with Client(create_server(factory_for(service))) as c:
        result = await c.call_tool("get_ticket", {"ticket_id": 99999})

    assert result.is_error is True
    assert text_of(result) == "Error executing tool get_ticket: Ticket 99999 was not found."


async def test_rate_limit_message_reaches_agent():
    service = FakeService(error=RateLimited("Freshdesk rate limit reached. Try again in about 30 seconds.", 30))
    async with Client(create_server(factory_for(service))) as c:
        result = await c.call_tool("list_tickets", {})

    assert result.is_error is True
    assert "about 30 seconds" in text_of(result)


async def test_unexpected_exception_details_are_masked():
    service = FakeService(error=RuntimeError(f"internal detail {FAKE_KEY}"))
    async with Client(create_server(factory_for(service))) as c:
        result = await c.call_tool("search_tickets", {"status": ["open"]})

    assert result.is_error is True
    assert FAKE_KEY not in text_of(result)
    assert "internal detail" not in text_of(result)


async def test_service_is_opened_once_and_closed(service):
    events: list[str] = []
    async with Client(create_server(factory_for(service, events))) as c:
        await c.call_tool("list_tickets", {})
        await c.call_tool("get_ticket", {"ticket_id": 1})
        assert events == ["open"]
    assert events == ["open", "close"]


async def test_each_tool_call_logs_outcome_without_arguments(caplog):
    service = FakeService()
    with caplog.at_level(logging.INFO, logger="freshdesk_connector"):
        async with Client(create_server(factory_for(service))) as c:
            await c.call_tool("search_tickets", {"keyword": "secret-customer-words"})
            service.error = NotFound("Ticket 5 was not found.")
            await c.call_tool("get_ticket", {"ticket_id": 5})
            service.error = RuntimeError("boom")
            await c.call_tool("list_tickets", {})

    lines = [r.getMessage() for r in caplog.records if r.name == "freshdesk_connector.server"]
    assert lines[0].startswith("tool=search_tickets outcome=ok ms=")
    assert lines[1].startswith("tool=get_ticket outcome=NotFound ms=")
    assert lines[2].startswith("tool=list_tickets outcome=unexpected_error ms=")
    assert "secret-customer-words" not in caplog.text


# --- end to end: MCP → service → client → mocked Freshdesk ------------------------------


@pytest.fixture
def settings(monkeypatch):
    return Settings(_env_file=None, domain="acme", api_key=FAKE_KEY)


@pytest.fixture
def freshdesk():
    with respx.mock(base_url="https://acme.freshdesk.com/api/v2", assert_all_called=False) as router:
        router.get("/ticket_fields").respond(200, json=[{
            "name": "status",
            "choices": {"2": ["Open", "Open"], "3": ["Pending", "Pending"], "4": ["Resolved", "Resolved"],
                        "5": ["Closed", "Closed"], "6": ["Waiting on Customer", "x"]},
        }])
        yield router


async def test_end_to_end_unresolved_search(settings, freshdesk):
    route = freshdesk.get("/search/tickets").respond(200, json=load_fixture("search_tickets.json"))

    async with Client(create_server(freshdesk_service_factory(settings))) as c:
        result = await c.call_tool("search_tickets", {"status": ["unresolved"], "priority": ["high"]})

    assert result.is_error is False
    assert route.calls.last.request.url.params["query"] == '"(status:2 OR status:3 OR status:6) AND priority:3"'
    ticket = result.structured_content["tickets"][0]
    assert ticket == {**ticket, "id": 12345, "status": "open", "priority": "high"}
    assert "description" not in ticket and "custom_fields" not in ticket


async def test_end_to_end_get_ticket_hides_secrets_and_extra_pii(settings, freshdesk):
    body = load_fixture("ticket_view.json")
    body["requester"]["ip_address"] = "203.0.113.7"
    freshdesk.get("/tickets/12345").respond(200, json=body)

    async with Client(create_server(freshdesk_service_factory(settings))) as c:
        result = await c.call_tool("get_ticket", {"ticket_id": 12345})

    assert result.structured_content["requester"] == {"id": 9001, "name": "Test Customer", "email": "customer@example.com"}
    assert "203.0.113.7" not in text_of(result)
    assert FAKE_KEY not in text_of(result)


async def test_end_to_end_auth_failure(settings, freshdesk):
    freshdesk.get("/tickets/1").respond(401)

    async with Client(create_server(freshdesk_service_factory(settings))) as c:
        result = await c.call_tool("get_ticket", {"ticket_id": 1})

    assert result.is_error is True
    assert text_of(result).endswith("Freshdesk authentication failed. Check the connector's API key configuration.")
    assert FAKE_KEY not in text_of(result)


# --- entry point --------------------------------------------------------------------------


@pytest.mark.parametrize(("level", "httpx_level"), [("INFO", logging.WARNING), ("DEBUG", logging.NOTSET)])
def test_configure_logging_keeps_httpx_urls_out_of_info_logs(level, httpx_level):
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.NOTSET)
    try:
        configure_logging(level)
        assert httpx_logger.level == httpx_level
    finally:
        httpx_logger.setLevel(logging.NOTSET)


def test_main_reports_missing_config_without_values(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("FRESHDESK_DOMAIN", raising=False)
    monkeypatch.setenv("FRESHDESK_API_KEY", FAKE_KEY)

    assert main([]) == 2
    err = capsys.readouterr().err
    assert "FRESHDESK_DOMAIN" in err
    assert FAKE_KEY not in err
