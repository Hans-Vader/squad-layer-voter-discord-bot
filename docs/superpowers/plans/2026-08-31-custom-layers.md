# Custom Maps / Layers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an organizer register a map's layers by hand from Discord, so a modded or unreleased map can go on the ballot without a redeploy.

**Architecture:** Custom maps live per guild in a new `custom_layers` table holding only what the admin typed. A materialization step expands each stored map into ordinary `layer_cache` rows tagged `custom:<guild_id>`, so every existing query path — map dropdown, mode dropdown, faction/unit pickers, vehicle info, voting, history — treats them like any other layer. Everything derivable (gamemode, version, faction metadata) is re-resolved against the live cache on each materialization, so `/refresh_layers` can keep wiping `layer_cache` wholesale.

**Tech Stack:** Python 3.12, discord.py ≥ 2.0, SQLite (stdlib `sqlite3`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-custom-layers-design.md`

## Global Constraints

- Python 3.12; discord.py `>=2.0.0`. No new dependencies.
- Tests run in Docker — there is no local Python environment:
  ```
  docker run --rm -e DISCORD_BOT_TOKEN=test -v "$PWD":/app -w /app python:3.12-slim \
    sh -c "pip install -q -r requirements.txt -r requirements-dev.txt && python -m pytest -q"
  ```
  Referred to below as `$PYTEST`. Append `tests/test_x.py::test_y -v` to the
  `python -m pytest` part to run a single test.
- `tests/test_winner_copy_text.py::test_copy_text_real_layer_fits_event_description`
  **fails before this work starts** — `reference/` is gitignored and empty in a
  clean checkout. Do not try to fix it; the baseline is 58 passed, 1 failed.
- Source name for a guild's custom layers: `custom:<guild_id>`, prefix constant
  `CUSTOM_SOURCE_PREFIX = "custom:"`.
- Discord limits that bound the design: 25 options per select, 5 action rows per
  message, no selects inside modals.
- i18n: every user-facing string goes through `t(key, lang)` with a `de` and an
  `en` entry in the flat `_STRINGS` dict in `bot/i18n.py`.
- Views that navigate away from themselves must call `self.stop()` before the
  replacement is attached (see the `AutoDisableView` docstring at `bot/bot.py:909`).
- Work on branch `feat/custom-layers`, which already carries the spec commits.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `bot/config.py` (modify) | `CUSTOM_SOURCE_PREFIX` constant — the leaf module both `database.py` and `utils.py` can import without a cycle |
| `bot/database.py` (modify) | `custom_layers` table, its CRUD, and the cache queries custom layers need (`get_faction_reference`, `get_gamemode_samples`, `has_layers_for_source`, `delete_layers`); `get_unique_sources()` learns to hide custom sources |
| `bot/custom_layers.py` (create) | Everything about custom layers that does not need Discord: parsing, the gamemode token map, reference-source resolution, faction construction, materialization, save/delete. Imports `database`, `config`, `utils` only |
| `bot/utils.py` (modify) | `source_label()` — renders `custom:123` as a localized name |
| `bot/bot.py` (modify) | Discord surface only: the Admin button, the sub-panel, the modal, the details view, and the wiring of source resolution / units lookup / refresh + boot hooks |
| `bot/i18n.py` (modify) | 26 new keys, de + en |
| `tests/test_custom_layers.py` (create) | Parser, token map, faction builder, materialization, DB round-trips |
| `tests/test_custom_maps_ui.py` (create) | View composition, in the style of `tests/test_admin_panel_buttons.py` |
| `README.md`, `USER_GUIDE.md`, `ARCHITECTURE.md` (modify) | Documentation |

`bot/bot.py` is ~6900 lines already. Keeping the non-Discord logic in its own
module is what makes the bulk of this feature testable without a client, and
stops the big file from growing by another few hundred lines.

---

## Task 1: Database layer

**Files:**
- Modify: `bot/config.py` (append near the layer-source block, after line 119)
- Modify: `bot/database.py:110-186` (schema), `bot/database.py:474-486` (`get_unique_sources`), plus new functions
- Test: `tests/test_custom_layers.py` (create)

**Interfaces:**
- Consumes: `_get_conn`, `_dumps`, `_loads`, `_source_filter`, `upsert_layer` — all existing in `bot/database.py`
- Produces:
  - `config.CUSTOM_SOURCE_PREFIX: str`
  - `database.custom_source(guild_id: int) -> str`
  - `database.upsert_custom_map(guild_id: int, map_name: str, payload: dict) -> None`
  - `database.get_custom_maps(guild_id: int) -> list[dict]` — items are `{"guild_id": int, "map_name": str, "payload": dict}`
  - `database.get_all_custom_maps() -> list[dict]` — same item shape
  - `database.delete_custom_map(guild_id: int, map_name: str) -> bool`
  - `database.delete_layers(source: str, map_name: str) -> int`
  - `database.has_layers_for_source(source: str) -> bool`
  - `database.get_faction_reference(allowed_sources: list[str] | None = None) -> dict[str, dict]`
  - `database.get_gamemode_samples(allowed_sources: list[str] | None = None) -> list[tuple[str, str]]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_custom_layers.py`:

```python
"""Custom maps: storage, parsing and materialization."""

import pytest

import database


def _seed_layer(db, raw_name, source, map_name, gamemode, factions=None,
                layer_version="v1"):
    """Put one layer row in the cache with the shape fetch_and_cache_layers writes."""
    db.upsert_layer(
        raw_name=raw_name, source=source, map_name=map_name, map_id=map_name.lower(),
        gamemode=gamemode, layer_version=layer_version,
        factions=factions if factions is not None else [],
        team1_alliances=[], team2_alliances=[], map_size_km=None,
    )


def test_custom_source_name():
    assert database.custom_source(42) == "custom:42"


def test_custom_map_roundtrip(temp_db):
    payload = {"layers": ["Belaya_TC_v1"], "factions": ["USA"], "units": []}
    temp_db.upsert_custom_map(1, "Belaya", payload)

    maps = temp_db.get_custom_maps(1)
    assert [m["map_name"] for m in maps] == ["Belaya"]
    assert maps[0]["payload"] == payload
    assert maps[0]["guild_id"] == 1
    assert temp_db.get_custom_maps(2) == []


def test_custom_map_upsert_overwrites(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v1"]})
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v2"]})
    assert temp_db.get_custom_maps(1)[0]["payload"]["layers"] == ["Belaya_TC_v2"]


def test_get_all_custom_maps_spans_guilds(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": []})
    temp_db.upsert_custom_map(2, "Kokan", {"layers": []})
    assert {(m["guild_id"], m["map_name"]) for m in temp_db.get_all_custom_maps()} == {
        (1, "Belaya"), (2, "Kokan")}


def test_delete_custom_map_reports_whether_it_existed(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": []})
    assert temp_db.delete_custom_map(1, "Belaya") is True
    assert temp_db.delete_custom_map(1, "Belaya") is False


def test_get_unique_sources_hides_custom(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert temp_db.get_unique_sources() == ["main"]


def test_has_layers_for_source(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert temp_db.has_layers_for_source("custom:1") is True
    assert temp_db.has_layers_for_source("custom:2") is False


def test_delete_layers_is_scoped_to_source_and_map(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    _seed_layer(temp_db, "Kokan_TC_v1", "custom:1", "Kokan", "TerritoryControl")
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:2", "Belaya", "TerritoryControl")

    assert temp_db.delete_layers("custom:1", "Belaya") == 1
    assert temp_db.get_unique_maps(allowed_sources=["custom:1"]) == ["Kokan"]
    assert temp_db.get_unique_maps(allowed_sources=["custom:2"]) == ["Belaya"]


def test_get_faction_reference_prefers_entry_with_default_unit(temp_db):
    _seed_layer(temp_db, "X_AAS_v1", "main", "X", "AAS", factions=[
        {"factionId": "USA", "factionName": "", "defaultUnit": "",
         "alliance": "", "availableOnTeams": [1, 2], "unitTypes": []}])
    _seed_layer(temp_db, "Y_AAS_v1", "main", "Y", "AAS", factions=[
        {"factionId": "USA", "factionName": "United States Army",
         "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR",
         "availableOnTeams": [1, 2],
         "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"}]}])

    ref = temp_db.get_faction_reference(["main"])
    assert ref["USA"]["defaultUnit"] == "USA_LO_CombinedArms"
    assert ref["USA"]["alliance"] == "BLUFOR"
    assert ref["USA"]["factionName"] == "United States Army"


def test_get_gamemode_samples_one_per_mode(temp_db):
    _seed_layer(temp_db, "Anvil_TC_v1", "main", "Anvil", "TerritoryControl")
    _seed_layer(temp_db, "Kokan_TC_v1", "main", "Kokan", "TerritoryControl")
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")

    samples = dict(temp_db.get_gamemode_samples(["main"]))
    assert set(samples) == {"TerritoryControl", "AAS"}
    assert samples["TerritoryControl"].endswith("_TC_v1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_custom_layers.py -v`
Expected: FAIL — `AttributeError: module 'database' has no attribute 'custom_source'`, and `test_get_unique_sources_hides_custom` fails on `['custom:1', 'main'] != ['main']`.

- [ ] **Step 3: Add the constant to `bot/config.py`**

Append after the `LAYERS_JSON_SOURCES` block (around line 119):

```python
# Per-guild, admin-defined layers live in layer_cache under this source prefix
# (e.g. "custom:123456789"). Defined here so database.py and utils.py can both
# reach it without importing one another.
CUSTOM_SOURCE_PREFIX = "custom:"
```

- [ ] **Step 4: Add the table to `bot/database.py`**

Inside the `conn.executescript("""…""")` block in `init_db()`, after the
`source_units` table and before `events`:

```sql
        -- Admin-defined maps, one row per (guild, map). `payload` holds only
        -- what the organizer entered: {"layers": [rawName, ...],
        -- "factions": [...], "units": [...]}. Everything derivable is
        -- re-resolved when the rows are materialized into layer_cache, so this
        -- table survives a /refresh_layers that wipes the cache.
        CREATE TABLE IF NOT EXISTS custom_layers (
            guild_id   INTEGER NOT NULL,
            map_name   TEXT    NOT NULL,
            payload    TEXT    NOT NULL DEFAULT '{}',
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, map_name)
        );
```

`CREATE TABLE IF NOT EXISTS` inside the existing script means existing
databases pick the table up on the next start — no migration step needed.

- [ ] **Step 5: Add the import and the source helper to `bot/database.py`**

After `from typing import Optional` (line 19):

```python
from config import CUSTOM_SOURCE_PREFIX
```

Then, immediately after `_source_filter` (which ends at line 287):

```python
def custom_source(guild_id: int) -> str:
    """Source name a guild's admin-defined layers are cached under."""
    return f"{CUSTOM_SOURCE_PREFIX}{guild_id}"
```

- [ ] **Step 6: Add the cache queries to `bot/database.py`**

After `get_unique_gamemodes` (ends line 456):

```python
def get_gamemode_samples(allowed_sources: Optional[list[str]] = None) -> list[tuple[str, str]]:
    """One (gamemode, raw_name) pair per distinct gamemode.

    Custom layers are typed as raw names ("Belaya_TC_v1") but the cache stores
    the verbose gamemode ("TerritoryControl"). One sample per mode is all it
    takes to derive that mapping from the data instead of hardcoding it.
    """
    where, params = _source_filter(allowed_sources, prefix=" WHERE ")
    sql = f"SELECT gamemode, MIN(raw_name) FROM layer_cache{where} GROUP BY gamemode"
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [(gm, rn) for gm, rn in rows if gm and rn]


def get_faction_reference(allowed_sources: Optional[list[str]] = None) -> dict:
    """{factionId: {factionName, alliance, defaultUnit}} from cached layers.

    Custom layers borrow these values so their factions carry the same names,
    alliances and — crucially — the same `defaultUnit` object names, which is
    what lets _resolve_unit_object_key find their vehicle loadouts. Entries
    without a defaultUnit (bare string factions) are kept only until one with a
    defaultUnit shows up.
    """
    where, params = _source_filter(allowed_sources, prefix=" WHERE ")
    sql = f"SELECT factions_json FROM layer_cache{where}"
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    ref: dict = {}
    for (fj,) in rows:
        for fac in json.loads(fj):
            if not isinstance(fac, dict):
                continue
            fac_id = fac.get("factionId", "")
            if not fac_id:
                continue
            existing = ref.get(fac_id)
            if existing and existing.get("defaultUnit"):
                continue
            ref[fac_id] = {
                "factionName": fac.get("factionName", "") or "",
                "alliance": fac.get("alliance", "") or "",
                "defaultUnit": fac.get("defaultUnit", "") or "",
            }
    return ref


def has_layers_for_source(source: str) -> bool:
    """Whether the cache holds at least one layer for this source."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM layer_cache WHERE source = ? LIMIT 1", (source,)
    ).fetchone()
    conn.close()
    return row is not None


def delete_layers(source: str, map_name: str) -> int:
    """Drop the materialized layer_cache rows of one custom map."""
    conn = _get_conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM layer_cache WHERE source = ? AND map_name = ?",
            (source, map_name),
        )
    count = cur.rowcount
    conn.close()
    return count
```

- [ ] **Step 7: Hide custom sources in `get_unique_sources`**

Replace `bot/database.py:474-486` with:

```python
def get_unique_sources() -> list[str]:
    """Return sorted list of distinct *fetched* source names in the cache.

    Per-guild custom sources are excluded: they are never offered in a source
    picker (an admin's own maps aren't a data set you opt into), and they are
    appended automatically when an event's sources are resolved.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT source FROM layer_cache "
        "WHERE source NOT LIKE ? ORDER BY source",
        (f"{CUSTOM_SOURCE_PREFIX}%",),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]
```

- [ ] **Step 8: Add the custom-map CRUD to `bot/database.py`**

After `get_unique_sources`, before the `# Events` section header:

```python
# ---------------------------------------------------------------------------
# Custom (admin-defined) maps
# ---------------------------------------------------------------------------

def upsert_custom_map(guild_id: int, map_name: str, payload: dict):
    """Store or replace one guild's custom map definition."""
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO custom_layers (guild_id, map_name, payload)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id, map_name) DO UPDATE SET
                 payload=excluded.payload""",
            (guild_id, map_name, _dumps(payload)),
        )
    conn.close()


def get_custom_maps(guild_id: int) -> list[dict]:
    """All custom maps of one guild, as {guild_id, map_name, payload}."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT map_name, payload FROM custom_layers WHERE guild_id = ? "
        "ORDER BY map_name",
        (guild_id,),
    ).fetchall()
    conn.close()
    return [{"guild_id": guild_id, "map_name": name, "payload": _loads(p)}
            for name, p in rows]


def get_all_custom_maps() -> list[dict]:
    """Every guild's custom maps — used to rebuild the cache after a refresh."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT guild_id, map_name, payload FROM custom_layers "
        "ORDER BY guild_id, map_name"
    ).fetchall()
    conn.close()
    return [{"guild_id": gid, "map_name": name, "payload": _loads(p)}
            for gid, name, p in rows]


def delete_custom_map(guild_id: int, map_name: str) -> bool:
    """Remove one custom map definition. Returns whether a row was deleted."""
    conn = _get_conn()
    with conn:
        cur = conn.execute(
            "DELETE FROM custom_layers WHERE guild_id = ? AND map_name = ?",
            (guild_id, map_name),
        )
    deleted = cur.rowcount > 0
    conn.close()
    return deleted
```

Also add `custom_layers` to the table list in the module docstring at the top
of `bot/database.py`:

```
- custom_layers: admin-defined maps, materialized into layer_cache
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `$PYTEST tests/test_custom_layers.py -v`
Expected: PASS, all 10 tests.

- [ ] **Step 10: Run the full suite**

Run: `$PYTEST`
Expected: 68 passed, 1 failed (the known `test_winner_copy_text` baseline failure).

- [ ] **Step 11: Commit**

```bash
git add bot/config.py bot/database.py tests/test_custom_layers.py
git commit -m "feat: add custom_layers table and its cache queries"
```

---

## Task 2: Parsing the pasted layer list

**Files:**
- Create: `bot/custom_layers.py`
- Test: `tests/test_custom_layers.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks (pure functions)
- Produces:
  - `custom_layers.CustomLayerError(key: str, **params)` — carries `.key` (an i18n key) and `.params`
  - `custom_layers.MAX_LAYERS_PER_MAP: int = 25`
  - `custom_layers.MAX_MAP_NAME_LENGTH: int = 50`
  - `custom_layers.split_layer_lines(text: str) -> list[str]`
  - `custom_layers.split_raw_name(raw_name: str) -> tuple[str, str, str | None]` — `(map_token, gamemode_token, layer_version)`
  - `custom_layers.parse_custom_layers(text: str) -> tuple[str, list[dict]]` — layer dicts are `{"raw_name", "gamemode_token", "layer_version"}`
  - `custom_layers.normalize_map_name(value: str, fallback: str) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_custom_layers.py`:

```python
import custom_layers as cl

BULLETED = """
- Belaya_TC_v1
- Belaya_Skirmish_v1
- Belaya_RAAS_v1
"""

BARE = """Belaya_TC_v1

Belaya_Skirmish_v1


Belaya_RAAS_v1
"""


def test_both_input_formats_produce_the_same_result():
    assert cl.parse_custom_layers(BULLETED) == cl.parse_custom_layers(BARE)


def test_parses_map_mode_and_version():
    map_name, layers = cl.parse_custom_layers("Belaya_TC_v1\nBelaya_Invasion_v2")
    assert map_name == "Belaya"
    assert layers[0] == {"raw_name": "Belaya_TC_v1",
                         "gamemode_token": "TC", "layer_version": "v1"}
    assert layers[1] == {"raw_name": "Belaya_Invasion_v2",
                         "gamemode_token": "Invasion", "layer_version": "v2"}


def test_gamemode_token_sits_in_front_of_the_version():
    # AlBasrah_AAS_v3_CL — the trailing token must not be mistaken for the mode
    assert cl.split_raw_name("AlBasrah_AAS_v3_CL") == ("AlBasrah", "AAS", "v3")


def test_layer_without_a_version():
    assert cl.split_raw_name("Belaya_RAAS") == ("Belaya", "RAAS", None)


def test_duplicate_lines_collapse():
    _, layers = cl.parse_custom_layers("Belaya_TC_v1\nBelaya_TC_v1")
    assert len(layers) == 1


def test_rejects_empty_input():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("   \n\n  ")
    assert exc.value.key == "custom_map.err_empty"


def test_rejects_invalid_lines():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("Belaya_TC_v1\nnot a layer name")
    assert exc.value.key == "custom_map.err_invalid_lines"
    assert "not a layer name" in exc.value.params["lines"]


def test_rejects_layers_from_different_maps():
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers("Belaya_TC_v1\nKokan_AAS_v1")
    assert exc.value.key == "custom_map.err_mixed_maps"
    assert "Belaya" in exc.value.params["maps"]
    assert "Kokan" in exc.value.params["maps"]


def test_map_token_comparison_ignores_case():
    map_name, layers = cl.parse_custom_layers("Belaya_TC_v1\nbelaya_AAS_v1")
    assert map_name == "Belaya"          # first spelling wins
    assert len(layers) == 2


def test_rejects_more_than_the_layer_cap():
    text = "\n".join(f"Belaya_AAS_v{i}" for i in range(1, cl.MAX_LAYERS_PER_MAP + 2))
    with pytest.raises(cl.CustomLayerError) as exc:
        cl.parse_custom_layers(text)
    assert exc.value.key == "custom_map.err_too_many"


def test_normalize_map_name_falls_back_and_truncates():
    assert cl.normalize_map_name("  Belaya Downs ", "Belaya") == "Belaya Downs"
    assert cl.normalize_map_name("", "Belaya") == "Belaya"
    assert len(cl.normalize_map_name("x" * 200, "Belaya")) == cl.MAX_MAP_NAME_LENGTH
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_custom_layers.py -v`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'custom_layers'`.

- [ ] **Step 3: Create `bot/custom_layers.py`**

```python
#!/usr/bin/env python3
"""
Admin-defined custom maps.

Custom layers are stored per guild in the `custom_layers` table and
materialized into `layer_cache` under a `custom:<guild_id>` source, so every
existing query path — map dropdown, mode dropdown, faction/unit pickers,
vehicle info, voting, history — treats them like any other layer.

The stored payload keeps only what the organizer actually typed or picked.
Gamemode, version and faction metadata are re-derived from the live cache on
each materialization, which is why /refresh_layers can keep wiping layer_cache
wholesale.

This module deliberately holds no Discord code: bot.py owns the views, this
owns the rules.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger("layer_vote.custom")

# One map's layers all land in the same mode dropdown, which is a Discord
# select — 25 options is the hard ceiling.
MAX_LAYERS_PER_MAP = 25
MAX_MAP_NAME_LENGTH = 50

_LAYER_RE = re.compile(r"^[A-Za-z0-9]+(?:_[A-Za-z0-9.]+)+$")
_VERSION_RE = re.compile(r"^v\d+$")
_BULLET_RE = re.compile(r"^[-*•]\s+")


class CustomLayerError(Exception):
    """Input rejected. `key` is an i18n key, `params` its format arguments."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


def split_layer_lines(text: str) -> list[str]:
    """Paste box → deduped raw names, order preserved.

    Accepts the bulleted and the bare form equally; any number of blank lines
    and any surrounding whitespace is ignored.
    """
    seen: dict[str, None] = {}
    for line in (text or "").splitlines():
        name = _BULLET_RE.sub("", line.strip()).strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def split_raw_name(raw_name: str) -> tuple[str, str, Optional[str]]:
    """Split "Belaya_TC_v1" into ("Belaya", "TC", "v1").

    The gamemode token is the one directly in front of the version — the same
    rule utils._squadcalc_mode_token uses, read in reverse, so a trailing
    suffix like AlBasrah_AAS_v3_CL doesn't get mistaken for the mode. Without a
    version the second token is taken.
    """
    parts = raw_name.split("_")
    version = None
    mode_idx = 1
    for i, part in enumerate(parts[1:], start=1):
        if _VERSION_RE.match(part):
            version = part
            mode_idx = i - 1
            break
    mode_token = parts[mode_idx] if 0 < mode_idx < len(parts) else ""
    return parts[0], mode_token, version


def parse_custom_layers(text: str) -> tuple[str, list[dict]]:
    """Parse the paste box into a map token and its layers.

    Returns (map_token, [{"raw_name", "gamemode_token", "layer_version"}]).
    Raises CustomLayerError carrying an i18n key for every rejection.
    """
    names = split_layer_lines(text)
    if not names:
        raise CustomLayerError("custom_map.err_empty")

    invalid = [n for n in names if not _LAYER_RE.match(n)]
    if invalid:
        raise CustomLayerError("custom_map.err_invalid_lines",
                               lines=", ".join(invalid[:10]))

    if len(names) > MAX_LAYERS_PER_MAP:
        raise CustomLayerError("custom_map.err_too_many",
                               max=MAX_LAYERS_PER_MAP, count=len(names))

    layers = []
    tokens: dict[str, str] = {}  # lowercased token → first spelling seen
    for name in names:
        map_token, mode_token, version = split_raw_name(name)
        tokens.setdefault(map_token.lower(), map_token)
        layers.append({"raw_name": name, "gamemode_token": mode_token,
                       "layer_version": version})

    if len(tokens) > 1:
        raise CustomLayerError("custom_map.err_mixed_maps",
                               maps=", ".join(sorted(tokens.values())))

    return next(iter(tokens.values())), layers


def normalize_map_name(value: str, fallback: str) -> str:
    """Display name for the map: the admin's input, else the parsed token."""
    name = (value or "").strip()[:MAX_MAP_NAME_LENGTH]
    return name or fallback
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/test_custom_layers.py -v`
Expected: PASS, 21 tests.

- [ ] **Step 5: Commit**

```bash
git add bot/custom_layers.py tests/test_custom_layers.py
git commit -m "feat: parse pasted custom layer lists"
```

---

## Task 3: Reference source and the gamemode token map

**Files:**
- Modify: `bot/custom_layers.py`
- Test: `tests/test_custom_layers.py` (append)

**Interfaces:**
- Consumes: `database.get_unique_sources`, `database.get_gamemode_samples` (Task 1); `custom_layers.split_raw_name` (Task 2); `config.LAYERS_JSON_SOURCES`; `utils.SQUADCALC_COMPATIBLE_SOURCE`
- Produces:
  - `custom_layers.resolve_reference_source() -> str | None`
  - `custom_layers.build_gamemode_token_map(source: str) -> dict[str, str]`
  - `custom_layers.inactive_gamemodes(layers: list[dict], allowed_gamemodes: list[str], token_map: dict[str, str]) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_custom_layers.py`:

```python
def test_resolve_reference_source_prefers_main(temp_db):
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")
    _seed_layer(temp_db, "Sanxian_AAS_v1", "supermod", "Sanxian", "AAS")
    assert cl.resolve_reference_source() == "main"


def test_resolve_reference_source_falls_back_to_any_cached_source(temp_db):
    _seed_layer(temp_db, "Sanxian_AAS_v1", "supermod", "Sanxian", "AAS")
    assert cl.resolve_reference_source() == "supermod"


def test_resolve_reference_source_ignores_custom_sources(temp_db):
    _seed_layer(temp_db, "Belaya_TC_v1", "custom:1", "Belaya", "TerritoryControl")
    assert cl.resolve_reference_source() is None


def test_gamemode_token_map_is_derived_from_the_cache(temp_db):
    _seed_layer(temp_db, "Anvil_TC_v1", "main", "Anvil", "TerritoryControl")
    _seed_layer(temp_db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS")

    token_map = cl.build_gamemode_token_map("main")
    assert token_map["TC"] == "TerritoryControl"
    assert token_map["AAS"] == "AAS"


def test_inactive_gamemodes_lists_modes_the_guild_switched_off():
    token_map = {"TC": "TerritoryControl", "AAS": "AAS"}
    layers = [
        {"raw_name": "Belaya_TC_v1", "gamemode_token": "TC", "layer_version": "v1"},
        {"raw_name": "Belaya_AAS_v1", "gamemode_token": "AAS", "layer_version": "v1"},
        {"raw_name": "Belaya_Skirmish_v1", "gamemode_token": "Skirmish",
         "layer_version": "v1"},
    ]
    assert cl.inactive_gamemodes(layers, ["AAS", "RAAS"], token_map) == [
        "TerritoryControl", "Skirmish"]


def test_inactive_gamemodes_empty_allowlist_means_no_warning():
    assert cl.inactive_gamemodes(
        [{"raw_name": "x_AAS_v1", "gamemode_token": "AAS", "layer_version": "v1"}],
        [], {}) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_custom_layers.py -k "reference or gamemode" -v`
Expected: FAIL — `AttributeError: module 'custom_layers' has no attribute 'resolve_reference_source'`.

- [ ] **Step 3: Implement**

Add the imports at the top of `bot/custom_layers.py`, after `from typing import Optional`:

```python
import database as db
from config import LAYERS_JSON_SOURCES
from utils import SQUADCALC_COMPATIBLE_SOURCE
```

Append to `bot/custom_layers.py`:

```python
def resolve_reference_source() -> Optional[str]:
    """The cached source whose faction and unit data custom layers borrow.

    Prefers the SquadCalc-compatible main-game source, then the first
    configured source that is actually cached, then whatever is there. Returns
    None when no fetched layers are cached at all — custom layers cannot be
    materialized in that state, since there is no faction metadata to copy.
    """
    cached = db.get_unique_sources()  # already excludes custom:* sources
    if SQUADCALC_COMPATIBLE_SOURCE in cached:
        return SQUADCALC_COMPATIBLE_SOURCE
    for name, _url in LAYERS_JSON_SOURCES:
        if name in cached:
            return name
    return cached[0] if cached else None


def build_gamemode_token_map(source: str) -> dict[str, str]:
    """{raw-name token: canonical gamemode}, derived from the cache.

    layer_cache stores "TerritoryControl" while raw names say "TC", so a typed
    token has to be translated before it can be matched against
    allowed_gamemodes. Deriving the map from the data keeps this correct when
    upstream renames a mode, and needs no hardcoded table.
    """
    mapping: dict[str, str] = {}
    for gamemode, raw_name in db.get_gamemode_samples([source]):
        _, token, _ = split_raw_name(raw_name)
        if token:
            mapping.setdefault(token, gamemode)
    return mapping


def inactive_gamemodes(layers: list[dict], allowed_gamemodes: list[str],
                       token_map: dict[str, str]) -> list[str]:
    """Canonical gamemodes among these layers that the guild has switched off.

    Custom layers pass through the normal gamemode filter, so a mode the guild
    doesn't allow silently never reaches a dropdown. The save confirmation says
    so out loud instead.
    """
    allowed = set(allowed_gamemodes or [])
    if not allowed:
        return []
    out: list[str] = []
    for layer in layers:
        token = layer.get("gamemode_token", "")
        mode = token_map.get(token, token)
        if mode and mode not in allowed and mode not in out:
            out.append(mode)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/test_custom_layers.py -v`
Expected: PASS, 27 tests.

- [ ] **Step 5: Commit**

```bash
git add bot/custom_layers.py tests/test_custom_layers.py
git commit -m "feat: derive reference source and gamemode token map from the cache"
```

---

## Task 4: Faction construction, materialization, save and delete

**Files:**
- Modify: `bot/custom_layers.py`
- Test: `tests/test_custom_layers.py` (append)

**Interfaces:**
- Consumes: `database.get_custom_maps`, `get_all_custom_maps`, `upsert_custom_map`, `delete_custom_map`, `delete_layers`, `custom_source`, `get_faction_reference`, `upsert_layer`, `get_unique_unit_types` (Tasks 1 + existing); `resolve_reference_source`, `build_gamemode_token_map`, `split_raw_name` (Tasks 2-3)
- Produces:
  - `custom_layers.build_custom_factions(faction_ids: list[str], unit_types: list[str], reference: dict, all_units: list[str]) -> list[dict]`
  - `custom_layers.materialize_custom_layers(guild_id: int | None = None) -> int`
  - `custom_layers.save_custom_map(guild_id: int, map_name: str, raw_names: list[str], faction_ids: list[str], unit_types: list[str]) -> int`
  - `custom_layers.remove_custom_map(guild_id: int, map_name: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_custom_layers.py`:

```python
REFERENCE = {
    "USA": {"factionName": "United States Army",
            "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR"},
    "RGF": {"factionName": "Russian Ground Forces",
            "defaultUnit": "RGF_LO_CombinedArms", "alliance": "REDFOR"},
}
ALL_UNITS = ["CombinedArms", "Mechanized", "Motorized"]


def _seed_reference_cache(db):
    """Two main-source layers carrying the faction metadata custom maps borrow."""
    factions = [
        {"factionId": "USA", "factionName": "United States Army",
         "defaultUnit": "USA_LO_CombinedArms", "alliance": "BLUFOR",
         "availableOnTeams": [1, 2],
         "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"},
                       {"type": "Mechanized", "name": "Mechanized"}]},
        {"factionId": "RGF", "factionName": "Russian Ground Forces",
         "defaultUnit": "RGF_LO_CombinedArms", "alliance": "REDFOR",
         "availableOnTeams": [1, 2],
         "unitTypes": [{"type": "CombinedArms", "name": "CombinedArms"},
                       {"type": "Motorized", "name": "Motorized"}]},
    ]
    _seed_layer(db, "AlBasrah_AAS_v1", "main", "Al Basrah", "AAS", factions=factions)
    _seed_layer(db, "Anvil_TC_v1", "main", "Anvil", "TerritoryControl",
                factions=factions)


def test_build_custom_factions_empty_selection_means_everything():
    out = cl.build_custom_factions([], [], REFERENCE, ALL_UNITS)
    assert [f["factionId"] for f in out] == ["RGF", "USA"]      # sorted
    assert [u["type"] for u in out[0]["unitTypes"]] == ALL_UNITS


def test_build_custom_factions_is_a_cross_product():
    out = cl.build_custom_factions(["USA"], ["Mechanized"], REFERENCE, ALL_UNITS)
    assert len(out) == 1
    assert out[0]["unitTypes"] == [{"type": "Mechanized", "name": "Mechanized"}]


def test_build_custom_factions_borrows_metadata_and_spans_both_teams():
    out = cl.build_custom_factions(["USA"], [], REFERENCE, ALL_UNITS)
    assert out[0]["defaultUnit"] == "USA_LO_CombinedArms"
    assert out[0]["alliance"] == "BLUFOR"
    assert out[0]["factionName"] == "United States Army"
    assert out[0]["availableOnTeams"] == [1, 2]


def test_build_custom_factions_tolerates_an_unknown_faction():
    out = cl.build_custom_factions(["MADEUP"], ["CombinedArms"], REFERENCE, ALL_UNITS)
    assert out[0]["factionId"] == "MADEUP"
    assert out[0]["defaultUnit"] == ""


def test_save_materializes_into_the_cache(temp_db):
    _seed_reference_cache(temp_db)
    written = cl.save_custom_map(1, "Belaya",
                                 ["Belaya_TC_v1", "Belaya_AAS_v1"], ["USA"], [])
    assert written == 2

    source = temp_db.custom_source(1)
    assert temp_db.get_unique_maps(allowed_sources=[source]) == ["Belaya"]

    layer = temp_db.get_layer_by_raw_name("Belaya_TC_v1", allowed_sources=[source])
    assert layer["gamemode"] == "TerritoryControl"     # token translated
    assert layer["layer_version"] == "v1"
    assert [f["factionId"] for f in layer["factions"]] == ["USA"]
    assert layer["team1_allowed_alliances"] == []


def test_materialization_survives_a_cache_wipe(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])

    temp_db.clear_layer_cache()
    _seed_reference_cache(temp_db)                     # what a refresh restores
    assert cl.materialize_custom_layers() == 1

    source = temp_db.custom_source(1)
    modes = temp_db.get_modes_for_map("Belaya", allowed_sources=[source])
    assert [m["display"] for m in modes] == ["TerritoryControl v1"]


def test_materialization_without_a_reference_source_is_a_no_op(temp_db):
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v1"],
                                            "factions": [], "units": []})
    assert cl.materialize_custom_layers() == 0


def test_resaving_drops_layers_that_are_gone(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1", "Belaya_AAS_v1"], [], [])
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])

    source = temp_db.custom_source(1)
    assert temp_db.get_layer_by_raw_name("Belaya_AAS_v1",
                                         allowed_sources=[source]) is None
    assert temp_db.get_layer_by_raw_name("Belaya_TC_v1",
                                         allowed_sources=[source]) is not None


def test_remove_clears_both_the_definition_and_the_cache(temp_db):
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])

    assert cl.remove_custom_map(1, "Belaya") is True
    assert temp_db.get_custom_maps(1) == []
    assert temp_db.get_unique_maps(allowed_sources=[temp_db.custom_source(1)]) == []
    assert cl.remove_custom_map(1, "Belaya") is False


