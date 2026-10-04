"""Read-only HTTP client for the Freshdesk REST API v2.

This module is the only place that knows Freshdesk URLs, authentication, HTTP
status codes and retry rules. It returns raw Freshdesk JSON; turning that into
agent-facing models is the service layer's job.

The client exposes GET requests only. There is deliberately no generic
`request(method, ...)`, so nothing built on top of it can write to Freshdesk.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from .config import Settings
from .errors import (
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

logger = logging.getLogger(__name__)

RETRYABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 4.0
BACKOFF_JITTER = 0.25  # +/- 25%
LOW_QUOTA_FRACTION = 0.1  # warn when fewer than 10% of the per-minute calls remain
MAX_ERROR_DETAIL_CHARS = 200

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class TicketListPage:
    """One page of GET /tickets."""

    tickets: list[dict[str, Any]]
    has_next: bool


@dataclass(frozen=True)
class TicketSearchPage:
    """One page of GET /search/tickets (Freshdesk returns at most 30 per page)."""

    tickets: list[dict[str, Any]]
    total: int


def build_http_client(settings: Settings) -> httpx.AsyncClient:
    """Create the httpx client that holds the Freshdesk base URL and credentials."""
    return httpx.AsyncClient(
        base_url=settings.base_url,
        # Freshdesk: API key as the username, any dummy password.
        auth=httpx.BasicAuth(settings.api_key.get_secret_value(), "X"),
        timeout=httpx.Timeout(settings.timeout_seconds),
        headers={"Accept": "application/json", "User-Agent": "freshdesk-connector/0.1"},
        # A redirect would mean something is wrong; never follow it with credentials.
        follow_redirects=False,
    )


class FreshdeskClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        max_attempts: int = 3,
        max_retry_wait_seconds: float = 10.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._max_attempts = max_attempts
        self._max_retry_wait = max_retry_wait_seconds
        self._sleep = sleep

    async def list_tickets(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        order_by: Literal["created_at", "updated_at", "due_by", "status"] = "created_at",
        order_type: Literal["asc", "desc"] = "desc",
        updated_since: datetime | None = None,
    ) -> TicketListPage:
        """GET /tickets. Freshdesk only returns tickets created in the last 30 days
        unless `updated_since` is given."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "order_by": order_by,
            "order_type": order_type,
        }
        if updated_since is not None:
            params["updated_since"] = _format_utc(updated_since)

        response = await self._get("/tickets", params)
        body = _json(response)
        if not isinstance(body, list):
            raise UnexpectedResponse("Unexpected ticket list format from Freshdesk.")
        return TicketListPage(tickets=body, has_next="next" in response.links)

    async def get_ticket(self, ticket_id: int, include: Sequence[str] = ()) -> dict[str, Any]:
        """GET /tickets/{id}, optionally embedding e.g. "requester" or "stats"."""
        if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id < 1:
            raise InvalidInput("Ticket ID must be a positive integer.")

        params = {"include": ",".join(include)} if include else None
        try:
            response = await self._get(f"/tickets/{ticket_id}", params)
        except NotFound:
            raise NotFound(f"Ticket {ticket_id} was not found.") from None

        body = _json(response)
        if not isinstance(body, dict):
            raise UnexpectedResponse("Unexpected ticket format from Freshdesk.")
        return body

    async def search_tickets(self, query: str, page: int = 1) -> TicketSearchPage:
        """GET /search/tickets with a Freshdesk filter query (without the outer quotes)."""
        response = await self._get("/search/tickets", {"query": f'"{query}"', "page": page})
        body = _json(response)
        results = body.get("results") if isinstance(body, dict) else None
        total = body.get("total") if isinstance(body, dict) else None
        if not isinstance(results, list) or not isinstance(total, int):
            raise UnexpectedResponse("Unexpected search result format from Freshdesk.")
        return TicketSearchPage(tickets=results, total=total)

    async def list_ticket_fields(self) -> list[dict[str, Any]]:
        """GET /ticket_fields. Used once at startup to read the account's status names."""
        body = _json(await self._get("/ticket_fields"))
        if not isinstance(body, list):
            raise UnexpectedResponse("Unexpected ticket field format from Freshdesk.")
        return body

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with bounded retries on 429, 5xx, timeouts and network errors."""
        for attempt in range(1, self._max_attempts + 1):
            started = time.monotonic()
            wait: float | None = None
            try:
                response = await self._http.get(path, params=params)
            except httpx.TimeoutException:
                error: ConnectorError = RequestTimeout("The request to Freshdesk timed out.")
                logger.warning("GET %s timed out attempt=%d", path, attempt)
            except httpx.TransportError as exc:
                error = NetworkError("Could not reach Freshdesk.")
                logger.warning("GET %s network error=%s attempt=%d", path, type(exc).__name__, attempt)
            else:
                _log_response(path, response, attempt, started)
                if response.is_success:
                    return response
                error, wait = self._classify_failure(response)

            if attempt == self._max_attempts:
                raise error
            delay = wait if wait is not None else _backoff(attempt)
            logger.warning("GET %s retrying in %.1fs (%s)", path, delay, type(error).__name__)
            await self._sleep(delay)

        raise AssertionError("unreachable")  # pragma: no cover

    def _classify_failure(self, response: httpx.Response) -> tuple[ConnectorError, float | None]:
        """Return a retryable error and an optional wait, or raise a permanent error."""
        status = response.status_code

        if status == 429:
            retry_after = _retry_after_seconds(response)
            error = RateLimited(_rate_limit_message(retry_after), retry_after=retry_after)
            if retry_after is not None and retry_after > self._max_retry_wait:
                # Waiting this long would hang the agent's tool call; fail fast instead.
                raise error
            return error, retry_after

        if status in RETRYABLE_SERVER_STATUSES:
            return ServiceUnavailable(f"Freshdesk is temporarily unavailable (HTTP {status})."), None

        raise _permanent_error(response)


def _permanent_error(response: httpx.Response) -> ConnectorError:
    status = response.status_code
    if status == 400:
        return InvalidRequest(f"Freshdesk rejected the request: {_validation_details(response)}")
    if status == 401:
        return AuthenticationFailed(
            "Freshdesk authentication failed. Check the connector's API key configuration."
        )
    if status == 403:
        return PermissionDenied(
            "The Freshdesk account used by the connector is not allowed to perform this request."
        )
    if status == 404:
        return NotFound("The requested Freshdesk resource was not found.")
    return UnexpectedResponse(f"Unexpected response from Freshdesk (HTTP {status}).")


def _validation_details(response: httpx.Response) -> str:
    """Summarize Freshdesk's `errors[].field/message`; nothing else from the body is used."""
    try:
        errors = response.json().get("errors")
    except (ValueError, AttributeError):
        errors = None
    if not isinstance(errors, list):
        return "invalid request."

    parts = []
    for item in errors:
        if isinstance(item, dict):
            field, message = item.get("field"), item.get("message")
            text = f"{field}: {message}" if field else str(message)
            parts.append(text)
    detail = "; ".join(parts) or "invalid request."
    return detail[:MAX_ERROR_DETAIL_CHARS]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _rate_limit_message(retry_after: float | None) -> str:
    if retry_after is None:
        return "Freshdesk rate limit reached. Try again shortly."
    return f"Freshdesk rate limit reached. Try again in about {round(retry_after)} seconds."


def _backoff(attempt: int) -> float:
    delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)
    return delay * random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise UnexpectedResponse("Freshdesk returned a response that could not be read.") from None


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_response(path: str, response: httpx.Response, attempt: int, started: float) -> None:
    elapsed_ms = round((time.monotonic() - started) * 1000)
    remaining = response.headers.get("X-RateLimit-Remaining")
    logger.info(
        "GET %s status=%d attempt=%d ms=%d ratelimit_remaining=%s",
        path, response.status_code, attempt, elapsed_ms, remaining,
    )
    total = response.headers.get("X-RateLimit-Total")
    if remaining and total and remaining.isdigit() and total.isdigit():
        if int(remaining) < int(total) * LOW_QUOTA_FRACTION:
            logger.warning("Freshdesk rate limit nearly used: %s of %s calls left", remaining, total)
