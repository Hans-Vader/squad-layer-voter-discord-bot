# Squad Layer Vote Bot — Architecture & Internals

## What this is

The Squad Layer Vote Bot is a Discord bot (Python, discord.py, SQLite, Docker) that collects Squad map/layer suggestions from server members through interactive dropdown menus, then runs a vote on the collected suggestions using Discord's **native poll** feature. Layer data is ingested from one or more external SquadLayerList-style `layers.json` files and cached locally; organizers can blacklist maps/factions/units/gamemodes, gate events to specific roles/users, and have winners recorded to a history that blocks recently-played layers from being re-suggested. It supports persistent embeds (buttons survive restarts) and per-guild English/German localization. This document explains how the system works under the hood; for the feature catalog and command tables see [README.md](README.md), and for step-by-step operator/player procedures see [USER_GUIDE.md](USER_GUIDE.md).

> Note: all bot application code lives under `bot/`. Paths below are written relative to the repo root.

---

## How it works — the event lifecycle / state machine

Each event is a single row in the `events` table. Its `phase` field is the state-machine variable, and a set of timer fields (`suggestion_start_time`, `suggestion_duration_seconds`, `suggestion_end_time`, `voting_duration_hours`, `voting_end_time`) plus pointers (`event_message_id`, `poll_message_id`, `vote_thread_id`) drive transitions. Every phase change is performed under a per-guild `asyncio.Lock` (`_get_guild_lock`), persisted via `db.save_event`, after which `_update_event_embed` re-renders the public embed and re-selects the persistent View for the new phase.

The phases are:

```
created ──► suggestions_open ──► suggestions_closed ──► voting ──► completed
                  ▲                      │  │
                  └──── reopen ──────────┘  └── (skip when count ≤ max) ──► voting
```

There are **two independent drivers** of every transition: organizer interactions through the embed **Admin panel buttons** (not slash commands), and the single background scheduler loop `check_events_loop()`.

| Transition | Manual driver (Admin panel button) | Automatic driver (scheduler) |
|---|---|---|
| `created → suggestions_open` | `admin_open_suggestions` ("Open Suggestions") — bot.py:1864 | when `suggestion_start_time` has passed (propagates `suggestion_duration_seconds` into `suggestion_end_time`) |
| `suggestions_open → suggestions_closed` | `admin_close_suggestions` / `_do_close_suggestions` (with confirm), reached via the **Manage Suggestions** sub-panel — bot.py:2513 | `_handle_suggestion_timeout` at `suggestion_end_time`, **only when there are more suggestions than vote slots** |
| `suggestions_closed → suggestions_open` | `admin_reopen_suggestions` ("Reopen Suggestions", clears auto-close timer) — bot.py:1974 | — |
| `suggestions_open → voting` (**skips closed**) | — | `_handle_suggestion_timeout` when `suggestion_count ≤ max_voting_layers` → `_auto_start_poll` (bot.py:2403) |
| `suggestions_closed → voting` | `admin_select_for_vote` → `ConfirmSelectionButton` → `_do_start_vote` (sets `selected_for_vote`, `phase=voting`, calls `_start_poll`) — bot.py:2012 | — |
| `voting → completed` | `admin_end_vote` ("End Vote") → `_resolve_poll_winner` — bot.py:2467 | scheduler detects `message.poll.is_finalised()` (Discord auto-close at duration) |

The state machine is **not strictly linear**: the scheduler can jump `suggestions_open → voting` directly, and `admin_reopen_suggestions` provides a backward `suggestions_closed → suggestions_open` edge. `_handle_suggestion_timeout` (bot.py:5350) has three outcomes at timeout: count ≤ `max_voting_layers` → auto-start voting; **0 suggestions** → land in `suggestions_closed` as a dead end (no winner, never completes); more suggestions than slots → stay in `suggestions_closed` and **ping the organizer role** for manual selection.

On `completed` (either path), the winner is resolved, `winning_layer` and `winning_layer_command` (the RCON `AdminChangeLayer` string) are stored, and — only if a winner exists — the result is appended to `voting_history`.

### The scheduler

