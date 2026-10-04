"""docs/mcp-tools.json must match what the server actually advertises."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from export_tool_spec import SPEC_PATH, build_spec, render  # noqa: E402

pytestmark = pytest.mark.anyio


async def test_committed_spec_matches_running_server():
    expected = render(await build_spec())
    committed = SPEC_PATH.read_text(encoding="utf-8")
    assert committed == expected, (
        "docs/mcp-tools.json is out of date. Run: uv run python scripts/export_tool_spec.py"
    )


def test_spec_lists_exactly_three_read_only_tools():
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    assert [t["name"] for t in spec["tools"]] == ["list_tickets", "get_ticket", "search_tickets"]
    for tool in spec["tools"]:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert "inputSchema" in tool and "outputSchema" in tool
    assert spec["server"]["write_tools"] == []
