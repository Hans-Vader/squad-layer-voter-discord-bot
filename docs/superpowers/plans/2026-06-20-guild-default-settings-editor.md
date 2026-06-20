# Guild Default Settings Editor (`/config_defaults`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an organizer-gated `/config_defaults` slash command that opens the existing per-event DM edit dialog against the guild's default settings, so organizers can change the defaults new events inherit.

**Architecture:** Make the existing event editor *target-aware*. Introduce an `EditTarget` abstraction with two concrete implementations — `EventEditTarget` (wraps today's behavior) and `GuildEditTarget` (reads/writes `guild_settings`). Every editor helper/view/modal gains a `target` keyword argument defaulting to the event target, so the per-event path is unchanged. A new property table `_GUILD_EDIT_PROPERTIES` and a `duration_str` editor kind cover the guild-only fields.

**Tech Stack:** Python 3, discord.py ≥ 2.0, SQLite (stdlib `sqlite3`), pytest (new dev dependency).

## Global Constraints

- **No AI-attribution in commits.** Never add `Co-Authored-By: Claude …`, `🤖 Generated with …`, or any AI-attribution trailer to commit messages (user global rule). Plain messages only.
- **Bilingual i18n.** Every new `_STRINGS` entry in `bot/i18n.py` must define both `"de"` and `"en"`.
- **Event editor parity.** The per-event Admin → Edit dialog must behave byte-for-byte as before. `target` defaults to the event target everywhere; never change event-path behavior.
- **Module layout.** `bot/` has no `__init__.py`; modules import each other flatly (`import database as db`). Tests put `bot/` on `sys.path` via `pythonpath = bot` in `pytest.ini`. `import bot` must resolve to `bot/bot.py`, so the repo root must NOT be on `sys.path` (keep `conftest.py` in `tests/`, not at repo root).
- **Leave untracked files alone.** Do not stage or modify the pre-existing untracked `CHANGELOG.md` or `docs/auto_advance_toggle_design.md`.
- **Default value of `default_max_voting_layers` is `10`** (matches today's hardcoded `build_default_event` value).
- **`MAX_VOTING_DURATION_HOURS = 336`**; `max_voting_layers` range is `1..10`.

## File Structure

- `pytest.ini` (create) — pytest config: `pythonpath = bot`, `testpaths = tests`.
- `requirements-dev.txt` (create) — `pytest`.
- `tests/conftest.py` (create) — `temp_db` fixture pointing `database.DB_FILE` at a tmp file.
- `tests/test_imports.py` (create) — guards that `import bot` resolves correctly.
- `tests/test_guild_settings.py` (create) — data-layer tests (round-trip, merge).
- `tests/test_build_default_event.py` (create) — `default_max_voting_layers` wiring.
- `tests/test_duration_str.py` (create) — `validate_duration_str` + formatting.
- `tests/test_edit_targets.py` (create) — guild target read/write/persist + property-table sanity.
- `bot/database.py` (modify) — add `default_max_voting_layers`; wire `build_default_event`.
- `bot/i18n.py` (modify) — new `config_defaults.*` keys.
- `bot/bot.py` (modify) — `validate_duration_str`, `duration_str` formatting, `EditTarget` classes + instances + `_GUILD_EDIT_PROPERTIES` + `_apply_guild_property`, target-aware refactor of all editor helpers/views, `_open_edit_session`, `/config_defaults`.

---

## Task 1: pytest infrastructure

**Files:**
- Create: `pytest.ini`, `requirements-dev.txt`, `tests/conftest.py`, `tests/test_imports.py`, `tests/test_guild_settings.py`

**Interfaces:**
- Consumes: `database.DB_FILE`, `database.init_db`, `database.save_guild_settings`, `database.get_guild_settings`, `database.DEFAULT_GUILD_SETTINGS` (all existing).
- Produces: `temp_db` fixture (returns the `database` module backed by a temp SQLite file).

- [ ] **Step 1: Create `requirements-dev.txt`**

```
pytest>=7.0
```

- [ ] **Step 2: Create `pytest.ini`** (repo root)

```ini
[pytest]
pythonpath = bot
testpaths = tests
```

- [ ] **Step 3: Create `tests/conftest.py`**

```python
import pytest

import database


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the DB layer at a throwaway SQLite file and create the schema."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_file))
    database.init_db()
    return database
```

- [ ] **Step 4: Create `tests/test_imports.py`** (guards the `import bot` resolution)

```python
def test_import_database():
    import database
    assert hasattr(database, "DEFAULT_GUILD_SETTINGS")


def test_import_bot_module():
    # Must resolve to bot/bot.py (the module), not an empty `bot` namespace
    # package. If this fails, the repo root leaked onto sys.path.
    import bot
    assert hasattr(bot, "parse_duration_to_seconds")
```

- [ ] **Step 5: Create `tests/test_guild_settings.py`**

```python
def test_save_and_get_guild_settings_roundtrip(temp_db):
    db = temp_db
    db.save_guild_settings(123, {"language": "de", "max_total_suggestions": 10})
    got = db.get_guild_settings(123)
    assert got["language"] == "de"
    assert got["max_total_suggestions"] == 10
    # Unset keys fall back to the hard-coded defaults via the merge.
    assert got["allowed_gamemodes"] == db.DEFAULT_GUILD_SETTINGS["allowed_gamemodes"]


def test_get_guild_settings_returns_none_when_unconfigured(temp_db):
    assert temp_db.get_guild_settings(999) is None
```

- [ ] **Step 6: Run the suite to verify it passes**

Run: `pip install -r requirements.txt -r requirements-dev.txt && pytest -q`
Expected: PASS (4 tests). If `test_import_bot_module` fails with an `AttributeError`, the repo root is on `sys.path` — confirm there is no `conftest.py` at the repo root.

- [ ] **Step 7: Commit**

```bash
git add pytest.ini requirements-dev.txt tests/conftest.py tests/test_imports.py tests/test_guild_settings.py
git commit -m "test: add pytest harness with temp-DB fixture"
```

---

## Task 2: Add `default_max_voting_layers` guild default

**Files:**
- Modify: `bot/database.py:54-79` (`DEFAULT_GUILD_SETTINGS`)
- Test: `tests/test_guild_settings.py`

**Interfaces:**
- Produces: `DEFAULT_GUILD_SETTINGS["default_max_voting_layers"] == 10`, surfaced through `get_guild_settings`' merge.

- [ ] **Step 1: Write the failing test** (append to `tests/test_guild_settings.py`)

```python
def test_default_max_voting_layers_present_via_merge(temp_db):
    db = temp_db
    db.save_guild_settings(1, {"language": "en"})  # legacy row, no new key
    got = db.get_guild_settings(1)
    assert got["default_max_voting_layers"] == 10
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_guild_settings.py::test_default_max_voting_layers_present_via_merge -q`
Expected: FAIL with `KeyError: 'default_max_voting_layers'`

- [ ] **Step 3: Add the key.** In `bot/database.py`, inside `DEFAULT_GUILD_SETTINGS`, immediately after the `"default_mirror_match": False,` line (currently `database.py:78`), add:

```python
    "default_max_voting_layers": 10,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_guild_settings.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/database.py tests/test_guild_settings.py
git commit -m "feat: add default_max_voting_layers guild default"
```

---

## Task 3: Wire `default_max_voting_layers` into event creation

**Files:**
- Modify: `bot/database.py:744` (inside `build_default_event`)
- Test: `tests/test_build_default_event.py` (create)

**Interfaces:**
- Consumes: `DEFAULT_GUILD_SETTINGS["default_max_voting_layers"]` (Task 2).
- Produces: `build_default_event(settings=...)` sets `event["max_voting_layers"]` from `settings["default_max_voting_layers"]`, falling back to `10`.

- [ ] **Step 1: Write the failing tests** (`tests/test_build_default_event.py`)

```python
def test_build_default_event_uses_guild_default_max_voting_layers(temp_db):
    db = temp_db
    settings = {**db.DEFAULT_GUILD_SETTINGS, "default_max_voting_layers": 6}
    ev = db.build_default_event(settings=settings)
    assert ev["max_voting_layers"] == 6


def test_build_default_event_falls_back_to_10_when_settings_none(temp_db):
    db = temp_db
    ev = db.build_default_event(settings=None)
    assert ev["max_voting_layers"] == 10


def test_build_default_event_falls_back_to_10_when_key_missing(temp_db):
    db = temp_db
    ev = db.build_default_event(settings={"language": "en"})
    assert ev["max_voting_layers"] == 10
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `pytest tests/test_build_default_event.py -q`
Expected: FAIL — `test_build_default_event_uses_guild_default_max_voting_layers` asserts `6 == 10`.

- [ ] **Step 3: Wire the value.** In `bot/database.py`, in `build_default_event`, replace the line:

```python
        "max_voting_layers": 10,
```

with:

```python
        "max_voting_layers": int((settings or DEFAULT_GUILD_SETTINGS)
                                 .get("default_max_voting_layers", 10) or 10),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_build_default_event.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/database.py tests/test_build_default_event.py
git commit -m "feat: build_default_event honors default_max_voting_layers"
```

---

## Task 4: `duration_str` validation + formatting helpers

**Files:**
- Modify: `bot/bot.py` — add `validate_duration_str` after `parse_voting_duration_input` (currently ends `bot.py:121`); add a `duration_str` branch to `_format_property_value` (`bot.py:3632-3664`)
- Test: `tests/test_duration_str.py` (create)

**Interfaces:**
- Consumes: `bot.parse_duration_to_seconds` (existing).
- Produces:
  - `validate_duration_str(raw: str) -> tuple[bool, Optional[str]]` — `("" / whitespace) -> (True, None)`; valid duration -> `(True, <trimmed str>)`; invalid non-empty -> `(False, None)`.
  - `_format_property_value(value, "duration_str")` -> the stored string, or `"—"` when falsy.

- [ ] **Step 1: Write the failing tests** (`tests/test_duration_str.py`)

```python
import bot as botmod


def test_validate_duration_str_empty_is_none():
    assert botmod.validate_duration_str("") == (True, None)
    assert botmod.validate_duration_str("   ") == (True, None)
    assert botmod.validate_duration_str(None) == (True, None)


def test_validate_duration_str_valid_keeps_trimmed_string():
    assert botmod.validate_duration_str(" 1h ") == (True, "1h")
    assert botmod.validate_duration_str("30m") == (True, "30m")
    assert botmod.validate_duration_str("2d") == (True, "2d")


def test_validate_duration_str_invalid():
    assert botmod.validate_duration_str("abc") == (False, None)
    assert botmod.validate_duration_str("-5") == (False, None)


def test_format_duration_str():
    assert botmod._format_property_value("1h", "duration_str") == "1h"
    assert botmod._format_property_value(None, "duration_str") == "—"
    assert botmod._format_property_value("", "duration_str") == "—"
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `pytest tests/test_duration_str.py -q`
Expected: FAIL with `AttributeError: module 'bot' has no attribute 'validate_duration_str'`

- [ ] **Step 3: Add `validate_duration_str`.** In `bot/bot.py`, immediately after `parse_voting_duration_input` (after the line `return max(1, round(seconds / 3600))`, `bot.py:121`), add:

```python


def validate_duration_str(raw) -> "tuple[bool, Optional[str]]":
    """Validate a duration *string* for the guild-defaults editor.

    Empty/whitespace -> (True, None) (clears the default). A parseable
    duration -> (True, <trimmed string>) stored verbatim (not
    re-canonicalized). A non-empty unparseable value -> (False, None).
    """
    s = (raw or "").strip()
    if not s:
        return True, None
    if parse_duration_to_seconds(s) is None:
        return False, None
    return True, s
```

- [ ] **Step 4: Add the `duration_str` format branch.** In `_format_property_value`, immediately after the `vote_duration` branch (after its `return _format_duration_seconds(int(value) * 3600)`, `bot.py:3654`), add:

```python
    if kind == "duration_str":
        return value if value else "—"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_duration_str.py -q`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add bot/bot.py tests/test_duration_str.py
git commit -m "feat: add duration_str validation and formatting helpers"
```

---

## Task 5: i18n keys for the guild-defaults dialog

**Files:**
- Modify: `bot/i18n.py` — add a `config_defaults.*` block inside `_STRINGS`.

**Interfaces:**
- Produces these `t()` keys (used by Tasks 6–8): `config_defaults.title`, `config_defaults.dm_intro`, `config_defaults.prop.voting_duration`, `config_defaults.prop.max_voting_layers`, `config_defaults.prop.allow_multiple_votes`, `config_defaults.prop.mirror_match`, `config_defaults.prop.suggestion_duration`, `config_defaults.prop.suggestion_start`, `config_defaults.prop.suggestion_start_note`.

- [ ] **Step 1: Add the block.** In `bot/i18n.py`, add the following entries inside the `_STRINGS` dict (place them right after the existing `config.sources_updated` entry near `i18n.py:118` for locality):

```python
    # ── Guild defaults editor (/config_defaults) ──────────────────────────
    "config_defaults.title": {
        "de": "Standardwerte für neue Events",
        "en": "Defaults for new events",
    },
    "config_defaults.dm_intro": {
        "de": ("Bearbeite die Standardwerte, die für **neue** Events verwendet "
               "werden. Bestehende Events bleiben unverändert."),
        "en": ("Edit the defaults applied to **new** events. Existing events "
               "are unaffected."),
    },
    "config_defaults.prop.voting_duration": {
        "de": "Standard-Abstimmungsdauer",
        "en": "Default Voting Duration",
    },
    "config_defaults.prop.max_voting_layers": {
        "de": "Standard-Max. Abstimmungs-Layer",
        "en": "Default Max Voting Layers",
    },
    "config_defaults.prop.allow_multiple_votes": {
        "de": "Standard-Mehrfachauswahl",
        "en": "Default Multiple-Choice Voting",
    },
    "config_defaults.prop.mirror_match": {
        "de": "Standard-Mirror Match",
        "en": "Default Mirror Match",
    },
    "config_defaults.prop.suggestion_duration": {
        "de": "Standard-Vorschlagsdauer",
        "en": "Default Suggestion Duration",
    },
    "config_defaults.prop.suggestion_start": {
        "de": "Standard-Vorschlagsstart (Versatz)",
        "en": "Default Suggestion Start (offset)",
    },
    "config_defaults.prop.suggestion_start_note": {
        "de": ("Versatz ab Erstellungszeit (z. B. `1h` = Vorschläge öffnen 1 "
               "Stunde nach Erstellung). Leer = manuell."),
        "en": ("Offset from creation time (e.g. `1h` = suggestions open 1 hour "
               "after creation). Empty = manual."),
    },
```

- [ ] **Step 2: Verify the module imports and the keys resolve**

Run: `pytest tests/test_imports.py -q`
Expected: PASS. Then sanity-check in a shell:
`cd bot && python -c "from i18n import t; print(t('config_defaults.title','de'), '|', t('config_defaults.prop.max_voting_layers','en'))"`
Expected: `Standardwerte für neue Events | Default Max Voting Layers`

- [ ] **Step 3: Commit**

```bash
git add bot/i18n.py
git commit -m "i18n: add /config_defaults dialog strings (en/de)"
```

---

## Task 6: `EditTarget` abstraction, guild property table, guild persist core

**Files:**
- Modify: `bot/bot.py` — add code block immediately after `_find_edit_property` (`bot.py:3706-3707`) and before `_build_edit_main_embed` (`bot.py:3710`).
- Test: `tests/test_edit_targets.py` (create)

**Interfaces:**
- Consumes: `_read_event_property`, `_write_event_property` (`bot.py:3667-3679`), `_EDIT_PROPERTIES` (`bot.py:3686`), `display_name` (from `utils`), `db.*`, `_resolve_event_sources`, `_resolve_offered_sources`, `_get_guild_lock`, `_update_event_embed`, `t`. (All exist; method bodies resolve names at call time.)
- Produces:
  - `_GUILD_EDIT_PROPERTIES: list[dict]` — 15 guild-editable properties.
  - `class EditTarget` (base) with `EventEditTarget` and `GuildEditTarget`.
  - `_EVENT_TARGET` and `_GUILD_TARGET` instances (used as the default `target=` everywhere in Task 7).
  - `_apply_guild_property(guild_id, prop, value_or_transform) -> Any` — sync read-modify-write of one guild setting.

- [ ] **Step 1: Write the failing tests** (`tests/test_edit_targets.py`)

```python
import bot as botmod


def test_guild_edit_properties_keys_exist_in_defaults():
    keys = [p["key"] for p in botmod._GUILD_EDIT_PROPERTIES]
    assert len(keys) == 15
    assert len(keys) == len(set(keys))  # no dupes
    for k in keys:
        assert k in botmod.db.DEFAULT_GUILD_SETTINGS, k
    assert "event_name" not in keys  # not editable at guild level


def test_guild_target_read_write_is_flat():
    settings = {"max_total_suggestions": 25}
    prop = {"key": "max_total_suggestions"}
    assert botmod._GUILD_TARGET.read(settings, prop) == 25
    botmod._GUILD_TARGET.write(settings, prop, 10)
    assert settings["max_total_suggestions"] == 10


def test_apply_guild_property_persists_value(temp_db):
    db = temp_db
    db.save_guild_settings(5, {"language": "en"})
    botmod._apply_guild_property(5, {"key": "max_total_suggestions"}, 7)
    assert db.get_guild_settings(5)["max_total_suggestions"] == 7


def test_apply_guild_property_seeds_defaults_when_unconfigured(temp_db):
    db = temp_db
    botmod._apply_guild_property(9, {"key": "default_mirror_match"}, True)
    got = db.get_guild_settings(9)
    assert got["default_mirror_match"] is True
    assert got["max_total_suggestions"] == 25  # materialized from defaults


def test_apply_guild_property_accepts_transform(temp_db):
    db = temp_db
    db.save_guild_settings(3, {"blacklisted_units": ["A"]})
    botmod._apply_guild_property(
        3, {"key": "blacklisted_units"}, lambda cur: sorted((set(cur or []) | {"B"})))
    assert db.get_guild_settings(3)["blacklisted_units"] == ["A", "B"]
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `pytest tests/test_edit_targets.py -q`
Expected: FAIL with `AttributeError: module 'bot' has no attribute '_GUILD_EDIT_PROPERTIES'`

- [ ] **Step 3: Insert the implementation.** In `bot/bot.py`, immediately after `_find_edit_property` (after its `return next(...)` line, `bot.py:3707`) and before the comment block preceding `_build_edit_main_embed`, insert:

```python


# Guild-defaults editor property table. Mirrors _EDIT_PROPERTIES but targets
# the flat guild settings dict. event_name is omitted (no guild meaning); the
# default_* keys map the per-event concepts to their guild-default storage.
_GUILD_EDIT_PROPERTIES: list[dict] = [
    {"key": "allowed_gamemodes",          "label_key": "edit.prop.allowed_gamemodes",          "kind": "list",          "source": db.get_unique_gamemodes},
    {"key": "blacklisted_maps",           "label_key": "edit.prop.blacklisted_maps",           "kind": "list",          "source": db.get_unique_maps},
    {"key": "blacklisted_factions",       "label_key": "edit.prop.blacklisted_factions",       "kind": "list",          "source": db.get_unique_factions},
    {"key": "blacklisted_units",          "label_key": "edit.prop.blacklisted_units",          "kind": "list",          "source": db.get_unique_unit_types},
    {"key": "max_suggestions_per_user",   "label_key": "edit.prop.max_per_user",               "kind": "int",           "min": 1, "max": 10},
    {"key": "max_total_suggestions",      "label_key": "edit.prop.max_total",                  "kind": "int",           "min": 1, "max": 25},
    {"key": "max_self_removals_per_user", "label_key": "edit.prop.max_self_removals",          "kind": "int",           "min": 0, "max": 10},
    {"key": "history_lookback_events",    "label_key": "edit.prop.history_lookback",           "kind": "int",           "min": 0, "max": 50},
    {"key": "allowed_sources",            "label_key": "edit.prop.allowed_sources",            "kind": "list",          "source": db.get_unique_sources},
    {"key": "default_voting_duration_hours",  "label_key": "config_defaults.prop.voting_duration",      "kind": "vote_duration"},
    {"key": "default_max_voting_layers",  "label_key": "config_defaults.prop.max_voting_layers",        "kind": "int", "min": 1, "max": 10},
    {"key": "default_allow_multiple_votes",   "label_key": "config_defaults.prop.allow_multiple_votes", "kind": "bool"},
    {"key": "default_mirror_match",       "label_key": "config_defaults.prop.mirror_match",            "kind": "bool", "note_key": "edit.prop.mirror_match_note"},
    {"key": "default_suggestion_duration","label_key": "config_defaults.prop.suggestion_duration",     "kind": "duration_str"},
    {"key": "default_suggestion_start",   "label_key": "config_defaults.prop.suggestion_start",        "kind": "duration_str", "note_key": "config_defaults.prop.suggestion_start_note"},
]


def _apply_guild_property(guild_id: int, prop: dict, value_or_transform):
    """Read-modify-write a single guild setting. Sync core of the guild persist.

    Seeds from DEFAULT_GUILD_SETTINGS when the guild has no row yet, so the
    saved blob materializes a full settings dict. `value_or_transform` may be a
    value or a callable receiving the current value.
    """
    settings = db.get_guild_settings(guild_id) or dict(db.DEFAULT_GUILD_SETTINGS)
    key = prop["key"]
    if callable(value_or_transform):
        value = value_or_transform(settings.get(key))
    else:
        value = value_or_transform
    settings[key] = value
    db.save_guild_settings(guild_id, settings)
    return value


class EditTarget:
    """What the DM edit dialog operates on. Subclasses bind it to an event or
    to the guild defaults. `target` defaults to the event target everywhere so
    the per-event path is unchanged."""

    kind = "event"
    properties: list[dict] = []
    has_phase_lock = False
    shows_event_link = False

    def load(self, guild_id: int, db_id):
        raise NotImplementedError

    def read(self, obj: dict, prop: dict):
        raise NotImplementedError

    def write(self, obj: dict, prop: dict, value) -> None:
        raise NotImplementedError

    async def persist(self, guild_id: int, db_id, prop: dict, value_or_transform) -> bool:
        raise NotImplementedError

    def overview_title(self, obj: dict, db_id, guild_id: int, lang: str) -> str:
        raise NotImplementedError

    def overview_description(self, lang: str) -> str:
        return f"{t('edit.title', lang)}\n{t('edit.select_property', lang)}"

    def scope_sources(self, obj: dict, guild_id: int) -> list:
        raise NotImplementedError


class EventEditTarget(EditTarget):
    kind = "event"
    properties = _EDIT_PROPERTIES
    has_phase_lock = True
    shows_event_link = True

    def load(self, guild_id, db_id):
        record = db.get_event_by_db_id(guild_id, db_id)
        return record["event"] if record else None

    def read(self, obj, prop):
        return _read_event_property(obj, prop["key"], prop.get("target", "event"))

    def write(self, obj, prop, value):
        _write_event_property(obj, prop["key"], prop.get("target", "event"), value)

    async def persist(self, guild_id, db_id, prop, value_or_transform):
        lock = _get_guild_lock(guild_id)
        async with lock:
            record = db.get_event_by_db_id(guild_id, db_id)
            if not record:
                return False
            event = record["event"]
            if callable(value_or_transform):
                value = value_or_transform(self.read(event, prop))
            else:
                value = value_or_transform
            self.write(event, prop, value)
            db.save_event(record["db_id"], event)
        await _update_event_embed(db_id)
        return True

    def overview_title(self, obj, db_id, guild_id, lang):
        return display_name(obj, db_id, lang=lang)

    def scope_sources(self, obj, guild_id):
        settings = db.get_guild_settings(guild_id) or {}
        return _resolve_event_sources(obj, settings)


class GuildEditTarget(EditTarget):
    kind = "guild"
    properties = _GUILD_EDIT_PROPERTIES
    has_phase_lock = False
    shows_event_link = False

    def load(self, guild_id, db_id):
        return db.get_guild_settings(guild_id) or dict(db.DEFAULT_GUILD_SETTINGS)

    def read(self, obj, prop):
        return obj.get(prop["key"])

    def write(self, obj, prop, value):
        obj[prop["key"]] = value

    async def persist(self, guild_id, db_id, prop, value_or_transform):
        lock = _get_guild_lock(guild_id)
        async with lock:
            _apply_guild_property(guild_id, prop, value_or_transform)
        return True

    def overview_title(self, obj, db_id, guild_id, lang):
        return t("config_defaults.title", lang)

    def overview_description(self, lang):
        return f"{t('config_defaults.dm_intro', lang)}\n{t('edit.select_property', lang)}"

    def scope_sources(self, obj, guild_id):
        return _resolve_offered_sources(obj)


_EVENT_TARGET = EventEditTarget()
_GUILD_TARGET = GuildEditTarget()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_edit_targets.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the whole suite (no regressions)**

Run: `pytest -q`
Expected: PASS (all tests so far)

- [ ] **Step 6: Commit**

```bash
git add bot/bot.py tests/test_edit_targets.py
git commit -m "feat: add EditTarget abstraction and guild property table"
```

---

## Task 7: Make the editor helpers and views target-aware

This is one cohesive refactor: thread a `target` keyword argument (default `_EVENT_TARGET`) through every editor helper, view, and modal, and route all object access through `target`. Because the default is the event target, the per-event path is unchanged. The module must stay importable after every step; full verification is the import test plus a manual per-event regression at the end.

**Files:**
- Modify: `bot/bot.py` — `_build_edit_main_embed`, `EditMainView`, `_show_property_editor`, `EditListView`, `EditBoolView`, `EditScalarView`, `EditDateTimeModal`, `EditScalarModal`, `EditStringModal`, `_refresh_main_view`, `_bounce_to_main`, `_persist_property_value`, `_apply_edit`, `_show_scoped_blacklist_source_picker`, `_show_scoped_blacklist_editor`, `ScopedBlacklistSourceView`, `ScopedBlacklistView`.

**Interfaces:**
- Consumes: `_EVENT_TARGET`, `_GUILD_TARGET`, `EditTarget` (Task 6); `validate_duration_str` (Task 4).
- Produces: every listed callable accepts a `target` keyword (default `_EVENT_TARGET`); views expose `self.target`.

- [ ] **Step 1: Replace `_build_edit_main_embed`** (`bot.py:3710-3732`) with:

```python
def _build_edit_main_embed(obj: dict, db_id, guild_id: int, lang: str,
                           updated_label: Optional[str] = None, *,
                           target: "EditTarget" = _EVENT_TARGET) -> discord.Embed:
    """Property overview embed shown at the top of every DM dialog state."""
    embed = discord.Embed(
        title=target.overview_title(obj, db_id, guild_id, lang),
        description=target.overview_description(lang),
        color=discord.Color.blurple(),
    )
    for prop in target.properties:
        value = target.read(obj, prop)
        formatted = _format_property_value(value, prop["kind"])
        embed.add_field(
            name=t(prop["label_key"], lang),
            value=f"`{formatted}`",
            inline=True,
        )
    if updated_label:
        embed.add_field(
            name="​",
            value=f"✅ {t('edit.updated_inline', lang, prop=updated_label)}",
            inline=False,
        )
    return embed
```

- [ ] **Step 2: Replace `EditMainView`** (`bot.py:3963-4021`) with:

```python
class EditMainView(ui.View):
    """Top-level DM view: a property dropdown + a Done button."""

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str, *,
                 target: "EditTarget" = _EVENT_TARGET):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.target = target

        options = [
            discord.SelectOption(label=t(prop["label_key"], lang)[:100], value=prop["key"])
            for prop in target.properties
        ]
        select = ui.Select(
            placeholder=t("edit.pick_property_placeholder", lang),
            options=options, min_values=1, max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

        done = ui.Button(
            label=t("edit.done", lang),
            style=discord.ButtonStyle.secondary, emoji="🛑",
        )
        done.callback = self._on_done
        self.add_item(done)

    async def _on_select(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        prop = next((p for p in self.target.properties if p["key"] == key), None)
        if not prop:
            return
        await _show_property_editor(interaction, self.user_id, self.db_id,
                                    self.guild_id, self.lang, prop, target=self.target)

    async def _on_done(self, interaction: discord.Interaction):
        _close_session(self.user_id)
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass
        text = t("edit.finished", self.lang)
        if self.target.shows_event_link:
            url = _event_message_url(self.guild_id, self.db_id)
            if url:
                text = f"{text} [{t('edit.event_link', self.lang)}]({url})"
        try:
            await interaction.channel.send(text)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)
```

- [ ] **Step 3: Replace `_show_property_editor`** (`bot.py:4024-4122`) with:

```python
async def _show_property_editor(interaction: discord.Interaction, user_id: int,
                                db_id, guild_id: int, lang: str, prop: dict, *,
                                target: "EditTarget" = _EVENT_TARGET) -> None:
    """Render the editor UI for a specific property, replacing the main view."""
    obj = target.load(guild_id, db_id)
    if obj is None:
        _close_session(user_id)
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
            view=None,
        )
        return

    current = target.read(obj, prop)
    label = t(prop["label_key"], lang)

    if (target.has_phase_lock and prop["key"] == "suggestion_start_time"
            and obj.get("phase", "created") != "created"):
        await _bounce_to_main(interaction, user_id, db_id, guild_id, lang,
                              t("edit.locked_phase", lang), target=target)
        return

    if prop["kind"] == "list":
        if prop["key"] in ("blacklisted_maps", "blacklisted_factions"):
            await _show_scoped_blacklist_source_picker(
                interaction, user_id, db_id, guild_id, lang, prop, obj, target=target)
            return

        choices = prop["source"]() if prop.get("source") else []
        if not choices:
            fallback_view = EditMainView(user_id, db_id, guild_id, lang, target=target)
            _set_active_view(user_id, fallback_view)
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("cache.empty", lang), color=discord.Color.orange()),
                view=fallback_view,
            )
            return
        visible = choices[:25]
        current_set = set(current or [])
        if prop["key"] == "allowed_sources" and not current_set:
            current_set = set(visible)
        initial_selected = current_set & set(visible)

        view = EditListView(user_id, db_id, guild_id, lang, prop, visible,
                            initial_selected, target=target)
        _set_active_view(user_id, view)
        await interaction.response.edit_message(
            embed=_edit_list_embed(prop, lang), view=view)

    elif prop["kind"] == "bool":
        view = EditBoolView(user_id, db_id, guild_id, lang, prop, bool(current), target=target)
        _set_active_view(user_id, view)
        desc = t("edit.bool_prompt", lang, value=_format_property_value(current, "bool"))
        if prop.get("note_key"):
            desc = f"{desc}\n\n{t(prop['note_key'], lang)}"
        embed = discord.Embed(title=label, description=desc, color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=view)

    elif prop["kind"] == "string":
        view = EditScalarView(user_id, db_id, guild_id, lang, prop, target=target)
        _set_active_view(user_id, view)
        fallback = t("event.fallback_name", lang, db_id=db_id)
        desc = t(
            "edit.string_prompt", lang,
            current=_format_property_value(current, "string"),
            max=EVENT_NAME_MAX_LENGTH,
            fallback=fallback,
        )
        embed = discord.Embed(title=label, description=desc, color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=view)

    else:  # int / duration / duration_str / vote_duration / datetime — via Modal
        view = EditScalarView(user_id, db_id, guild_id, lang, prop, target=target)
        _set_active_view(user_id, view)
        if prop["kind"] == "int":
            desc = t("edit.int_prompt", lang,
                     current=_format_property_value(current, "int"),
                     min=prop.get("min", "—"), max=prop.get("max", "—"))
        elif prop["kind"] == "datetime":
            desc = t("edit.datetime_prompt", lang,
                     current=_format_property_value(current, "datetime"))
        else:
            desc = t("edit.duration_prompt", lang,
                     current=_format_property_value(current, prop["kind"]))
        if prop.get("note_key"):
            desc = f"{desc}\n\n{t(prop['note_key'], lang)}"
        embed = discord.Embed(title=label, description=desc, color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=view)
