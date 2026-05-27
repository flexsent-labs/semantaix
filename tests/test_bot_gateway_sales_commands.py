"""Unit tests for the Story 12.02 sales-command argument parsers.

These helpers live in ``services.bot_gateway.app.sales_commands`` and are
deliberately split out from the dispatch glue so we can pin down the
``/service_add | description`` separator semantics, integer validation on
``/service_remove``, and the optional ``@customer`` arg on ``/sales_state``
without spinning up the whole webhook.

The parsers raise :class:`SalesCommandUsageError` on bad input; callers in
:mod:`services.bot_gateway.app.main` turn that into a single-line Russian
usage DM (the messages live alongside the parsers so the tests can assert
the exact strings).
"""

from __future__ import annotations

import pytest

from services.bot_gateway.app.sales_commands import (
    SERVICE_ADD_USAGE,
    SERVICE_REMOVE_USAGE,
    SalesCommandUsageError,
    parse_sales_state,
    parse_service_add,
    parse_service_remove,
)

# --- parse_service_add ------------------------------------------------------


def test_parse_service_add_name_only() -> None:
    assert parse_service_add("/service_add каньонинг") == ("каньонинг", None)


def test_parse_service_add_name_and_description() -> None:
    assert parse_service_add(
        "/service_add каньонинг | Каньонинг — это…"
    ) == ("каньонинг", "Каньонинг — это…")


def test_parse_service_add_strips_surrounding_whitespace() -> None:
    assert parse_service_add(
        "/service_add   каньонинг   |   Каньонинг — это…   "
    ) == ("каньонинг", "Каньонинг — это…")


def test_parse_service_add_only_first_pipe_splits_description() -> None:
    # A description that happens to contain `|` (e.g. URL or table row)
    # must NOT split twice. Only the first pipe is the separator.
    assert parse_service_add(
        "/service_add каньонинг | первая часть | вторая часть"
    ) == ("каньонинг", "первая часть | вторая часть")


def test_parse_service_add_rejects_missing_args() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_add("/service_add")
    assert str(exc.value) == SERVICE_ADD_USAGE


def test_parse_service_add_rejects_blank_name() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_add("/service_add    ")
    assert str(exc.value) == SERVICE_ADD_USAGE


def test_parse_service_add_rejects_empty_name_with_pipe() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_add("/service_add  | desc only")
    assert str(exc.value) == SERVICE_ADD_USAGE


def test_parse_service_add_rejects_empty_description_after_pipe() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_add("/service_add каньонинг |   ")
    assert str(exc.value) == SERVICE_ADD_USAGE


def test_parse_service_add_usage_message_includes_canonical_example() -> None:
    # Anchor the wording so any future drift is caught in code review.
    assert SERVICE_ADD_USAGE == "Использование: /service_add <название> [| описание]"


# --- parse_service_remove ---------------------------------------------------


def test_parse_service_remove_returns_positive_int() -> None:
    assert parse_service_remove("/service_remove 12") == 12


def test_parse_service_remove_strips_whitespace() -> None:
    assert parse_service_remove("/service_remove   42  ") == 42


def test_parse_service_remove_rejects_missing_arg() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_remove("/service_remove")
    assert str(exc.value) == SERVICE_REMOVE_USAGE


def test_parse_service_remove_rejects_non_int() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_remove("/service_remove abc")
    assert str(exc.value) == SERVICE_REMOVE_USAGE


def test_parse_service_remove_rejects_zero() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_remove("/service_remove 0")
    assert str(exc.value) == SERVICE_REMOVE_USAGE


def test_parse_service_remove_rejects_negative() -> None:
    with pytest.raises(SalesCommandUsageError) as exc:
        parse_service_remove("/service_remove -3")
    assert str(exc.value) == SERVICE_REMOVE_USAGE


def test_parse_service_remove_rejects_extra_tokens() -> None:
    # Future-proof: the contract is "single positive integer". Anything
    # extra is a usage error — better than silently truncating.
    with pytest.raises(SalesCommandUsageError):
        parse_service_remove("/service_remove 12 garbage")


def test_parse_service_remove_usage_message() -> None:
    assert SERVICE_REMOVE_USAGE == "Использование: /service_remove <id>"


# --- parse_sales_state ------------------------------------------------------


def test_parse_sales_state_no_args_returns_none() -> None:
    assert parse_sales_state("/sales_state") is None


def test_parse_sales_state_extracts_username() -> None:
    assert parse_sales_state("/sales_state @customer") == "@customer"


def test_parse_sales_state_strips_surrounding_whitespace() -> None:
    assert parse_sales_state("/sales_state   @bob  ") == "@bob"


def test_parse_sales_state_username_without_at_is_normalized() -> None:
    # Operators sometimes type a bare username; canonicalize to ``@name``.
    assert parse_sales_state("/sales_state bob") == "@bob"


def test_parse_sales_state_blank_username_returns_none() -> None:
    assert parse_sales_state("/sales_state    ") is None
