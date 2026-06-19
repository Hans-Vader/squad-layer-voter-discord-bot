# Guild Default Settings Editor (`/config_defaults`)

**Date:** 2026-06-20
**Status:** Approved — ready for implementation plan
**Branch:** `feature/guild-default-settings-editor`

## Problem

The bot has a rich per-event editor — the **Admin → Edit** DM dialog — that lets an
organizer change ~16 properties of a single event (gamemodes, blacklists, suggestion
limits, voting parameters, timers, etc.). Each event snapshots the relevant guild
settings at creation time, so editing one event never affects another.

There is **no user-facing way to change the guild-wide defaults that new events start
from.** Those defaults live in `DEFAULT_GUILD_SETTINGS` (`bot/database.py:54`) and are
only consumed — never written — by the running bot:

- The 8 `EVENT_CONFIG_KEYS` (gamemodes, 3 blacklists, 4 limits) are copied into
  `event["config"]` at creation by `_snapshot_event_config` (`database.py:705`).
- The 5 `default_*` keys (`default_suggestion_start`, `default_suggestion_duration`,
  `default_voting_duration_hours`, `default_allow_multiple_votes`,
  `default_mirror_match`) pre-fill the creation wizard (`bot.py:5070`).

The legacy guild-wide config commands (`/config_suggestions`, `/config_blacklist`,
`/config_layer_sources`, `/config_gamemodes`) were removed in "Phase 3"; their picker
UIs were reborn as the **per-event** DM dialog. The only remaining guild-settings
writers are `/setup`, `/set_organizer_role`, `/set_language`, `/set_log_channel`, which
touch only `organizer_role_id`, `log_channel_id`, `language`. As a side effect, several
i18n keys (`settings.create_suggestion_defaults`, `config.create_suggestion_updated`,
etc.) are now orphaned/dead.

## Goal

Add an organizer-gated slash command `/config_defaults` that opens the **same DM edit
dialog** as the per-event editor, but pointed at the guild's default settings, so an
organizer can change the defaults that new events inherit.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Permission gating | **Organizer role** (`check_organizer`), like the per-event Admin → Edit dialog |
| Reuse strategy | **Target-aware refactor** — one editor, two targets (event is the default target; guild target added) |
| Field scope | Per-event parity **minus `event_name`**, **plus** a new `default_max_voting_layers` |
| Command name | `/config_defaults` |
| Effect on existing events | **New events only.** Existing events keep their creation snapshot; the `allowed_sources` live-cap behavior is unchanged |
| Testing | Add a minimal **pytest** suite for the data/logic layer + manual verification of the Discord UI |

## Non-goals (out of scope)

- "Re-apply defaults to existing events" — existing events are intentionally frozen at
  their creation snapshot.
- "Reset to factory defaults" button.
- Any change to how defaults are *consumed* beyond the single wiring needed for
  `default_max_voting_layers` (see §1).

---

## Design

### 1. Data-model changes (`bot/database.py`)