```

- [ ] **Step 4: Update `EditListView`** (`bot.py:4133-4206`). Change the constructor signature and add `self.target`; pass `target=` to the child view, `_apply_edit`, and `_refresh_main_view`:

Constructor line `def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str,\n                 prop: dict, choices: list[str], selected: set):` becomes:

```python
    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, choices: list[str], selected: set, *,
                 target: "EditTarget" = _EVENT_TARGET):
```

Add after `self.selected = set(selected)`:

```python
        self.target = target
```

In `_on_select`, the `new_view = EditListView(...)` call gains `, target=self.target` as the last argument:

```python
        new_view = EditListView(
            self.user_id, self.db_id, self.guild_id, self.lang,
            self.prop, self.choices, self.selected, target=self.target)
```

In `_on_done`, the `_apply_edit(...)` call gains `, target=self.target`:

```python
        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, new_value, target=self.target)
```

In `_on_cancel`, the `_refresh_main_view(...)` call gains `, target=self.target`:

```python
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)
```

- [ ] **Step 5: Update `EditBoolView`** (`bot.py:4209-4255`). Constructor signature → add `*, target=_EVENT_TARGET`; store `self.target = target` (after `self.prop = prop`). In `_make_setter`'s inner `cb`, the `_apply_edit(...)` call gains `, target=self.target`. In `_on_cancel`, the `_refresh_main_view(...)` call gains `, target=self.target`.

```python
    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, current_value: bool, *,
                 target: "EditTarget" = _EVENT_TARGET):
