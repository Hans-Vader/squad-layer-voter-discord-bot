# Custom Maps / Layers — Design

Date: 2026-08-31
Status: approved, ready for implementation planning

## Problem

Layers come exclusively from the `layers.json` URLs in `LAYERS_JSON_URL`. A
community running a modded or unreleased map has no way to put it on the
ballot. Organizers need to register a map's layers by hand, from Discord,
without a redeploy.

## Scope

An organizer opens **Admin → Custom Maps**, pastes the raw layer names of one
map, optionally narrows the factions and unit types, and saves. The layers then
behave like any other layer in the suggestion flow: map dropdown, mode
dropdown, faction and unit pickers, vehicle info, voting, history.

One map per run. Maps are per guild. They survive `/refresh_layers`. They can
be deleted; re-adding the same map overwrites it.

Out of scope: an edit flow (re-adding overwrites), per-team faction lists,
per-faction unit lists, map size, minimap images, SquadCalc deep links.

## Decisions

| Question | Decision |
|----------|----------|
| Visibility | Per guild. Source name `custom:<guild_id>`. |
| Source filter | The guild's custom source is appended when resolving an event's sources (whenever it has rows) — a newly added map shows up in a running event immediately. It is never offered in a source picker. |
| Management | Add + delete. Re-adding overwrites. No edit flow. |
| Gamemode filter | Applies normally. The save confirmation warns which of the entered modes are currently inactive in the guild defaults. |
| Storage | Own table `custom_layers` as the source of truth, materialized into `layer_cache` after every refresh and after every mutation. |

## Data model

```sql
CREATE TABLE IF NOT EXISTS custom_layers (
    guild_id   INTEGER NOT NULL,
    map_name   TEXT    NOT NULL,   -- display name, e.g. "Belaya"
    payload    TEXT    NOT NULL,   -- JSON, see below
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, map_name)
);
```

`payload`:

```json
{
  "layers": ["Belaya_TC_v1", "Belaya_AAS_v3"],
  "factions": ["USA", "RGF"],
  "units": ["CombinedArms", "Mechanized"]
}
```

The payload stores only what the admin actually chose. Everything derivable —
gamemode, layer version, faction metadata, and the meaning of an empty
`factions` / `units` list — is resolved at materialization time against the
current cache, never frozen at save time. A later `/refresh_layers` that adds a
faction to the game, or renames a mode, is picked up on the next
materialization without touching the stored payload.

New functions in `bot/database.py`:

- `upsert_custom_map(guild_id, map_name, payload) -> None`
- `get_custom_maps(guild_id) -> list[dict]` — `{map_name, payload, created_at}`
- `get_all_custom_maps() -> list[dict]` — adds `guild_id`, used on boot
- `delete_custom_map(guild_id, map_name) -> bool`
- `delete_layers(source, map_name) -> int` — drops the materialized rows
- `get_faction_reference(allowed_sources) -> dict` — `{factionId: {factionName, alliance, defaultUnit}}`, scanned out of `factions_json`; the first entry carrying a non-empty `defaultUnit` wins, since bare string faction entries have none
- `get_gamemode_samples(allowed_sources) -> list[tuple[str, str]]` — one `(gamemode, raw_name)` pair per distinct gamemode, the input for the token map
- `has_layers_for_source(source) -> bool`

`custom_source(guild_id) -> f"custom:{guild_id}"` lives in `bot/database.py`
next to the other source helpers, so both `bot.py` and the DB layer can use it.

## Materialization

`materialize_custom_layers(guild_id: int | None = None) -> int` in `bot/custom_layers.py`:

1. Read `get_custom_maps(guild_id)` (or `get_all_custom_maps()` when `None`).
2. Resolve the reference source (below), its `get_faction_reference`, and the
   token→gamemode map.
3. Build `factions_data` per `_build_custom_factions`.
4. For each raw name, derive gamemode and version, then
   `db.upsert_layer(raw_name=…, source=custom_source(gid), map_name=…,
   map_id="", gamemode=…, layer_version=…, factions=factions_data,
   team1_alliances=[], team2_alliances=[], map_size_km=None)`.

