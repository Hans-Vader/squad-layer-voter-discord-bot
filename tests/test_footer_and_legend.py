"""Event-embed footer and the Info panel's legend.

The footer names where the 🗺️ icons on that board actually lead; the legend
explains the shorthand the board actually shows. Both are derived, so both
tests build events rather than asserting on a fixed string table.
"""

import pytest

import bot as botmod
import database as db
import utils

WORKSHOP = "https://steamcommunity.com/sharedfiles/filedetails/?id=3025678901"


def _suggestion(**over):
    s = {
        "id": "s1",
        "map_name": "Kokan",
        "gamemode": "RAAS",
        "layer_version": "v1",
        "raw_name": "Kokan_RAAS_v1",
        "source": "main",
        "team1_faction": "USA",
        "team1_unit": "Mechanized",
        "team2_faction": "RGF",
        "team2_unit": "Mechanized",
        "user_name": "tester",
    }
    s.update(over)
    return s


def _event(phase="suggestions_open", **over):
    # allowed_sources is set on purpose: with neither event nor guild sources,
    # _event_uses_supermod falls back to the whole configured source list and
    # would report supermod active (see the legacy-fallback test below).
    e = {"phase": phase, "suggestions": [_suggestion()],
         "allowed_sources": ["main"]}
    e.update(over)
    return e


@pytest.fixture
def squadcalc(monkeypatch):
    monkeypatch.setattr(utils, "SQUADCALC_BASE_URL", "https://squadcalc.app")


def _footer(event, settings=None):
    embed = utils.build_event_embed(event, settings or {"language": "en"}, 1)
    return embed.footer.text if embed.footer else None


# ── Footer ───────────────────────────────────────────────────────────────────

def test_footer_names_squadcalc_for_main_layers(squadcalc):
    assert "SquadCalc" in _footer(_event())


def test_footer_names_the_workshop_when_that_is_the_only_destination(squadcalc):
    event = _event(suggestions=[_suggestion(source="custom:1",
                                            workshop_url=WORKSHOP)])
    footer = _footer(event)
    assert "Steam Workshop" in footer and "SquadCalc" not in footer


def test_footer_names_both_on_a_mixed_board(squadcalc):
    event = _event(suggestions=[
        _suggestion(),
        _suggestion(id="s2", source="custom:1", workshop_url=WORKSHOP),
    ])
    footer = _footer(event)
    assert "SquadCalc" in footer and "Steam Workshop" in footer


def test_no_footer_when_nothing_is_clickable(squadcalc):
    # A supermod layer resolves to no SquadCalc URL, so its icon is only a
    # tooltip — there is no destination to announce.
    assert _footer(_event(suggestions=[_suggestion(source="supermod")])) is None


def test_no_footer_without_suggestions(squadcalc):
    assert _footer(_event(suggestions=[])) is None


def test_no_footer_when_squadcalc_is_disabled(monkeypatch):
    # SQUADCALC_BASE_URL unset: main layers render a bare emoji. Pinned rather
    # than inherited — .env.dist ships a base URL, so the ambient value differs
    # between checkouts.
    monkeypatch.setattr(utils, "SQUADCALC_BASE_URL", "")
    assert _footer(_event()) is None


def test_footer_speaks_only_for_the_ballot_on_a_live_voting_board(squadcalc):
    # With more than max_voting suggestions the poll carries a subset, and the
    # board renders bars for that subset only. A workshop-only layer left off
    # the ballot must not make the footer promise the Steam Workshop.
    event = _event(
        phase="voting",
        selected_for_vote=["s1"],
        suggestions=[
            _suggestion(),
            _suggestion(id="s2", source="custom:1", workshop_url=WORKSHOP),
        ],
    )
    embed = utils.build_event_embed(event, {"language": "en"}, 1,
                                    vote_counts={"s1": 3})
    footer = embed.footer.text
    assert "SquadCalc" in footer and "Steam Workshop" not in footer


def test_footer_ignores_a_collapsed_tail(squadcalc):
    # A board long enough to collapse its tail into "… and N more" never shows
    # those icons, so they must not shape the footer either.
    suggestions = [_suggestion(id=f"s{i}", map_name=f"Map{i}") for i in range(30)]
    suggestions[-1] = _suggestion(id="tail", source="custom:1",
                                  workshop_url=WORKSHOP)
    event = _event(suggestions=suggestions)
    embed = utils.build_event_embed(event, {"language": "en"}, 1)
    assert len(embed.fields) <= 25
    assert "Steam Workshop" not in embed.footer.text


def test_footer_follows_the_winner_once_completed(squadcalc):
    event = _event(phase="completed", suggestions=[],
                   winning_layer=_suggestion())
    assert "SquadCalc" in _footer(event)

    event["winning_layer"] = _suggestion(source="custom:1",
                                         workshop_url=WORKSHOP)
    assert "Steam Workshop" in _footer(event)