```
```python
        self.prop = prop
        self.target = target
```
```python
        async def cb(interaction: discord.Interaction):
            await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                              self.lang, self.prop, value, target=self.target)
```
```python
    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)
```

- [ ] **Step 6: Update `EditScalarView`** (`bot.py:4258-4304`). Constructor signature → add `*, target=_EVENT_TARGET`; store `self.target = target` (after `self.prop = prop`). In `_on_edit`, pass `target=self.target` to the modal. In `_on_cancel`, pass `target=self.target` to `_refresh_main_view`.

```python
    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, *, target: "EditTarget" = _EVENT_TARGET):
```
```python
        self.prop = prop
        self.target = target
```
```python
        modal = modal_cls(self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, target=self.target)
```
```python
    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)
```

- [ ] **Step 7: Update the three modals** (`EditDateTimeModal` `bot.py:4307`, `EditScalarModal` `bot.py:4348`, `EditStringModal` `bot.py:4406`). For **each**: constructor signature gains `*, target: "EditTarget" = _EVENT_TARGET`; add `self.target = target` after `self.prop = prop`; the `_apply_edit(...)` call in `on_submit` gains `, target=self.target`.

Example for `EditDateTimeModal.__init__` and its submit:

```python
    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, *, target: "EditTarget" = _EVENT_TARGET):
