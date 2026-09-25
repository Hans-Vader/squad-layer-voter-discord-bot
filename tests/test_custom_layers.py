"""Custom maps: storage, parsing and materialization."""

import pytest

import custom_layers as cl
import database
import utils


def _seed_layer(db, raw_name, source, map_name, gamemode, factions=None,
                layer_version="v1", variant=None):
    """Put one layer row in the cache with the shape fetch_and_cache_layers writes."""
    db.upsert_layer(
        raw_name=raw_name, source=source, map_name=map_name, map_id=map_name.lower(),
        gamemode=gamemode, layer_version=layer_version,
        factions=factions if factions is not None else [],
        team1_alliances=[], team2_alliances=[], map_size_km=None,
        variant=variant,
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
    # The parsed token is capped too: _LAYER_RE puts no bound on it, and an
    # over-long name outgrows both the modal field that prefills it on an edit
    # and the 100-char select values the pickers match on.
    assert len(cl.normalize_map_name("", "A" * 200)) == cl.MAX_MAP_NAME_LENGTH


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


REFERENCE_FACTIONS = [
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


def _seed_reference_cache(db):
    """Two main-source layers carrying the faction metadata custom maps borrow."""
    _seed_layer(db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS",
                factions=REFERENCE_FACTIONS)
    _seed_layer(db, "Anvil_TC_v1", "main", "Anvil", "TerritoryControl",
                factions=REFERENCE_FACTIONS)


def _seed_stock_maps(db, *maps):
    """Main-source AAS layers for these (token, map name) pairs — Chora and
    Narva by default — carrying the faction data packs borrow."""
    for token, map_name in maps or (("Chora", "Chora"), ("Narva", "Narva")):
        _seed_layer(db, f"{token}_AAS_v1", "main", map_name, "AAS",
                    factions=REFERENCE_FACTIONS)


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
    assert botmod._resolve_event_sources({"allowed_sources": ["main"]}, 1) == ["main"]


def test_event_sources_include_the_custom_source_when_selected(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    assert botmod._resolve_event_sources(
        {"allowed_sources": ["main", "custom:1"]}, 1) == ["main", "custom:1"]


def test_units_source_redirects_custom_to_the_reference(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    assert botmod._units_source("main") == "main"
    assert botmod._units_source("custom:1") == "main"


def test_guild_default_does_not_narrow_a_running_event(temp_db):
    # The guild defaults seed a new event and then stop mattering, exactly like
    # every other value in EVENT_CONFIG_KEYS. Editing them must not reach into
    # an event already in flight.
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    temp_db.save_guild_settings(1, {"allowed_sources": ["main"]})

    assert botmod._resolve_event_sources(
        {"allowed_sources": ["main", "custom:1"]}, 1) == ["main", "custom:1"]


def test_event_sources_are_empty_only_when_the_guild_has_none(temp_db):
    # Nothing cached, so there is genuinely nothing to offer. The resolver says
    # so honestly rather than returning a list that downstream reads as "no
    # source filter" — the callers turn this into a "cache empty" reply.
    import bot as botmod
    assert botmod._resolve_event_sources({}, 1) == []


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
    temp_db.delete_layers_by_raw_names(source, ["Belaya_TC_v1"])  # stale cache for the other map
    assert cl.save_custom_map(1, "Kokan", ["Kokan_AAS_v1"], [], []) == 1
    # Saving Kokan must not have touched Belaya's rows either way.
    assert temp_db.get_layer_by_raw_name("Belaya_TC_v1",
                                         allowed_sources=[source]) is None


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


def test_event_allowed_sources_choices_are_not_capped(temp_db):
    # The guild default no longer filters a running event, so capping the
    # event's own choice list would hide options that work perfectly well.
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    temp_db.save_guild_settings(1, {"allowed_sources": ["main"]})

    prop = next(p for p in botmod._EDIT_PROPERTIES if p["key"] == "allowed_sources")
    assert prop["source"](1) == ["main", "custom:1"]


def test_guild_allowed_sources_choices_are_not_capped_by_themselves(temp_db):
    # The guild-level allowed_sources entry (/config_defaults) IS the cap, so
    # its own choice list must stay uncapped -- otherwise a source disabled
    # there could never be re-enabled again.
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    temp_db.save_guild_settings(1, {"allowed_sources": ["main"]})

    prop = next(p for p in botmod._GUILD_EDIT_PROPERTIES if p["key"] == "allowed_sources")
    assert prop["source"](1) == ["main", "custom:1"]


def test_colliding_map_name_flags_a_prefix_in_either_direction(temp_db):
    # The blacklist filter matches by case-insensitive prefix across every
    # source, so either direction of prefix means both maps would be hidden.
    _seed_layer(temp_db, "BelayaDowns_AAS_v1", "main", "Belaya Downs", "AAS")
    assert cl.colliding_map_name("Belaya") == "Belaya Downs"
    assert cl.colliding_map_name("belaya downs extra") == "Belaya Downs"
    assert cl.colliding_map_name("Kokan") is None


def test_colliding_map_name_ignores_custom_maps(temp_db):
    # Re-using an existing custom map's name is the documented way to replace
    # it, so only fetched sources are compared against.
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert cl.colliding_map_name("Belaya") is None


def test_colliding_map_name_is_none_when_nothing_was_fetched(temp_db):
    # get_unique_maps(allowed_sources=[]) means "no filter", which would
    # compare against every guild's custom rows. The empty case is guarded.
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert cl.colliding_map_name("Belaya") is None


def test_colliding_map_name_tolerates_blank_input(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    assert cl.colliding_map_name("   ") is None


# ── Steam Workshop link ──────────────────────────────────────────────────────

WORKSHOP = "https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901"


@pytest.mark.parametrize("entered", [
    WORKSHOP,
    "http://steamcommunity.com/sharedfiles/filedetails/?id=3025678901",
    "https://www.steamcommunity.com/sharedfiles/filedetails/?id=3025678901",
    "https://steamcommunity.com/workshop/filedetails/?id=3025678901",
    "https://steamcommunity.com/sharedfiles/filedetails?id=3025678901",
    # Steam prepends snr= from a browse page and l= from the language switcher,
    # so id= is not always the first query parameter.
    "https://steamcommunity.com/sharedfiles/filedetails/?snr=1_5_9__405&id=3025678901",
    "https://steamcommunity.com/sharedfiles/filedetails/?l=german&id=3025678901",
    # Tracking junk and a quote that would otherwise break the masked link.
    'https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901&searchtext="x',
    "  " + WORKSHOP + "  ",
])
def test_normalize_workshop_url_rebuilds_canonical_url(entered):
    assert cl.normalize_workshop_url(entered) == WORKSHOP


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_normalize_workshop_url_empty_is_none(blank):
    assert cl.normalize_workshop_url(blank) is None


@pytest.mark.parametrize("bad", [
    "https://example.com/mod/123",
    "https://steamcommunity.com/sharedfiles/filedetails/",
    "steamcommunity.com/app/393380",
    "3025678901",
    "javascript:alert(1)",
    # Host spoofing: the pattern is anchored, so neither a suffixed host nor
    # the real host appearing in a path is enough.
    "https://steamcommunity.com.evil.com/sharedfiles/filedetails/?id=1",
    "https://evil.com/steamcommunity.com/sharedfiles/filedetails/?id=1",
    # id= must be a parameter of its own, and numeric.
    "https://steamcommunity.com/sharedfiles/filedetails/?myid=1",
    "https://steamcommunity.com/sharedfiles/filedetails/?id=abc",
    # Non-ASCII digits would template into a dead link.
    "https://steamcommunity.com/sharedfiles/filedetails/?id=١٢٣",
])
def test_normalize_workshop_url_rejects_non_workshop_links(bad):
    with pytest.raises(cl.CustomLayerError) as e:
        cl.normalize_workshop_url(bad)
    assert e.value.key == "custom_map.err_bad_workshop_url"


def test_save_custom_map_stores_workshop_url(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS",
                factions=[{"factionId": "USA", "factionName": "United States",
                           "defaultUnit": "USA_LO_CombinedArms",
                           "availableOnTeams": [1, 2],
                           "unitTypes": [{"type": "CombinedArms"}],
                           "alliance": "BLUFOR"}])
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [], WORKSHOP)

    assert cl.workshop_url_for(1, "Belaya_TC_v1") == WORKSHOP
    assert cl.workshop_url_for(1, "Kokan_TC_v1") is None
    assert cl.workshop_url_for(2, "Belaya_TC_v1") is None


def test_save_custom_map_without_workshop_url(temp_db):
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    assert cl.workshop_url_for(1, "Belaya_TC_v1") is None


# ── Mod layers on stock maps: the cache ─────────────────────────────────────

def test_init_db_adds_the_variant_column_to_an_existing_cache(tmp_path, monkeypatch):
    # A cache created before mod layers has no `variant` column; init_db adds
    # it in place, keeping the rows.
    import sqlite3
    db_file = tmp_path / "old.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE layer_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "raw_name TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'default', "
        "map_name TEXT NOT NULL, map_id TEXT NOT NULL, gamemode TEXT NOT NULL, "
        "layer_version TEXT, map_size_km REAL, "
        "factions_json TEXT NOT NULL DEFAULT '[]', "
        "team1_allowed_alliances TEXT NOT NULL DEFAULT '[]', "
        "team2_allowed_alliances TEXT NOT NULL DEFAULT '[]', "
        "cached_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "UNIQUE(raw_name, source))")
    conn.execute("INSERT INTO layer_cache (raw_name, source, map_name, map_id, gamemode) "
                 "VALUES ('Chora_AAS_v1', 'main', 'Chora', 'chora', 'AAS')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DB_FILE", str(db_file))

    database.init_db()

    layer = database.get_layer_by_raw_name("Chora_AAS_v1")
    assert layer["map_name"] == "Chora"
    assert layer["variant"] is None


def test_layer_rows_carry_their_variant(temp_db):
    _seed_layer(temp_db, "Chora_AAS_v1", "main", "Chora", "AAS")
    _seed_layer(temp_db, "SEC_26_Chora_AAS_v2", "custom:1", "Chora", "AAS",
                layer_version="v2", variant="SEC 26")
    assert temp_db.get_layer_by_raw_name("Chora_AAS_v1")["variant"] is None
    assert temp_db.get_layer_by_raw_name("SEC_26_Chora_AAS_v2")["variant"] == "SEC 26"


def test_modes_list_vanilla_first_then_each_variant(temp_db):
    _seed_layer(temp_db, "Chora_AAS_v2", "main", "Chora", "AAS", layer_version="v2")
    _seed_layer(temp_db, "Chora_AAS_v1", "main", "Chora", "AAS")
    _seed_layer(temp_db, "SEC_36_Chora_TC_v1", "custom:1", "Chora",
                "TerritoryControl", variant="SEC 36")
    _seed_layer(temp_db, "SEC_26_Chora_AAS_v1", "custom:1", "Chora", "AAS",
                variant="SEC 26")

    modes = temp_db.get_modes_for_map("Chora", allowed_sources=["main", "custom:1"])

    assert [m["display"] for m in modes] == [
        "AAS v1", "AAS v2", "SEC 26 · AAS v1", "SEC 36 · TerritoryControl v1"]
    assert [m["variant"] for m in modes] == [None, None, "SEC 26", "SEC 36"]


def test_a_layer_lookup_can_be_pinned_to_one_map(temp_db):
    # The same raw name may sit in two sources on two different maps.
    _seed_layer(temp_db, "Chora_AAS_v1", "main", "Chora", "AAS")
    _seed_layer(temp_db, "Chora_AAS_v1", "custom:1", "MyChora", "AAS")
    both = ["main", "custom:1"]
    assert temp_db.get_layer_by_raw_name(
        "Chora_AAS_v1", allowed_sources=both, map_name="MyChora")["source"] == "custom:1"
    assert temp_db.get_layer_by_raw_name(
        "Chora_AAS_v1", allowed_sources=both, map_name="Chora")["source"] == "main"


def test_map_raw_names_pairs_every_row_of_a_source(temp_db):
    _seed_layer(temp_db, "FoolsRoad_AAS_v1", "main", "Fool's Road", "AAS")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert temp_db.get_map_raw_names(["main"]) == [("Fool's Road", "FoolsRoad_AAS_v1")]


def test_own_custom_maps_leave_out_mod_layer_rows(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    _seed_layer(temp_db, "SEC_26_Chora_AAS_v2", "custom:1", "Chora", "AAS",
                variant="SEC 26")
    _seed_layer(temp_db, "Kokan_TC_v1", "custom:2", "Kokan", "TerritoryControl")
    assert temp_db.get_own_custom_maps(1) == ["Belaya"]


def test_delete_layers_by_raw_names_is_scoped_to_one_source(temp_db):
    _seed_layer(temp_db, "SEC_26_Chora_AAS_v2", "custom:1", "Chora", "AAS",
                variant="SEC 26")
    _seed_layer(temp_db, "SEC_36_Chora_AAS_v1", "custom:1", "Chora", "AAS",
                variant="SEC 36")
    _seed_layer(temp_db, "SEC_26_Chora_AAS_v2", "custom:2", "Chora", "AAS",
                variant="SEC 26")

    assert temp_db.delete_layers_by_raw_names("custom:1", ["SEC_26_Chora_AAS_v2"]) == 1
    assert temp_db.delete_layers_by_raw_names("custom:1", []) == 0

    assert [m["raw_name"] for m in temp_db.get_modes_for_map(
        "Chora", allowed_sources=["custom:1"])] == ["SEC_36_Chora_AAS_v1"]
    assert temp_db.has_layers_for_source("custom:2") is True


# ── Mod layers on stock maps: parsing ───────────────────────────────────────

MAP_TOKENS = {
    "chora": "Chora", "mutaha": "Mutaha", "gorodok": "Gorodok",
    "goosebay": "Goose Bay", "narva": "Narva", "albasrah": "Al Basrah",
    "sumari": "Sumari Bala", "logar": "Logar Valley", "harju": "Harju",
    "yehorivka": "Yehorivka", "fallujah": "Fallujah",
    "blackcoast": "Black Coast", "foolsroad": "Fool's Road",
}

SEC_26 = """SEC_26_Chora_AAS_v2
SEC_26_Mutaha_PAAS_v1
SEC_26_Gorodok_TC_v1
SEC_26_GooseBay_AAS_v1
SEC_26_Narva_AAS_v3
SEC_26_Mutaha_TC_v1
SEC_26_AlBasrah_AAS_v1
SEC_26_Narva_AAS_v1
SEC_26_Sumari_AAS_v1
SEC_26_Logar_AAS_v1
SEC_26_Harju_AAS_v1
SEC_26_Yehorivka_AAS_v1
SEC_26_Chora_AAS_v1
SEC_26_Fallujah_AAS_v1
SEC_26_BlackCoast_RAAS_V1
SEC_26_Gorodok_RAAS_V1
"""


def test_version_token_ignores_case():
    assert cl.split_raw_name("Belaya_TC_V1") == ("Belaya", "TC", "v1")


def test_map_token_map_is_derived_from_the_cache(temp_db):
    _seed_layer(temp_db, "FoolsRoad_AAS_v1", "main", "Fool's Road", "AAS")
    _seed_layer(temp_db, "FoolsRoad_RAAS_v1", "main", "Fool's Road", "RAAS")
    _seed_layer(temp_db, "Sumari_AAS_v1", "main", "Sumari Bala", "AAS")
    _seed_layer(temp_db, "Chora_AAS_v1", "supermod", "SPM | Chora", "AAS")
    assert cl.build_map_token_map("main") == {
        "foolsroad": "Fool's Road", "sumari": "Sumari Bala"}


def test_map_token_map_drops_a_token_that_names_two_maps(temp_db):
    _seed_layer(temp_db, "SPM_Chora_AAS_v1", "supermod", "SPM | Chora", "AAS")
    _seed_layer(temp_db, "SPM_Narva_AAS_v1", "supermod", "SPM | Narva", "AAS")
    assert cl.build_map_token_map("supermod") == {}


def test_a_mod_layer_is_placed_on_its_stock_map():
    assert cl.split_pack_name("SEC_26_Chora_AAS_v2", MAP_TOKENS) == {
        "raw_name": "SEC_26_Chora_AAS_v2", "map_name": "Chora",
        "gamemode_token": "AAS", "layer_version": "v2", "variant": "SEC 26"}


def test_a_mod_layer_reads_an_uppercase_version():
    layer = cl.split_pack_name("SEC_26_BlackCoast_RAAS_V1", MAP_TOKENS)
    assert (layer["map_name"], layer["gamemode_token"], layer["layer_version"]) == (
        "Black Coast", "RAAS", "v1")


def test_every_leftover_token_joins_the_variant():
    # Two layers differing only in "Winter"/"Summer" must never render alike.
    layer = cl.split_pack_name("Mod_Narva_Winter_AAS_v1", MAP_TOKENS)
    assert layer["variant"] == "Mod Winter"
    assert layer["gamemode_token"] == "AAS"


def test_a_name_without_a_mode_has_an_empty_gamemode_token():
    assert cl.split_pack_name("Rivals_W1_FoolsRoad", MAP_TOKENS)["gamemode_token"] == ""
    assert cl.split_pack_name("SEC_26_Chora_v1", MAP_TOKENS)["gamemode_token"] == ""


def test_a_leading_stock_map_token_is_not_a_mod_layer():
    # Shaped like a main-game layer: stays a classic custom map, where the
    # name-collision guard deals with it.
    assert cl.split_pack_name("Chora_AAS_v1", MAP_TOKENS) is None
    assert cl.split_pack_name("Belaya_TC_v1", MAP_TOKENS) is None


def test_a_whole_sec_pack_parses_onto_twelve_maps():
    name, layers = cl.parse_custom_layers(SEC_26, MAP_TOKENS)
    assert name == "SEC 26"
    assert cl.is_pack(layers)
    assert len(layers) == 16
    assert len({layer["map_name"] for layer in layers}) == 12
    assert {layer["variant"] for layer in layers} == {"SEC 26"}


def test_a_pack_may_mix_variants():
    name, layers = cl.parse_custom_layers(
        "SEC_26_Chora_AAS_v2\nSEC_36_Chora_AAS_v1", MAP_TOKENS)
    assert name == "SEC 26"
    assert [layer["variant"] for layer in layers] == ["SEC 26", "SEC 36"]


def test_a_pack_layer_without_a_mode_is_refused():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("Rivals_W1_FoolsRoad\nOSI_W1_Chora", MAP_TOKENS)
    assert exc.value.key == "custom_map.err_pack_no_mode"
    assert "Rivals_W1_FoolsRoad" in exc.value.params["lines"]
    assert "OSI_W1_Chora" in exc.value.params["lines"]


def test_pack_and_classic_lines_are_not_mixed():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("SEC_26_Chora_AAS_v2\nBelaya_TC_v1", MAP_TOKENS)
    assert exc.value.key == "custom_map.err_mixed_kinds"
    assert exc.value.params["lines"] == "Belaya_TC_v1"


def test_a_vanilla_name_inside_a_pack_is_named_in_the_refusal():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("SEC_26_Chora_AAS_v2\nChora_AAS_v1", MAP_TOKENS)
    assert exc.value.key == "custom_map.err_mixed_kinds"
    assert exc.value.params["lines"] == "Chora_AAS_v1"


def test_a_classic_map_parses_the_same_with_map_tokens():
    text = "Belaya_TC_v1\nBelaya_RAAS_v1"
    assert cl.parse_custom_layers(text, MAP_TOKENS) == cl.parse_custom_layers(text)
    assert not cl.is_pack(cl.parse_custom_layers(text, MAP_TOKENS)[1])


# ── Mod layers on stock maps: storage ───────────────────────────────────────

def test_a_pack_materializes_onto_its_stock_maps(temp_db):
    _seed_stock_maps(temp_db)
    _seed_layer(temp_db, "Chora_TC_v1", "main", "Chora", "TerritoryControl",
                factions=REFERENCE_FACTIONS)

    written = cl.save_custom_map(
        1, "SEC 26", ["SEC_26_Chora_AAS_v2", "SEC_26_Chora_TC_v1", "SEC_26_Narva_AAS_v1"],
        ["USA"], [], pack=True)

    assert written == 3
    layer = temp_db.get_layer_by_raw_name("SEC_26_Chora_TC_v1",
                                          allowed_sources=["custom:1"])
    assert layer["map_name"] == "Chora"
    assert layer["variant"] == "SEC 26"
    assert layer["gamemode"] == "TerritoryControl"      # token translated
    assert layer["layer_version"] == "v1"
    assert [f["factionId"] for f in layer["factions"]] == ["USA"]
    assert temp_db.get_unique_maps(allowed_sources=["custom:1"]) == ["Chora", "Narva"]
    assert temp_db.get_own_custom_maps(1) == []
    assert temp_db.get_custom_maps(1)[0]["payload"]["pack"] is True


def test_resaving_a_pack_drops_only_its_own_rows(temp_db):
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2", "SEC_26_Narva_AAS_v1"],
                       [], [], pack=True)
    cl.save_custom_map(1, "SEC 36", ["SEC_36_Chora_AAS_v1"], [], [], pack=True)

    cl.save_custom_map(1, "SEC 26", ["SEC_26_Narva_AAS_v1"], [], [], pack=True)

    src = temp_db.custom_source(1)
    assert temp_db.get_layer_by_raw_name("SEC_26_Chora_AAS_v2", allowed_sources=[src]) is None
    assert temp_db.get_layer_by_raw_name("SEC_36_Chora_AAS_v1", allowed_sources=[src]) is not None
    assert temp_db.get_layer_by_raw_name("SEC_26_Narva_AAS_v1", allowed_sources=[src]) is not None


def test_removing_a_pack_leaves_another_pack_on_the_same_map(temp_db):
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2"], [], [], pack=True)
    cl.save_custom_map(1, "SEC 36", ["SEC_36_Chora_AAS_v1"], [], [], pack=True)

    assert cl.remove_custom_map(1, "SEC 26") is True

    modes = temp_db.get_modes_for_map("Chora", allowed_sources=[temp_db.custom_source(1)])
    assert [m["raw_name"] for m in modes] == ["SEC_36_Chora_AAS_v1"]


def test_pack_layers_whose_stock_map_is_unknown_are_skipped(temp_db):
    # main failed to fetch and another source stands in as the reference, or
    # upstream renamed a map: the layer is skipped, never filed under a
    # guessed name like the pack's own.
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2", "SEC_26_Narva_AAS_v1"],
                       [], [], pack=True)

    temp_db.clear_layer_cache()
    _seed_stock_maps(temp_db, ("Narva", "Narva"))
    assert cl.materialize_custom_layers() == 1
    assert temp_db.get_unique_maps(allowed_sources=["custom:1"]) == ["Narva"]

    temp_db.clear_layer_cache()
    _seed_layer(temp_db, "SPM_Narva_AAS_v1", "supermod", "SPM | Narva", "AAS",
                factions=REFERENCE_FACTIONS)
    assert cl.materialize_custom_layers() == 0
    assert temp_db.get_unique_maps(allowed_sources=["custom:1"]) == []


def test_claimed_layer_names_the_entry_that_lists_it(temp_db):
    temp_db.upsert_custom_map(1, "SEC 26", {"layers": ["SEC_26_Chora_AAS_v2"],
                                            "pack": True})
    assert cl.claimed_layer(1, ["sec_26_chora_aas_v2"], {"SEC Copy"}) == (
        "sec_26_chora_aas_v2", "SEC 26")
    assert cl.claimed_layer(1, ["SEC_26_Chora_AAS_v2"], {"SEC 26"}) is None  # its own entry
    assert cl.claimed_layer(2, ["SEC_26_Chora_AAS_v2"], set()) is None       # another guild


def test_a_packs_workshop_link_is_found_by_raw_name(temp_db):
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2"], [], [], WORKSHOP, pack=True)
    assert cl.workshop_url_for(1, "SEC_26_Chora_AAS_v2") == WORKSHOP
    assert cl.workshop_url_for(1, "Chora") is None
