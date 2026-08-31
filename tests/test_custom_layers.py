"""Custom maps: storage, parsing and materialization."""

import pytest

import custom_layers as cl
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


BULLETED = """
- Belaya_TC_v1
- Belaya_Skirmish_v1
- Belaya_RAAS_v1
"""

BARE = """Belaya_TC_v1

Belaya_Skirmish_v1


Belaya_RAAS_v1
"""


def test_both_input_formats_produce_the_same_result():
    assert cl.parse_custom_layers(BULLETED) == cl.parse_custom_layers(BARE)


def test_parses_map_mode_and_version():
    map_name, layers = cl.parse_custom_layers("Belaya_TC_v1\nBelaya_Invasion_v2")
    assert map_name == "Belaya"
    assert layers[0] == {"raw_name": "Belaya_TC_v1",
                         "gamemode_token": "TC", "layer_version": "v1"}
    assert layers[1] == {"raw_name": "Belaya_Invasion_v2",
                         "gamemode_token": "Invasion", "layer_version": "v2"}


def test_gamemode_token_sits_in_front_of_the_version():
    # AlBasrah_AAS_v3_CL — the trailing token must not be mistaken for the mode
    assert cl.split_raw_name("AlBasrah_AAS_v3_CL") == ("AlBasrah", "AAS", "v3")


def test_layer_without_a_version():
    assert cl.split_raw_name("Belaya_RAAS") == ("Belaya", "RAAS", None)


def test_duplicate_lines_collapse():
    _, layers = cl.parse_custom_layers("Belaya_TC_v1\nBelaya_TC_v1")
    assert len(layers) == 1


def test_rejects_empty_input():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("   \n\n  ")
    assert exc.value.key == "custom_map.err_empty"


def test_rejects_invalid_lines():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("Belaya_TC_v1\nnot a layer name")
    assert exc.value.key == "custom_map.err_invalid_lines"
    assert "not a layer name" in exc.value.params["lines"]


def test_rejects_layers_from_different_maps():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("Belaya_TC_v1\nKokan_AAS_v1")
    assert exc.value.key == "custom_map.err_mixed_maps"
    assert "Belaya" in exc.value.params["maps"]
    assert "Kokan" in exc.value.params["maps"]


def test_map_token_comparison_ignores_case():
    map_name, layers = cl.parse_custom_layers("Belaya_TC_v1\nbelaya_AAS_v1")
    assert map_name == "Belaya"          # first spelling wins
    assert len(layers) == 2


def test_rejects_more_than_the_layer_cap():
    text = "\n".join(f"Belaya_AAS_v{i}" for i in range(1, cl.MAX_LAYERS_PER_MAP + 2))
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers(text)
    assert exc.value.key == "custom_map.err_too_many"


def test_normalize_map_name_falls_back_and_truncates():
    assert cl.normalize_map_name("  Belaya Downs ", "Belaya") == "Belaya Downs"
    assert cl.normalize_map_name("", "Belaya") == "Belaya"
    assert len(cl.normalize_map_name("x" * 200, "Belaya")) == cl.MAX_MAP_NAME_LENGTH


def test_case_variant_duplicates_collapse():
    map_name, layers = cl.parse_custom_layers(
        "Belaya_TC_v1\nbelaya_tc_v1\nBELAYA_TC_V1")
    assert map_name == "Belaya"
    assert [l["raw_name"] for l in layers] == ["Belaya_TC_v1"]  # first spelling wins


def test_rejects_a_layer_with_no_gamemode_token():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("Belaya_v1")
    assert exc.value.key == "custom_map.err_invalid_lines"
    assert "Belaya_v1" in exc.value.params["lines"]


def test_resolve_reference_source_prefers_main(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    _seed_layer(temp_db, "Sanxian_AAS_v1", "supermod", "Sanxian", "AAS")
    assert cl.resolve_reference_source() == "main"


def test_resolve_reference_source_falls_back_to_any_cached_source(temp_db):
    _seed_layer(temp_db, "Sanxian_AAS_v1", "supermod", "Sanxian", "AAS")
    assert cl.resolve_reference_source() == "supermod"


def test_resolve_reference_source_ignores_custom_sources(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert cl.resolve_reference_source() is None


def test_gamemode_token_map_is_derived_from_the_cache(temp_db):
    _seed_layer(temp_db, "Anvil_TC_v1", "main", "Anvil", "TerritoryControl")
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")

    token_map = cl.build_gamemode_token_map("main")
    assert token_map["TC"] == "TerritoryControl"
    assert token_map["AAS"] == "AAS"


def test_inactive_gamemodes_lists_modes_the_guild_switched_off():
    token_map = {"TC": "TerritoryControl", "AAS": "AAS"}
    layers = [
        {"raw_name": "Belaya_TC_v1", "gamemode_token": "TC", "layer_version": "v1"},
        {"raw_name": "Belaya_AAS_v1", "gamemode_token": "AAS", "layer_version": "v1"},
        {"raw_name": "Belaya_Skirmish_v1", "gamemode_token": "Skirmish",
         "layer_version": "v1"},
    ]
    assert cl.inactive_gamemodes(layers, ["AAS", "RAAS"], token_map) == [
        "TerritoryControl", "Skirmish"]


def test_inactive_gamemodes_empty_allowlist_means_no_warning():
    assert cl.inactive_gamemodes(
        [{"raw_name": "x_AAS_v1", "gamemode_token": "AAS", "layer_version": "v1"}],
        [], {}) == []