```
```python
        self.prop = prop
        self.target = target
```
```python
        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, value, via_modal=True, target=self.target)
```

Apply the identical three edits to `EditScalarModal` and `EditStringModal`.

- [ ] **Step 8: Add `duration_str` handling to `EditScalarModal`.** In `EditScalarModal.__init__`, the input is currently `required=True`; make `duration_str` optional. Replace the `self.value_input = ui.TextInput(...)` block (`bot.py:4364-4370`) with:

```python
        self.value_input = ui.TextInput(
            label=t("edit.input_label", lang)[:45],
            placeholder=placeholder,
            required=prop["kind"] != "duration_str",
            max_length=20,
        )
```

In `EditScalarModal.on_submit`, insert a `duration_str` branch immediately before the final `else:  # duration` branch (before `bot.py:4395`):

```python
        elif self.prop["kind"] == "duration_str":
            ok, value = validate_duration_str(raw)
            if not ok:
                await interaction.response.send_message(
                    t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
                return
```

- [ ] **Step 9: Replace `_persist_property_value`** (`bot.py:4699-4723`) with the target delegate (the old body now lives in `EventEditTarget.persist`):

```python
async def _persist_property_value(guild_id: int, db_id, prop: dict,
                                  value_or_transform, *,
                                  target: "EditTarget" = _EVENT_TARGET) -> bool:
    """Persist a property value via the target (lock + write + optional refresh).

    `value_or_transform` may be a value or a callable receiving the current
    value (invoked inside the lock for atomicity). Returns False when the
    underlying object is gone (event deleted); guild persist always returns True.
    """
    return await target.persist(guild_id, db_id, prop, value_or_transform)
```

