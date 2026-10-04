"""The offline demo (demo/) runs the real MCP server, service and client against an
in-memory fake Freshdesk. These tests keep the demo working and check the agent
flow it is meant to show."""

import sys
from pathlib import Path

import pytest
from mcp import Client

sys.path.insert(0, str(Path(__file__).parent.parent / "demo"))
from mcp_cli import offline_server  # noqa: E402

pytestmark = pytest.mark.anyio


async def test_agent_can_follow_the_narrowing_hint_to_the_true_oldest_tickets():
    async with Client(offline_server("normal")) as c:
        first = (await c.call_tool(
            "search_tickets", {"status": ["unresolved"], "order": "oldest_first"})).structured_content
        assert first["exhaustive"] is False and first["total"] > 90

        hint = next(n for n in first["notes"] if "created_before=" in n)
        earliest = hint.split('created_before="')[1][:10]
        second = (await c.call_tool("search_tickets", {
            "status": ["unresolved"], "order": "oldest_first", "created_before": earliest,
        })).structured_content

    assert second["exhaustive"] is True
    assert second["tickets"][0]["id"] < first["tickets"][0]["id"]  # found genuinely older tickets
    assert second["tickets"][0]["created_at"] <= first["tickets"][0]["created_at"]


async def test_offline_scenarios_surface_safe_errors_and_partial_results():
    async with Client(offline_server("rate-limit")) as c:
        partial = (await c.call_tool(
            "search_tickets", {"status": ["unresolved"], "keyword": "payment"})).structured_content
    assert (partial["scanned"], partial["exhaustive"]) == (30, False)
    assert "retry in about 60 seconds" in partial["notes"][0]

    async with Client(offline_server("bad-key")) as c:
        result = await c.call_tool("get_ticket", {"ticket_id": 1001})
    assert result.is_error is True
    assert result.content[0].text.endswith("Freshdesk authentication failed. Check the connector's API key configuration.")


async def test_detail_drops_fields_the_agent_must_not_see():
    async with Client(offline_server("normal")) as c:
        ticket = (await c.call_tool("get_ticket", {"ticket_id": 1042})).structured_content

    assert set(ticket["requester"]) == {"id", "name", "email"}  # fake also sends ip_address
    assert "custom_fields" not in ticket and "cc_emails" not in ticket
    if ticket["status"] == "closed":
        assert ticket["closed_at"] is not None
