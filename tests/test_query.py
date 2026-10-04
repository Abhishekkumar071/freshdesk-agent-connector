from datetime import date, datetime

import pytest

from freshdesk_connector.errors import InvalidInput
from freshdesk_connector.query import MAX_QUERY_LENGTH, build_query


def test_no_filters_gives_empty_query():
    assert build_query() == ""


def test_single_status_has_no_parentheses():
    assert build_query(status_codes=[2]) == "status:2"


def test_multiple_values_are_ored_and_sorted():
    assert build_query(status_codes=[3, 2, 3]) == "(status:2 OR status:3)"


def test_all_filters_combined():
    query = build_query(
        status_codes=[2, 3],
        priority_codes=[4, 3],
        tag="payment",
        ticket_type="Refund Request",
        created_after=date(2026, 1, 1),
        created_before=date(2026, 3, 31),
    )
    assert query == (
        "(status:2 OR status:3) AND (priority:3 OR priority:4) AND tag:'payment' "
        "AND type:'Refund Request' AND created_at:>'2026-01-01' AND created_at:<'2026-03-31'"
    )


def test_datetime_is_reduced_to_date():
    assert build_query(created_after=datetime(2026, 1, 1, 15, 30)) == "created_at:>'2026-01-01'"


def test_terms_are_trimmed():
    assert build_query(tag="  upi-failure ") == "tag:'upi-failure'"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "pay'ment",
        "x' OR status:5 OR tag:'y",
        'quote"d',
        "a(b)",
        "x" * 65,
        "naïve",
    ],
)
def test_unsafe_tag_or_type_rejected(value):
    with pytest.raises(InvalidInput):
        build_query(tag=value)
    with pytest.raises(InvalidInput):
        build_query(ticket_type=value)


def test_date_range_must_be_ordered():
    with pytest.raises(InvalidInput):
        build_query(created_after=date(2026, 2, 1), created_before=date(2026, 1, 1))


def test_same_day_range_allowed():
    day = date(2026, 1, 1)
    assert build_query(created_after=day, created_before=day).count("2026-01-01") == 2


def test_query_length_limit_enforced():
    with pytest.raises(InvalidInput):
        build_query(status_codes=range(1000, 1000 + MAX_QUERY_LENGTH))
