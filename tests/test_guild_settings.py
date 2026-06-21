def test_save_and_get_guild_settings_roundtrip(temp_db):
    db = temp_db
    db.save_guild_settings(123, {"language": "de", "max_total_suggestions": 10})
    got = db.get_guild_settings(123)
    assert got["language"] == "de"
    assert got["max_total_suggestions"] == 10
    # Unset keys fall back to the hard-coded defaults via the merge.
    assert got["allowed_gamemodes"] == db.DEFAULT_GUILD_SETTINGS["allowed_gamemodes"]


def test_get_guild_settings_returns_none_when_unconfigured(temp_db):
    assert temp_db.get_guild_settings(999) is None


def test_default_max_voting_layers_present_via_merge(temp_db):
    db = temp_db
    db.save_guild_settings(1, {"language": "en"})  # legacy row, no new key
    got = db.get_guild_settings(1)
    assert got["default_max_voting_layers"] == 10
