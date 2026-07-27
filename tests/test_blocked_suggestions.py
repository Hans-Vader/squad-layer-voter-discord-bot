def test_only_winners_are_blocked(temp_db):
    db = temp_db
    loser = {"map": "Yehorivka", "layer": "Yehorivka_RAAS_v1"}
    winner = {"map": "Gorodok", "layer": "Gorodok_AAS_v1"}
    db.save_voting_history(1, 2, [loser, winner], winner)

    assert db.get_blocked_suggestions(1, 2, 12) == [winner]
