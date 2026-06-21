def test_build_default_event_uses_guild_default_max_voting_layers(temp_db):
    db = temp_db
    settings = {**db.DEFAULT_GUILD_SETTINGS, "default_max_voting_layers": 6}
    ev = db.build_default_event(settings=settings)
    assert ev["max_voting_layers"] == 6


def test_build_default_event_falls_back_to_10_when_settings_none(temp_db):
    db = temp_db
    ev = db.build_default_event(settings=None)
    assert ev["max_voting_layers"] == 10


def test_build_default_event_falls_back_to_10_when_key_missing(temp_db):
    db = temp_db
    ev = db.build_default_event(settings={"language": "en"})
    assert ev["max_voting_layers"] == 10
