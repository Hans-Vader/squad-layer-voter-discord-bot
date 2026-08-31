"""Composition of the Custom Maps admin views."""

import asyncio

import discord

import bot as botmod
import custom_layers as cl  # noqa: F401  (import guard: the view calls into it)


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
