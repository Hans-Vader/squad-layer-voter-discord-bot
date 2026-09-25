"""A mod layer's variant ("SEC 26") everywhere a layer is shown or compared."""

import utils

VANILLA = {"map_name": "Chora", "gamemode": "AAS", "layer_version": "v2",
           "team1_faction": "USA", "team1_unit": "CombinedArms",
           "team2_faction": "RGF", "team2_unit": "CombinedArms",
           "source": "main", "raw_name": "Chora_AAS_v2", "user_name": "x"}
SEC26 = {**VANILLA, "layer_variant": "SEC 26", "raw_name": "SEC_26_Chora_AAS_v2",
         "source": "custom:1"}
SEC36 = {**SEC26, "layer_variant": "SEC 36", "raw_name": "SEC_36_Chora_AAS_v2"}


def test_format_mode_without_a_variant_is_unchanged():
    assert utils.format_mode("AAS", "v2") == "AAS v2"
    assert utils.format_mode("AAS", None) == "AAS"
    assert utils.format_mode("AAS", "v2", compact=True) == "AASv2"
    assert utils.format_mode("AAS", "", compact=True) == "AAS"


def test_format_mode_leads_with_the_variant():
    assert utils.format_mode("AAS", "v2", "SEC 26") == "SEC 26 · AAS v2"
    assert utils.format_mode("AAS", "v2", "SEC 26", compact=True) == "SEC26 AASv2"


def test_poll_option_tells_vanilla_and_both_packs_apart():
    texts = {utils.format_layer_poll_option(s) for s in (VANILLA, SEC26, SEC36)}
    assert len(texts) == 3
    assert utils.format_layer_poll_option(SEC26).startswith("Chora SEC26 AASv2 ")


def test_poll_option_keeps_the_variant_when_truncated():
    long = {**SEC36, "map_name": "Manicouagan",
            "team1_faction": "PLAAGF", "team2_faction": "USMC"}
    text = utils.format_layer_poll_option(long)
    assert len(text) <= 55
    assert text.startswith("Manicouagan SEC36 AASv2 ")


def test_board_entry_short_form_and_tooltip_show_the_variant():
    assert "**1. Chora**: SEC 26 · AAS v2 ⚔️" in utils.format_suggestion_entry(1, SEC26)
    assert utils.format_layer_short(SEC26).startswith("Chora SEC 26 · AAS v2 — ")
    assert "Chora SEC 26 · AAS v2 — " in utils._build_layer_tooltip(SEC26)


def test_winner_copy_text_shows_the_variant():
    text = utils.build_winner_copy_text({"winning_layer": SEC26}, "en")
    assert text.startswith("🗺️ Chora — SEC 26 · AAS v2")


def test_vanilla_and_mod_layers_are_not_duplicates():
    assert not utils.suggestion_matches(VANILLA, SEC26)
    assert not utils.suggestion_matches(SEC26, SEC36)
    assert utils.suggestion_matches(SEC26, dict(SEC26))


def test_legacy_suggestions_without_a_variant_still_match():
    legacy = dict(VANILLA)                     # stored before this change
    fresh = {**VANILLA, "layer_variant": None}  # a vanilla pick today
    assert utils.suggestion_matches(legacy, fresh)