`check_events_loop()` (bot.py:5449) is the **sole** background loop. It is hand-rolled (`while not closed` + `asyncio.sleep`), **not** a `discord.ext.tasks.loop`, and is started exactly once from `on_ready` via `bot.loop.create_task`, guarded by `bot._background_loop_started`. Each pass reads `db.get_all_active_events_global()` and, per event by phase: auto-opens suggestions; calls `_handle_suggestion_timeout`; throttled live-refreshes vote counts during voting (every `LIVE_VOTE_REFRESH_SECONDS`, 60s); and completes finalised polls. The loop **self-tunes its sleep**: normally `EVENT_CHECK_INTERVAL` (10s), dropping to `EVENT_CHECK_INTERVAL_FAST` (1s) when any event has a boundary within `EVENT_CRITICAL_WINDOW` (60s).

On startup, `setup_hook` (bot.py:145) re-attaches one persistent View per active event (chosen by phase via `_view_for_phase`) so embed buttons survive restarts, and `on_ready` auto-fetches layers if the cache is empty.

---

## Architecture at a glance

All application code lives under `bot/`:

| Module | Owns |
|---|---|
| `bot.py` | Everything Discord-facing: the `LayerVoteBot` client, all slash commands, the `discord.ui` Views/Selects/Buttons, the suggestion-flow handlers, poll creation/resolution, the layer-fetch pipeline, and the background scheduler loop. (This is the bulk of the codebase.) |
| `database.py` | The SQLite persistence layer — connection factory, schema, JSON-blob (de)serialization, and all CRUD helpers for guild settings, layer cache, events, and voting history. |
| `utils.py` | Pure rendering/formatting helpers: embed builders, poll-option text (`format_layer_poll_option`), SquadCalc deep links and map-icon markdown. |
| `config.py` | Process-level config read from env vars at import (token, admin IDs, layer sources, SquadCalc base URL), plus non-env constants (loop intervals, layer-exclusion rules). |
| `i18n.py` | Flat de/en translation dictionary and the single `t(key, lang, **kwargs)` lookup function. |

### How a user interaction flows through the system

```
User clicks a button on an event embed
        │
        ▼
discord.py matches the component's custom_id  (event_action:<action>:<db_id>)
against a registered persistent View  ── no manual on_interaction dispatcher
        │
        ▼
View callback (bot.py) → module-level handler (handle_suggest_start / handle_info /
        handle_admin_panel …) receives the baked-in db_id
        │
        ▼
read/validate via database.py (get_event_by_db_id — guild-enforced)
        │
        ▼
mutate in-memory SuggestState (dropdown chain)  OR  mutate event under guild lock
        │
        ▼
db.save_event  →  _update_event_embed (debounced 2s)  →  re-render embed
        │                                                 + re-attach phase View
        ▼
utils.py builds embed text / poll options / SquadCalc links
```

The convention `event_action:<action>:<db_id>` is the single source of truth for which event a click targets — baking `db_id` into every `custom_id` is what allows multiple events to coexist in one channel without their buttons colliding.

---

## Subsystems in detail

### Commands

12 slash commands, all registered via `@bot.tree.command` and synced in `setup_hook` (and on `/sync`). They fall into three permission tiers enforced by helper guards. See README.md for the full per-command tables; the key facts here are the tiers and the naming gotchas.

- **Admin-only** (Discord server admin, `check_admin` / `is_guild_admin`): `/setup`, `/set_organizer_role`, `/set_language` (also refreshes active embeds), `/set_log_channel`, `/sync`. (bot.py:4385-4479)
- **Organizer-only** (`check_organizer` / `has_organizer_role`): `/refresh_layers` (bot.py:4493), `/create_layer_suggestion` (bot.py:4517), `/delete_event` (bot.py:4831), `/update` (bot.py:4976), `/history_add` (bot.py:5077), `/history_remove` (bot.py:5300).
- **User-level** (configured guild only, no role check): `/history` (bot.py:5002).

