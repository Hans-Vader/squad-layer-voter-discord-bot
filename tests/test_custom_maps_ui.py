"""Composition of the Custom Maps admin views."""

import asyncio

import discord

import bot as botmod
import custom_layers as cl
from i18n import t


def _actions(view):
    return [c.action for c in view.children if hasattr(c, "action")]


def _selects(view):
    return [c for c in view.children if isinstance(c, discord.ui.Select)]


def _buttons(view):
    return [c for c in view.children if isinstance(c, discord.ui.Button)]


def test_admin_panel_offers_custom_maps_in_every_phase():
    for phase in ("created", "suggestions_open", "suggestions_closed",
                  "voting", "completed"):
        view = botmod.AdminPanelView(phase, "de", 1)
        assert "custom_maps" in _actions(view), phase


def test_custom_maps_view_has_add_edit_delete_and_back():
    view = botmod.CustomMapsView("de", 1, [
        {"guild_id": 1, "map_name": "Belaya",
         "payload": {"layers": ["Belaya_TC_v1"]}},
    ])
    assert len(_selects(view)) == 2        # delete + edit pickers
    assert len(_buttons(view)) == 2        # add + back
    assert view.db_id == 1


def test_custom_maps_pickers_do_not_share_option_objects():
    # Two Selects holding the same SelectOption instances would show each
    # other's defaults; each picker gets its own.
    view = botmod.CustomMapsView("de", 1, [
        {"guild_id": 1, "map_name": "Belaya",
         "payload": {"layers": ["Belaya_TC_v1"]}},
    ])
    shared = {id(o) for o in view.delete_select.options} & \
             {id(o) for o in view.edit_select.options}
    assert not shared
    assert [o.value for o in view.edit_select.options] == ["Belaya"]


def test_custom_maps_view_without_maps_has_no_pickers():
    view = botmod.CustomMapsView("de", 1, [])
    assert _selects(view) == []
    assert len(_buttons(view)) == 2


def test_custom_maps_delete_picker_lists_every_map():
    view = botmod.CustomMapsView("de", 1, [
        {"guild_id": 1, "map_name": "Belaya", "payload": {"layers": []}},
        {"guild_id": 1, "map_name": "Kokan", "payload": {"layers": []}},
    ])
    assert [o.value for o in _selects(view)[0].options] == ["Belaya", "Kokan"]


class _StubSelect:
    """Stands in for the delete picker so the test never touches discord internals."""

    def __init__(self, values):
        self.values = values


class _StubResponse:
    def __init__(self):
        self.edited = None
        self.sent = None
        self.modal = None

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_message(self, **kwargs):
        self.sent = kwargs

    async def send_modal(self, modal):
        self.modal = modal


class _StubInteraction:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.response = _StubResponse()


def test_delete_asks_before_removing_anything(temp_db):
    # Picking a map in the delete dropdown must not delete it — it puts a
    # confirmation in front, the way every other destructive admin action does.
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v1"],
                                            "factions": [], "units": []})

    view = botmod.CustomMapsView("de", 1, temp_db.get_custom_maps(1))
    view.delete_select = _StubSelect(["Belaya"])
    interaction = _StubInteraction(1)

    asyncio.run(view._delete(interaction))

    assert [m["map_name"] for m in temp_db.get_custom_maps(1)] == ["Belaya"]
    assert isinstance(interaction.response.edited["view"], botmod.ConfirmActionView)


def test_delete_is_scoped_to_the_acting_guild(temp_db):
    # The confirmation's callback is what actually deletes, and it must key on
    # the guild that confirmed — never on a value carried in the component.
    temp_db.upsert_custom_map(1, "Belaya", {"layers": [], "factions": [], "units": []})
    temp_db.upsert_custom_map(2, "Belaya", {"layers": [], "factions": [], "units": []})

    view = botmod.CustomMapsView("de", 1, temp_db.get_custom_maps(1))
    view.delete_select = _StubSelect(["Belaya"])
    asked = _StubInteraction(1)
    asyncio.run(view._delete(asked))

    # The confirmation view is whatever _delete put on the message; reaching it
    # that way keeps the production class free of test-only attributes.
    confirm = _StubInteraction(1)
    asyncio.run(asked.response.edited["view"]._confirm_callback(confirm, 1))

    assert temp_db.get_custom_maps(1) == []
    assert [m["map_name"] for m in temp_db.get_custom_maps(2)] == ["Belaya"]
    assert confirm.response.edited is not None   # the panel was redrawn


