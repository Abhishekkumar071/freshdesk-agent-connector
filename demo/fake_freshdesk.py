"""In-memory stand-in for the Freshdesk v2 API, for the offline demo only.

It mimics the behaviour in docs/freshdesk-api-notes.md (30 results per search page,
pages 1-10, newest first, `total`, 30-day list window, Link header, 401/404/400/429)
over a deterministic set of invented tickets. All names, emails and ticket contents
are fake.
"""

import json
import random
import re
from base64 import b64encode
from datetime import UTC, datetime, timedelta

import httpx

DEMO_DOMAIN = "demo.freshdesk.com"
DEMO_API_KEY = "demo-key-not-a-real-secret"

STATUS_CHOICES = {
    "2": ["Open", "Open"],
    "3": ["Pending", "Pending"],
    "4": ["Resolved", "Resolved"],
    "5": ["Closed", "Closed"],
    "6": ["Waiting on Customer", "Awaiting your Reply"],
    "7": ["Waiting on Third Party", "Being Processed"],
}

# (subject, description, type, tags)
TEMPLATES = [
    ("UPI payment failed but amount debited", "Customer paid via UPI, payment failed at checkout but money was debited.", "Payment", ["payment", "upi"]),
    ("Card payment declined at checkout", "Card payment failed with 'declined' twice; customer wants to retry.", "Payment", ["payment", "card"]),
    ("Payment failed, order not confirmed", "Netbanking payment failure; order stuck in pending state.", "Payment", ["payment"]),
    ("Refund not received after 7 days", "Customer cancelled order, refund still not credited.", "Refund", ["refund"]),
    ("Duplicate charge on credit card", "Customer was charged twice for one order.", "Payment", ["payment", "duplicate"]),
    ("Order delivered late", "Delivery arrived 5 days after the promised date.", "Delivery", ["delivery"]),
    ("Wrong item received", "Customer received a different size than ordered.", "Delivery", ["delivery", "return"]),
    ("Cannot log in to account", "Password reset email never arrives.", "Account", ["login"]),
    ("Invoice GST number incorrect", "Customer needs a corrected GST invoice.", "Billing", ["invoice"]),
    ("Settlement amount mismatch", "Merchant settlement lower than expected for last week.", "Payment", ["settlement"]),
]
CUSTOMERS = ["Asha Rao", "Vikram Shah", "Meera Iyer", "Rohan Gupta", "Neha Singh", "Arjun Nair"]


def build_tickets(count: int = 200, seed: int = 7) -> dict[int, dict]:
    """Deterministic fake tickets: ids 1001.., created over the last ~240 days."""
    rng = random.Random(seed)
    now = datetime.now(UTC).replace(microsecond=0)
    tickets = {}
    for i in range(count):
        ticket_id = 1001 + i
        subject, text, ticket_type, tags = TEMPLATES[i % len(TEMPLATES)]
        created = now - timedelta(days=240 * (count - i) / count, hours=rng.randint(0, 23))
        status = rng.choices([2, 3, 4, 5, 6, 7], weights=[40, 15, 20, 10, 10, 5])[0]
        customer = CUSTOMERS[i % len(CUSTOMERS)]
        tickets[ticket_id] = {
            "id": ticket_id,
            "subject": subject,
            "description": f"<div>{text} Order ORD-DEMO-{ticket_id}.</div>",
            "description_text": f"{text} Order ORD-DEMO-{ticket_id}.",
            "status": status,
            "priority": rng.choices([1, 2, 3, 4], weights=[20, 45, 25, 10])[0],
            "source": rng.choice([1, 2, 3, 7]),
            "type": ticket_type,
            "tags": tags,
            "requester_id": 9000 + (i % len(CUSTOMERS)),
            "responder_id": None,
            "group_id": None,
            "company_id": None,
            "created_at": _iso(created),
            "updated_at": _iso(created + timedelta(hours=rng.randint(1, 48))),
            "due_by": _iso(created + timedelta(days=3)),
            "fr_due_by": _iso(created + timedelta(hours=8)),
            "custom_fields": {"cf_internal_note": "should never reach the agent"},
            "cc_emails": ["finance-cc@example.com"],
            "spam": False,
            "_requester": {
                "id": 9000 + (i % len(CUSTOMERS)),
                "name": customer,
                "email": f"{customer.split()[0].lower()}@example.com",
                "phone": None, "mobile": None,
                "ip_address": "203.0.113.10",  # extra PII the connector must drop
                "first_seen": None, "last_seen": None,
            },
        }
    return tickets