`upsert_layer` is an upsert on `(raw_name, source)`, so the whole function is
idempotent and safe to call more than once.

Call sites:

- end of `fetch_and_cache_layers()`, after the per-source loop — `/refresh_layers`
  keeps wiping `layer_cache` wholesale, and the custom rows come straight back
- `on_ready`, immediately after the "auto-fetch if cache is empty" block
  (`bot.py:6885-6891`), unconditionally — covers the normal boot where the
  cache is already populated and no fetch runs, and is a harmless repeat when
  one did
- after every save: `materialize_custom_layers(guild_id)`
- delete removes the `custom_layers` row **and** calls
  `db.delete_layers(custom_source(gid), map_name)`

`map_id` is empty and `map_size_km` is `NULL`. Both are optional downstream:
`is_excluded_layer` only rejects known bad ids/names, and `get_map_sizes()`
skips rows with a NULL size.

### Reference source

Custom layers borrow faction metadata from the main game's cached data. The
reference source is `utils.SQUADCALC_COMPATIBLE_SOURCE` (`"main"`) when that
name is present in `db.get_unique_sources()`, otherwise the first entry of
`config.LAYERS_JSON_SOURCES`. When the cache is empty there is nothing to
borrow: saving is refused with `custom_map.no_reference_data`, pointing the
admin at `/refresh_layers`.

## Parsing

`parse_custom_layers(text) -> (map_token, layers)` in `bot/custom_layers.py`, pure
and directly testable.

1. Split on newlines; strip whitespace and a leading `- `, `* ` or `• `; drop
   empty lines; dedupe preserving order.
2. Every remaining line must match `^[A-Za-z0-9]+(?:_[A-Za-z0-9.]+)+$`.
   Offending lines are collected and reported together.
3. `map_token` is the part before the first `_`. All lines must agree
   case-insensitively; the first spelling wins. A mismatch lists the distinct
   tokens found.
4. `layer_version` comes from `_v(\d+)` → `v<N>`, else `None`.
5. The gamemode token is the token immediately before the version, else the
   second token — mirroring `utils._squadcalc_mode_token` in reverse.
6. Empty input, or more than 25 layers, is an error (Discord select limit).

It returns `(map_token, [{"raw_name", "gamemode_token", "layer_version"}])`.
Only `raw_name` is persisted; the derived fields feed validation and the
save-time gamemode warning, and materialization re-derives them with the same
helper so there is exactly one implementation of the rule.

### Gamemode token → canonical gamemode

`layer_cache` stores `TerritoryControl` while raw names say `TC`, so the parsed
token cannot be used directly against `allowed_gamemodes`. The mapping is
derived from the cache instead of hardcoded: for every distinct
`(gamemode, raw_name, layer_version)` in the reference source, compute the raw
token the same way `_squadcalc_mode_token` does and record
`token → gamemode`. Tokens with no match stay verbatim, so a genuinely new mode
still lands in the cache — it simply will not pass the gamemode filter until an
organizer adds it to `allowed_gamemodes`.

## Faction construction

`build_custom_factions(faction_ids, unit_types, reference, all_units) -> list[dict]`:

- `faction_ids` empty → every key of `reference`; `unit_types` empty → every
  type from `db.get_unique_unit_types([reference_source])`
- per faction: `{factionId, factionName, alliance, defaultUnit}` from
  `reference`, plus `availableOnTeams: [1, 2]` and
  `unitTypes: [{"type": u, "name": u} for u in unit_types]`
- `team1_allowed_alliances` and `team2_allowed_alliances` stay `[]`, so
  `get_factions_for_team` applies no alliance restriction and the alliance
  fallback table is never consulted

The borrowed `defaultUnit` (e.g. `USA_LO_CombinedArms`) is what makes vehicle
info work: `_resolve_unit_object_key` substitutes the type token into that
object name and looks it up in the units map. It never filters on source or
faction id.

## Module layout

`bot/bot.py` is already ~6900 lines, so everything that does not need Discord
goes into a new `bot/custom_layers.py`: parsing, the token map, the reference
source, faction construction, materialization, and the save/delete entry
points. It imports `database`, `config` and `utils` only. `bot.py` keeps the
views, the modal and the routing, and calls into the module.