LAYERS = [
    {"raw_name": "Belaya_TC_v1", "gamemode_token": "TC", "layer_version": "v1"},
    {"raw_name": "Belaya_AAS_v1", "gamemode_token": "AAS", "layer_version": "v1"},
]


def test_details_view_has_two_optional_selects_and_save():
    view = botmod.CustomMapDetailsView("de", 1, "Belaya", LAYERS,
                                       ["USA", "RGF"], ["CombinedArms"])
    selects = _selects(view)
    assert len(selects) == 2
    assert all(s.min_values == 0 for s in selects)
    assert [o.value for o in selects[0].options] == ["USA", "RGF"]
    assert [o.value for o in selects[1].options] == ["CombinedArms"]
    assert len(_buttons(view)) == 1        # save


def test_details_view_caps_selects_at_the_discord_limit():
    many = [f"F{i}" for i in range(40)]
    view = botmod.CustomMapDetailsView("de", 1, "Belaya", LAYERS, many, many)
    for select in _selects(view):
        assert len(select.options) == 25
    assert view.truncated is True


def test_details_view_starts_with_nothing_selected():
    view = botmod.CustomMapDetailsView("de", 1, "Belaya", LAYERS,
                                       ["USA"], ["CombinedArms"])
    assert view.selected_factions == []
    assert view.selected_units == []


def test_modal_carries_three_text_inputs():
    modal = botmod.CustomMapModal("de", 1)
    assert len(modal.children) == 3
    assert modal.layers_input.required is True
    assert modal.name_input.required is False
    assert modal.workshop_input.required is False


def test_details_view_carries_the_workshop_url_to_the_save(temp_db):
    _seed_reference_cache(temp_db)
    workshop = "https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901"
    view = botmod.CustomMapDetailsView("en", 1, "Belaya", LAYERS,
                                       ["USA"], ["CombinedArms"], workshop)
    asyncio.run(view._save(_StubInteraction(1)))

    assert cl.workshop_url_for(1, "Belaya_TC_v1") == workshop


def _seed_reference_cache(db):
    """One main-source layer carrying the faction metadata a custom map borrows."""
    db.upsert_layer(
        raw_name="AlBasrah_AAS_v1", source="main", map_name="Al Basrah",
        map_id="albasrah", gamemode="AAS", layer_version="v1",
        factions=[{"factionId": "USA", "factionName": "United States Army",
                   "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR",
                   "availableOnTeams": [1, 2],
                   "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"}]}],
        team1_alliances=[], team2_alliances=[], map_size_km=None,
    )


def test_save_does_not_claim_success_when_nothing_was_written(temp_db):
    # No fetched source cached, so materialization has no faction metadata to
    # borrow and writes nothing. The view must say so instead of "saved".
    view = botmod.CustomMapDetailsView(
        "en", 1, "Belaya",
        [{"raw_name": "Belaya_TC_v1", "gamemode_token": "TC", "layer_version": "v1"}],
        ["USA"], ["CombinedArms"])
    interaction = _StubInteraction(1)

    asyncio.run(view._save(interaction))

    description = interaction.response.edited["embed"].description
    assert t("custom_map.no_reference_data", "en") in description
    assert "saved" not in description.lower()


def test_save_reports_success_when_layers_were_written(temp_db):
    _seed_reference_cache(temp_db)
    view = botmod.CustomMapDetailsView(
        "en", 1, "Belaya",
        [{"raw_name": "Belaya_TC_v1", "gamemode_token": "TC", "layer_version": "v1"}],
        ["USA"], ["CombinedArms"])
    interaction = _StubInteraction(1)

    asyncio.run(view._save(interaction))

    description = interaction.response.edited["embed"].description
    assert t("custom_map.saved", "en", map="Belaya", count=1) in description
    assert [m["map_name"] for m in temp_db.get_custom_maps(1)] == ["Belaya"]


# ---------------------------------------------------------------------------
# Map picker: custom maps get their own dropdown
# ---------------------------------------------------------------------------

def test_custom_maps_get_their_own_bucket():
    groups = botmod._group_maps_by_size(["Belaya"], {}, {"Belaya"})
    assert groups["custom"] == ["Belaya"]
    assert groups["medium"] == []


def test_unsized_regular_map_still_falls_back_to_medium():
    # Only custom maps are exempt; a fetched map with no size keeps the old
    # behaviour, so nothing changes for guilds without custom maps.
    groups = botmod._group_maps_by_size(["Narva"], {}, set())
    assert groups["medium"] == ["Narva"]
    assert groups["custom"] == []


