"""Custom maps: storage, parsing and materialization."""

import pytest

import database


def _seed_layer(db, raw_name, source, map_name, gamemode, factions=None,
                layer_version="v1"):
    """Put one layer row in the cache with the shape fetch_and_cache_layers writes."""
    db.upsert_layer(
        raw_name=raw_name, source=source, map_name=map_name, map_id=map_name.lower(),
        gamemode=gamemode, layer_version=layer_version,
        factions=factions if factions is not None else [],
        team1_alliances=[], team2_alliances=[], map_size_km=None,
    )


def test_custom_source_name():
    assert database.custom_source(42) == "custom:42"


def test_custom_map_roundtrip(temp_db):
    payload = {"layers": ["Belaya_TC_v1"], "factions": ["USA"], "units": []}
    temp_db.upsert_custom_map(1, "Belaya", payload)

    maps = temp_db.get_custom_maps(1)
    assert [m["map_name"] for m in maps] == ["Belaya"]
    assert maps[0]["payload"] == payload
    assert maps[0]["guild_id"] == 1
    assert temp_db.get_custom_maps(2) == []


def test_custom_map_upsert_overwrites(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v1"]})
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v2"]})
    assert temp_db.get_custom_maps(1)[0]["payload"]["layers"] == ["Belaya_TC_v2"]


def test_get_all_custom_maps_spans_guilds(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": []})
    temp_db.upsert_custom_map(2, "Kokan", {"layers": []})
    assert {(m["guild_id"], m["map_name"]) for m in temp_db.get_all_custom_maps()} == {
        (1, "Belaya"), (2, "Kokan")}


def test_delete_custom_map_reports_whether_it_existed(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": []})
    assert temp_db.delete_custom_map(1, "Belaya") is True
    assert temp_db.delete_custom_map(1, "Belaya") is False


def test_get_unique_sources_hides_custom(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert temp_db.get_unique_sources() == ["main"]


def test_has_layers_for_source(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert temp_db.has_layers_for_source("custom:1") is True
    assert temp_db.has_layers_for_source("custom:2") is False


def test_delete_layers_is_scoped_to_source_and_map(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    _seed_layer(temp_db, "Kokan_TC_v1", "custom:1", "Kokan", "TerritoryControl")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:2", "Belaya", "TerritoryControl")

    assert temp_db.delete_layers("custom:1", "Belaya") == 1
    assert temp_db.get_unique_maps(allowed_sources=["custom:1"]) == ["Kokan"]
    assert temp_db.get_unique_maps(allowed_sources=["custom:2"]) == ["Belaya"]


def test_get_faction_reference_prefers_entry_with_default_unit(temp_db):
    _seed_layer(temp_db, "X_AAS_v1", "main", "X", "AAS", factions=[
        {"factionId": "USA", "factionName": "", "defaultUnit": "",
         "alliance": "", "availableOnTeams": [1, 2], "unitTypes": []}])
    _seed_layer(temp_db, "Y_AAS_v1", "main", "Y", "AAS", factions=[
        {"factionId": "USA", "factionName": "United States Army",
         "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR",
         "availableOnTeams": [1, 2],
         "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"}]}])

    ref = temp_db.get_faction_reference(["main"])
    assert ref["USA"]["defaultUnit"] == "USA_LO_CombinedArms"
    assert ref["USA"]["alliance"] == "BLUFOR"
    assert ref["USA"]["factionName"] == "United States Army"


def test_get_gamemode_samples_one_per_mode(temp_db):
    _seed_layer(temp_db, "Anvil_TC_v1", "main", "Anvil", "TerritoryControl")
    _seed_layer(temp_db, "Kokan_TC_v1", "main", "Kokan", "TerritoryControl")
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")

    samples = dict(temp_db.get_gamemode_samples(["main"]))
    assert set(samples) == {"TerritoryControl", "AAS"}
    assert samples["TerritoryControl"].endswith("_TC_v1")