- [ ] **Step 10: Replace `_apply_edit`** (`bot.py:4744-4753`) with:

```python
async def _apply_edit(interaction: discord.Interaction, user_id: int,
                      db_id, guild_id: int, lang: str,
                      prop: dict, value, via_modal: bool = False, *,
                      target: "EditTarget" = _EVENT_TARGET) -> None:
    """Persist an edit, refresh the embed, return to the main view."""
    if not await _persist_property_value(guild_id, db_id, prop, value, target=target):
        await _notify_event_gone(interaction, user_id, lang, via_modal=via_modal)
        return
    label = t(prop["label_key"], lang)
    await _refresh_main_view(interaction, user_id, db_id, guild_id, lang,
                             updated_label=label, via_modal=via_modal, target=target)
```

- [ ] **Step 11: Replace `_refresh_main_view`** (`bot.py:3919-3960`) with:

```python
async def _refresh_main_view(interaction: discord.Interaction, user_id: int,
                             db_id, guild_id: int, lang: str,
                             updated_label: Optional[str] = None,
                             via_modal: bool = False, *,
                             target: "EditTarget" = _EVENT_TARGET) -> None:
    """Re-render the property selector after an edit or cancel."""
    obj = target.load(guild_id, db_id)
    if obj is None:
        _close_session(user_id)
        if via_modal:
            try:
                await interaction.response.send_message(
                    t("event.no_event", lang), ephemeral=True)
            except discord.InteractionResponded:
                pass
        else:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
                view=None,
            )
        return

    embed = _build_edit_main_embed(obj, db_id, guild_id, lang,
                                   updated_label=updated_label, target=target)
    view = EditMainView(user_id, db_id, guild_id, lang, target=target)
    _set_active_view(user_id, view)

    if via_modal:
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        session = _active_edit_sessions.get(user_id)
        dm_msg = session.get("dm_message") if session else None
        if dm_msg is not None:
            await dm_msg.edit(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
```