**Naming gotchas:** the create-event command is `/create_layer_suggestion`, not `/create_event`. There is **no** `/start_vote` slash command — voting starts from the Admin panel button or the auto-advance path. The `/config_*` commands were removed in "Phase 3" (comment at bot.py:4482-4490); per-event config (gamemodes, blacklists, limits, voting params) now lives in the **Admin → Edit DM dialog**, which writes the per-event `config` snapshot. Manual phase transitions are driven by `AdminPanelView` / `AdminButton` (bot.py:2285-2429), **not** by slash commands. The panel is one level deep: in `suggestions_open` the **Manage Suggestions** button swaps the panel for `ManageSuggestionsView` (bot.py:2331), which carries Close Suggestions, Remove Suggestion and a Back button that re-renders the panel via `handle_admin_panel(..., edit=True)`. `AdminButton` reads `self.view.db_id`, so it works unchanged in either view.

### Interaction / UI — suggestion dropdown chain + persistent views

The UI is built entirely on `discord.ui` primitives. Each active event's embed carries one **persistent** View (`EventActionView`, `VotingPhaseView`, or `CompletedPhaseView`) chosen by `_view_for_phase(db_id, phase, lang)` (bot.py:859). These are constructed with `timeout=None` and every button gets a stable `custom_id` of the form `event_action:<action>:<db_id>`. Routing is **implicit**: there is no `on_interaction` handler; discord.py dispatches by matching the component's `custom_id` against registered persistent views (`add_view`) plus any view on a freshly-sent/edited message. `setup_hook` re-registers each event's View by `message_id` on startup, and `_do_update_embed` (bot.py:4333) re-attaches a fresh phase View on every embed edit.

The suggestion flow is a chain of ephemeral, single-message dropdown steps driven by `interaction.response.edit_message`:

```
Suggest button → handle_suggest_start (validates config/phase/role-gate/cap/cache)
   → [Source (only if >1 allowed source)] → Map → Mode
   → Team1 Faction → Team1 Unit → Team2 Faction → Team2 Unit
   → ConfirmSuggestionView (Submit/Cancel) → handle_suggest_submit
```

Mid-flow state lives in a module-global `_suggest_sessions` dict (`user_id → SuggestState`, bot.py:872/905). Each Select callback looks up the state, writes the picked value, re-reads guild+event settings **live** via `_state_event_settings` (so concurrent admin DM edits take effect immediately), queries `db` for the next level's options, and edits the same ephemeral message into the next step. Unit steps are auto-skipped (`Default`) when a faction has no selectable unit types.

These transient flow Views are deliberately **not** persistent (`timeout=600`, no `custom_id`), so a restart mid-flow simply drops the session and the next click hits the localized "timeout" branch (`state is None`). `AdminButton` (bot.py:1801) is a noted exception — it carries a `custom_id` (`admin:<action>`) even though its parent `AdminPanelView` is non-persistent.

`handle_suggest_submit` (bot.py:1540) pops the state, takes the guild lock, re-validates phase and the total-suggestion cap (**hard-clamped to 25** because suggestions become poll answers), builds the suggestion dict (short uuid id, resolved faction names + unit prefixes, `raw_name`, `source`), rejects duplicates (within the event and against recent history), appends to `event["suggestions"]`, persists, confirms in the ephemeral message, and triggers the debounced embed refresh.

Two Discord platform limits shape the UI: the **25-options-per-Select** cap (maps are bucketed Small/Medium/Large via `GroupedMapSelectView`, bot.py:1145; faction/unit lists sliced `[:25]`) and the **5-components-per-View** cap (only 3 map buckets used).

### Voting (Discord native polls)

Selected suggestions become a `discord.Poll`. Both build paths — admin-driven `_start_poll` (bot.py:2338) and background `_auto_start_poll` (bot.py:2403) — share identical building logic: `question = t("vote.poll_question")`, `duration = timedelta(hours=voting_duration_hours)` (default 24), `multiple = allow_multiple_votes`. Answer text comes from `format_layer_poll_option` (utils.py:327), abbreviated/truncated to **≤55 chars** for Discord's poll-answer limit.

