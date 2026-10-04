"""Error types raised by the connector.

Every error carries a `message` that is safe to show to the agent: it is written
by the connector and never contains secrets. The only response text passed on is
Freshdesk's field-level validation message for HTTP 400, truncated.
"""


class ConnectorError(Exception):
    """Base class for all expected connector failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidInput(ConnectorError):
    """Input rejected by the connector before calling Freshdesk."""


class InvalidRequest(ConnectorError):
    """Freshdesk rejected the request (HTTP 400)."""


class AuthenticationFailed(ConnectorError):
    """The API key was rejected (HTTP 401)."""


class PermissionDenied(ConnectorError):
    """The API key's agent lacks access, or the account is restricted (HTTP 403)."""


class NotFound(ConnectorError):
    """The requested resource does not exist (HTTP 404)."""


class RateLimited(ConnectorError):
    """Freshdesk rate limit hit (HTTP 429) and retrying was not possible."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServiceUnavailable(ConnectorError):
    """Freshdesk returned a server error (5xx) on every attempt."""


class RequestTimeout(ConnectorError):
    """Freshdesk did not respond in time on every attempt."""


class NetworkError(ConnectorError):
    """Freshdesk could not be reached (DNS, connection refused, reset, ...)."""


class UnexpectedResponse(ConnectorError):
    """Freshdesk responded in a way the connector does not expect."""