- [ ] **Step 12: Replace `_bounce_to_main`** (`bot.py:4445-4454`) with:

```python
async def _bounce_to_main(interaction: discord.Interaction, user_id: int,
                          db_id, guild_id: int, lang: str,
                          message: str, *,
                          target: "EditTarget" = _EVENT_TARGET) -> None:
    """Show a brief notice and reattach the main edit view."""
    fallback_view = EditMainView(user_id, db_id, guild_id, lang, target=target)
    _set_active_view(user_id, fallback_view)
    await interaction.response.edit_message(
        embed=discord.Embed(description=message, color=discord.Color.orange()),
        view=fallback_view,
    )
```

- [ ] **Step 13: Update the scoped blacklist source picker** (`_show_scoped_blacklist_source_picker`, `bot.py:4457-4485`). Replace its signature and the source resolution; thread `target` to all callees and the source view:

```python
async def _show_scoped_blacklist_source_picker(
        interaction: discord.Interaction, user_id: int, db_id,
        guild_id: int, lang: str, prop: dict, obj: dict, *,
        target: "EditTarget" = _EVENT_TARGET) -> None:
    """Step 1 of the scoped blacklist edit: pick a source."""
    sources = target.scope_sources(obj, guild_id)

    if not sources:
        await _bounce_to_main(interaction, user_id, db_id, guild_id, lang,
                              t("cache.empty", lang), target=target)
        return

    if len(sources) == 1:
        await _show_scoped_blacklist_editor(
            interaction, user_id, db_id, guild_id, lang, prop, sources[0], target=target)
        return

    view = ScopedBlacklistSourceView(user_id, db_id, guild_id, lang, prop, sources, target=target)
    _set_active_view(user_id, view)
    embed = discord.Embed(
        title=t(prop["label_key"], lang),
        description=t("suggest.select_source", lang),
        color=discord.Color.blurple(),
    )
    await interaction.response.edit_message(embed=embed, view=view)
```

