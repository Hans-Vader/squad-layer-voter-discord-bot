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
