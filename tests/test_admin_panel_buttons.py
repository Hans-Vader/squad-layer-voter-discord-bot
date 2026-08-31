import bot as botmod


def _actions(view):
    return [c.action for c in view.children if hasattr(c, "action")]


def test_open_phase_nests_suggestion_actions():
    view = botmod.AdminPanelView("suggestions_open", "de", 1, suggestion_count=3)
    actions = _actions(view)
    assert "manage_suggestions" in actions
    assert "close_suggestions" not in actions
    assert "remove_suggestion" not in actions


def test_submenu_holds_close_and_remove():
    view = botmod.ManageSuggestionsView("de", 1, suggestion_count=3)
    assert _actions(view) == ["close_suggestions", "remove_suggestion"]
    assert view.db_id == 1
    # plus a Back button, which carries no action attribute
    assert len(view.children) == 3


def test_submenu_hides_remove_without_suggestions():
    view = botmod.ManageSuggestionsView("de", 1, suggestion_count=0)
    assert _actions(view) == ["close_suggestions"]


def test_closed_phase_keeps_flat_buttons():
    view = botmod.AdminPanelView("suggestions_closed", "de", 1, suggestion_count=3)
    actions = _actions(view)
    assert "select_for_vote" in actions
    assert "reopen_suggestions" in actions
    assert "remove_suggestion" in actions
    assert "manage_suggestions" not in actions