def test_custom_bucket_comes_after_the_size_buckets():
    groups = botmod._group_maps_by_size(
        ["Narva", "Skorpo", "Belaya"],
        {"Narva": 4.0, "Skorpo": 5.0},
        {"Belaya"},
    )
    assert [k for k, v in groups.items() if v] == ["medium", "large", "custom"]


def test_a_handful_of_custom_maps_still_uses_one_flat_dropdown():
    maps = [f"Map{i:02d}" for i in range(25)]
    view = botmod._build_map_picker_view(maps, "en", {}, set(maps))
    assert isinstance(view, botmod.MapSelectView)
    assert len(_selects(view)[0].options) == 25


def test_more_than_25_custom_maps_split_across_dropdowns_losing_none():
    # The case that used to build a single 26-option Select and fail the whole
    # suggestion flow with an HTTP 400.
    maps = [f"Map{i:02d}" for i in range(26)]
    view = botmod._build_map_picker_view(maps, "en", {}, set(maps))

    selects = _selects(view)
    assert len(selects) == 2
    assert all(len(s.options) <= 25 for s in selects)
    offered = [o.value for s in selects for o in s.options]
    assert sorted(offered) == sorted(maps)          # nothing silently dropped


def test_custom_dropdown_is_labelled_as_its_own_thing():
    from i18n import t

    view = botmod._build_map_picker_view(
        ["Narva", "Belaya"], "en", {"Narva": 4.0}, {"Belaya"})
    placeholders = [s.placeholder for s in _selects(view)]
    assert any(t("source.custom", "en") in p for p in placeholders)
    # ...and the custom map is not sitting in the size dropdown
    custom_select = next(s for s in _selects(view)
                         if t("source.custom", "en") in s.placeholder)
    assert [o.value for o in custom_select.options] == ["Belaya"]


# ── Editing an existing map ──────────────────────────────────────────────────

WORKSHOP = "https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901"


def _entry(**payload):
    full = {"layers": ["Belaya_TC_v1", "Belaya_RAAS_v1"],
            "factions": ["USA"], "units": ["CombinedArms"],
            "workshop_url": WORKSHOP}
    full.update(payload)
    return {"guild_id": 1, "map_name": "Belaya", "payload": full}


def test_edit_modal_is_prefilled_from_the_stored_map():
    modal = botmod.CustomMapModal("de", 1, entry=_entry())

    # .default is what Discord renders into the field; .value only fills in
    # once the form comes back.
    assert modal.layers_input.default == "Belaya_TC_v1\nBelaya_RAAS_v1"
    assert modal.name_input.default == "Belaya"
    assert modal.workshop_input.default == WORKSHOP
    assert modal.original_name == "Belaya"
    assert modal.preselected_factions == ["USA"]
    assert modal.preselected_units == ["CombinedArms"]


def test_add_modal_has_nothing_prefilled():
    modal = botmod.CustomMapModal("de", 1)
    assert modal.layers_input.default is None
    assert modal.name_input.default is None
    assert modal.workshop_input.default is None
    assert modal.original_name is None


def test_details_view_starts_from_the_stored_selection():
    view = botmod.CustomMapDetailsView(
        "de", 1, "Belaya", LAYERS, ["USA", "RGF"], ["CombinedArms", "Armored"],
        original_name="Belaya", preselected_factions=["USA"],
        preselected_units=["CombinedArms"])

    # Seeded, not empty: an empty list means "every faction" downstream.
    assert view.selected_factions == ["USA"]
    assert view.selected_units == ["CombinedArms"]
    assert {o.value: o.default for o in view.faction_select.options} == {
        "USA": True, "RGF": False}
    assert {o.value: o.default for o in view.unit_select.options} == {
        "CombinedArms": True, "Armored": False}


def test_edit_that_never_touches_the_selects_keeps_the_factions(temp_db):
    # The select callbacks only fire when the dropdown is opened. Saving an
    # edit that only changed the Workshop link must not widen the map.
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], ["USA"], ["CombinedArms"])

    view = botmod.CustomMapDetailsView(
        "en", 1, "Belaya", LAYERS, ["USA", "RGF"], ["CombinedArms", "Armored"],
        workshop_url=WORKSHOP, original_name="Belaya",
        preselected_factions=["USA"], preselected_units=["CombinedArms"])
    asyncio.run(view._save(_StubInteraction(1)))

    payload = temp_db.get_custom_maps(1)[0]["payload"]
    assert payload["factions"] == ["USA"]
    assert payload["units"] == ["CombinedArms"]
    assert payload["workshop_url"] == WORKSHOP


