import bot as botmod


def test_guild_edit_properties_keys_exist_in_defaults():
    keys = [p["key"] for p in botmod._GUILD_EDIT_PROPERTIES]
    assert len(keys) == 15
    assert len(keys) == len(set(keys))  # no dupes
    for k in keys:
        assert k in botmod.db.DEFAULT_GUILD_SETTINGS, k
    assert "event_name" not in keys  # not editable at guild level


def test_guild_target_read_write_is_flat():
    settings = {"max_total_suggestions": 25}
    prop = {"key": "max_total_suggestions"}
    assert botmod._GUILD_TARGET.read(settings, prop) == 25
    botmod._GUILD_TARGET.write(settings, prop, 10)
    assert settings["max_total_suggestions"] == 10


def test_apply_guild_property_persists_value(temp_db):
    db = temp_db
    db.save_guild_settings(5, {"language": "en"})
    botmod._apply_guild_property(5, {"key": "max_total_suggestions"}, 7)
    assert db.get_guild_settings(5)["max_total_suggestions"] == 7
    assert db.get_guild_settings(5)["language"] == "en"


def test_apply_guild_property_seeds_defaults_when_unconfigured(temp_db):
    db = temp_db
    botmod._apply_guild_property(9, {"key": "default_mirror_match"}, True)
    got = db.get_guild_settings(9)
    assert got["default_mirror_match"] is True
    assert got["max_total_suggestions"] == 25  # materialized from defaults


def test_apply_guild_property_accepts_transform(temp_db):
    db = temp_db
    db.save_guild_settings(3, {"blacklisted_units": ["A"]})
    botmod._apply_guild_property(
        3, {"key": "blacklisted_units"}, lambda cur: sorted((set(cur or []) | {"B"})))
    assert db.get_guild_settings(3)["blacklisted_units"] == ["A", "B"]