- [ ] **Step 14: Update the scoped blacklist editor** (`_show_scoped_blacklist_editor`, `bot.py:4498-4546`). Replace its signature and the object load/read; thread `target` to `_bounce_to_main` and the `ScopedBlacklistView`. Only the first lines and the calls change; the bucket-building stays identical:

Signature:
```python
async def _show_scoped_blacklist_editor(
        interaction: discord.Interaction, user_id: int, db_id,
        guild_id: int, lang: str, prop: dict, source: str, *,
        target: "EditTarget" = _EVENT_TARGET) -> None:
```

Replace the load+read block (`record = db.get_event_by_db_id(...)` through `bl_set = set(blacklist)`) with:
```python
    obj = target.load(guild_id, db_id)
    if obj is None:
        await _notify_event_gone(interaction, user_id, lang)
        return
    blacklist = target.read(obj, prop) or []
    bl_set = set(blacklist)
    source_filter = [source] if source else None
```

Both `_bounce_to_main(...)` calls in this function gain `, target=target`. The final view construction becomes:
```python
    view = ScopedBlacklistView(user_id, db_id, guild_id, lang, prop, source, buckets, target=target)
```

- [ ] **Step 15: Update `ScopedBlacklistSourceView`** (`bot.py:4549-4587`). Constructor signature gains `*, target=_EVENT_TARGET`; store `self.target = target` (after `self.prop = prop`). In `_on_select`, the `_show_scoped_blacklist_editor(...)` call gains `, target=self.target`. In `_on_cancel`, the `_refresh_main_view(...)` call gains `, target=self.target`.

```python
    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, sources: list[str], *,
                 target: "EditTarget" = _EVENT_TARGET):
```
```python
        self.prop = prop
        self.target = target
```
```python
        await _show_scoped_blacklist_editor(
            interaction, self.user_id, self.db_id, self.guild_id, self.lang,
            self.prop, source, target=self.target)
```
```python
    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)
```

- [ ] **Step 16: Update `ScopedBlacklistView`** (`bot.py:4590-4696`). Constructor signature gains `*, target=_EVENT_TARGET`; store `self.target = target` (after `self.buckets = buckets`). In `_make_callback`, the inner `new_view = ScopedBlacklistView(...)` gains `, target=self.target`. In `_on_done`, the `_persist_property_value(...)` call gains `, target=self.target` and the trailing `_refresh_main_view(...)` gains `, target=self.target`. In `_on_cancel`, the `_refresh_main_view(...)` call gains `, target=self.target`.

```python
    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, source: str, buckets: list, *,
                 target: "EditTarget" = _EVENT_TARGET):
```
```python
        self.buckets = buckets
        self.target = target
```
```python
            new_view = ScopedBlacklistView(
                self.user_id, self.db_id, self.guild_id, self.lang,
                self.prop, self.source, self.buckets, target=self.target)
```
```python
        ok = await _persist_property_value(
            self.guild_id, self.db_id, self.prop, transform, target=self.target)
        if not ok:
            await _notify_event_gone(interaction, self.user_id, self.lang)
            return
        label = t(self.prop["label_key"], self.lang)
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, updated_label=label,
                                 target=self.target)
```
```python
    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)
```

- [ ] **Step 17: Verify the module imports and tests pass**

Run: `pytest -q`
Expected: PASS (all tests; `test_import_bot_module` confirms `bot.py` still imports with the refactor).

- [ ] **Step 18: Manual per-event regression** (the safety net for the refactor — there are no UI unit tests). With the bot running in a test guild, open an event's Admin → Edit dialog and confirm it is unchanged: edit a list field (gamemodes), a scoped blacklist (maps), a bool (mirror match), an int (max total), the voting duration, and the suggestion start time; confirm each persists and the public embed refreshes, and that the `suggestion_start_time` phase-lock still triggers once the event has left the `created` phase.

- [ ] **Step 19: Commit**

```bash
git add bot/bot.py
git commit -m "refactor: make the DM edit dialog target-aware (event default unchanged)"
```

---

## Task 8: `_open_edit_session` helper and `/config_defaults` command

**Files:**
- Modify: `bot/bot.py` — add `_open_edit_session` near `admin_edit_event` (`bot.py:3769`); rewrite `admin_edit_event` to delegate; add `cmd_config_defaults` in the Setup & Config command block (after `cmd_refresh_layers`, `bot.py:4990`).

**Interfaces:**
- Consumes: `_active_edit_sessions`, `SESSION_STALE_AFTER_SECONDS`, `_force_close_stale_session`, `_build_edit_main_embed`, `EditMainView`, `_EVENT_TARGET`, `_GUILD_TARGET`, `check_guild_configured`, `check_organizer`, `t`, `time.monotonic`.
- Produces: `_open_edit_session(interaction, *, target, db_id, guild_id, lang, via_component)`; `/config_defaults` slash command.

- [ ] **Step 1: Add `_open_edit_session`.** Insert immediately before `admin_edit_event` (`bot.py:3769`):

