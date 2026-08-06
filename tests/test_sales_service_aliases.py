"""Typo-tolerant service-name recognition for customer sales turns."""

from __future__ import annotations

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.russian_sales_intent import is_sales_intent
from services.api.app.sales.service_aliases import (
    contains_offered_service,
    matched_service_groups,
)


def test_common_service_typos_match_the_same_service_family() -> None:
    normalizer = get_russian_normalizer()

    assert matched_service_groups(
        "квадрациклы", normalizer=normalizer
    ) == {"квадроцикл"}
    assert matched_service_groups(
        "квадрацыклах", normalizer=normalizer
    ) == {"квадроцикл"}
    assert matched_service_groups(
        "квадроцикал", normalizer=normalizer
    ) == {"квадроцикл"}
    assert matched_service_groups("баги", normalizer=normalizer) == {"багги"}


def test_unrelated_words_do_not_become_services() -> None:
    normalizer = get_russian_normalizer()

    assert matched_service_groups("", normalizer=normalizer) == frozenset()
    assert matched_service_groups("???", normalizer=normalizer) == frozenset()
    assert not contains_offered_service("вертолёт", normalizer=normalizer)
    assert not contains_offered_service("какая сегодня погода", normalizer=normalizer)


def test_typo_service_name_activates_sales_intent() -> None:
    normalizer = get_russian_normalizer()

    assert is_sales_intent(
        "Хочу поездить на квадрацыклах", normalizer=normalizer
    )
    assert is_sales_intent("Хочу покататься на баги", normalizer=normalizer)