def test_rename_moves_the_map_and_drops_the_old_rows(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    src = temp_db.custom_source(1)
    assert len(temp_db.get_modes_for_map("Belaya", allowed_sources=[src])) == 1

    view = botmod.CustomMapDetailsView(
        "en", 1, "Belaya Winter", LAYERS, ["USA"], ["CombinedArms"],
        original_name="Belaya")
    interaction = _StubInteraction(1)
    asyncio.run(view._save(interaction))

    assert [m["map_name"] for m in temp_db.get_custom_maps(1)] == ["Belaya Winter"]
    assert temp_db.get_modes_for_map("Belaya", allowed_sources=[src]) == []
    assert temp_db.get_modes_for_map("Belaya Winter", allowed_sources=[src]) != []
    # The notice must say renamed, not saved — and t() swallows a bad
    # placeholder, so the rendered string is what gets pinned.
    body = interaction.response.edited["embed"].description
    assert "Belaya" in body and "Belaya Winter" in body
    assert t("custom_map.renamed", "en", old="Belaya", map="Belaya Winter",
             count=len(LAYERS)) in body


def test_rename_onto_another_custom_map_is_refused(temp_db):
    # Re-using a name in the add flow replaces that map on purpose; a rename
    # would destroy one the admin never named, so it is refused.
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    cl.save_custom_map(1, "Kokan", ["Kokan_RAAS_v1"], [], [])

    modal = botmod.CustomMapModal("en", 1, entry=_entry())
    modal.layers_input._value = "Belaya_TC_v1"
    modal.name_input._value = "Kokan"
    modal.workshop_input._value = ""
    interaction = _StubInteraction(1)

    asyncio.run(modal.on_submit(interaction))

    assert interaction.response.edited is None
    assert "Kokan" in interaction.response.sent["embed"].description
    assert sorted(m["map_name"] for m in temp_db.get_custom_maps(1)) == \
        ["Belaya", "Kokan"]
    assert temp_db.get_custom_maps(1)[1]["payload"]["layers"] == ["Kokan_RAAS_v1"]


def test_panel_marks_the_maps_that_carry_a_workshop_link(temp_db):
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [], WORKSHOP)
    cl.save_custom_map(1, "Kokan", ["Kokan_RAAS_v1"], [], [])
    interaction = _StubInteraction(1)

    asyncio.run(botmod._render_custom_maps(interaction, 1, "en"))

    body = interaction.response.edited["embed"].description
    assert "**Belaya** 🔗" in body
    assert "**Kokan** 🔗" not in body


def test_rename_differing_only_in_case_is_refused(temp_db):
    # SQLite keys the store case-sensitively, but get_unique_maps filters the
    # blacklist by case-insensitive prefix — two such names would be
    # indistinguishable there, and blacklisting either would hide both.
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    cl.save_custom_map(1, "Kokan", ["Kokan_RAAS_v1"], [], [])

    modal = botmod.CustomMapModal("en", 1, entry=_entry())
    modal.layers_input._value = "Belaya_TC_v1"
    modal.name_input._value = "kokan"
    modal.workshop_input._value = ""
    interaction = _StubInteraction(1)

    asyncio.run(modal.on_submit(interaction))

    assert interaction.response.edited is None
    assert sorted(m["map_name"] for m in temp_db.get_custom_maps(1)) == \
        ["Belaya", "Kokan"]


def test_rename_that_only_changes_case_is_allowed(temp_db):
    # Fixing the capitalisation of your own map must not trip the guard.
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "belaya", ["Belaya_TC_v1"], [], [])

    view = botmod.CustomMapDetailsView(
        "en", 1, "Belaya", LAYERS, ["USA"], ["CombinedArms"],
        original_name="belaya")
    asyncio.run(view._save(_StubInteraction(1)))

    assert [m["map_name"] for m in temp_db.get_custom_maps(1)] == ["Belaya"]


