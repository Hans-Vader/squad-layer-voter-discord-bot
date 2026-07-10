import utils


def test_vote_bar_empty_when_no_votes():
    assert utils.format_vote_bar(0, 10) == "░" * 20


def test_vote_bar_half():
    assert utils.format_vote_bar(5, 10) == "█" * 10 + "░" * 10


def test_vote_bar_full_at_100_percent():
    assert utils.format_vote_bar(10, 10) == "█" * 20


def test_vote_bar_never_rounds_a_real_vote_to_empty():
    # 1 of 100 rounds to 0 blocks mathematically; force at least one.
    bar = utils.format_vote_bar(1, 100)
    assert bar.startswith("█")
    assert len(bar) == 20


def test_vote_bar_all_empty_when_total_is_zero():
    assert utils.format_vote_bar(3, 0) == "░" * 20


def test_vote_bar_rounds_half_up():
    # 1 of 8 (12.5%) => 2.5 blocks; half-up => 3 (Python round() would give 2).
    assert utils.format_vote_bar(1, 8) == "█" * 3 + "░" * 17


def test_vote_result_entry_floors_real_vote_percentage():
    # 1 of 300 rounds to 0% naively; a real vote must show >=1% and a block.
    line = utils._format_vote_result_entry(1, _suggestion("a", "Narva"),
                                           count=1, total=300)
    assert "· 1%" in line
    assert "█" in line


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


def _voting_event():
    return {
        "name": "Test Event",
        "phase": "voting",
        "suggestions": [
            _suggestion("a", "Narva"),      # ballot, mid
            _suggestion("b", "Yehorivka"),  # ballot, leader
            _suggestion("c", "Gorodok"),    # ballot, last
            _suggestion("d", "Skorpo"),     # NOT on the ballot
        ],
        "selected_for_vote": ["a", "b", "c"],
    }


def _field_text(embed):
    return "\n".join(f.value for f in embed.fields)


def test_voting_embed_sorts_by_votes_and_hides_non_ballot():
    event = _voting_event()
    settings = {"language": "de"}
    vote_counts = {"a": 8, "b": 12, "c": 5}

    embed = utils.build_event_embed(event, settings, 1, vote_counts=vote_counts)

    text = _field_text(embed)
    # Leader (Yehorivka=12) before Narva=8 before Gorodok=5.
    assert text.index("Yehorivka") < text.index("Narva") < text.index("Gorodok")
    # Non-ballot suggestion is not shown during voting.
    assert "Skorpo" not in text
    # Live-results header + top medal present.
    assert any("Live-Ergebnis" in (f.name or "") for f in embed.fields)
    assert "🥇" in text


def test_voting_embed_falls_back_when_counts_unavailable():
    # Empty dict = poll fetch failed → plain listing, all suggestions shown.
    event = _voting_event()
    embed = utils.build_event_embed(event, {"language": "de"}, 1, vote_counts={})
    text = _field_text(embed)
    assert "Skorpo" in text
    assert "🥇" not in text


def test_voting_embed_keeps_ballot_layer_with_unreadable_count():
    # 'c' (Gorodok) is on the ballot but missing from vote_counts (its poll-
    # option text no longer matched) — it must still render, not vanish.
    event = _voting_event()
    embed = utils.build_event_embed(event, {"language": "de"}, 1,
                                    vote_counts={"a": 5, "b": 3})
    text = _field_text(embed)
    assert "Gorodok" in text     # ballot layer with unreadable count kept
    assert "Skorpo" not in text  # non-ballot layer still hidden


def test_voting_embed_no_medals_on_fresh_poll():
    # Nobody has voted yet → no fake leader; medals are suppressed.
    event = _voting_event()
    embed = utils.build_event_embed(event, {"language": "de"}, 1,
                                    vote_counts={"a": 0, "b": 0, "c": 0})
    text = _field_text(embed)
    assert not any(m in text for m in ("🥇", "🥈", "🥉"))


def test_voting_embed_one_field_per_entry():
    # Voting board: one embed field per ballot suggestion → uniform spacing.
    event = _voting_event()  # 3 ballot (a,b,c) + 1 non-ballot (d)
    embed = utils.build_event_embed(event, {"language": "de"}, 1,
                                    vote_counts={"a": 8, "b": 12, "c": 5})
    ballot_fields = [f for f in embed.fields if "🗺️" in (f.value or "")]
    assert len(ballot_fields) == 3
    assert all(f.value.count("🗺️") == 1 for f in ballot_fields)  # one entry each
    # First ballot field carries the header; the rest use the zero-width name.
    assert "Live-Ergebnis" in ballot_fields[0].name
    assert all(f.name == "​" for f in ballot_fields[1:])


def test_suggestion_listing_one_field_per_entry():
    # Suggestion phase now also gets one field per entry (even spacing).
    suggestions = [_suggestion(str(i), f"Map{i}") for i in range(6)]
    event = {"name": "E", "phase": "suggestions_open", "suggestions": suggestions}
    embed = utils.build_event_embed(event, {"language": "de"}, 1)
    listing_fields = [f for f in embed.fields if "🗺️" in (f.value or "")]
    assert len(listing_fields) == 6
    assert all(f.value.count("🗺️") == 1 for f in listing_fields)


def test_listing_caps_at_discord_field_limit():
    # Far more suggestions than fit: never exceed Discord's 25-field cap; the
    # overflow folds into a trailing "… und N weitere" field.
    suggestions = [_suggestion(str(i), f"Map{i}") for i in range(30)]
    event = {"name": "E", "phase": "suggestions_open", "suggestions": suggestions}
    embed = utils.build_event_embed(event, {"language": "de"}, 1)
    assert len(embed.fields) <= 25
    assert any("weitere" in (f.value or "") for f in embed.fields)
