import pytest

import bot as botmod
import custom_layers as cl
import utils


WORKSHOP = "https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901"


def _suggestion(**over):
    s = {
        "map_name": "Kokan",
        "gamemode": "TerritoryControl",
        "layer_version": "v1",
        "raw_name": "Kokan_TC_v1",
        "source": "main",
        "team1_faction": "GFI",
        "team1_unit": "CombinedArms",
        "team1_unit_prefix": "MO",
        "team2_faction": "MEI",
        "team2_unit": "CombinedArms",
        "team2_unit_prefix": "MO",
    }
    s.update(over)
    return s


@pytest.fixture
def squadcalc(monkeypatch):
    monkeypatch.setattr(utils, "SQUADCALC_BASE_URL", "https://squadcalc.app")


def test_tc_layer_uses_raw_name_mode_token(squadcalc):
    # SquadCalc labels the layer "TC v1", not "TerritoryControl v1".
    assert "layer=TCv1" in utils.build_squadcalc_url(_suggestion())


def test_cl_suffix_does_not_leak_into_layer_param(squadcalc):
    url = utils.build_squadcalc_url(_suggestion(
        map_name="Al Basrah", gamemode="AAS", layer_version="v3",
        raw_name="AlBasrah_AAS_v3_CL"))
    assert "map=AlBasrah&layer=AASv3" in url


def test_falls_back_to_gamemode_without_raw_name(squadcalc):
    s = _suggestion(gamemode="AAS", raw_name=None)
    assert "layer=AASv1" in utils.build_squadcalc_url(s)


def test_workshop_url_wins_over_the_squadcalc_fallback(squadcalc, monkeypatch):
    # A custom map never resolves to a SquadCalc URL, so without a Workshop
    # link the icon lands on SquadCalc's homepage (_TOOLTIP_NOOP_URL).
    monkeypatch.setattr(utils, "_TOOLTIP_NOOP_URL", "https://squadcalc.app")
    custom = _suggestion(source="custom:1")
    assert "squadcalc.app" in utils.build_map_icon_markdown(custom)

    custom["workshop_url"] = WORKSHOP
    assert utils.build_map_icon_markdown(custom).startswith(f"[🗺️]({WORKSHOP} \"")


def test_workshop_url_wins_for_main_source_too(squadcalc):
    # Defensive ordering: an explicit link is never overridden by the params.
    markdown = utils.build_map_icon_markdown(_suggestion(workshop_url=WORKSHOP))
    assert WORKSHOP in markdown and "?map=" not in markdown


# ── The layer link line: same destination as the 🗺️ icon ────────────────────

def test_link_line_names_the_workshop_when_the_layer_carries_one(squadcalc):
    line = botmod._layer_link_line(
        _suggestion(source="custom:1", workshop_url=WORKSHOP), "en")
    assert WORKSHOP in line and "Steam Workshop" in line


def test_link_line_names_squadcalc_for_a_main_layer(squadcalc):
    line = botmod._layer_link_line(_suggestion(), "en")
    assert "SquadCalc" in line and "squadcalc.app" in line


def test_link_line_is_empty_for_a_custom_map_without_a_link(squadcalc):
    # No Workshop link and no SquadCalc URL for a custom source — nothing to
    # offer, so the confirmation shows no line rather than a dead one.
    assert botmod._layer_link_line(_suggestion(source="custom:1"), "en") == ""


def test_link_line_and_map_icon_agree_on_the_destination(squadcalc):
    # The two must never point at different places for the same layer.
    for suggestion in (_suggestion(),
                       _suggestion(source="custom:1", workshop_url=WORKSHOP)):
        line = botmod._layer_link_line(suggestion, "en")
        icon = utils.build_map_icon_markdown(suggestion)
        assert line, f"no link line for {suggestion['source']}"
        url = line.split("](")[1].rstrip(")")
        assert url in icon


def test_link_line_and_map_icon_both_vanish_when_squadcalc_is_off(monkeypatch):
    # Integration off and a main layer: the icon degrades to a bare emoji, so
    # the line must not claim a destination either.
    monkeypatch.setattr(utils, "SQUADCALC_BASE_URL", "")
    assert botmod._layer_link_line(_suggestion(), "en") == ""
    assert utils.build_map_icon_markdown(_suggestion()) == "🗺️"


def test_listed_suggestion_entry_carries_the_workshop_link(squadcalc):
    # Pins the event embed's suggestion list, which the voting board and the
    # winner block both reach through the same helper.
    entry = utils.format_suggestion_entry(
        1, _suggestion(source="custom:1", workshop_url=WORKSHOP))
    assert WORKSHOP in entry


# ── The shared lookup ────────────────────────────────────────────────────────

def test_custom_workshop_url_reads_the_stored_map(temp_db):
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [], WORKSHOP)
    assert botmod._custom_workshop_url("custom:1", 1, "Belaya") == WORKSHOP
    assert botmod._custom_workshop_url("custom:1", 1, "Kokan") is None


def test_custom_workshop_url_skips_fetched_sources(temp_db, monkeypatch):
    # A main-game layer can never carry one, so the database is not touched.
    monkeypatch.setattr(cl.db, "get_custom_maps",
                        lambda *a, **k: pytest.fail("queried for a fetched source"))
    assert botmod._custom_workshop_url("main", 1, "Kokan") is None
    assert botmod._custom_workshop_url("", 1, "Kokan") is None
