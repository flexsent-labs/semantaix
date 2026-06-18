from platform_common.settings import AppSettings


def test_platform_admin_usernames_includes_both_admin_fields():
    settings = AppSettings(
        admin_telegram_username="@admin_one",
        hitl_config_admin_username="@admin_two",
    )
    assert settings.platform_admin_usernames() == frozenset({"@admin_one", "@admin_two"})


def test_is_platform_admin_username_normalizes_and_matches():
    settings = AppSettings(admin_telegram_username="@ajdevy")
    assert settings.is_platform_admin_username("ajdevy") is True
    assert settings.is_platform_admin_username("@ajdevy") is True
    assert settings.is_platform_admin_username("@other") is False
    assert settings.is_platform_admin_username(None) is False
