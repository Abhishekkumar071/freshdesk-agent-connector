"""Entry point: `python -m freshdesk_connector [--transport stdio|http]`.

Wires the real dependencies (settings → httpx client → FreshdeskClient →
TicketService) into the MCP server and runs it.
"""

import argparse
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import ValidationError

from .client import FreshdeskClient, build_http_client
from .config import Settings
from .server import create_server
from .service import TicketService, create_ticket_service


def freshdesk_service_factory(settings: Settings):
    @asynccontextmanager
    async def factory() -> AsyncIterator[TicketService]:
        async with build_http_client(settings) as http:
            client = FreshdeskClient(
                http,
                max_attempts=settings.max_attempts,
                max_retry_wait_seconds=settings.max_retry_wait_seconds,
            )
            yield await create_ticket_service(client, settings.domain)

    return factory


def configure_logging(level: str) -> None:
    # stdout carries the MCP protocol on stdio, so logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if level != "DEBUG":
        # httpx logs every full request URL at INFO; the client already logs the
        # path, status and timing, so keep httpx's own lines for debugging only.
        logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Freshdesk ticket MCP server.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP only. Keep on localhost unless "
                        "an authenticating proxy is in front.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP only.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    try:
        settings = Settings()
    except ValidationError as exc:
        fields = ", ".join(f"FRESHDESK_{str(e['loc'][0]).upper()}" for e in exc.errors())
        print(f"Configuration error: missing or invalid {fields}. See .env.example.", file=sys.stderr)
        return 2

    server = create_server(freshdesk_service_factory(settings))
    if args.transport == "http":
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