The **10-answer cap** is enforced purely by slicing — `for s in selected[:10]: poll.add_answer(...)` — in both builders; extras are silently dropped (the selection UI independently caps at `min(max_voting_layers, 10)`, bot.py:2030). Gated (allow-listed) events post the poll inside a private locked thread via `_create_voting_thread`; open events post directly in the channel. The returned poll message's id, `voting_end_time` (from `poll.expires_at`, falling back to `now()+duration`), and `vote_thread_id` are saved on the event under lock.

Close is detected two ways: **passive** — the scheduler sees `message.poll.is_finalised()` (Discord auto-closes at duration; bot.py:5516); **active** — an admin presses End Vote, and `_resolve_poll_winner` (bot.py:2521) calls `message.end_poll()` to force-close. Winner is a **strict plurality**: iterate `message.poll.answers` tracking the highest `vote_count`; a winner only counts if `best_votes > 0`. It is matched back to a suggestion by exact `format_layer_poll_option` **text equality**, with a fallback to the first selected suggestion if no text matches.

Edge cases worth knowing: **ties** break deterministically toward the first-encountered answer (comparison is `> best_votes`); because match text is truncated to 55 chars, two layers that collide after truncation could mismatch and the fallback may record the wrong layer; a **zero-vote poll** completes with no winner and **no** history row. On completion the RCON command is generated by `build_admin_change_layer` (bot.py:683) — `AdminChangeLayer <raw_name> <fac1>+<unit1> <fac2>+<unit2>`, resolving `Default`/`?` units to the layer's real default loadout (fallback `CombinedArms`), returning `None` if essentials are missing. The completed-phase embed (utils.py:557) shows the winner plus the command in a fenced copy block; recent winners feed both the Info panel (bot.py:1705, last 3 via `get_recent_history`) and `/history`. Live counts are refreshed during voting via `_fetch_vote_counts` (bot.py:4303).

### Persistence (SQLite)

Implemented entirely in `database.py` as a thin, function-based wrapper over a single file at `data/layer_vote.db` (relative to the working dir; `data/` is auto-created). There is **no ORM and no long-lived connection** — every function opens a fresh connection via `_get_conn()`, which enables `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON`, commits if writing, and closes. `init_db()` (idempotent, `CREATE TABLE IF NOT EXISTS`) runs on every startup.

Five tables (plus `source_units`, a small per-source vehicle-data cache alongside `layer_cache`):

| Table | Role | Location |
|---|---|---|
| `guild_settings` | Per-guild config as a JSON blob merged over `DEFAULT_GUILD_SETTINGS` on read (organizer role, log channel, language, allowed gamemodes, blacklists, caps, `history_lookback_events`, allowed sources, create defaults). | database.py:113-118 |
| `layer_cache` | Cached Squad layer metadata, rebuilt from `layers.json`. `UNIQUE(raw_name, source)`; indexed on `(map_name, gamemode)` and `source`. | database.py:120-139 |
| `custom_layers` | Admin-defined maps: one row per `(guild_id, map_name)`, holding only what the organizer entered as a `payload` JSON blob (raw layer names, chosen factions, chosen unit types, optional Steam Workshop URL) plus `created_at`. The source of truth that `custom_layers.materialize_custom_layers()` expands into `layer_cache` rows. | database.py:160-166 |
| `events` | Per-channel cycles; the whole event (incl. `suggestions`, `selected_for_vote`, votes, `winning_layer`, per-event `config`) lives in the `event_data` JSON blob. Indexed on `(guild_id, status)` and `(guild_id, channel_id, status)`. Multiple active events per channel allowed. | database.py:168-181 |
| `voting_history` | Completed events: `all_suggestions` (JSON), `winning_layer` (JSON, nullable), `completed_at`. | database.py:183-193 |

Key design points:

- **JSON blobs over normalized tables.** Suggestions and votes are **not** separate rows — they live inside `events.event_data`, so they are not SQL-queryable, only loadable/parsable. Custom `_dumps`/`_loads` with a `{"__datetime__": iso}` wrapper round-trip `datetime` objects through the TEXT columns (database.py:32-50); external tooling reading these blobs must understand that wrapper.
- **Soft delete** by mutating `status` to a unique `completed_<id>` / `deleted_<id>` string (rows are preserved; `status` is therefore not a fixed enum).
- **Guild isolation is enforced in application code, not the schema.** `get_event_by_db_id` is guild-scoped (a deliberate security boundary against cross-guild button clicks); `get_active_event_unsafe` skips the guild filter and is for trusted background tasks only. There are no declared FK constraints despite `foreign_keys=ON`.
- **Per-event config snapshotting** (`_snapshot_event_config` / `EVENT_CONFIG_KEYS`, around `build_default_event` at database.py:646-723) so editing one event's settings never affects guild defaults or sibling events.
- **Restart recovery:** all durable state is on disk; on restart `init_db()` runs and the bot rehydrates every still-`active` event via `get_all_active_events_global()`, resuming timers from the stored timestamps. WAL mode produces `-wal`/`-shm` sidecar files.

See `docs/layer_files_reference.md` for the layer-file schema and known data quirks.

### Layer data — fetch / cache from SquadLayerList

Layer data originates from one or more SquadLayerList-style `layers.json` files (default: fantinodavide's `SquadLayerList` main). `config.py` parses `LAYERS_JSON_URL` into `LAYERS_JSON_SOURCES` — a list of `(source_name, url)` tuples. The source name is the path segment immediately before `/layers.json` (`.../refs/heads/main/layers.json` → `main`, `.../mods/supermod/layers.json` → `supermod`); **two URLs deriving the same name is a fatal startup error** (`_build_layers_json_sources`, config.py:62-119).

`fetch_and_cache_layers()` (bot.py:231) opens one `aiohttp` session and GETs each source. Any source that fails (non-200, network error, malformed JSON) is logged and **skipped**; if **all** sources fail it raises `RuntimeError` and **leaves the existing cache untouched**. Once at least one succeeds it calls `db.clear_layer_cache()` (full wipe) then re-populates.

> **Important correction to a common misconception:** there is **no cross-source "later source wins" merge.** Each source is cached independently and tagged with its `source`; the table is keyed `UNIQUE(raw_name, source)`, so the same `rawName` legitimately coexists across sources as distinct rows. "Last wins" dedup applies **only within a single source file** (`fetch_and_cache_layers` builds a per-source `dict[rawName→layer]`). Source order in config only affects iteration order, not overwrites. (Note: `.env.dist` contains a comment implying later-source-wins; the code does not behave that way.)

Per source, `_build_faction_meta_map` (bot.py:295) parses the `Units` block into `{factionID → {alliance, factionName}}` (covering modded `SU_*` factions the hardcoded `ALLIANCE_FACTIONS` fallback doesn't know). `_cache_source_layers` (bot.py:345) parses each layer: `rawName` (fallback `Name`), `mapName` (via `_MAP_NAME_OVERRIDES` for display shortening), `mapId`, `gamemode`, `layerVersion` (parsed from a `_v<N>` token when the field is missing), map size via `_parse_map_size_km` (uses `abs()` to tolerate a sign-typo `-4.1x4.1 km`), faction/unit extraction (default loadout synthesized/prepended since the source never lists it), and team alliance restrictions. Layers are dropped if missing essentials or if `config.is_excluded_layer` matches (Jensen's Range / typo `JesensRange` / `Supermod_JensensRange`, `Jensen`/`Tutorial` map-name prefixes, `Training` gamemode). Each layer is written via `db.upsert_layer` (`INSERT … ON CONFLICT(raw_name, source) DO UPDATE`).

**Refresh is not periodic.** Startup auto-fetch fires **only when the cache is empty** (`on_ready`, gated on `get_layer_cache_count() == 0`); otherwise the cache persists across restarts and is updated only on demand via `/refresh_layers`.

**SquadCalc integration** (utils.py:55-275): `build_squadcalc_url` produces a parameterized SquadCalc deep link **only for `source == "main"`** (the SquadCalc-compatible source). For SPM/SU/supermod layers, `build_map_icon_markdown` renders the 🗺️ icon as a masked link to a no-op Discord URL carrying just a hover tooltip (map + version + full faction names) rather than 404-ing on SquadCalc. When `SQUADCALC_BASE_URL` is empty, main-source layers fall back to a plain emoji. A suggestion carrying a `workshop_url` (custom maps only, see below) outranks both: the icon points at the mod's Steam Workshop page, since SquadCalc has no data for admin-defined maps.

The event embed's footer is **derived from those same targets**, not configured: `build_event_embed` collects every layer whose icon actually reached a field into a local `rendered` list, and `_icon_link_kinds` reports which destinations those icons carry — so the footer names SquadCalc, the Steam Workshop, or both, and is omitted entirely when nothing on the board is clickable. Deriving it from `event["suggestions"]` instead would overclaim twice over: a live voting board renders bars for the ballot only (`selected_for_vote`, capped at Discord's 10 answers), and a long board collapses its tail into "… and N more". A tooltip-only link counts as no destination. The SuperMod abbreviation legend used to occupy that slot; it now lives in the Info panel (`build_legend_lines` → `bot._build_info_embed`), which has room for every abbreviation `format_suggestion_entry` applies rather than a single line. The SuperMod entry is keyed on the event's active sources (SPM/SU and GoingDark are raw-name prefixes, not table entries); every other entry is derived from the listed suggestions, so the legend only explains shorthand that is on screen.

