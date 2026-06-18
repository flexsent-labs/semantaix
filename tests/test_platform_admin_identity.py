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


def test_bot_gateway_platform_admin_usernames_helper(monkeypatch):
    import services.bot_gateway.app.main as bot_main

    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin_one")
    monkeypatch.setattr(
        bot_main.settings, "hitl_config_admin_username", "@admin_two"
    )
    assert bot_main._platform_admin_usernames() == frozenset(
        {"@admin_one", "@admin_two"}
    )


def test_material_downloader_factory_uses_project_storage_dir(tmp_path, monkeypatch):
    import services.bot_gateway.app.main as bot_main
    from services.bot_gateway.app.telegram_file_download import TelegramFileDownloader

    storage_dir = tmp_path / "sales_materials" / "1"
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "test-token")
    downloader = bot_main._material_downloader_factory(storage_dir)
    assert isinstance(downloader, TelegramFileDownloader)
    assert downloader._storage_dir == storage_dir
