"""Build Freshdesk filter-search query strings from typed, validated inputs.

The agent never writes Freshdesk query syntax. Free-text values (tag, type) are
restricted to a safe character set so they cannot close the '...' quoting or
add AND/OR clauses.
"""

import re
from collections.abc import Iterable
from datetime import date

from .errors import InvalidInput

MAX_QUERY_LENGTH = 512  # Freshdesk limit, excluding the outer double quotes
_SAFE_TERM_RE = re.compile(r"[A-Za-z0-9 _.\-]{1,64}")


def build_query(
    *,
    status_codes: Iterable[int] = (),
    priority_codes: Iterable[int] = (),
    tag: str | None = None,
    ticket_type: str | None = None,
    created_after: date | None = None,
    created_before: date | None = None,
) -> str:
    """Return e.g. "(status:2 OR status:3) AND tag:'payment'"; empty string if no filters.

    Date bounds are inclusive (Freshdesk's :> and :< mean >= and <=).
    """
    if created_after and created_before and created_after > created_before:
        raise InvalidInput("created_after must be on or before created_before.")

    clauses = []
    if status_codes:
        clauses.append(_any_of("status", status_codes))
    if priority_codes:
        clauses.append(_any_of("priority", priority_codes))
    if tag is not None:
        clauses.append(f"tag:'{_safe_term('tag', tag)}'")
    if ticket_type is not None:
        clauses.append(f"type:'{_safe_term('ticket_type', ticket_type)}'")
    if created_after:
        clauses.append(f"created_at:>'{created_after.strftime('%Y-%m-%d')}'")
    if created_before:
        clauses.append(f"created_at:<'{created_before.strftime('%Y-%m-%d')}'")

    query = " AND ".join(clauses)
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidInput("Too many filters for one Freshdesk search; use fewer values.")
    return query


def _any_of(field: str, codes: Iterable[int]) -> str:
    parts = [f"{field}:{code}" for code in sorted(set(codes))]
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _safe_term(name: str, value: str) -> str:
    term = value.strip()
    if not _SAFE_TERM_RE.fullmatch(term):
        raise InvalidInput(
            f"{name} must be 1-64 characters: letters, digits, spaces, '-', '_' or '.'."
        )
    return term
