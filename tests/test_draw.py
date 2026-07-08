import bot as botmod
import utils


def _suggestion(sid, map_name):
    return {
        "id": sid,
        "map_name": map_name,
        "gamemode": "RAAS",
        "layer_version": "v1",
        "team1_faction": "USA",
        "team1_unit": "CombinedArms",
        "team2_faction": "RUS",
        "team2_unit": "CombinedArms",
        "user_name": "tester",
    }


def _counts(*pairs):
    """(suggestion, count) pairs -> [(poll_option_text, count)] as the poll would carry."""
    return [(utils.format_layer_poll_option(s), c) for s, c in pairs]


def test_unique_winner():
    a, b, c = _suggestion("a", "Narva"), _suggestion("b", "Yehorivka"), _suggestion("c", "Gorodok")
    winner, tied = botmod._tally_poll(_counts((a, 3), (b, 7), (c, 2)), [a, b, c])
    assert winner is b
    assert tied == []


def test_two_way_tie_returns_both_in_ballot_order():
    a, b, c = _suggestion("a", "Narva"), _suggestion("b", "Yehorivka"), _suggestion("c", "Gorodok")
    winner, tied = botmod._tally_poll(_counts((a, 5), (b, 5), (c, 2)), [a, b, c])
    assert winner is None
    assert [s["id"] for s in tied] == ["a", "b"]


def test_three_way_tie():
    a, b, c = _suggestion("a", "Narva"), _suggestion("b", "Yehorivka"), _suggestion("c", "Gorodok")
    winner, tied = botmod._tally_poll(_counts((a, 4), (b, 4), (c, 4)), [a, b, c])
    assert winner is None
    assert [s["id"] for s in tied] == ["a", "b", "c"]


def test_no_votes_is_no_winner():
    a, b = _suggestion("a", "Narva"), _suggestion("b", "Yehorivka")
    winner, tied = botmod._tally_poll(_counts((a, 0), (b, 0)), [a, b])
    assert winner is None
    assert tied == []


def test_empty_poll_is_no_winner():
    winner, tied = botmod._tally_poll([], [])
    assert winner is None
    assert tied == []


def test_unmatched_text_falls_back_to_first_selected():
    a, b = _suggestion("a", "Narva"), _suggestion("b", "Yehorivka")
    # A single top answer whose text matches nothing on the ballot -> legacy fallback.
    winner, tied = botmod._tally_poll([("Ghost Layer XYZ", 4)], [a, b])
    assert winner is a
    assert tied == []


def test_draw_pending_embed_lists_only_tied_layers():
    event = {
        "name": "Test Event",
        "phase": "draw_pending",
        "suggestions": [
            _suggestion("a", "Narva"),
            _suggestion("b", "Yehorivka"),
            _suggestion("c", "Gorodok"),
        ],
        "selected_for_vote": ["a", "b", "c"],
        "draw_tied_ids": ["a", "b"],
    }
    embed = utils.build_event_embed(event, {"language": "en"}, 1)
    text = "\n".join(f.value for f in embed.fields)
    assert "Narva" in text
    assert "Yehorivka" in text
    assert "Gorodok" not in text  # not tied -> not listed