def test_materialization_is_scoped_when_a_guild_is_given(temp_db):
    _seed_reference_cache(temp_db)
    temp_db.upsert_custom_map(1, "Belaya", {"layers": ["Belaya_TC_v1"],
                                            "factions": [], "units": []})
    temp_db.upsert_custom_map(2, "Kokan", {"layers": ["Kokan_TC_v1"],
                                           "factions": [], "units": []})
    assert cl.materialize_custom_layers(1) == 1
    assert temp_db.has_layers_for_source(temp_db.custom_source(2)) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_custom_layers.py -k "custom_factions or materializ or save or remove" -v`
Expected: FAIL — `AttributeError: module 'custom_layers' has no attribute 'build_custom_factions'`.

- [ ] **Step 3: Implement**

Append to `bot/custom_layers.py`:

```python
def build_custom_factions(faction_ids: list[str], unit_types: list[str],
                          reference: dict, all_units: list[str]) -> list[dict]:
    """Build the layer_cache `factions` structure for a custom map.

    An empty selection means "everything the reference source knows". The
    organizer picks one faction list and one unit list for the whole map — not
    one per team, not one per faction — so this is a plain cross product and
    every faction is available on both teams.

    The borrowed `defaultUnit` is what makes vehicle info work:
    bot._resolve_unit_object_key substitutes the chosen type token into that
    object name and looks the result up in the reference source's Units block.
    """
    ids = list(faction_ids) if faction_ids else sorted(reference)
    units = list(unit_types) if unit_types else list(all_units)
    unit_entries = [{"type": u, "name": u} for u in units]

    out = []
    for fac_id in ids:
        meta = reference.get(fac_id, {})
        out.append({
            "factionId": fac_id,
            "factionName": meta.get("factionName", ""),
            "defaultUnit": meta.get("defaultUnit", ""),
            "availableOnTeams": [1, 2],
            "unitTypes": list(unit_entries),
            "alliance": meta.get("alliance", ""),
        })
    return out


