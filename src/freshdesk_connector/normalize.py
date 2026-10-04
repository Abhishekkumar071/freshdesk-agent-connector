"""Convert raw Freshdesk ticket JSON into the agent-facing models.

Only fields listed in the models are kept. Notably dropped: HTML bodies, custom
fields, CC/BCC/forward addresses, and requester IP / activity timestamps.
"""

from html.parser import HTMLParser
from typing import Any

from pydantic import ValidationError

from .errors import UnexpectedResponse
from .models import Requester, TicketDetail, TicketSummary
from .statuses import StatusCatalog

DESCRIPTION_MAX_CHARS = 2000

PRIORITY_LABELS = {1: "low", 2: "medium", 3: "high", 4: "urgent"}
PRIORITY_CODES = {label: code for code, label in PRIORITY_LABELS.items()}
SOURCE_LABELS = {
    1: "email",
    2: "portal",
    3: "phone",
    7: "chat",
    9: "feedback_widget",
    10: "outbound_email",
}


def to_summary(raw: dict[str, Any], statuses: StatusCatalog, domain: str) -> TicketSummary:
    try:
        return TicketSummary(**_summary_fields(raw, statuses, domain))
    except (KeyError, TypeError, ValidationError) as exc:
        raise UnexpectedResponse("Unexpected ticket format from Freshdesk.") from exc


def to_detail(raw: dict[str, Any], statuses: StatusCatalog, domain: str) -> TicketDetail:
    try:
        description = plain_text(raw)
        truncated = len(description) > DESCRIPTION_MAX_CHARS
        if truncated:
            description = description[:DESCRIPTION_MAX_CHARS].rstrip() + "…"
        stats = raw.get("stats") or {}
        return TicketDetail(
            **_summary_fields(raw, statuses, domain),
            source=_source_label(raw.get("source")),
            description=description,
            description_truncated=truncated,
            requester=_requester(raw.get("requester")),
            first_responded_at=stats.get("first_responded_at"),
            resolved_at=stats.get("resolved_at"),
            closed_at=stats.get("closed_at"),
        )
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise UnexpectedResponse("Unexpected ticket format from Freshdesk.") from exc


def plain_text(raw: dict[str, Any]) -> str:
    """The ticket description as plain text: Freshdesk's own description_text if
    present, otherwise the HTML description with tags removed."""
    text = raw.get("description_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    html = raw.get("description")
    return _strip_html(html) if isinstance(html, str) else ""


def _summary_fields(raw: dict[str, Any], statuses: StatusCatalog, domain: str) -> dict[str, Any]:
    ticket_id = raw["id"]
    priority = raw.get("priority")
    return {
        "id": ticket_id,
        "subject": raw.get("subject") or "(no subject)",
        "status": statuses.label(raw["status"]),
        "priority": PRIORITY_LABELS.get(priority, f"priority_{priority}"),
        "type": raw.get("type"),
        "tags": raw.get("tags") or [],
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
        "due_by": raw.get("due_by"),
        "requester_id": raw.get("requester_id"),
        "assigned_agent_id": raw.get("responder_id"),
        "group_id": raw.get("group_id"),
        "url": f"https://{domain}/a/tickets/{ticket_id}",
    }


def _source_label(code: Any) -> str:
    if code is None:
        return "unknown"
    return SOURCE_LABELS.get(code, f"source_{code}")


def _requester(raw: Any) -> Requester | None:
    if not isinstance(raw, dict):
        return None
    return Requester(id=raw.get("id"), name=raw.get("name"), email=raw.get("email"))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(" ".join(parser.parts).split())
