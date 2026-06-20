def test_import_database():
    import database
    assert hasattr(database, "DEFAULT_GUILD_SETTINGS")


def test_import_bot_module():
    # Must resolve to bot/bot.py (the module), not an empty `bot` namespace
    # package. If this fails, the repo root leaked onto sys.path.
    import bot
    assert hasattr(bot, "parse_duration_to_seconds")