def materialize_custom_layers(guild_id: Optional[int] = None) -> int:
    """Expand the stored custom maps into layer_cache rows. Returns rows written.

    Idempotent — upsert_layer keys on (raw_name, source) — so it is safe to run
    after a refresh, on boot and after every save. A no-op when no fetched
    source is cached, since there would be no faction metadata to borrow.
    """
    entries = (db.get_custom_maps(guild_id) if guild_id is not None
               else db.get_all_custom_maps())
    if not entries:
        return 0

    source = resolve_reference_source()
    if source is None:
        logger.warning(
            "No fetched layer source cached — %d custom map(s) not materialized",
            len(entries))
        return 0

    reference = db.get_faction_reference([source])
    all_units = db.get_unique_unit_types([source])
    token_map = build_gamemode_token_map(source)

    written = 0
    for entry in entries:
        payload = entry.get("payload") or {}
        factions = build_custom_factions(payload.get("factions") or [],
                                         payload.get("units") or [],
                                         reference, all_units)
        for raw_name in payload.get("layers") or []:
            _, token, version = split_raw_name(raw_name)
            db.upsert_layer(
                raw_name=raw_name,
                source=db.custom_source(entry["guild_id"]),
                map_name=entry["map_name"],
                map_id="",
                gamemode=token_map.get(token, token),
                layer_version=version,
                factions=factions,
                team1_alliances=[],
                team2_alliances=[],
                map_size_km=None,
            )
            written += 1
    return written


