import json
from pathlib import Path

import pytest

import utils


def _winner(source="main"):
    return {
        "map_name": "Fallujah",
        "gamemode": "AAS",
        "layer_version": "v1",
        "raw_name": "Fallujah_AAS_v1",
        "source": source,
        "team1_faction": "USMC",
        "team1_faction_name": "United States Marine Corps",
        "team1_unit": "LightInfantry",
        "team1_unit_prefix": "LO",
        "team2_faction": "IMF",
        "team2_faction_name": "Irregular Militia Forces",
        "team2_unit": "LightInfantry",
        "team2_unit_prefix": "LO",
        "team1_vehicles": [
            {"name": "MATV TOW", "count": 3, "vehType": "TD"},
            {"name": "Quad Bike", "count": 9, "vehType": "ULTV"},
        ],
        "team2_vehicles": [
            {"name": "T-62", "count": 1, "vehType": "MBT"},
        ],
    }


def _event(source="main"):
    return {
        "phase": "completed",
        "winning_layer": _winner(source),
        "winning_layer_command":
            "AdminChangeLayer Fallujah_AAS_v1 USMC+LightInfantry IMF+LightInfantry",
    }


@pytest.fixture
def squadcalc(monkeypatch):
    monkeypatch.setattr(utils, "SQUADCALC_BASE_URL", "https://squadcalc.app")


def test_copy_text_contains_all_sections_in_order(squadcalc):
    text = utils.build_winner_copy_text(_event(), "de")
    # Masked-link syntax with escaped brackets (visible text, survives copy).
    assert text.startswith("🗺️ Fallujah — AAS v1 — 🔗 \\[SquadCalc\\](https://squadcalc.app/?")
    assert "team1unit=USMC_LO_LightInfantry" in text
    # Short faction ids on the ⚔️ line (chars saved for the event description).
    assert "⚔️ USMC/LightInfantry vs IMF/LightInfantry" in text
    # Fences are escaped (visible ```), so copying the rendered text keeps them.
    # No "⚙️ Layer setzen" header line anymore — the block stands alone.
    assert "⚙️" not in text
    assert "\\`\\`\\`AdminChangeLayer Fallujah_AAS_v1 USMC+LightInfantry IMF+LightInfantry\\`\\`\\`" in text
    # Vehicle sections: team 1 before team 2, combat classes first. Short
    # content → shrink ladder inactive → class labels kept.
    assert "🚛 Team 1 — USMC\n" in text
    assert "🚛 Team 2 — IMF\n" in text
    assert "🎯 3× MATV TOW [ATGM]" in text
    assert "⚔️ 1× T-62 [MBT]" in text
    assert text.index("Team 1") < text.index("Team 2")
    assert text.index("\\`\\`\\`") < text.index("Team 1")  # command block before vehicles


def test_copy_text_omits_squadcalc_link_for_supermod(squadcalc):
    text = utils.build_winner_copy_text(_event(source="supermod"), "en")
    assert "squadcalc.app" not in text
    assert "🔗" not in text
    assert text.startswith("🗺️ Fallujah — AAS v1\n")


def test_copy_text_none_without_winner():
    assert utils.build_winner_copy_text({"phase": "completed"}, "en") is None


def test_copy_text_skips_missing_optional_parts(squadcalc):
    event = _event()
    event["winning_layer"].pop("team1_vehicles")
    event["winning_layer"].pop("team2_vehicles")
    event.pop("winning_layer_command")
    text = utils.build_winner_copy_text(event, "en")
    assert "🚛" not in text
    assert "`" not in text
    assert "⚔️ USMC/LightInfantry vs IMF/LightInfantry" in text


def _pasted_len(text):
    """Length of the text after pasting (Discord strips the escape \\)."""
    return len(text.replace("\\", ""))


def test_copy_text_real_layer_fits_event_description(squadcalc):
    # Real worst-case loadouts (incl. boats) from the reference data.
    data = json.loads((Path(__file__).parent.parent / "reference" /
                       "layers.json").read_text())
    event = _event()
    event["winning_layer"]["team1_vehicles"] = \
        data["Units"]["USMC_LO_LightInfantry"]["vehicles"]
    event["winning_layer"]["team2_vehicles"] = \
        data["Units"]["IMF_LO_LightInfantry"]["vehicles"]
    text = utils.build_winner_copy_text(event, "de")
    assert _pasted_len(text) <= utils.WINNER_COPY_MAX


def test_copy_text_huge_list_trims_to_limit(squadcalc):
    event = _event()
    event["winning_layer"]["team1_vehicles"] = [
        {"name": f"Very Long Vehicle Name {i:02}", "count": 2, "vehType": "MRAP"}
        for i in range(60)
    ]
    text = utils.build_winner_copy_text(event, "de")
    assert _pasted_len(text) <= utils.WINNER_COPY_MAX
    assert "weitere" in text  # vehicles.more tail marks the trimmed list