That split is also what makes the bulk of this feature testable without a
Discord client.

## UI

### Entry point

`AdminPanelView` gains one button in every phase, alongside `edit_event` /
`set_event_roles` / `delete_event`:

```python
self.add_item(AdminButton("custom_maps", t("admin.custom_maps", lang),
                          discord.ButtonStyle.secondary, "🗺️"))
```

`AdminButton.callback` routes `custom_maps` to `admin_custom_maps(interaction,
db_id)`. It stays out of the `("open_suggestions", "reopen_suggestions",
"copy_result")` exemption tuple, so the parent panel is retired via
`self.view.stop()` when the sub-panel replaces the message.

Custom maps are guild-scoped, but the panel is reached through an event, so the
sub-panel carries `db_id` purely to navigate back.

### `CustomMapsView` (sub-panel)

Same shape as `ManageSuggestionsView`: an `AutoDisableView` with `self.db_id`,
bound via `_bind`, replacing the panel message through `edit_message`.

- **Add Map** button → `CustomMapModal`
- **Delete** select, listing the guild's custom maps (omitted when there are
  none) — deletes immediately; re-adding is the undo
- **Back** → `self.stop()` then `handle_admin_panel(interaction, db_id, edit=True)`

The embed lists the existing custom maps with their layer counts, or
`custom_map.none`.

### `CustomMapModal` (step 1)

Discord modals accept text inputs only, so the selects cannot live here — the
same reason `EventScheduleModal` hands off to `EventCreateConfirmView`.

- paragraph input, required: layer names, one per line
- short input, optional: display name; defaults to the parsed map token, and is
  stripped and capped at 50 characters

`on_submit` parses. On error it replies with an ephemeral embed listing what
was wrong and leaves the sub-panel intact. On success it swaps the message for
the details screen with `interaction.response.edit_message` — valid for a modal
opened from a component interaction. Should that prove unreliable at runtime,
fall back to `send_message(..., ephemeral=True)`; nothing downstream depends on
which message the screen lands in.

### `CustomMapDetailsView` (steps 2 and 3, one screen)

Two multi-selects and a save button fit in three of the five allowed action
rows, so the faction and unit steps share a screen rather than chaining two
messages.

- faction select: `min_values=0`, options from `get_faction_reference` keys
- unit select: `min_values=0`, options from
  `db.get_unique_unit_types([reference_source])`
- **Save** → `db.upsert_custom_map`, `materialize_custom_layers(guild_id)`,
  result embed