def save_custom_map(guild_id: int, map_name: str, raw_names: list[str],
                    faction_ids: list[str], unit_types: list[str]) -> int:
    """Store one custom map and materialize it. Returns layers written.

    The cached rows are dropped first so a re-save that removes a layer doesn't
    leave the old one behind — upsert alone would never delete it.
    """
    db.upsert_custom_map(guild_id, map_name, {
        "layers": list(raw_names),
        "factions": list(faction_ids),
        "units": list(unit_types),
    })
    db.delete_layers(db.custom_source(guild_id), map_name)
    # Materialization covers every map of the guild, so its row count would
    # over-report this one. The caller wants this map's size.
    materialize_custom_layers(guild_id)
    return len(raw_names)


def remove_custom_map(guild_id: int, map_name: str) -> bool:
    """Delete a custom map and its materialized rows."""
    db.delete_layers(db.custom_source(guild_id), map_name)
    return db.delete_custom_map(guild_id, map_name)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/test_custom_layers.py -v`
Expected: PASS, 37 tests.

- [ ] **Step 5: Run the full suite**

Run: `$PYTEST`
Expected: 95 passed, 1 failed (baseline).

- [ ] **Step 6: Commit**

```bash
git add bot/custom_layers.py tests/test_custom_layers.py
git commit -m "feat: materialize custom maps into the layer cache"
```

---

## Task 5: Wire custom layers into the existing flows

**Files:**
- Modify: `bot/utils.py` — add `source_label` next to `_SUPERMOD_SOURCE` (line 534)
- Modify: `bot/bot.py:25-39` (imports), `bot/bot.py:316` (`fetch_and_cache_layers` tail), `bot/bot.py:660` (`_units_source`), `bot/bot.py:701`, `bot/bot.py:1218-1238` (`_resolve_event_sources`), `bot/bot.py:1289`, `bot/bot.py:1292`, `bot/bot.py:1381`, `bot/bot.py:1869`, `bot/bot.py:2216`, `bot/bot.py:4409`, `bot/bot.py:6382`, `bot/bot.py:6384`, `on_ready` (around line 6891)
- Test: `tests/test_custom_layers.py` (append)

**Interfaces:**
- Consumes: `custom_layers.materialize_custom_layers`, `custom_layers.resolve_reference_source` (Tasks 3-4); `database.custom_source`, `database.has_layers_for_source` (Task 1); `config.CUSTOM_SOURCE_PREFIX` (Task 1)
- Produces:
  - `utils.source_label(source: str, lang: str = "en") -> str`
  - `bot._resolve_event_sources(event: dict, settings: dict, guild_id: int = 0) -> list[str]`
  - `bot._units_source(source: str) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_custom_layers.py`:

```python
import utils


