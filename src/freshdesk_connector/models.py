"""Agent-facing response models. These are the connector's output contract;
raw Freshdesk payloads never leave the service layer."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TicketSummary(BaseModel):
    id: int
    subject: str
    status: str = Field(description='Status label, e.g. "open", "pending", "waiting_on_customer".')
    priority: str = Field(description='"low", "medium", "high" or "urgent".')
    type: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    due_by: datetime | None
    requester_id: int | None
    assigned_agent_id: int | None
    group_id: int | None
    url: str = Field(description="Link to the ticket in the Freshdesk agent UI.")


class Requester(BaseModel):
    id: int | None
    name: str | None
    email: str | None


class TicketDetail(TicketSummary):
    source: str = Field(description='Channel, e.g. "email", "portal", "phone", "chat".')
    description: str = Field(description="Plain-text description, possibly truncated.")
    description_truncated: bool
    requester: Requester | None
    first_responded_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None


class TicketPage(BaseModel):
    tickets: list[TicketSummary]
    page: int
    has_more: bool = Field(description="True if the next page has more tickets.")
    notes: list[str] = Field(description="Caveats about what this result covers.")


class SearchResult(BaseModel):
    """One envelope for every search. The scan fields are set only when the
    connector itself scanned a bounded set of tickets (keyword search, or
    oldest-first ordering); they are null for plain Freshdesk paging."""

    search_mode: Literal["native", "keyword_scan"]
    tickets: list[TicketSummary]
    page: int
    has_more: bool = Field(description="True if requesting the next page returns more tickets.")
    total: int = Field(description="Tickets matching the filters in Freshdesk (before any keyword match).")
    notes: list[str] = Field(description="Caveats about what this result covers.")
    keyword: str | None = None
    max_scan: int | None = Field(default=None, description="Most tickets the connector will scan.")
    scanned: int | None = Field(default=None, description="Tickets actually scanned.")
    matched: int | None = Field(default=None, description="Scanned tickets that matched the keyword.")
    exhaustive: bool | None = Field(
        default=None,
        description="False means matching tickets may exist beyond what was scanned.",
    )