```python
async def _open_edit_session(interaction: discord.Interaction, *,
                             target: "EditTarget", db_id, guild_id: int,
                             lang: str, via_component: bool) -> None:
    """Reserve a DM edit session for `target` and DM the user the overview.

    `via_component=True` responds by editing the triggering message (used by
    the Admin → Edit button); `False` responds with an ephemeral message
    (used by slash commands).
    """
    user = interaction.user

    async def _respond(embed: discord.Embed):
        if via_component:
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    existing = _active_edit_sessions.get(user.id)
    if existing is not None:
        last = existing.get("last_activity", 0)
        if time.monotonic() - last < SESSION_STALE_AFTER_SECONDS:
            await _respond(discord.Embed(description=t("edit.session_active", lang),
                                         color=discord.Color.orange()))
            return
        await _force_close_stale_session(user.id)

    obj = target.load(guild_id, db_id)
    if obj is None:
        await _respond(discord.Embed(description=t("event.no_event", lang),
                                     color=discord.Color.red()))
        return

    try:
        dm = await user.create_dm()
    except discord.Forbidden:
        await _respond(discord.Embed(description=t("edit.dm_blocked", lang),
                                     color=discord.Color.red()))
        return

    session: dict = {
        "db_id": db_id,
        "guild_id": guild_id,
        "lang": lang,
        "dm_message": None,
        "active_view": None,
        "last_activity": time.monotonic(),
        "target": target,
    }
    _active_edit_sessions[user.id] = session

    embed = _build_edit_main_embed(obj, db_id, guild_id, lang, target=target)
    view = EditMainView(user.id, db_id, guild_id, lang, target=target)
    try:
        dm_msg = await dm.send(embed=embed, view=view)
    except discord.Forbidden:
        _active_edit_sessions.pop(user.id, None)
        await _respond(discord.Embed(description=t("edit.dm_blocked", lang),
                                     color=discord.Color.red()))
        return
    session["dm_message"] = dm_msg
    session["active_view"] = view

    await _respond(discord.Embed(description=f"📨 {t('edit.dm_sent', lang)}",
                                 color=discord.Color.green()))
```

- [ ] **Step 2: Rewrite `admin_edit_event`** (`bot.py:3769-3838`) to delegate:

```python
async def admin_edit_event(interaction: discord.Interaction, db_id: int):
    """Kick off a DM edit session for this event. Triggered by Admin → Edit."""
    settings = db.get_guild_settings(interaction.guild_id) or {}
    lang = settings.get("language", "en")
    await _open_edit_session(interaction, target=_EVENT_TARGET, db_id=db_id,
                             guild_id=interaction.guild_id, lang=lang,
                             via_component=True)
```

- [ ] **Step 3: Add the `/config_defaults` command.** Insert after `cmd_refresh_layers` (`bot.py:4990` block) in the Setup & Config section:

```python
@bot.tree.command(name="config_defaults",
                  description="Edit the default settings new events start from (organizer only)")
async def cmd_config_defaults(interaction: discord.Interaction):
    """Open the guild-defaults DM editor (same dialog as Admin → Edit)."""
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return
    lang = settings.get("language", "en")
    await _open_edit_session(interaction, target=_GUILD_TARGET, db_id=None,
                             guild_id=interaction.guild_id, lang=lang,
                             via_component=False)
```

- [ ] **Step 4: Verify imports/tests still pass**

Run: `pytest -q`
Expected: PASS (no new unit tests; this confirms the module imports with the new command registered).

- [ ] **Step 5: Manual verification of the full feature.** With the bot running in a test guild:
  1. As an organizer, run `/config_defaults`. Confirm the bot replies ephemerally "📨 …" and DMs the overview with all 15 fields and the "Existing events are unaffected" intro.
  2. Edit each kind: a list (gamemodes), a scoped blacklist (maps), a bool (default mirror match), an int (default max voting layers), the default voting duration, and the `duration_str` default suggestion start (set `2h`, then clear it to empty). Confirm each persists (re-open the field to see the new value).
  3. Run `/create_layer_suggestion`: confirm the wizard prefills from the new defaults (start offset, voting duration, multiple-votes/mirror toggles) and the created event's `max_voting_layers` matches `default_max_voting_layers`.
  4. Confirm an event created *before* the change is unchanged.
  5. As a non-organizer, run `/config_defaults` and confirm rejection (`general.requires_organizer`).
  6. Confirm the per-event Admin → Edit dialog still works (already covered in Task 7 Step 18).

- [ ] **Step 6: Commit**

```bash
git add bot/bot.py
git commit -m "feat: add /config_defaults guild-defaults editor command"
```

---

## Task 9: Documentation

**Files:**
- Modify: `README.md`, `USER_GUIDE.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Document the command in `README.md`.** In the commands/Setup table, add a row for `/config_defaults` (organizer-only): "Edit the guild-wide defaults new events start from (opens the same DM dialog as Admin → Edit)." Note that changes affect only newly created events.

- [ ] **Step 2: Document it in `USER_GUIDE.md`.** Add a short subsection under the organizer/configuration area explaining that `/config_defaults` opens a DM dialog identical to Admin → Edit Event but for the guild defaults; list the fields; and state explicitly that edits apply to new events only (existing events keep their creation snapshot; the layer-source cap still applies live).

- [ ] **Step 3: Commit**

```bash
git add README.md USER_GUIDE.md
git commit -m "docs: document /config_defaults command"
```

---

## Self-Review

**1. Spec coverage**

| Spec item | Task |
|---|---|
| §1 add `default_max_voting_layers` + no migration | Task 2 |
| §1 wire `build_default_event` | Task 3 |
| §2 `EditTarget` (event + guild), session carries target | Tasks 6, 7, 8 |
| §3 `_GUILD_EDIT_PROPERTIES` (15 fields, no `event_name`) | Task 6 |
| §3 new `duration_str` kind (validate + format + modal, trimmed string, empty→None) | Tasks 4, 7 (Step 8) |
| §4 reused scoped blacklist / allowed_sources preselect / sessions | Task 7 (Steps 3, 13–16) |
| §5 `/config_defaults` organizer-gated + `_open_edit_session` | Task 8 |
| §6 i18n keys (en/de), revived intent of orphaned defaults strings | Task 5 |
| §7 new-events-only behavior; existing events unaffected | Tasks 3, 8 (Step 5.4) |
| Testing: pytest data/logic layer | Tasks 1–6 |
| Testing: manual UI incl. per-event regression | Task 7 (Step 18), Task 8 (Step 5) |

No gaps.

**2. Placeholder scan:** No `TBD`/`TODO`/"add error handling"/"similar to Task N"; every code step shows complete code. ✓

**3. Type consistency:** `target` is `EditTarget`-typed throughout; `_EVENT_TARGET`/`_GUILD_TARGET` defined in Task 6 before any default-arg use in Task 7; `validate_duration_str` (Task 4) used in Task 7 Step 8; `_apply_guild_property` signature `(guild_id, prop, value_or_transform)` matches its test and `GuildEditTarget.persist` call; `_open_edit_session` keyword args match both call sites. `db_id` typed loosely (`int`/`None`) since the guild target passes `None`. ✓

## Execution Handoff

Per your instruction: after the tasks are implemented, run `/superpowers:requesting-code-review`, then commit any review-driven fixes. (Task-level commits above are the normal TDD cadence; the review happens after Task 9.)