Both selects are capped at 25 options (Discord's limit) with
`custom_map.truncated` noted in the embed when the cap bites. Squad's main game
is well under 25 on both axes today; the cap is a guard, not an expected path.

The result embed states the map name, the layer count, and — when any parsed
gamemode is missing from the guild's `allowed_gamemodes` —
`custom_map.gamemode_warning` naming those modes.

## Integration points

| Place | Change |
|-------|--------|
| `db.get_unique_sources()` | add `WHERE source NOT LIKE 'custom:%'` — keeps custom sources out of the creation wizard's picker, the Edit Event dialog, `_resolve_offered_sources`, and the legacy fallback in `_resolve_event_sources` (4 call sites, all want the exclusion) |
| `_resolve_event_sources(event, settings)` | gains `guild_id` and appends `custom_source(guild_id)` **when that source actually has cached rows** (3 call sites: `bot.py:1289`, `bot.py:4409`, `bot.py:6382`) |
| `db.get_source_units(source)` | the 3 call sites (`bot.py:701`, `bot.py:1869`, `bot.py:2216`) resolve `custom:*` to the reference source first, via a small `_units_source(source)` helper |
| `utils.source_label(source, lang)` | new — renders a custom source as a localized "Custom" instead of `custom:123456789`. Used at the suggest-flow source picker (`bot.py:1292`), the source line in the suggest embed (`bot.py:1381`) and the `/history_add` picker (`bot.py:6384`) |
| `utils.build_squadcalc_url` | unchanged — already returns `None` for non-`main` sources, so custom layers get the fallback map icon |
| `utils._event_uses_supermod` | unchanged — a custom source is never the supermod source |

Two consequences worth stating outright:

- The custom source is appended **conditionally**, gated on
  `db.has_layers_for_source(custom_source(guild_id))`. Appending it
  unconditionally would push every guild past the `if len(sources) > 1` gate in
  the suggest flow and in `/history_add`, adding a source-picker step to
  servers that have no custom maps at all.
- Once a guild *does* have custom maps, that extra picker step is expected: the
  user chooses between the fetched source and their own maps, exactly as a
  guild with supermod enabled already does today.

## Error handling

| Case | Behaviour |
|------|-----------|
| No parseable lines | ephemeral error, `custom_map.err_empty` |
| Lines failing the pattern | ephemeral error listing them, `custom_map.err_invalid_lines` |
| Lines from different maps | ephemeral error listing the distinct tokens, `custom_map.err_mixed_maps` |
| More than 25 layers | ephemeral error, `custom_map.err_too_many` |
| Empty layer cache | save refused, `custom_map.no_reference_data` |
| Map name already exists | silent overwrite; the result embed says the map was replaced |
| More than 25 factions or units available | select capped, `custom_map.truncated` in the embed |

Input is organizer-gated and every DB write is parameterized; the line pattern
already restricts raw names to `[A-Za-z0-9_.]`, so nothing user-supplied
reaches an embed unescaped in a way that matters.

## Testing

`tests/test_custom_layers.py`, on the existing `temp_db` fixture:

- `parse_custom_layers` accepts both the bulleted and the bare list from the
  request and yields identical output
- mixed map tokens, an invalid line, empty input, and 26 layers each raise
- version and gamemode extraction, including `Belaya_TC_v1` → token `TC`
- token→gamemode mapping derived from a seeded cache turns `TC` into
  `TerritoryControl`, and leaves an unknown token alone
- `_build_custom_factions` with empty selections yields every reference faction
  and unit; with a partial selection yields the cross product; every entry has
  `availableOnTeams == [1, 2]` and a borrowed `defaultUnit`
- `get_unique_sources()` hides `custom:%` rows
- `materialize_custom_layers()` after `clear_layer_cache()` restores the rows,
  and `get_modes_for_map` then returns them
- `delete_custom_map` + `delete_layers` leave neither table with a trace

Run: `docker run --rm -e DISCORD_BOT_TOKEN=test -v "$PWD":/app -w /app
python:3.12-slim sh -c "pip install -q -r requirements.txt -r
requirements-dev.txt && python -m pytest -q"`.
`tests/test_winner_copy_text.py::test_copy_text_real_layer_fits_event_description`
fails in that container for an unrelated reason — `reference/` is gitignored
and empty.

## i18n

New flat keys in `bot/i18n.py`, de and en: `admin.custom_maps`,
`custom_map.panel_title`, `custom_map.panel_desc`, `custom_map.none`,
`custom_map.add`, `custom_map.delete_placeholder`, `custom_map.modal_title`,
`custom_map.field_layers`, `custom_map.field_display_name`,
`custom_map.details_title`, `custom_map.details_desc`,
`custom_map.factions_placeholder`, `custom_map.units_placeholder`,
`custom_map.save`, `custom_map.saved`, `custom_map.replaced`,
`custom_map.deleted`, `custom_map.gamemode_warning`, `custom_map.truncated`,
`custom_map.no_reference_data`, `source.custom`, `custom_map.err_empty`,
`custom_map.err_invalid_lines`, `custom_map.err_mixed_maps`,
`custom_map.err_too_many`.

## Documentation

- `README.md` — a Custom Maps feature bullet and the Admin-panel paragraph
- `USER_GUIDE.md` — the Admin Panel list, plus a short "Adding a Custom Map"
  section for organizers covering the two input formats and what an empty
  faction or unit selection means
- `ARCHITECTURE.md` — the `custom_layers` table, the `custom:<guild_id>` source
  convention, the materialization hook in `fetch_and_cache_layers`, and the
  always-appended source in `_resolve_event_sources`
