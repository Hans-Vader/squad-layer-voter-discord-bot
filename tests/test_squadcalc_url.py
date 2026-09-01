import pytest

import utils


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

    workshop = "https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901"
    custom["workshop_url"] = workshop
    assert utils.build_map_icon_markdown(custom).startswith(f"[🗺️]({workshop} \"")


def test_workshop_url_wins_for_main_source_too(squadcalc):
    # Defensive ordering: an explicit link is never overridden by the params.
    workshop = "https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901"
    markdown = utils.build_map_icon_markdown(_suggestion(workshop_url=workshop))
    assert workshop in markdown and "?map=" not in markdown
