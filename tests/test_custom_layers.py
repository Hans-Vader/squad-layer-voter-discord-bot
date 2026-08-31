"""Custom maps: storage, parsing and materialization."""

import pytest

import custom_layers as cl
import database
import utils


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


REFERENCE = {
    "USA": {"factionName": "United States Army",
            "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR"},
    "RGF": {"factionName": "Russian Ground Forces",
            "defaultUnit": "RGF_LO_CombinedArms", "alliance": "REDFOR"},
}
ALL_UNITS = ["CombinedArms", "Mechanized", "Motorized"]


def _seed_reference_cache(db):
    """Two main-source layers carrying the faction metadata custom maps borrow."""
    factions = [
        {"factionId": "USA", "factionName": "United States Army",
         "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR",
         "availableOnTeams": [1, 2],
         "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"},
                       {"type": "Mechanized", "name": "Mechanized"}]},
        {"factionId": "RGF", "factionName": "Russian Ground Forces",
         "defaultUnit": "RGF_LO_CombinedArms", "alliance": "REDFOR",
         "availableOnTeams": [1, 2],
         "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"},
                       {"type": "Motorized", "name": "Motorized"}]},
    ]
    _seed_layer(db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS", factions=factions)
    _seed_layer(db, "Anvil_TC_v1", "main", "Anvil", "TerritoryControl",
                factions=factions)


def test_build_custom_factions_empty_selection_means_everything():
    out = cl.build_custom_factions([], [], REFERENCE, ALL_UNITS)
    assert [f["factionId"] for f in out] == ["RGF", "USA"]      # sorted
    assert [u["type"] for u in out[0]["unitTypes"]] == ALL_UNITS


def test_build_custom_factions_is_a_cross_product():
    out = cl.build_custom_factions(["USA"], ["Mechanized"], REFERENCE, ALL_UNITS)
    assert len(out) == 1
    assert out[0]["unitTypes"] == [{"type": "Mechanized", "name": "Mechanized"}]


def test_build_custom_factions_borrows_metadata_and_spans_both_teams():
    out = cl.build_custom_factions(["USA"], [], REFERENCE, ALL_UNITS)
    assert out[0]["defaultUnit"] == "USA_LO_CombinedArms"
    assert out[0]["alliance"] == "BLUFOR"
    assert out[0]["factionName"] == "United States Army"
    assert out[0]["availableOnTeams"] == [1, 2]


def test_build_custom_factions_tolerates_an_unknown_faction():
    out = cl.build_custom_factions(["MADEUP"], ["CombinedArms"], REFERENCE, ALL_UNITS)
    assert out[0]["factionId"] == "MADEUP"
    assert out[0]["defaultUnit"] == ""


def test_save_materializes_into_the_cache(temp_db):
    _seed_reference_cache(temp_db)
    written = cl.save_custom_map(1, "Belaya",
                                 ["Belaya_TC_v1", "Belaya_AAS_v1"], ["USA"], [])
    assert written == 2

    source = temp_db.custom_source(1)
    assert temp_db.get_unique_maps(allowed_sources=[source]) == ["Belaya"]

    layer = temp_db.get_layer_by_raw_name("Belaya_TC_v1", allowed_sources=[source])
    assert layer["gamemode"] == "TerritoryControl"     # token translated
    assert layer["layer_version"] == "v1"
    assert [f["factionId"] for f in layer["factions"]] == ["USA"]
    assert layer["team1_allowed_alliances"] == []


def test_materialization_survives_a_cache_wipe(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])

    temp_db.clear_layer_cache()
    _seed_reference_cache(temp_db)                     # what a refresh restores
    assert cl.materialize_custom_layers() == 1

    source = temp_db.custom_source(1)
    modes = temp_db.get_modes_for_map("Belaya", allowed_sources=[source])
    assert [m["display"] for m in modes] == ["TerritoryControl v1"]


def test_materialization_without_a_reference_source_is_a_no_op(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v1"],
                                            "factions": [], "units": []})
    assert cl.materialize_custom_layers() == 0


def test_resaving_drops_layers_that_are_gone(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1", "Belaya_AAS_v1"], [], [])
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])

    source = temp_db.custom_source(1)
    assert temp_db.get_layer_by_raw_name("Belaya_AAS_v1",
                                         allowed_sources=[source]) is None
    assert temp_db.get_layer_by_raw_name("Belaya_TC_v1",
                                         allowed_sources=[source]) is not None


def test_remove_clears_both_the_definition_and_the_cache(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])

    assert cl.remove_custom_map(1, "Belaya") is True
    assert temp_db.get_custom_maps(1) == []
    assert temp_db.get_unique_maps(allowed_sources=[temp_db.custom_source(1)]) == []
    assert cl.remove_custom_map(1, "Belaya") is False


