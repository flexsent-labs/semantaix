from services.api.app.russian_text import RussianNormalizer


def test_normalize_lowercases_and_substitutes_slang():
    normalizer = RussianNormalizer()
    assert "что" in normalizer.normalize("Чё там?")
    assert "не знаю" in normalizer.normalize("ХЗ")
    assert "деньги" in normalizer.normalize("Где мои бабки?")
    assert "может быть" in normalizer.normalize("мб завтра")


def test_normalize_preserves_words_outside_slang_dict():
    normalizer = RussianNormalizer()
    out = normalizer.normalize("Сколько времени?")
    assert "сколько" in out
    assert "времени" in out


def test_lemmas_reduce_inflection_to_canonical_form():
    normalizer = RussianNormalizer()
    inflected = normalizer.lemmas("Возврат денег занимает пять рабочих дней")
    canonical = normalizer.lemmas("деньги")
    # The canonical lemma form pymorphy3 picks for деньги/денег/деньгами is
    # the same across all three inflections; we don't depend on which exact
    # form (singular vs plural) it picks, only that they align.
    assert canonical, "expected at least one lemma"
    assert canonical[0] in inflected
    assert normalizer.lemmas("денег")[0] == canonical[0]
    assert normalizer.lemmas("деньгами")[0] == canonical[0]


def test_lemmas_substitutes_slang_then_lemmatizes():
    normalizer = RussianNormalizer()
    lemmas = normalizer.lemmas("Где бабки?")
    canonical = normalizer.lemmas("деньги")
    # бабки -> деньги -> same lemma as деньги.
    assert canonical[0] in lemmas
    assert "бабки" not in lemmas


def test_lemmas_drop_punctuation_only_tokens():
    normalizer = RussianNormalizer()
    lemmas = normalizer.lemmas("«Ответ:» — это всё!")
    assert all(any(ch.isalnum() for ch in lemma) for lemma in lemmas)


def test_contains_profanity_true_for_known_mat():
    normalizer = RussianNormalizer()
    assert normalizer.contains_profanity("Это полный пиздец")
    assert normalizer.contains_profanity("блядь, что происходит")


def test_contains_profanity_false_for_clean_text():
    normalizer = RussianNormalizer()
    assert not normalizer.contains_profanity("Возврат денег занимает 5 рабочих дней")
    assert not normalizer.contains_profanity("Сегодня хорошая погода в Москве")


def test_normalize_handles_mixed_alphabet():
    normalizer = RussianNormalizer()
    out = normalizer.normalize("Open the прога please")
    assert "программа" in out
    assert "open" in out


def test_get_russian_normalizer_returns_singleton():
    from services.api.app.russian_text import get_russian_normalizer

    first = get_russian_normalizer()
    second = get_russian_normalizer()
    assert first is second


def test_lemmas_skips_pure_symbol_tokens():
    normalizer = RussianNormalizer()
    # Symbols outside _PUNCT_STRIP (e.g. * @ #) survive the strip and must
    # be skipped via the isalnum gate, not crash lemmatization.
    lemmas = normalizer.lemmas("hello *** world @@ #")
    assert "*" not in lemmas
    assert "@" not in lemmas
    assert "hello" in lemmas
    assert "world" in lemmas


def test_contains_profanity_custom_lemmas_uses_only_provided_set():
    normalizer = RussianNormalizer()
    # Word not in the default file but in the per-project override.
    assert normalizer.contains_profanity("это шикарный пиджак") is False
    assert (
        normalizer.contains_profanity(
            "это шикарный пиджак", custom_lemmas=["пиджак"]
        )
        is True
    )
    # Default-banned words become harmless when the project's list omits them.
    assert (
        normalizer.contains_profanity(
            "Это полный пиздец", custom_lemmas=["безобидное"]
        )
        is False
    )
    # Blank/empty entries are tolerated.
    assert (
        normalizer.contains_profanity(
            "обычный текст", custom_lemmas=["", "   ", "слово"]
        )
        is False
    )


def test_is_known_word_recognises_real_and_rejects_gibberish():
    # Story 12.74 (round-18 R18-6) — distinguishes real Russian words from
    # keyboard-mash, used by the gibberish guard.
    normalizer = RussianNormalizer()
    assert normalizer.is_known_word("завтра") is True
    assert normalizer.is_known_word("Хочу") is True  # case-insensitive
    assert normalizer.is_known_word("фыва") is False
    assert normalizer.is_known_word("asdfgh") is False
