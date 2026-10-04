"""Ticket status codes and their names for one Freshdesk account.

Freshdesk accounts can define custom statuses (a fresh trial already has
"Waiting on Customer" = 6, "Waiting on Third Party" = 7, ...). Their names are
only available from GET /ticket_fields, so the catalog is loaded once at startup.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import InvalidInput

logger = logging.getLogger(__name__)

UNRESOLVED = "unresolved"
RESOLVED_CODE = 4
CLOSED_CODE = 5
DEFAULT_STATUS_NAMES = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}


def slugify(name: str) -> str:
    """'Waiting on Customer' -> 'waiting_on_customer'."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class StatusCatalog:
    """Maps status codes to agent-facing labels and back."""

    def __init__(self, names: Mapping[int, str]) -> None:
        self._label_by_code: dict[int, str] = {}
        self._code_by_label: dict[str, int] = {}
        for code, name in sorted(names.items()):
            label = slugify(name) or f"status_{code}"
            if label in self._code_by_label or label == UNRESOLVED:
                label = f"{label}_{code}"
            self._label_by_code[code] = label
            self._code_by_label[label] = code

    @classmethod
    def default(cls) -> "StatusCatalog":
        return cls(DEFAULT_STATUS_NAMES)

    @classmethod
    def from_ticket_fields(cls, fields: Iterable[Any]) -> "StatusCatalog":
        """Build from GET /ticket_fields; falls back to the four built-in statuses."""
        for field in fields:
            if isinstance(field, dict) and field.get("name") == "status":
                names = _parse_choices(field.get("choices"))
                if names:
                    return cls(names)
        logger.warning("No usable status field in ticket_fields; using built-in statuses")
        return cls.default()

    @property
    def labels(self) -> list[str]:
        """Every value accepted by `codes_for`, including the 'unresolved' shortcut."""
        return [UNRESOLVED, *self._code_by_label]

    @property
    def unresolved_codes(self) -> list[int]:
        return [c for c in self._label_by_code if c not in (RESOLVED_CODE, CLOSED_CODE)]

    def label(self, code: int) -> str:
        return self._label_by_code.get(code, f"status_{code}")

    def codes_for(self, labels: Iterable[str]) -> list[int]:
        codes: set[int] = set()
        for raw in labels:
            label = raw.strip().lower()
            if label == UNRESOLVED:
                codes.update(self.unresolved_codes)
            elif label in self._code_by_label:
                codes.add(self._code_by_label[label])
            else:
                raise InvalidInput(
                    f"Unknown status {raw!r}. Valid values: {', '.join(self.labels)}."
                )
        return sorted(codes)


def _parse_choices(choices: Any) -> dict[int, str]:
    """Freshdesk shape: {"2": ["Open", "Open"], "6": ["Waiting on Customer", "Awaiting your Reply"]}.
    The first name is the agent-facing one."""
    if not isinstance(choices, dict):
        return {}
    names: dict[int, str] = {}
    for code, value in choices.items():
        name = value[0] if isinstance(value, list) and value else value
        if str(code).isdigit() and isinstance(name, str) and name.strip():
            names[int(code)] = name.strip()
    return names
