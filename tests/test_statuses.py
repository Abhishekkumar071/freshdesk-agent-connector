import pytest

from freshdesk_connector.errors import InvalidInput
from freshdesk_connector.statuses import StatusCatalog, slugify

# Shape observed on the trial account (docs/02 §11.1).
TRIAL_FIELDS = [
    {"name": "requester", "choices": {}},
    {
        "name": "status",
        "choices": {
            "2": ["Open", "Open"],
            "3": ["Pending", "Pending"],
            "4": ["Resolved", "Resolved"],
            "5": ["Closed", "Closed"],
            "6": ["Waiting on Customer", "Awaiting your Reply"],
            "7": ["Waiting on Third Party", "Being Processed"],
            "9000": ["Assigned to AI Agent", "Assigned to AI Agent"],
        },
    },
]


@pytest.fixture
def catalog() -> StatusCatalog:
    return StatusCatalog.from_ticket_fields(TRIAL_FIELDS)


def test_slugify():
    assert slugify("Waiting on Customer") == "waiting_on_customer"
    assert slugify("  On-Hold (VIP) ") == "on_hold_vip"


def test_labels_from_ticket_fields_use_agent_names(catalog):
    assert catalog.label(2) == "open"
    assert catalog.label(6) == "waiting_on_customer"
    assert catalog.label(9000) == "assigned_to_ai_agent"
    assert catalog.labels == [
        "unresolved", "open", "pending", "resolved", "closed",
        "waiting_on_customer", "waiting_on_third_party", "assigned_to_ai_agent",
    ]


def test_unknown_code_gets_generic_label(catalog):
    assert catalog.label(42) == "status_42"


def test_unresolved_includes_custom_statuses(catalog):
    assert catalog.unresolved_codes == [2, 3, 6, 7, 9000]
    assert catalog.codes_for(["unresolved"]) == [2, 3, 6, 7, 9000]


def test_codes_for_is_case_insensitive_and_deduplicated(catalog):
    assert catalog.codes_for([" Open ", "open", "WAITING_ON_CUSTOMER"]) == [2, 6]


def test_codes_for_unknown_label_lists_valid_values(catalog):
    with pytest.raises(InvalidInput) as excinfo:
        catalog.codes_for(["escalated"])
    assert "'escalated'" in excinfo.value.message
    assert "waiting_on_customer" in excinfo.value.message


@pytest.mark.parametrize(
    "fields",
    [
        [],
        [{"name": "status", "choices": None}],
        [{"name": "status", "choices": {"x": ["Bad"], "2": []}}],
        ["not a dict"],
    ],
)
def test_unusable_fields_fall_back_to_builtin_statuses(fields):
    catalog = StatusCatalog.from_ticket_fields(fields)
    assert catalog.labels == ["unresolved", "open", "pending", "resolved", "closed"]


def test_plain_string_choice_values_are_accepted():
    catalog = StatusCatalog.from_ticket_fields(
        [{"name": "status", "choices": {"2": "Open", "8": "On Hold"}}]
    )
    assert catalog.label(8) == "on_hold"


def test_colliding_names_stay_distinct():
    catalog = StatusCatalog({2: "Open", 3: "OPEN", 10: "Unresolved"})
    assert catalog.label(2) == "open"
    assert catalog.label(3) == "open_3"
    assert catalog.label(10) == "unresolved_10"
    assert catalog.codes_for(["open_3"]) == [3]
