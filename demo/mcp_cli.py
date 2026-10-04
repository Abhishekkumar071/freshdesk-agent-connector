"""Minimal MCP client for demos: list the connector's tools or call one.

Live (real Freshdesk, credentials from .env; starts the server over stdio):
    uv run python demo/mcp_cli.py list
    uv run python demo/mcp_cli.py call search_tickets '{"status": ["unresolved"]}'

Offline (same MCP server, service and client; httpx talks to an in-memory fake
Freshdesk with 200 invented tickets):
    uv run python demo/mcp_cli.py --offline call get_ticket '{"ticket_id": 1001}'
    uv run python demo/mcp_cli.py --offline --scenario rate-limit call search_tickets '{"keyword": "payment"}'

Prints what an agent would see: the tool result as JSON, or the tool error text.
"""

import argparse
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import Client, StdioServerParameters

sys.path.insert(0, str(Path(__file__).parent))
from fake_freshdesk import DEMO_API_KEY, DEMO_DOMAIN, FakeFreshdesk  # noqa: E402

from freshdesk_connector.client import FreshdeskClient  # noqa: E402
from freshdesk_connector.server import create_server  # noqa: E402
from freshdesk_connector.service import create_ticket_service  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent


def offline_server(scenario: str):
    """The real MCP server, wired to the fake Freshdesk instead of the network."""
    fake = FakeFreshdesk(scenario)

    @asynccontextmanager
    async def factory():
        http = httpx.AsyncClient(
            base_url=f"https://{DEMO_DOMAIN}/api/v2",
            auth=httpx.BasicAuth(DEMO_API_KEY, "X"),
            transport=fake.transport(),
        )
        async with http:
            client = FreshdeskClient(http, max_attempts=3, max_retry_wait_seconds=10)
            yield await create_ticket_service(client, DEMO_DOMAIN)

    return create_server(factory)


def live_server() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "freshdesk_connector", "--log-level", "WARNING"],
        cwd=str(PROJECT_DIR),
    )


async def run(args: argparse.Namespace) -> int:
    target = offline_server(args.scenario) if args.offline else live_server()
    async with Client(target) as client:
        if args.command == "list":
            for tool in (await client.list_tools()).tools:
                print(f"## {tool.name}  (read_only={tool.annotations.read_only_hint})")
                print(tool.description.strip())
                print("input schema:", json.dumps(tool.input_schema["properties"]))
                print()
            return 0

        result = await client.call_tool(args.tool, json.loads(args.arguments))
        if result.is_error:
            print("TOOL ERROR:", result.content[0].text)
            return 1
        print(json.dumps(result.structured_content, indent=2))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use the in-memory fake Freshdesk")
    parser.add_argument("--scenario", default="normal", choices=["normal", "rate-limit", "bad-key"],
                        help="offline only")
    parser.add_argument("--verbose", action="store_true", help="show connector logs on stderr")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list tools")
    call = sub.add_parser("call", help="call a tool")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    args = parser.parse_args()
    if not args.verbose:
        logging.disable(logging.WARNING)  # keep the output to what the agent sees
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