1. Add one key to `DEFAULT_GUILD_SETTINGS` (`database.py:54`):
   `"default_max_voting_layers": 10` (10 matches today's hardcoded creation value).

2. Make it meaningful at creation. `build_default_event` (`database.py:722`) currently
   hardcodes `"max_voting_layers": 10` (`database.py:744`) and `_finalize_event_creation`
   (`bot.py:5300`) never overrides it. Change that line to read from settings:

   ```python
   "max_voting_layers": int((settings or DEFAULT_GUILD_SETTINGS)
                            .get("default_max_voting_layers", 10) or 10),
   ```

   This is the **only consumer-side change required.** Every other default already
   flows into new events:
   - The 8 config keys via `_snapshot_event_config` (`database.py:705`) — editing the
     guild value changes the snapshot new events capture.
   - The 5 `default_*` keys via the wizard prefill (`bot.py:5070`–`5148`) +
     `_finalize_event_creation` overwriting `build_default_event`'s placeholders.
   - `allowed_sources` via the existing live-cap logic (`bot/utils.py:442`,
     `bot/bot.py` source resolution) and the creation-time offered-sources set.

3. **No DB migration.** Guild settings are a single JSON blob and `get_guild_settings`
   (`database.py:187`) returns `{**DEFAULT_GUILD_SETTINGS, **stored}`, so legacy rows
   transparently gain `default_max_voting_layers=10`. The first `/config_defaults` save
   materializes the full merged dict (including all `default_*` keys) into the row,
   which is harmless.

### 2. The `EditTarget` abstraction (`bot/bot.py`)

Introduce a small structure encapsulating the differences between editing an event and
editing guild settings. The existing event editor becomes the `event` target; behavior
must stay **identical**.

```python
@dataclass
class EditTarget:
    kind: str                      # "event" | "guild"
    properties: list[dict]         # _EDIT_PROPERTIES | _GUILD_EDIT_PROPERTIES
    # load the mutable object (event dict / settings dict) or None
    load: Callable[[int, Optional[int]], Optional[dict]]
    read: Callable[[dict, dict], Any]          # (obj, prop) -> value
    write: Callable[[dict, dict, Any], None]   # (obj, prop, value)
    # persist under the guild lock: reload -> write -> save -> refresh
    persist: Callable[..., Awaitable[bool]]
    refresh: Optional[Callable[[int], Awaitable[None]]]  # event: _update_event_embed; guild: None
    overview_title: Callable[..., str]
    has_phase_lock: bool           # event True, guild False
    shows_event_link: bool         # Done line: event True, guild False
```

| Capability | `event` target | `guild` target |
|---|---|---|
| `properties` | `_EDIT_PROPERTIES` | `_GUILD_EDIT_PROPERTIES` |
| `load(guild_id, db_id)` | `db.get_event_by_db_id` → `record["event"]` | `db.get_guild_settings(guild_id) or dict(DEFAULT_GUILD_SETTINGS)` |
| `read/write(obj, prop)` | `event[k]` or `event["config"][k]` (per `prop["target"]`) | flat `settings[k]` (guild props have no `target` split) |
| `persist` | lock → reload event → write → `db.save_event` → `_update_event_embed` | lock → reload settings → write → `db.save_guild_settings` → no refresh |
| `overview_title` | event display name | localized "Guild Defaults" |
| `has_phase_lock` | yes (`suggestion_start_time`) | no |
| `shows_event_link` | yes | no |

**Low-churn threading.** The `target` is stored in the existing
`_active_edit_sessions[user_id]` session dict (`bot.py:3608`). The shared helpers read
the target **from the session via `user_id`**, which they already have, so most view and
modal constructor signatures do not change:

- `_build_edit_main_embed` (`bot.py:3710`) — iterate `target.properties`, use
  `target.read`, title from `target.overview_title`.
- `EditMainView` (`bot.py:3963`) — build dropdown options from `target.properties`
  (read target from session in `__init__` via `user_id`); the Done handler emits the
  event link only when `target.shows_event_link`.
- `_show_property_editor` (`bot.py:4024`) — load via `target.load`, read via
  `target.read`; apply the `suggestion_start_time` phase-lock only when
  `target.has_phase_lock`; the `allowed_sources` select-all-when-empty preselect and the
  scoped blacklist branch are keyed on `prop["key"]` and work for both targets.
- `_persist_property_value` (`bot.py:4699`) → delegates to `target.persist`.
- `_apply_edit` (`bot.py:4744`) → uses `target.persist` + `target.refresh`.
- `_read_event_property` / `_write_event_property` (`bot.py:3667`) generalize into the
  target's `read`/`write`.
- Session helpers (`_set_active_view`, `_close_session`, `_refresh_main_view`,
  `_bounce_to_main`, timeout/stale handlers around `bot.py:3842`–`3963`) already operate
  off the session, so they need no new params.

The leaf views/modals (`EditListView` `bot.py:4133`, `EditBoolView` `bot.py:4209`,
`EditScalarView`, `EditScalarModal` `bot.py:4348`, `EditDateTimeModal` `bot.py:4307`,
`EditStringModal` `bot.py:4406`) collect a value and call back into `_apply_edit`, which
resolves the target from the session — so they stay essentially as-is.

### 3. Guild property table (`_GUILD_EDIT_PROPERTIES`)

15 fields (per-event parity minus `event_name`), in display order:

| # | guild key | kind | constraints / source | notes |
|---|---|---|---|---|
| 1 | `allowed_gamemodes` | list | `db.get_unique_gamemodes` | shared key |
| 2 | `blacklisted_maps` | list | `db.get_unique_maps` | scoped picker |
| 3 | `blacklisted_factions` | list | `db.get_unique_factions` | scoped picker |
| 4 | `blacklisted_units` | list | `db.get_unique_unit_types` | |
| 5 | `max_suggestions_per_user` | int | 1–10 | |
| 6 | `max_total_suggestions` | int | 1–25 | |
| 7 | `max_self_removals_per_user` | int | 0–10 | |
| 8 | `history_lookback_events` | int | 0–50 | |
| 9 | `allowed_sources` | list | `db.get_unique_sources` | `[]` = all (the cap) |
| 10 | `default_voting_duration_hours` | vote_duration | | |
| 11 | `default_max_voting_layers` | int | 1–10 | **new key (§1)** |
| 12 | `default_allow_multiple_votes` | bool | | |
| 13 | `default_mirror_match` | bool | | mirror-match note |
| 14 | `default_suggestion_duration` | `duration_str` | | **new kind** |
| 15 | `default_suggestion_start` | `duration_str` | | **new kind**; offset-from-creation note |

Guild props omit the event-only `target` field ("config"/"event"); the guild
`EditTarget.read/write` are flat on the settings dict.

**New `duration_str` kind.** Edits a duration *string* (e.g. `"1h"`, `"30m"`), validated
by `parse_duration_to_seconds` (reject non-empty unparseable input); on success the
**trimmed input string** is stored verbatim (not re-canonicalized). Empty input stores
`None` (clears the default → manual phase / no timer). Reuses `EditScalarModal` with a
duration-string validator. `_format_property_value` renders the stored string, or `—`
when `None`. Distinct from the per-event `datetime` kind (absolute
`suggestion_start_time`) and `duration` kind (`suggestion_duration_seconds` in seconds).
`default_suggestion_start` carries an i18n help note clarifying it is an **offset from
event creation time**, matching the prefill logic at `bot.py:5072`–`5077`.

### 4. Reused behavior (free wins from one editor)

- The scoped map/faction blacklist picker (`_show_scoped_blacklist_source_picker`,
  `bot.py:4055`) works for the guild target automatically — same map/faction universe,
  reads/writes via the target.
- `allowed_sources` "select-all when empty" preselect (`bot.py:4073`) is key-based, so
  it works for both targets.
- Single-session-per-user, 600s view timeouts, stale-session cleanup, DM-blocked
  handling, and atomic persist under the per-guild lock (`_get_guild_lock`) are all
  inherited.

### 5. The command (`bot/bot.py`)

```
/config_defaults   (description: edit the guild-wide defaults new events start from)
```

- Gated by `check_organizer` (mirroring `/create_layer_suggestion`, `bot.py:5025`).
- Factor the session-setup + DM-send out of `admin_edit_event` (`bot.py:3769`) into a
  shared helper, e.g. `_open_edit_session(user, guild_id, lang, target)`, used by both
  the per-event Admin → Edit button and the new command.
- `admin_edit_event` triggers from a component (`interaction.response.edit_message`);
  the slash command responds via ephemeral `send_message` ("Check your DMs", reusing
  `edit.dm_sent`), with the same DM-blocked fallback (`edit.dm_blocked`) and
  stale/active-session handling (`edit.session_active`).
- The guild target's `load` tolerates a guild that never ran `/setup` by falling back to
  `dict(DEFAULT_GUILD_SETTINGS)` (in practice `check_organizer` already implies `/setup`
  ran, since `organizer_role_id` is only set there).
- Register/sync the command in the existing setup/config command block.

### 6. i18n (`bot/i18n.py`, both `en` and `de`)

- Reuse `edit.prop.*` labels for the 8 shared config keys + `allowed_sources`.
- Add: a guild dialog title; labels for the `default_*` fields (e.g. "Default Voting
  Duration", "Default Multiple-Choice Voting", "Default Mirror Match", "Default
  Suggestion Duration", "Default Suggestion Start (offset)"); label for
  `default_max_voting_layers`; the offset help note for `default_suggestion_start`; the
  `/config_defaults` description.
- Revive the orphaned `settings.create_suggestion_defaults` /
  `config.create_suggestion_updated` keys where they fit, rather than leaving dead keys.

### 7. Behavior & precedence (unchanged consumer model)

- Editing a guild default changes what **new** events start with (config keys
  snapshotted at creation; `default_*` keys prefill the wizard; `default_max_voting_layers`
  via the new wiring in §1).
- **Existing events are unaffected** — they keep their creation-time snapshot. The sole
  exception is `allowed_sources`, which the bot already applies as a *live cap* on active
  events (pre-existing behavior, unchanged).
- Precedence remains: per-event explicit value > guild default > hardcoded fallback.

---

## Testing

### Add a minimal `pytest` suite (`tests/`)

Add `pytest` as a dev dependency (e.g. `requirements-dev.txt` or a documented note).
Cover the **pure data/logic layer** (the part the Discord UI can't reach cheaply):

1. `get_guild_settings` / `save_guild_settings` round-trip, including the new
   `default_max_voting_layers` key and the `default_*` keys.
2. `DEFAULT_GUILD_SETTINGS` merge: a legacy row lacking `default_max_voting_layers`
   resolves to `10` via `get_guild_settings`.
3. `build_default_event` consumes `settings["default_max_voting_layers"]`, and falls
   back to `10` when `settings` is `None` or the key is absent.
4. `duration_str` parse/validate/store round-trip: `"1h"`, `"30m"` accepted; `"abc"`
   rejected; empty → `None`.
5. Guild `EditTarget.read` / `write` / `persist` against a temporary SQLite DB (write a
   field, reload, assert persisted; assert no event refresh is attempted).

Tests run against a temp DB file (or `DB_PATH` pointed at a tmp path), not the
committed `data/layer_vote.db`.

### Manual verification (Discord UI)

Run the bot in a test guild and:

1. As an organizer, run `/config_defaults`; confirm the DM dialog opens with all 15
   fields and current values.
2. Edit one field of each kind: list (incl. a scoped blacklist), bool, int,
   vote_duration, and `duration_str` (incl. clearing to empty). Confirm each persists.
3. Create a new event and confirm it reflects the changed defaults (wizard prefill +
   snapshot + `max_voting_layers`).
4. Confirm an **existing** event is unchanged after editing guild defaults.
5. **Regression:** open the per-event Admin → Edit dialog and confirm it behaves
   exactly as before (the event target must be byte-for-byte equivalent).
6. Confirm a non-organizer is rejected by `/config_defaults`.

## Risks

- Refactoring the working editor with no automated UI tests is the main risk. Mitigated
  by: (a) the `event` target reproducing current behavior, (b) the manual regression
  pass of the per-event dialog (test step 5), and (c) the pytest suite covering the data
  layer.

## Affected files

- `bot/database.py` — `DEFAULT_GUILD_SETTINGS`, `build_default_event`.
- `bot/bot.py` — `EditTarget`, `_GUILD_EDIT_PROPERTIES`, target-aware refactor of the
  editor helpers/views, new `duration_str` kind, `_open_edit_session` helper,
  `/config_defaults` command.
- `bot/i18n.py` — new + revived keys (en/de).
- `tests/` — new pytest suite.
- `requirements-dev.txt` (or equivalent) — `pytest`.
- Docs (`README.md`, `USER_GUIDE.md`) — document `/config_defaults` (follow-up; can be
  folded into implementation).