class FakeFreshdesk:
    """httpx transport handler. `scenario`: normal | rate-limit | bad-key."""

    def __init__(self, scenario: str = "normal") -> None:
        self.scenario = scenario
        self.tickets = build_tickets()
        self.requests: list[str] = []
        self._auth = "Basic " + b64encode(f"{DEMO_API_KEY}:X".encode()).decode()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        if request.method != "GET":
            return _json(405, {"description": "Method not allowed"})
        if self.scenario == "bad-key" or request.headers.get("Authorization") != self._auth:
            return _json(401, {"code": "invalid_credentials", "message": "You have to be logged in to perform this action."})

        path = request.url.path.removeprefix("/api/v2")
        params = request.url.params
        if path == "/ticket_fields":
            return _json(200, [{"name": "status", "choices": STATUS_CHOICES}])
        if path == "/tickets":
            return self._list(request, params)
        if match := re.fullmatch(r"/tickets/(\d+)", path):
            return self._view(int(match.group(1)), params.get("include", ""))
        if path == "/search/tickets":
            return self._search(params)
        return _json(404, {})

    def _list(self, request: httpx.Request, params) -> httpx.Response:
        since = params.get("updated_since")
        if since:
            rows = [t for t in self.tickets.values() if t["updated_at"] >= since]
        else:
            cutoff = _iso(datetime.now(UTC) - timedelta(days=30))
            rows = [t for t in self.tickets.values() if t["created_at"] >= cutoff]
        key = params.get("order_by", "created_at")
        rows.sort(key=lambda t: t[key], reverse=params.get("order_type", "desc") == "desc")
        page, per_page = int(params.get("page", 1)), int(params.get("per_page", 30))
        chunk = rows[(page - 1) * per_page: page * per_page]
        headers = {}
        if page * per_page < len(rows):
            headers["Link"] = f'<{request.url.copy_set_param("page", page + 1)}>; rel="next"'
        return _json(200, [_public(t) for t in chunk], headers)

    def _view(self, ticket_id: int, include: str) -> httpx.Response:
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            return _json(404, {})
        body = _public(ticket, with_description=True)
        if "requester" in include:
            body["requester"] = ticket["_requester"]
        if "stats" in include:
            done = ticket["updated_at"]
            body["stats"] = {
                "first_responded_at": ticket["fr_due_by"],
                "resolved_at": done if ticket["status"] in (4, 5) else None,
                "closed_at": done if ticket["status"] == 5 else None,
            }
        return _json(200, body)

    def _search(self, params) -> httpx.Response:
        page = int(params.get("page", 1))
        if not 1 <= page <= 10:
            return _json(400, {"description": "Validation failed", "errors": [
                {"field": "page", "message": "It should be a Positive Integer less than or equal to 10", "code": "invalid_value"}]})
        if self.scenario == "rate-limit" and page >= 2:
            return _json(429, {}, {"Retry-After": "60"})
        rows = [t for t in self.tickets.values() if _matches(t, params["query"].strip('"'))]
        rows.sort(key=lambda t: t["created_at"], reverse=True)  # newest first, as observed
        chunk = rows[(page - 1) * 30: page * 30]
        return _json(200, {"results": [_public(t, with_description=True) for t in chunk], "total": len(rows)})


def _matches(ticket: dict, query: str) -> bool:
    """Evaluate the subset of Freshdesk query syntax the connector generates."""
    for clause in query.split(" AND "):
        alternatives = clause.strip("()").split(" OR ")
        if not any(_term(ticket, term) for term in alternatives):
            return False
    return True


def _term(ticket: dict, term: str) -> bool:
    field, op, value = re.fullmatch(r"(\w+):([<>]?)'?([^']*)'?", term.strip()).groups()
    if field in ("status", "priority"):
        return ticket[field] == int(value)
    if field == "tag":
        return value in ticket["tags"]
    if field == "type":
        return ticket["type"] == value
    if field == "created_at":
        day = ticket["created_at"][:10]
        return day >= value if op == ">" else day <= value  # inclusive, as verified live
    raise ValueError(f"fake Freshdesk does not support field {field!r}")


def _public(ticket: dict, with_description: bool = False) -> dict:
    hidden = {"_requester"} | (set() if with_description else {"description", "description_text"})
    return {k: v for k, v in ticket.items() if k not in hidden}


def _json(status: int, body, headers: dict | None = None) -> httpx.Response:
    headers = {"X-RateLimit-Total": "50", "X-RateLimit-Remaining": "42", **(headers or {})}
    return httpx.Response(status, content=json.dumps(body), headers={**headers, "Content-Type": "application/json"})


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