**Custom (admin-defined) maps** (`bot/custom_layers.py`): `custom_layers` is the
source of truth for maps an organizer entered by hand; its `payload` holds
only what was typed or picked — raw layer names, the chosen factions and
unit types, and an optional Steam Workshop URL. `materialize_custom_layers()` expands those rows into ordinary
`layer_cache` rows tagged `custom:<guild_id>`, re-deriving gamemode, version
and faction/unit metadata each time from a reference source
(`resolve_reference_source()`, which prefers the SquadCalc-compatible `main`
source) — this is why `/refresh_layers`, which wipes `layer_cache`
wholesale, never loses a custom map. It runs at the end of
`fetch_and_cache_layers()`, unconditionally again in `on_ready` (a harmless
repeat when a fetch just ran), and inside `save_custom_map()` after every
save; a delete instead removes the materialized rows directly
(`remove_custom_map()` → `db.delete_layers()`), since there is nothing left
to re-derive. When no fetched source is cached yet, materialization is a
no-op — the definition is still stored, and the next `/refresh_layers` or
save picks it up. `get_fetched_sources()` returns only what came from a
layers.json URL; `get_guild_sources(guild_id)` adds the guild's own custom
source when it holds layers. There is deliberately no guild-blind variant:
every caller has to say which of the two questions it is asking, because a
guild-blind source list reached from guild-scoped code is what leaked one
guild's custom rows into another's picker. `_resolve_event_sources` therefore
has no special case for custom sources at all — the event's own
`allowed_sources` governs them.

The guild defaults do **not** cap a running event. They seed the creation
wizard through `_resolve_offered_sources` and stop there, which puts layer
sources on the same footing as every other value in `EVENT_CONFIG_KEYS`.
They used to be applied as a live cap inside `_resolve_event_sources`, so
editing `/config_defaults` silently narrowed events already in flight — and
because the custom source was appended *after* that cap, the inconsistency
stayed invisible until custom sources became ordinary. One consequence to
keep in mind: with the cap gone, `_resolve_event_sources` can return an empty
list when the guild has no sources at all, and its callers must reject that
rather than pass it on — an empty list downstream means *no source filter*.

### Configuration & i18n

`config.py` calls `load_dotenv()` at import and reads a small set of env vars into module-level constants (no central `Config` object). The deliberate design split: **env vars are deployment-wide only; everything server-specific lives in the per-guild DB settings** managed via slash commands.

- `DISCORD_BOT_TOKEN` → `TOKEN`: the only required var; a missing token **warns but does not raise** (login fails later). (config.py:28)
- `ADMIN_IDS`: optional comma-separated bot-level superadmin IDs (list of strings) that **bypass all permission checks**. (config.py:33)
- `DEBUG_MODE`: optional bool (`== "true"`). (config.py:153)
- `LAYERS_JSON_URL`: single URL, comma-separated list, or **JSON-array string** (the JSON form exists so URLs containing commas survive). (config.py:62)
- `SQUADCALC_BASE_URL`: optional base for SquadCalc links (trailing slash stripped). (config.py:150)
- Non-env constants: loop intervals `EVENT_CHECK_INTERVAL`/`_FAST`/`EVENT_CRITICAL_WINDOW` (config.py:37-45, with a startup sanity-check assertion) and the layer-exclusion rules (config.py:121-146).
- `PUID`/`PGID` appear **only in `.env.dist`** — they are Docker entrypoint concerns and are never read by Python.