def test_supermod_event_no_longer_puts_the_legend_in_the_footer(squadcalc):
    # The legend moved to the Info panel; the footer only speaks of link
    # destinations now.
    event = _event(allowed_sources=["supermod"],
                   suggestions=[_suggestion(source="supermod")])
    assert _footer(event, {"language": "en"}) is None


# ── Legend ───────────────────────────────────────────────────────────────────

def test_legend_explains_only_the_abbreviations_in_use():
    event = _event(suggestions=[
        _suggestion(gamemode="TerritoryControl", team1_unit="CombinedArms"),
    ])
    lines = utils.build_legend_lines(event, {}, "en")
    joined = "\n".join(lines)
    assert "TC = TerritoryControl" in joined
    assert "CombArms = CombinedArms" in joined
    # Nothing on this board is a shortened map name or LightInfantry.
    assert "Kamdesh" not in joined
    assert "LightInf" not in joined


def test_legend_groups_one_line_per_abbreviation_kind():
    event = _event(suggestions=[
        _suggestion(map_name="Kamdesh Highlands", gamemode="Invasion",
                    team1_unit="LightInfantry", team2_unit="CombinedArms"),
    ])
    lines = utils.build_legend_lines(event, {}, "en")
    assert lines == [
        "INV = Invasion",
        "CombArms = CombinedArms · LightInf = LightInfantry",
        "Kamdesh = Kamdesh Highlands",
    ]


def test_legend_is_empty_when_nothing_is_abbreviated():
    assert utils.build_legend_lines(_event(), {}, "en") == []


def test_legend_carries_the_supermod_line_when_that_source_is_active():
    event = _event(allowed_sources=["supermod"])
    lines = utils.build_legend_lines(event, {}, "en")
    assert lines and "SuperMod" in lines[0]


def test_legend_supermod_line_is_keyed_on_the_source_not_the_layers():
    # SPM/SU and GoingDark are raw-name prefixes, so the line follows the
    # event's active sources — a supermod event with no supermod suggestion
    # submitted yet still needs it, and a main-only event never does.
    event = _event(allowed_sources=["supermod"], suggestions=[])
    assert utils.build_legend_lines(event, {}, "en") == [
        "SPM/SU = SuperMod · GoingDark = SuperMod Night"]
    assert utils.build_legend_lines(_event(suggestions=[]), {}, "en") == []


def test_legend_falls_back_to_the_configured_sources(monkeypatch):
    # No event sources and no guild cap: _event_uses_supermod treats the full
    # configured list as active, which is how legacy events behave. The list is
    # pinned because it comes from LAYERS_JSON_URL, which a fresh checkout
    # leaves at the single main-game default.
    monkeypatch.setattr(utils, "LAYERS_JSON_SOURCES",
                        [("main", "http://x"), ("supermod", "http://y")])
    event = {"phase": "suggestions_open", "suggestions": []}
    assert utils.build_legend_lines(event, {}, "en") == [
        "SPM/SU = SuperMod · GoingDark = SuperMod Night"]
    # A guild that capped its sources to main gets no SuperMod line.
    assert utils.build_legend_lines(event, {"allowed_sources": ["main"]},
                                   "en") == []


def test_legend_deduplicates_across_suggestions():
    event = _event(suggestions=[
        _suggestion(gamemode="Invasion"),
        _suggestion(id="s2", gamemode="Invasion"),
    ])
    assert utils.build_legend_lines(event, {}, "en") == ["INV = Invasion"]


# ── Wiring: the legend reaches the Info panel ────────────────────────────────

class _StubUser:
    id = 42


class _StubInteraction:
    guild_id = 1
    user = _StubUser()


def _info_fields(event, settings, monkeypatch):
    monkeypatch.setattr(db, "get_recent_history", lambda *a, **k: [])
    embed = botmod._build_info_embed(_StubInteraction(), event, settings, 7, "en")
    return {f.name: f.value for f in embed.fields}


def test_info_panel_carries_the_legend(monkeypatch):
    event = _event(allowed_sources=["supermod"], suggestions=[
        _suggestion(gamemode="TerritoryControl", team1_unit="CombinedArms"),
    ])
    fields = _info_fields(event, {"language": "en"}, monkeypatch)

    legend = next(v for k, v in fields.items() if "Legend" in k)
    assert "SPM/SU = SuperMod" in legend
    assert "TC = TerritoryControl" in legend
    assert "CombArms = CombinedArms" in legend


def test_info_panel_omits_the_legend_when_there_is_nothing_to_explain(monkeypatch):
    fields = _info_fields(_event(), {"language": "en"}, monkeypatch)
    assert not [k for k in fields if "Legend" in k]
