from datetime import UTC, datetime

import pytest

from conftest import load_fixture
from freshdesk_connector.errors import UnexpectedResponse
from freshdesk_connector.normalize import DESCRIPTION_MAX_CHARS, plain_text, to_detail, to_summary
from freshdesk_connector.statuses import StatusCatalog

DOMAIN = "acme.freshdesk.com"
STATUSES = StatusCatalog({2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed", 6: "Waiting on Customer"})


def test_summary_from_list_fixture():
    raw = load_fixture("tickets_list.json")[1]

    ticket = to_summary(raw, STATUSES, DOMAIN)

    assert ticket.id == 12345
    assert ticket.subject == "Payment failed but amount debited"
    assert ticket.status == "open"
    assert ticket.priority == "high"
    assert ticket.type == "Payment"
    assert ticket.tags == ["payment", "upi"]
    assert ticket.created_at == datetime(2026, 9, 28, 10, 15, tzinfo=UTC)
    assert ticket.assigned_agent_id == 5001
    assert ticket.url == "https://acme.freshdesk.com/a/tickets/12345"


def test_summary_drops_vendor_and_pii_fields():
    raw = load_fixture("tickets_list.json")[1] | {"cc_emails": ["cc@example.com"]}

    dumped = to_summary(raw, STATUSES, DOMAIN).model_dump()

    for field in ("custom_fields", "cc_emails", "description", "spam", "fr_escalated"):
        assert field not in dumped


def test_detail_from_view_fixture():
    raw = load_fixture("ticket_view.json")

    ticket = to_detail(raw, STATUSES, DOMAIN)

    assert ticket.source == "email"
    assert ticket.description.startswith("My UPI payment failed")
    assert ticket.description_truncated is False
    assert ticket.requester.name == "Test Customer"
    assert ticket.requester.email == "customer@example.com"
    assert ticket.first_responded_at == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert ticket.resolved_at is None


def test_detail_keeps_only_requester_id_name_email():
    raw = load_fixture("ticket_view.json")
    raw["requester"] |= {"ip_address": "203.0.113.7", "last_seen": "2026-09-28T10:00:00Z"}

    requester = to_detail(raw, STATUSES, DOMAIN).requester.model_dump()

    assert set(requester) == {"id", "name", "email"}


def test_detail_without_embeds():
    raw = load_fixture("ticket_view.json")
    del raw["requester"], raw["stats"]

    ticket = to_detail(raw, STATUSES, DOMAIN)

    assert ticket.requester is None
    assert ticket.closed_at is None


def test_custom_and_unknown_codes_are_labelled():
    raw = load_fixture("tickets_list.json")[0] | {"status": 6, "priority": 9}

    ticket = to_summary(raw, STATUSES, DOMAIN)

    assert ticket.status == "waiting_on_customer"
    assert ticket.priority == "priority_9"


@pytest.mark.parametrize(("code", "label"), [(3, "phone"), (99, "source_99"), (None, "unknown")])
def test_source_labels(code, label):
    raw = load_fixture("ticket_view.json") | {"source": code}
    assert to_detail(raw, STATUSES, DOMAIN).source == label


def test_long_description_is_truncated():
    raw = load_fixture("ticket_view.json") | {"description_text": "x" * (DESCRIPTION_MAX_CHARS + 50)}

    ticket = to_detail(raw, STATUSES, DOMAIN)

    assert ticket.description_truncated is True
    assert len(ticket.description) == DESCRIPTION_MAX_CHARS + 1  # + ellipsis


def test_plain_text_prefers_description_text():
    assert plain_text({"description_text": " plain ", "description": "<b>html</b>"}) == "plain"


def test_plain_text_strips_html_when_text_missing():
    raw = {"description": "<div>Card&nbsp;declined</div><p>Order <b>#42</b> &amp; more</p><script></script>"}
    assert plain_text(raw) == "Card declined Order #42 & more"


def test_plain_text_empty_when_no_description():
    assert plain_text({}) == ""


def test_missing_subject_gets_placeholder():
    raw = load_fixture("tickets_list.json")[0] | {"subject": None}
    assert to_summary(raw, STATUSES, DOMAIN).subject == "(no subject)"


@pytest.mark.parametrize("missing", ["id", "status", "created_at"])
def test_missing_required_field_raises_unexpected_response(missing):
    raw = load_fixture("tickets_list.json")[0]
    del raw[missing]
    with pytest.raises(UnexpectedResponse):
        to_summary(raw, STATUSES, DOMAIN)
    with pytest.raises(UnexpectedResponse):
        to_detail(raw, STATUSES, DOMAIN)


def test_bad_timestamp_raises_unexpected_response():
    raw = load_fixture("tickets_list.json")[0] | {"created_at": "yesterday"}
    with pytest.raises(UnexpectedResponse):
        to_summary(raw, STATUSES, DOMAIN)