**i18n** (`i18n.py`) is a flat dict lookup with two languages, `de` and `en` (default `en`). All strings live in one module-level `_STRINGS` dict keyed by dotted paths (`"general.no_permission"`, `"embed.status_voting"`, …), each mapping to `{"de": ..., "en": ...}` with optional `str.format` placeholders. The single lookup function `t(key, lang="en", **kwargs)` degrades gracefully at three levels: missing key → literal `"[key]"` sentinel; missing translation → English fallback; bad format kwargs → unformatted template (`KeyError`/`IndexError` swallowed). Language is **per guild, not global** — callers resolve it via `db.get_guild_language(guild_id)` (database.py:204, defaults to `en`) and thread that code into every `t()` call. It is changed through `/setup` and `/set_language` writing the `language` field into the guild's settings row.

---

## Deployment

Dockerized via `docker-compose`. The image (`Dockerfile`) builds from `python:3.14-alpine`, installs deps with `pip install -r requirements.txt` (`build-base` + `libffi-dev` added as a temporary virtual `.build-deps`, then removed; `requirements.txt` is copied before the source so the dependency layer caches), copies `bot/` into `/app`, creates a non-root `appuser` from host `PUID`/`PGID` (default 1000), and runs `python bot.py`.

The `docker-compose.yml` service `layer-vote-bot` uses `restart: unless-stopped`, `env_file: .env`, `TZ=Europe/Berlin`, and mounts `./data:/app/data` (SQLite persistence — this is the durable state, so it must survive container recreation) plus `/etc/localtime:ro`. `PUID`/`PGID` flow from `.env` through build args into the Dockerfile for correct volume ownership.

**Dependencies** (pinned in `requirements.txt`): `discord.py>=2.0.0`, `python-dotenv>=0.19.2`, `aiohttp>=3.8.1`, `pynacl>=1.5.0` (voice/encryption support pulled in for Discord). **External runtime dependency:** the SquadLayerList GitHub repo (overridable via `LAYERS_JSON_URL`).

Manual run (per README): `cd bot` then `python bot.py`. The SQLite path defaults to `data/layer_vote.db` relative to the working directory and can be overridden with the `DB_PATH` env var (`bot/database.py`). See README.md for the full env-var and guild-settings tables.

---

## Where to look

| Concern | File(s) |
|---|---|
| Slash commands, permission tiers | `bot/bot.py:4385-5343` |
| Event state machine + manual transitions (Admin panel) | `bot/bot.py:2241-2560` |
| Background scheduler / auto-advance | `bot/bot.py:5449-5558` (`check_events_loop`), `bot.py:5350` (`_handle_suggestion_timeout`) |
| Persistent views + startup re-binding | `bot/bot.py:145` (`setup_hook`), `bot.py:859` (`_view_for_phase`) |
| Suggestion dropdown chain | `bot/bot.py:872-1540` |
| Poll creation / winner resolution / RCON command | `bot/bot.py:2338-2561`, `bot.py:683` (`build_admin_change_layer`) |
| Layer fetch / parse / cache | `bot/bot.py:231-446`, `config.py:62-146` |
| Custom (admin-defined) maps | `bot/custom_layers.py`, views in `bot/bot.py:2443-2687` |
| SquadCalc links, embed/poll formatting | `bot/utils.py` |
| SQLite schema + JSON-blob persistence | `bot/database.py` |
| Env config | `bot/config.py` |
| Translations | `bot/i18n.py` |
| Deployment | `Dockerfile`, `docker-compose.yml`, `.env.dist` |
| Layer-file schema & data quirks | `docs/layer_files_reference.md` |
| Feature catalog / command tables | `README.md` |
| Operator & player walkthrough | `USER_GUIDE.md` |
