"""Composition of the Custom Maps admin views."""

import asyncio

import discord

import bot as botmod
import custom_layers as cl  # noqa: F401  (import guard: the view calls into it)
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


def test_custom_maps_view_has_add_delete_and_back():
    view = botmod.CustomMapsView("de", 1, [
        {"guild_id": 1, "map_name": "Belaya",
         "payload": {"layers": ["Belaya_TC_v1"]}},
    ])
    assert len(_selects(view)) == 1        # delete picker
    assert len(_buttons(view)) == 2        # add + back
    assert view.db_id == 1


def test_custom_maps_view_without_maps_has_no_delete_picker():
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

    async def edit_message(self, **kwargs):
        self.edited = kwargs


class _StubInteraction:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.response = _StubResponse()


def test_delete_is_scoped_to_the_acting_guild(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": [], "factions": [], "units": []})
    temp_db.upsert_custom_map(2, "Belaya", {"layers": [], "factions": [], "units": []})

    view = botmod.CustomMapsView("de", 1, temp_db.get_custom_maps(1))
    view.delete_select = _StubSelect(["Belaya"])
    interaction = _StubInteraction(1)

    asyncio.run(view._delete(interaction))

    assert temp_db.get_custom_maps(1) == []
    assert [m["map_name"] for m in temp_db.get_custom_maps(2)] == ["Belaya"]
    assert interaction.response.edited is not None   # the panel was redrawn


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


def test_modal_carries_two_text_inputs():
    modal = botmod.CustomMapModal("de", 1)
    assert len(modal.children) == 2
    assert modal.layers_input.required is True
    assert modal.name_input.required is False


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