def test_editing_without_renaming_survives_a_fetched_name_collision(temp_db):
    # A fetched map whose name the blacklist cannot tell apart appears only
    # after the custom map was created. Editing it must stay possible as long
    # as the name is not what is changing.
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Al Basrah Winter", ["Basrah_TC_v1"], [], [])
    assert cl.colliding_map_name("Al Basrah Winter") == "Al Basrah"

    modal = botmod.CustomMapModal(
        "en", 1, entry={"guild_id": 1, "map_name": "Al Basrah Winter",
                        "payload": {"layers": ["Basrah_TC_v1"]}})
    modal.layers_input._value = "Basrah_TC_v1"
    modal.name_input._value = "Al Basrah Winter"
    modal.workshop_input._value = WORKSHOP
    interaction = _StubInteraction(1)

    asyncio.run(modal.on_submit(interaction))

    assert interaction.response.sent is None          # not refused
    assert isinstance(interaction.response.edited["view"],
                      botmod.CustomMapDetailsView)


def test_edit_picker_reads_the_map_fresh_from_the_store(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], ["USA"], [], WORKSHOP)

    # The panel was built from a stale snapshot; _edit must not trust it.
    view = botmod.CustomMapsView("en", 1, [
        {"guild_id": 1, "map_name": "Belaya", "payload": {"layers": []}}])
    view.edit_select = _StubSelect(["Belaya"])
    interaction = _StubInteraction(1)

    asyncio.run(view._edit(interaction))

    modal = interaction.response.modal
    assert isinstance(modal, botmod.CustomMapModal)
    assert modal.original_name == "Belaya"
    assert modal.layers_input.default == "Belaya_TC_v1"
    assert modal.workshop_input.default == WORKSHOP
    assert modal.preselected_factions == ["USA"]


def test_edit_picker_reports_a_map_that_is_already_gone(temp_db):
    # Deleted from another session while this panel sat open: an ephemeral
    # error, not an unhandled interaction.
    view = botmod.CustomMapsView("en", 1, [
        {"guild_id": 1, "map_name": "Belaya", "payload": {"layers": []}}])
    view.edit_select = _StubSelect(["Belaya"])
    interaction = _StubInteraction(1)

    asyncio.run(view._edit(interaction))

    assert interaction.response.modal is None
    assert interaction.response.sent["ephemeral"] is True
    assert "Belaya" in interaction.response.sent["embed"].description


# ── Mod-layer packs ──────────────────────────────────────────────────────────

def _seed_stock_maps(db):
    """Chora and Narva in main, carrying the faction data a pack borrows."""
    _seed_reference_cache(db)
    for token in ("Chora", "Narva"):
        db.upsert_layer(
            raw_name=f"{token}_AAS_v1", source="main", map_name=token,
            map_id=token.lower(), gamemode="AAS", layer_version="v1",
            factions=[{"factionId": "USA", "factionName": "United States Army",
                       "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR",
                       "availableOnTeams": [1, 2],
                       "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"}]}],
            team1_alliances=[], team2_alliances=[], map_size_km=None,
        )


def _submit(modal, layers, name=""):
    modal.layers_input._value = layers
    modal.name_input._value = name
    modal.workshop_input._value = ""
    interaction = _StubInteraction(1)
    asyncio.run(modal.on_submit(interaction))
    return interaction


SEC_PACK = [{"raw_name": "SEC_26_Chora_AAS_v2", "map_name": "Chora",
             "gamemode_token": "AAS", "layer_version": "v2", "variant": "SEC 26"}]


def test_modal_turns_mod_layers_into_a_pack(temp_db):
    # "Chora Pack" would collide with the stock map Chora as a map of its own;
    # a pack's name never reaches a map picker, so it is not refused.
    _seed_stock_maps(temp_db)
    interaction = _submit(botmod.CustomMapModal("en", 1),
                          "SEC_26_Chora_AAS_v2\nSEC_26_Narva_AAS_v1", name="Chora Pack")

    assert interaction.response.sent is None
    view = interaction.response.edited["view"]
    assert isinstance(view, botmod.CustomMapDetailsView)
    assert view.pack is True
    assert view.map_name == "Chora Pack"


def test_modal_names_a_pack_after_its_first_variant(temp_db):
    _seed_stock_maps(temp_db)
    interaction = _submit(botmod.CustomMapModal("en", 1), "SEC_26_Chora_AAS_v2")
    assert interaction.response.edited["view"].map_name == "SEC 26"