def test_materialization_is_scoped_when_a_guild_is_given(temp_db):
    _seed_reference_cache(temp_db)
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v1"],
                                            "factions": [], "units": []})
    temp_db.upsert_custom_map(2, "Kokan", {"layers": ["Kokan_TC_v1"],
                                           "factions": [], "units": []})
    assert cl.materialize_custom_layers(1) == 1
    assert temp_db.has_layers_for_source(temp_db.custom_source(2)) is False


def test_count_layers_is_scoped_to_source_and_map(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    _seed_layer(temp_db, "Kokan_TC_v1", "custom:1", "Kokan", "TerritoryControl")
    assert temp_db.count_layers("custom:1", "Belaya") == 1
    assert temp_db.count_layers("custom:1", "Nope") == 0


def test_save_reports_zero_when_nothing_could_be_materialized(temp_db):
    # No fetched source cached, so there is no faction metadata to borrow and
    # materialization writes nothing — the save must not claim success.
    written = cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    assert written == 0
    # The definition is still stored, so a later refresh can materialize it.
    assert [m["map_name"] for m in temp_db.get_custom_maps(1)] == ["Belaya"]


def test_source_label_hides_the_internal_custom_name():
    assert utils.source_label("main") == "main"
    assert utils.source_label("custom:123456789", "de") != "custom:123456789"
    assert utils.source_label("custom:123456789", "en") != "custom:123456789"


def test_event_sources_no_longer_append_the_custom_source(temp_db):
    # The custom source is an ordinary source now: an event that did not
    # select it does not get it, even though the guild has custom layers.
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    assert botmod._resolve_event_sources(
        {"allowed_sources": ["main"]}, {}, 1) == ["main"]


def test_event_sources_include_the_custom_source_when_selected(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    assert botmod._resolve_event_sources(
        {"allowed_sources": ["main", "custom:1"]}, {}, 1) == ["main", "custom:1"]


def test_units_source_redirects_custom_to_the_reference(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    assert botmod._units_source("main") == "main"
    assert botmod._units_source("custom:1") == "main"


def test_event_sources_never_resolve_to_an_unfiltered_list(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    # Event pinned to a source the guild cap no longer allows: the intersection
    # is empty, and an empty list downstream means "no filter" — which would
    # expose every guild's custom rows. The fallback is everything this guild
    # has, custom source included.
    assert botmod._resolve_event_sources(
        {"allowed_sources": ["gone"]}, {"allowed_sources": ["main"]}, 1) == [
            "main", "custom:1"]


def test_offered_sources_include_the_guilds_custom_source(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    assert botmod._resolve_offered_sources({}, 1) == ["main", "custom:1"]
    # ...and the guild default still caps it.
    assert botmod._resolve_offered_sources(
        {"allowed_sources": ["main"]}, 1) == ["main"]


def test_save_only_materializes_the_map_being_saved(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    cl.save_custom_map(1, "Kokan", ["Kokan_AAS_v1"], [], [])

    source = temp_db.custom_source(1)
    temp_db.delete_layers(source, "Belaya")          # stale cache for the other map
    assert cl.save_custom_map(1, "Kokan", ["Kokan_AAS_v1"], [], []) == 1
    # Saving Kokan must not have touched Belaya's rows either way.
    assert temp_db.count_layers(source, "Belaya") == 0


def test_get_fetched_sources_excludes_custom(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert temp_db.get_fetched_sources() == ["main"]


def test_guild_sources_append_only_the_guilds_own(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    _seed_layer(temp_db, "Kokan_TC_v1", "custom:2", "Kokan", "TerritoryControl")
    assert temp_db.get_guild_sources(1) == ["main", "custom:1"]
    assert temp_db.get_guild_sources(2) == ["main", "custom:2"]


def test_guild_sources_omit_a_custom_source_with_no_layers(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    assert temp_db.get_guild_sources(1) == ["main"]


def test_guild_sources_put_the_custom_source_last(temp_db):
    # Fetched sources sort alphabetically; the guild's own always comes last,
    # so a picker's order is stable and the custom entry reads as distinct.
    _seed_layer(temp_db, "zulu_AAS_v1", "zulu", "Zulu", "AAS")
    _seed_layer(temp_db, "alpha_AAS_v1", "alpha", "Alpha", "AAS")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert temp_db.get_guild_sources(1) == ["alpha", "zulu", "custom:1"]


def test_every_edit_property_source_takes_a_guild_id(temp_db):
    # A uniform Callable[[int], list[str]] is what lets a future change make
    # any of these guild-aware without another signature migration.
    import bot as botmod
    _seed_reference_cache(temp_db)
    tables = botmod._EDIT_PROPERTIES + botmod._GUILD_EDIT_PROPERTIES
    callables = [p["source"] for p in tables if p.get("source")]
    assert callables, "expected the property tables to carry source callables"
    for fn in callables:
        assert isinstance(fn(1), list)


def test_allowed_sources_property_offers_the_custom_source(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    for table in (botmod._EDIT_PROPERTIES, botmod._GUILD_EDIT_PROPERTIES):
        prop = next(p for p in table if p["key"] == "allowed_sources")
        assert prop["source"](1) == ["main", "custom:1"]