def test_source_label_hides_the_internal_custom_name():
    assert utils.source_label("main") == "main"
    assert utils.source_label("custom:123456789", "de") != "custom:123456789"
    assert utils.source_label("custom:123456789", "en") != "custom:123456789"


def test_event_sources_append_the_guilds_custom_source(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])

    sources = botmod._resolve_event_sources({"allowed_sources": ["main"]}, {}, 1)
    assert sources == ["main", "custom:1"]


def test_event_sources_skip_a_custom_source_with_no_layers(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    assert botmod._resolve_event_sources({"allowed_sources": ["main"]}, {}, 1) == ["main"]


def test_event_sources_without_a_guild_id_are_unchanged(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    cl.save_custom_map(1, "Belaya", ["Belaya_TC_v1"], [], [])
    assert botmod._resolve_event_sources({"allowed_sources": ["main"]}, {}) == ["main"]


def test_units_source_redirects_custom_to_the_reference(temp_db):
    import bot as botmod
    _seed_reference_cache(temp_db)
    assert botmod._units_source("main") == "main"
    assert botmod._units_source("custom:1") == "main"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_custom_layers.py -k "source_label or event_sources or units_source" -v`
Expected: FAIL — `AttributeError: module 'utils' has no attribute 'source_label'`, and `_resolve_event_sources() takes 2 positional arguments but 3 were given`.

- [ ] **Step 3: Add `source_label` to `bot/utils.py`**

Change the config import on line 17 to:

```python
from config import ADMIN_IDS, LAYERS_JSON_SOURCES, SQUADCALC_BASE_URL, CUSTOM_SOURCE_PREFIX
```

Add after `_SUPERMOD_SOURCE = "supermod"` (line 534):

```python
def source_label(source: str, lang: str = "en") -> str:
    """User-facing name for a layer source.

    A guild's admin-defined layers are stored under `custom:<guild_id>`; that
    internal name must never reach a dropdown or an embed.
    """
    if source.startswith(CUSTOM_SOURCE_PREFIX):
        return t("source.custom", lang)
    return source
```

- [ ] **Step 4: Import the new module and helper in `bot/bot.py`**

After `import database as db` (line 24):

```python
import custom_layers
```

Add `source_label` to the `from utils import (...)` block (line 27-39), next to
`build_squadcalc_url`.

- [ ] **Step 5: Teach `_resolve_event_sources` about the custom source**

Replace `bot/bot.py:1218-1238` with:

```python
def _resolve_event_sources(event: dict, settings: dict, guild_id: int = 0) -> list[str]:
    """Return the list of source names a user may pick from for this event.

    The event's stored `allowed_sources` (chosen by the admin at creation time)
    is the starting point. The guild's `allowed_sources` setting is then
    applied as a live cap — so changes to /config_layer_sources take effect
    immediately for already-active events, instead of being frozen at the
    moment the event was created.

    Falls back to all distinct sources currently in the cache when the event
    has no explicit selection (legacy events that predate this feature).

    The guild's own custom source is appended last, whenever it actually holds
    layers. Custom maps are never offered in a source picker, so an event that
    stored an explicit selection would otherwise never see a map the organizer
    added afterwards. The `has_layers_for_source` gate keeps guilds without
    custom maps from gaining a pointless source-picker step.
    """
    explicit = event.get("allowed_sources") or []
    candidate = list(explicit) if explicit else db.get_unique_sources()

    guild_allowed = settings.get("allowed_sources") or []
    if guild_allowed:
        candidate = [s for s in candidate if s in guild_allowed]

    if guild_id:
        custom = db.custom_source(guild_id)
        if custom not in candidate and db.has_layers_for_source(custom):
            candidate.append(custom)

    return candidate
```

- [ ] **Step 6: Pass the guild id at all three call sites**

`bot/bot.py:1289` — inside `handle_suggest_start`:

```python
    sources = _resolve_event_sources(event, settings, interaction.guild_id)
```

`bot/bot.py:4409` — inside `EventEditTarget.scope_sources`:

```python
        return _resolve_event_sources(obj, settings, guild_id)
```

`bot/bot.py:6382` — inside the `/history_add` command:

```python
    sources = _resolve_event_sources({}, settings, interaction.guild_id)
```

- [ ] **Step 7: Use the display label wherever a source reaches a user**

`bot/bot.py:1292`:

```python
        options = [discord.SelectOption(label=source_label(s, lang)[:100], value=s)
                   for s in sources[:25]]
```

`bot/bot.py:1381`:

```python
        desc = f"**{t('suggest.source_label', lang)}:** {source_label(state.source, lang)}\n{desc}"
```

`bot/bot.py:6384`:

```python
        options = [discord.SelectOption(label=source_label(s, lang)[:100], value=s)
                   for s in sources[:25]]
```

- [ ] **Step 8: Route the vehicle-loadout lookup for custom sources**

Add just above `get_team_vehicles` (`bot/bot.py:660`):

```python
def _units_source(source: str) -> str:
    """Which source's Units block describes a layer's vehicle loadouts.

    Custom layers have no Units block of their own — they borrow the reference
    source's, which is exactly where their factions' `defaultUnit` object names
    were copied from, so _resolve_unit_object_key finds the same entries.
    """
    if source.startswith(CUSTOM_SOURCE_PREFIX):
        return custom_layers.resolve_reference_source() or source
    return source
```

Add `CUSTOM_SOURCE_PREFIX` to the `from config import ...` line (`bot/bot.py:25`).

Then wrap the three lookups:

- `bot/bot.py:701` → `units_map = db.get_source_units(_units_source(source))`
- `bot/bot.py:1869` → `units_map = db.get_source_units(_units_source(state.source or ""))`
- `bot/bot.py:2216` → `units_map = db.get_source_units(_units_source(source))`

- [ ] **Step 9: Materialize after a refresh and on boot**

At the end of `fetch_and_cache_layers()`, just before its `return count`
(`bot/bot.py:316`), add:

```python
    # Custom maps are stored separately and were wiped along with the cache —
    # put them back so a refresh never costs an admin their hand-entered maps.
    restored = custom_layers.materialize_custom_layers()
    if restored:
        logger.info("Re-materialized %d custom layer(s) after refresh", restored)
```

In `on_ready`, right after the "Auto-fetch layers if cache is empty" block
(after line 6891), add:

```python
    # Covers the normal boot where the cache is already populated and no fetch
    # ran; a harmless repeat when one did, since materialization is idempotent.
    try:
        custom_layers.materialize_custom_layers()
    except Exception as e:
        logger.error(f"Failed to materialize custom layers on startup: {e}")
```

- [ ] **Step 10: Run the tests to verify they pass**

Run: `$PYTEST tests/test_custom_layers.py -v`
Expected: PASS, 42 tests.

- [ ] **Step 11: Run the full suite**

Run: `$PYTEST`
Expected: 100 passed, 1 failed (baseline). `tests/test_imports.py` in
particular must still pass — it catches a broken import in `bot.py`.

- [ ] **Step 12: Commit**

```bash
git add bot/bot.py bot/utils.py tests/test_custom_layers.py
git commit -m "feat: surface custom layers in suggestions, vehicles and refresh"
```

---

## Task 6: Translations

**Files:**
- Modify: `bot/i18n.py` — insert after the `admin.manage_suggestions` entry (around line 921)

**Interfaces:**
- Consumes: nothing
- Produces: the 26 i18n keys used by Tasks 5, 7, 8 and 9

- [ ] **Step 1: Add the keys**

Insert into the `_STRINGS` dict in `bot/i18n.py`, right after the
`"admin.manage_suggestions"` entry:

```python
    "admin.custom_maps": {
        "de": "Eigene Maps",
        "en": "Custom Maps",
    },
    "source.custom": {
        "de": "Eigene Maps",
        "en": "Custom Maps",
    },
    "custom_map.panel_title": {
        "de": "Eigene Maps",
        "en": "Custom Maps",
    },
    "custom_map.layer_count": {
        "de": "{count} Layer",
        "en": "{count} layers",
    },
    "custom_map.none": {
        "de": "Noch keine eigenen Maps angelegt.",
        "en": "No custom maps yet.",
    },
    "custom_map.add": {
        "de": "Map hinzufügen",
        "en": "Add Map",
    },
    "custom_map.delete_placeholder": {
        "de": "Eigene Map löschen …",
        "en": "Delete custom map…",
    },
    "custom_map.modal_title": {
        "de": "Eigene Map hinzufügen",
        "en": "Add Custom Map",
    },
    "custom_map.field_layers": {
        "de": "Layer — einer pro Zeile",
        "en": "Layers — one per line",
    },
    "custom_map.field_layers_hint": {
        "de": "Belaya_RAAS_v1\nBelaya_AAS_v1",
        "en": "Belaya_RAAS_v1\nBelaya_AAS_v1",
    },
    "custom_map.field_display_name": {
        "de": "Anzeigename (optional)",
        "en": "Display name (optional)",
    },
    "custom_map.details_title": {
        "de": "Fraktionen & Unit-Typen",
        "en": "Factions & Unit Types",
    },
    "custom_map.details_desc": {
        "de": "**{map}** — {count} Layer.\nNichts auswählen = alles, was das Hauptspiel kennt.",
        "en": "**{map}** — {count} layers.\nSelect nothing = everything the main game knows.",
    },
    "custom_map.factions_placeholder": {
        "de": "Fraktionen (leer = alle)",
        "en": "Factions (empty = all)",
    },
    "custom_map.units_placeholder": {
        "de": "Unit-Typen (leer = alle)",
        "en": "Unit types (empty = all)",
    },
    "custom_map.save": {
        "de": "Speichern",
        "en": "Save",
    },
    "custom_map.saved": {
        "de": "✅ **{map}** gespeichert — {count} Layer.",
        "en": "✅ **{map}** saved — {count} layers.",
    },
    "custom_map.replaced": {
        "de": "✅ **{map}** ersetzt — {count} Layer.",
        "en": "✅ **{map}** replaced — {count} layers.",
    },
    "custom_map.deleted": {
        "de": "🗑️ **{map}** gelöscht.",
        "en": "🗑️ **{map}** deleted.",
    },
    "custom_map.gamemode_warning": {
        "de": "⚠️ Diese Modes sind in den Guild-Defaults nicht aktiv und erscheinen daher nicht in Vorschlägen: {modes}",
        "en": "⚠️ These modes are inactive in the guild defaults, so they will not appear in suggestions: {modes}",
    },
    "custom_map.truncated": {
        "de": "Nur die ersten 25 Einträge werden angeboten (Discord-Limit).",
        "en": "Only the first 25 entries are offered (Discord limit).",
    },
    "custom_map.no_reference_data": {
        "de": "Der Layer-Cache ist leer — bitte zuerst `/refresh_layers` ausführen.",
        "en": "The layer cache is empty — run `/refresh_layers` first.",
    },
    "custom_map.err_empty": {
        "de": "Keine gültigen Layer erkannt.",
        "en": "No valid layers found.",
    },
    "custom_map.err_invalid_lines": {
        "de": "Ungültige Zeilen: {lines}",
        "en": "Invalid lines: {lines}",
    },
    "custom_map.err_mixed_maps": {
        "de": "Alle Layer müssen zur selben Karte gehören. Gefunden: {maps}",
        "en": "All layers must belong to the same map. Found: {maps}",
    },
    "custom_map.err_too_many": {
        "de": "Maximal {max} Layer pro Map ({count} eingegeben).",
        "en": "At most {max} layers per map ({count} given).",
    },
```

- [ ] **Step 2: Verify every key resolves in both languages**

Run:

```bash
docker run --rm -e DISCORD_BOT_TOKEN=test -v "$PWD":/app -w /app python:3.12-slim \
  sh -c "pip install -q -r requirements.txt && python -c \"
import sys; sys.path.insert(0, 'bot')
from i18n import _STRINGS, t
keys = [k for k in _STRINGS if k.startswith('custom_map.') or k in ('admin.custom_maps', 'source.custom')]
assert len(keys) == 26, len(keys)
for k in keys:
    for lang in ('de', 'en'):
        assert _STRINGS[k].get(lang), (k, lang)
print('ok', len(keys))
\""
```

Expected: `ok 26`

- [ ] **Step 3: Run the full suite**

Run: `$PYTEST`
Expected: 100 passed, 1 failed (baseline) — unchanged.

- [ ] **Step 4: Commit**

```bash
git add bot/i18n.py
git commit -m "feat: add custom map translations (de/en)"
```

---

## Task 7: Admin button and the Custom Maps sub-panel

**Files:**
- Modify: `bot/bot.py:2285-2329` (`AdminPanelView`), `bot/bot.py:2396-2429` (`AdminButton.callback`), and a new block before `class AdminButton`
- Test: `tests/test_custom_maps_ui.py` (create)

**Interfaces:**
- Consumes: `AdminButton`, `AutoDisableView`, `_bind`, `handle_admin_panel(interaction, db_id, edit=False)` (existing); `custom_layers.remove_custom_map` (Task 4); the i18n keys (Task 6)
- Produces:
  - `bot.CustomMapsView(lang: str, db_id: int, maps: list[dict])`
  - `bot.admin_custom_maps(interaction, db_id: int)` — coroutine
  - `bot._render_custom_maps(interaction, db_id: int, lang: str, notice: str = "")` — coroutine, edits the current message

- [ ] **Step 1: Write the failing tests**

Create `tests/test_custom_maps_ui.py`:

```python
"""Composition of the Custom Maps admin views."""

import discord

import bot as botmod


def _actions(view):
    return [c.action for c in view.children if hasattr(c, "action")]


def _selects(view):
    return [c for c in view.children if isinstance(c, discord.ui.Select)]


def _buttons(view):
    return [c for c in view.children if isinstance(c, discord.ui.Button)]


def test_admin_panel_offers_custom_maps_in_every_phase():
    for phase in ("created", "suggestions_open", "suggestions_closed",
                  "voting", "completed"):
        view = botmod.AdminPanelView(phase, "de", 1)
        assert "custom_maps" in _actions(view), phase


def test_custom_maps_view_has_add_delete_and_back():
    view = botmod.CustomMapsView("de", 1, [
        {"guild_id": 1, "map_name": "Belaya",
         "payload": {"layers": ["Belaya_TC_v1"]}},
    ])
    assert len(_selects(view)) == 1        # delete picker
    assert len(_buttons(view)) == 2        # add + back
    assert view.db_id == 1


def test_custom_maps_view_without_maps_has_no_delete_picker():
    view = botmod.CustomMapsView("de", 1, [])
    assert _selects(view) == []
    assert len(_buttons(view)) == 2


def test_custom_maps_delete_picker_lists_every_map():
    view = botmod.CustomMapsView("de", 1, [
        {"guild_id": 1, "map_name": "Belaya", "payload": {"layers": []}},
        {"guild_id": 1, "map_name": "Kokan", "payload": {"layers": []}},
    ])
    assert [o.value for o in _selects(view)[0].options] == ["Belaya", "Kokan"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_custom_maps_ui.py -v`
Expected: FAIL — `AttributeError: module 'bot' has no attribute 'CustomMapsView'`, and the panel test fails because `custom_maps` is not in the actions list.

- [ ] **Step 3: Add the button to `AdminPanelView`**

In `bot/bot.py`, immediately before the `delete_event` button at the end of
`AdminPanelView.__init__` (line 2327):

```python
        # Guild-scoped, not event-scoped: an organizer registers a map once and
        # every event in the guild can suggest it. It hangs off the event panel
        # because that's the only admin surface the bot has.
        self.add_item(AdminButton("custom_maps", t("admin.custom_maps", lang),
                                  discord.ButtonStyle.secondary, "🗺️"))
```

- [ ] **Step 4: Route the action**

In `AdminButton.callback`, add before the `delete_event` branch (line 2421):

```python
        elif self.action == "custom_maps":
            await admin_custom_maps(interaction, db_id)
```

`custom_maps` stays out of the `("open_suggestions", "reopen_suggestions",
"copy_result")` exemption tuple, so the panel view is retired by the existing
`self.view.stop()` when the sub-panel replaces the message.

- [ ] **Step 5: Add the sub-panel**

Insert into `bot/bot.py` immediately after `admin_manage_suggestions` and
before `class AdminButton` (line 2388):

```python
class CustomMapsView(AutoDisableView):
    """Sub-panel of the Admin panel for admin-defined maps.

    Reuses `AdminButton` for the entry point, which reads `self.view.db_id`, so
    this view exposes the same attribute the parent panel does. Deleting is
    immediate — re-adding the map is the undo, and the definition is four lines
    of text.
    """

    def __init__(self, lang: str, db_id: int, maps: list[dict]):
        super().__init__(timeout=120)
        self.lang = lang
        self.db_id = db_id

        add = ui.Button(label=t("custom_map.add", lang),
                        style=discord.ButtonStyle.success, emoji="➕", row=0)
        add.callback = self._add
        self.add_item(add)

        back = ui.Button(label=t("button.back", lang),
                         style=discord.ButtonStyle.secondary, emoji="⬅️", row=0)
        back.callback = self._back
        self.add_item(back)

        if maps:
            options = [
                discord.SelectOption(label=m["map_name"][:100],
                                     value=m["map_name"][:100],
                                     description=t("custom_map.layer_count", lang,
                                                   count=len(m["payload"].get("layers") or []))[:100])
                for m in maps[:25]
            ]
            self.delete_select = ui.Select(
                placeholder=t("custom_map.delete_placeholder", lang),
                options=options, min_values=1, max_values=1, row=1)
            self.delete_select.callback = self._delete
            self.add_item(self.delete_select)

    async def _add(self, interaction: discord.Interaction):
        # The modal replaces nothing yet — its submit handler edits this message.
        await interaction.response.send_modal(CustomMapModal(self.lang, self.db_id))

    async def _delete(self, interaction: discord.Interaction):
        map_name = self.delete_select.values[0]
        custom_layers.remove_custom_map(interaction.guild_id, map_name)
        self.stop()  # retire this view; _render_custom_maps attaches a fresh one
        await _render_custom_maps(interaction, self.db_id, self.lang,
                                  notice=t("custom_map.deleted", self.lang,
                                           map=map_name))

    async def _back(self, interaction: discord.Interaction):
        self.stop()  # retire this view; handle_admin_panel attaches a fresh one
        await handle_admin_panel(interaction, self.db_id, edit=True)


async def _render_custom_maps(interaction: discord.Interaction, db_id: int,
                              lang: str, notice: str = ""):
    """Draw (or redraw) the Custom Maps sub-panel over the current message."""
    maps = db.get_custom_maps(interaction.guild_id)

    if maps:
        lines = [
            "• **{}** — {}".format(
                m["map_name"],
                t("custom_map.layer_count", lang,
                  count=len(m["payload"].get("layers") or [])))
            for m in maps
        ]
        body = "\n".join(lines)
    else:
        body = t("custom_map.none", lang)

    embed = discord.Embed(
        title=t("custom_map.panel_title", lang),
        description=f"{notice}\n\n{body}" if notice else body,
        color=discord.Color.dark_red(),
    )
    view = CustomMapsView(lang, db_id, maps)
    await interaction.response.edit_message(embed=embed, view=_bind(view, interaction))


async def admin_custom_maps(interaction: discord.Interaction, db_id: int):
    """Replace the Admin panel with the custom-maps sub-panel."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"
    await _render_custom_maps(interaction, db_id, lang)
```

`CustomMapModal` does not exist yet — Task 8 adds it. Until then `_add` raises
`NameError` at click time, but nothing imports it at module load, so the tests
in this task pass. Task 8 closes the gap.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `$PYTEST tests/test_custom_maps_ui.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 7: Run the full suite**

Run: `$PYTEST`
Expected: 104 passed, 1 failed (baseline). `tests/test_admin_panel_buttons.py`
must still pass — the existing assertions there check for the *absence* of
specific actions, not for an exact button count, so the new button doesn't
break them.

- [ ] **Step 8: Commit**

```bash
git add bot/bot.py tests/test_custom_maps_ui.py
git commit -m "feat: add Custom Maps admin sub-panel"
```

---

## Task 8: The add-a-map wizard

**Files:**
- Modify: `bot/bot.py` — add `CustomMapModal` and `CustomMapDetailsView` after `CustomMapsView`
- Test: `tests/test_custom_maps_ui.py` (append)

**Interfaces:**
- Consumes: `custom_layers.parse_custom_layers`, `CustomLayerError`, `normalize_map_name`, `resolve_reference_source`, `build_gamemode_token_map`, `inactive_gamemodes`, `save_custom_map` (Tasks 2-4); `db.get_faction_reference`, `db.get_unique_unit_types` (Task 1 + existing); `_render_custom_maps` (Task 7)
- Produces:
  - `bot.CustomMapModal(lang: str, db_id: int)`
  - `bot.CustomMapDetailsView(lang: str, db_id: int, map_name: str, layers: list[dict], faction_ids: list[str], unit_types: list[str])`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_custom_maps_ui.py`:

```python
LAYERS = [
    {"raw_name": "Belaya_TC_v1", "gamemode_token": "TC", "layer_version": "v1"},
    {"raw_name": "Belaya_AAS_v1", "gamemode_token": "AAS", "layer_version": "v1"},
]


def test_details_view_has_two_optional_selects_and_save():
    view = botmod.CustomMapDetailsView("de", 1, "Belaya", LAYERS,
                                       ["USA", "RGF"], ["CombinedArms"])
    selects = _selects(view)
    assert len(selects) == 2
    assert all(s.min_values == 0 for s in selects)
    assert [o.value for o in selects[0].options] == ["USA", "RGF"]
    assert [o.value for o in selects[1].options] == ["CombinedArms"]
    assert len(_buttons(view)) == 1        # save


def test_details_view_caps_selects_at_the_discord_limit():
    many = [f"F{i}" for i in range(40)]
    view = botmod.CustomMapDetailsView("de", 1, "Belaya", LAYERS, many, many)
    for select in _selects(view):
        assert len(select.options) == 25
    assert view.truncated is True


def test_details_view_starts_with_nothing_selected():
    view = botmod.CustomMapDetailsView("de", 1, "Belaya", LAYERS,
                                       ["USA"], ["CombinedArms"])
    assert view.selected_factions == []
    assert view.selected_units == []


def test_modal_carries_two_text_inputs():
    modal = botmod.CustomMapModal("de", 1)
    assert len(modal.children) == 2
    assert modal.layers_input.required is True
    assert modal.name_input.required is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_custom_maps_ui.py -v`
Expected: FAIL — `AttributeError: module 'bot' has no attribute 'CustomMapDetailsView'`.

- [ ] **Step 3: Implement the modal and the details view**

Insert into `bot/bot.py` after `admin_custom_maps`:

```python
class CustomMapModal(ui.Modal):
    """Step 1 of adding a custom map: the raw layer names.

    Discord modals take text inputs only, so the faction and unit pickers can't
    live here — same reason EventScheduleModal hands off to a follow-up view.
    """

    def __init__(self, lang: str, db_id: int):
        super().__init__(title=t("custom_map.modal_title", lang)[:45])
        self.lang = lang
        self.db_id = db_id

        self.layers_input = ui.TextInput(
            label=t("custom_map.field_layers", lang)[:45],
            style=discord.TextStyle.paragraph,
            placeholder=t("custom_map.field_layers_hint", lang)[:100],
            required=True,
            max_length=2000,
        )
        self.add_item(self.layers_input)

        self.name_input = ui.TextInput(
            label=t("custom_map.field_display_name", lang)[:45],
            required=False,
            max_length=custom_layers.MAX_MAP_NAME_LENGTH,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            map_token, layers = custom_layers.parse_custom_layers(
                str(self.layers_input.value))
        except custom_layers.CustomLayerError as e:
            await interaction.response.send_message(
                embed=discord.Embed(description=t(e.key, self.lang, **e.params),
                                    color=discord.Color.red()),
                ephemeral=True)
            return

        # A select with zero options is rejected by Discord, so an unusable
        # reference source is refused here rather than at render time.
        source = custom_layers.resolve_reference_source()
        faction_ids = sorted(db.get_faction_reference([source])) if source else []
        unit_types = db.get_unique_unit_types([source]) if source else []
        if not faction_ids or not unit_types:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=t("custom_map.no_reference_data", self.lang),
                    color=discord.Color.red()),
                ephemeral=True)
            return

        map_name = custom_layers.normalize_map_name(
            str(self.name_input.value), map_token)

        view = CustomMapDetailsView(self.lang, self.db_id, map_name, layers,
                                    faction_ids, unit_types)
        embed = discord.Embed(
            title=t("custom_map.details_title", self.lang),
            description=t("custom_map.details_desc", self.lang,
                          map=map_name, count=len(layers)),
            color=discord.Color.dark_red(),
        )
        if view.truncated:
            embed.description += "\n" + t("custom_map.truncated", self.lang)

        # The modal was opened from a component on the sub-panel message, so
        # edit_message replaces that message with this screen.
        await interaction.response.edit_message(embed=embed,
                                                view=_bind(view, interaction))


class CustomMapDetailsView(AutoDisableView):
    """Steps 2 and 3: which factions and unit types the map supports.

    Both selects fit on one message (two of the five action rows), so the two
    steps share a screen rather than chaining two round trips. Selecting
    nothing means "everything the main game knows" — resolved at
    materialization, not frozen here.
    """

    def __init__(self, lang: str, db_id: int, map_name: str, layers: list[dict],
                 faction_ids: list[str], unit_types: list[str]):
        super().__init__(timeout=300)
        self.lang = lang
        self.db_id = db_id
        self.map_name = map_name
        self.layers = layers
        self.selected_factions: list[str] = []
        self.selected_units: list[str] = []
        self.truncated = len(faction_ids) > 25 or len(unit_types) > 25

        self.faction_select = ui.Select(
            placeholder=t("custom_map.factions_placeholder", lang),
            options=[discord.SelectOption(label=f[:100], value=f)
                     for f in faction_ids[:25]],
            min_values=0, max_values=min(len(faction_ids), 25) or 1, row=0)
        self.faction_select.callback = self._factions_changed
        self.add_item(self.faction_select)

        self.unit_select = ui.Select(
            placeholder=t("custom_map.units_placeholder", lang),
            options=[discord.SelectOption(label=u[:100], value=u)
                     for u in unit_types[:25]],
            min_values=0, max_values=min(len(unit_types), 25) or 1, row=1)
        self.unit_select.callback = self._units_changed
        self.add_item(self.unit_select)

        save = ui.Button(label=t("custom_map.save", lang),
                         style=discord.ButtonStyle.success, emoji="✅", row=2)
        save.callback = self._save
        self.add_item(save)

    async def _factions_changed(self, interaction: discord.Interaction):
        self.selected_factions = list(self.faction_select.values)
        await interaction.response.defer()

    async def _units_changed(self, interaction: discord.Interaction):
        self.selected_units = list(self.unit_select.values)
        await interaction.response.defer()

    async def _save(self, interaction: discord.Interaction):
        settings = db.get_guild_settings(interaction.guild_id) or {}
        existed = any(m["map_name"] == self.map_name
                      for m in db.get_custom_maps(interaction.guild_id))

        written = custom_layers.save_custom_map(
            interaction.guild_id, self.map_name,
            [layer["raw_name"] for layer in self.layers],
            self.selected_factions, self.selected_units)

        key = "custom_map.replaced" if existed else "custom_map.saved"
        notice = t(key, self.lang, map=self.map_name, count=written)

        source = custom_layers.resolve_reference_source()
        inactive = custom_layers.inactive_gamemodes(
            self.layers, settings.get("allowed_gamemodes") or [],
            custom_layers.build_gamemode_token_map(source) if source else {})
        if inactive:
            notice += "\n" + t("custom_map.gamemode_warning", self.lang,
                               modes=", ".join(inactive))

        self.stop()  # retire this view; _render_custom_maps attaches a fresh one
        await _render_custom_maps(interaction, self.db_id, self.lang, notice=notice)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/test_custom_maps_ui.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Run the full suite**

Run: `$PYTEST`
Expected: 108 passed, 1 failed (baseline).

- [ ] **Step 6: Manual smoke test**

The wizard is Discord-side and cannot be unit-tested end to end. In a test
guild:

1. `/refresh_layers`, then open an event's **Admin → Eigene Maps**
2. **Map hinzufügen**, paste the bulleted example from the spec, leave the name
   empty → the details screen must show `Belaya` and the layer count
3. Save with nothing selected → the panel lists `Belaya` with its layer count;
   a `Skirmish` layer must trigger the gamemode warning
4. **Suggest Layer** → the source picker now offers "Eigene Maps"; pick it →
   `Belaya` appears, its modes are `TerritoryControl v1` / `AAS v1` etc., and
   the confirm screen shows a vehicle list
5. Paste a list mixing two map names → the error names both

If the modal's `edit_message` in step 2 fails at runtime with "Interaction has
already been responded to" or leaves the panel stale, swap that one call for
`await interaction.response.send_message(embed=embed, view=..., ephemeral=True)`.
Nothing downstream depends on which message the details screen lands in.

- [ ] **Step 7: Commit**

```bash
git add bot/bot.py tests/test_custom_maps_ui.py
git commit -m "feat: add the custom map creation wizard"
```

---

## Task 9: Documentation

**Files:**
- Modify: `README.md:5-16` (features), `README.md:71` (admin panel paragraph)
- Modify: `USER_GUIDE.md:132-142` (Admin Panel list), plus a new section after it
- Modify: `ARCHITECTURE.md` — the layer-data section and the "Where to look" table

**Interfaces:**
- Consumes: everything from Tasks 1-8
- Produces: nothing consumed by other tasks

- [ ] **Step 1: Update `README.md`**

Add a feature bullet after the "Layer Data" bullet (line 8):

```markdown
- **Custom Maps**: Organizers can register a modded or unreleased map's layers by hand via **Admin → Custom Maps**; they survive `/refresh_layers` and behave like any other layer
```

In the Admin panel paragraph (line 71), extend the button list:

```markdown
**Select for Vote**, **End Vote**, **Custom Maps** (register a map's layers by hand), **Edit Event** …
```

- [ ] **Step 2: Update `USER_GUIDE.md`**

Add to the Admin Panel bullet list (after the "Select layers for voting" bullet):

```markdown
- **Custom Maps** — register a map the layer data doesn't know (see below)
```

Add a new section after "Admin Panel", before "Viewing Settings":

````markdown
### Adding a Custom Map

Running a modded or unreleased map? Register it once and it shows up in
suggestions like any other layer.

1. Open **Admin → Custom Maps → Add Map**
2. Paste the map's raw layer names, one per line. Both of these work:

   ```
   - Belaya_TC_v1
   - Belaya_RAAS_v1
   ```

   ```
   Belaya_TC_v1
   Belaya_RAAS_v1
   ```

   All layers must belong to the **same map** — the part before the first `_`.
   Add one map per run, up to 25 layers each. The optional **Display name**
   field controls how the map appears in the dropdown; leave it empty to use
   the name from the layer files.
3. Optionally narrow the **factions** and **unit types**. Selecting nothing
   means every faction and unit type the main game knows.
4. **Save**

Custom maps belong to your server, survive `/refresh_layers`, and appear as
their own **Custom Maps** entry in the suggestion flow's source picker. Vehicle
layouts are borrowed from the matching main-game factions. Saving the same map
name again replaces it; the delete dropdown on the panel removes it.

> A layer whose game mode is switched off in your defaults will not appear in
> suggestions — the save confirmation tells you which modes that affects.
````

- [ ] **Step 3: Update `ARCHITECTURE.md`**

Add `custom_layers` to the table list in the database section, and a paragraph
in the layer-data section:

```markdown
**Custom (admin-defined) maps.** `custom_layers` (guild_id, map_name, payload)
is the source of truth for maps an organizer entered by hand; the payload holds
only the raw layer names plus the chosen factions and unit types.
`custom_layers.materialize_custom_layers()` expands them into ordinary
`layer_cache` rows tagged `custom:<guild_id>`, re-deriving gamemode, version and
faction metadata from the reference source each time. It runs at the end of
`fetch_and_cache_layers()`, on boot in `on_ready`, and after every save or
delete — so `/refresh_layers` can keep wiping the cache wholesale.
`get_unique_sources()` hides `custom:%`, and `_resolve_event_sources()` appends
the guild's own custom source when it has rows, which is why custom maps are
never picked in a source dropdown yet always reachable.
```

Add to the "Where to look" table:

```markdown
| Custom (admin-defined) maps | `bot/custom_layers.py`, views in `bot/bot.py` |
```

- [ ] **Step 4: Verify the docs match the code**

Re-read each edited passage against the implementation — every button label
must match an i18n `en` string, and every function name must exist.

Run: `grep -n "custom_maps\|custom_layers" README.md USER_GUIDE.md ARCHITECTURE.md`

- [ ] **Step 5: Run the full suite one last time**

Run: `$PYTEST`
Expected: 108 passed, 1 failed (baseline).

- [ ] **Step 6: Commit**

```bash
git add README.md USER_GUIDE.md ARCHITECTURE.md
git commit -m "docs: document admin-defined custom maps"
```

---

## Done criteria

- `Admin → Custom Maps` registers a map from pasted raw names, with optional
  faction and unit narrowing, and lists and deletes what exists
- Custom layers appear in the suggestion flow under a localized "Custom Maps"
  source, carry vehicle info, and can win a vote and land in history
- `/refresh_layers` does not lose them
- A guild with no custom maps sees no behaviour change anywhere
- `$PYTEST` reports 108 passed, 1 failed (the pre-existing
  `test_winner_copy_text` failure)