def test_modal_refuses_a_layer_another_entry_lists(temp_db):
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2"], [], [], pack=True)

    interaction = _submit(botmod.CustomMapModal("en", 1),
                          "SEC_26_Chora_AAS_v2", name="SEC Copy")

    assert interaction.response.edited is None
    assert interaction.response.sent["embed"].description == t(
        "custom_map.err_layer_taken", "en", layer="SEC_26_Chora_AAS_v2", entry="SEC 26")
    assert [m["map_name"] for m in temp_db.get_custom_maps(1)] == ["SEC 26"]


def test_editing_a_pack_does_not_trip_over_its_own_layers(temp_db):
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2"], [], [], pack=True)
    entry = temp_db.get_custom_maps(1)[0]

    interaction = _submit(botmod.CustomMapModal("en", 1, entry=entry),
                          "SEC_26_Chora_AAS_v2\nSEC_26_Narva_AAS_v1", name="SEC 26")

    assert interaction.response.sent is None
    assert interaction.response.edited["view"].pack is True


def test_details_view_saves_a_pack(temp_db):
    _seed_stock_maps(temp_db)
    view = botmod.CustomMapDetailsView("en", 1, "SEC 26", SEC_PACK,
                                       ["USA"], ["CombinedArms"], pack=True)
    asyncio.run(view._save(_StubInteraction(1)))

    assert temp_db.get_custom_maps(1)[0]["payload"]["pack"] is True
    layer = temp_db.get_layer_by_raw_name("SEC_26_Chora_AAS_v2",
                                          allowed_sources=["custom:1"])
    assert layer["map_name"] == "Chora"


def test_panel_counts_the_maps_a_pack_spans(temp_db):
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2", "SEC_26_Chora_AAS_v1",
                                     "SEC_26_Narva_AAS_v1"], [], [], pack=True)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    interaction = _StubInteraction(1)

    asyncio.run(botmod._render_custom_maps(interaction, 1, "en"))

    pack_text = t("custom_map.layer_count_pack", "en", count=3, maps=2)
    body = interaction.response.edited["embed"].description
    assert f"**SEC 26** — {pack_text}" in body
    assert f"**Belaya** — {t('custom_map.layer_count', 'en', count=1)}" in body
    view = interaction.response.edited["view"]
    assert {o.value: o.description for o in view.edit_select.options}["SEC 26"] == pack_text


def test_map_step_keeps_stock_maps_out_of_the_custom_dropdown(temp_db):
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2"], [], [], pack=True)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    state = botmod.SuggestState(1, 10)
    state.allowed_sources = ["main", "custom:1"]
    botmod._choose_source(state, "main")
    interaction = _StubInteraction(1)

    asyncio.run(botmod._suggest_show_map_step(interaction, state, {}, "en", edit=False))

    selects = _selects(interaction.response.sent["view"])
    custom = next(s for s in selects if t("source.custom", "en") in s.placeholder)
    assert [o.value for o in custom.options] == ["Belaya"]
    offered = sorted(o.value for s in selects for o in s.options)
    assert offered == ["Al Basrah", "Belaya", "Chora", "Narva"]


def test_editing_a_pack_is_refused_while_its_stock_maps_are_missing(temp_db):
    # main failed to fetch and supermod stands in as the reference: it cannot
    # place SEC names on a stock map, so the lines parse as one classic map.
    # Saving that would turn the pack into a map called "SEC 26" for good.
    _seed_stock_maps(temp_db)
    cl.save_custom_map(1, "SEC 26", ["SEC_26_Chora_AAS_v2", "SEC_26_Narva_AAS_v1"],
                       [], [], pack=True)
    entry = temp_db.get_custom_maps(1)[0]
    temp_db.clear_layer_cache()
    for token in ("Chora", "Narva"):
        temp_db.upsert_layer(
            raw_name=f"SPM_{token}_AAS_v1", source="supermod",
            map_name=f"SPM | {token}", map_id=token.lower(), gamemode="AAS",
            layer_version="v1",
            factions=[{"factionId": "USA", "factionName": "United States Army",
                       "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR",
                       "availableOnTeams": [1, 2],
                       "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"}]}],
            team1_alliances=[], team2_alliances=[], map_size_km=None,
        )

    interaction = _submit(botmod.CustomMapModal("en", 1, entry=entry),
                          "SEC_26_Chora_AAS_v2\nSEC_26_Narva_AAS_v1", name="SEC 26")

    assert interaction.response.edited is None
    assert interaction.response.sent["embed"].description == t(
        "custom_map.err_pack_unplaceable", "en")
    assert temp_db.get_custom_maps(1)[0]["payload"]["pack"] is True
