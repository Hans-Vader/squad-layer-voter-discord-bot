# Event Name — Design

**Date:** 2026-05-22
**Status:** Approved (pending implementation plan)
**Scope:** Single feature — add an optional human-readable name to each event.

## Summary

Add an optional `event_name` field to events. When set, the name replaces the embed title across all phases, appears in the voting thread name, prefixes log-channel messages for that event, and titles the admin panel and DM edit dialog. When unset, every display surface falls back to `Event #{db_id}`.

The name is collected during event creation (new first field in the existing creation modal) and is editable at any time via the DM edit dialog.

## Motivation

The bot currently shows a phase-driven generic title ("Layer Vote — Suggestion Phase") on every event. Organizers running multiple parallel events have no way to distinguish them at a glance — embeds, voting threads, admin panels, and log entries all look interchangeable. Giving each event a human-readable name solves this without changing any of the underlying event lifecycle.

## Decisions (locked)

1. **`event_name` is optional.** Empty/null/whitespace-only → fallback to `Event #{db_id}`.
2. **The name replaces the existing `embed.title_*` strings.** Phase information is unaffected because it already lives in a separate `Status` field on the embed.
3. **Asked during creation** via a new first field on `EventScheduleModal`.
4. **Editable via DM edit** through a new `event_name` property using the same modal-via-button pattern as `int` / `duration` / `datetime` types.
5. **Visible surfaces:** embed title, voting thread name, log channel messages, admin panel embed, DM edit dialog header, role-picker description.
6. **Not in scope:** voting poll question, `/history` entries. Both keep their current strings.

## Data model

Add one optional key to the event JSON blob stored in the `events` table (`event_data` column):

```jsonc
{
  // ... all existing event fields
  "event_name": null   // string up to 100 chars, or null
}
```

- Add `"event_name": None` to `build_default_event()` (database.py:671) so newly created events have the slot. The signature gains an `event_name` keyword argument that the creation flow passes through.
- **No data migration.** Pre-existing events lack the key; `.get("event_name")` returns `None` and the display helper falls back to the ID. This is the same behavior as a cleared name.
- **Length limit: 100 characters.** Binding constraint is Discord's thread-name cap (100). Embed title is capped at 256 and modal `TextInput` at 4000, but capping at the smallest downstream surface means no surface ever needs truncation logic.

### Normalization on save

Applied identically at both write paths (creation modal submit and DM edit modal submit):

1. `strip()` leading/trailing whitespace.
2. Collapse any internal newlines (`\n`, `\r`) to single spaces — titles are single-line.
3. If the result is empty, store `None` (treat-as-cleared).

No other sanitization. Discord embed titles render a subset of markdown verbatim; the name is shown as the organizer typed it.

## Display helper — single source of truth

Add to `utils.py`:

```python
def display_name(event: dict, *, lang: str = DEFAULT_LANGUAGE) -> str:
    name = (event.get("event_name") or "").strip()
    return name or t("event.fallback_name", lang, db_id=event["db_id"])
```

Every surface that displays the event's identity calls `display_name(event)`. The fallback rule exists in this one place.

**New i18n key:**

```python
"event.fallback_name": {
    "de": "Event #{db_id}",
    "en": "Event #{db_id}",
},
```

## Creation flow — modify `EventScheduleModal`

`EventScheduleModal` (bot.py:4191–4254) currently has three `TextInput` fields. Add a fourth as the **first** entry:

| Position | Field | Required | Max | Style | i18n label |
|----------|-------|----------|-----|-------|------------|
| 1 | **Event name** | No | 100 | short | `event.wizard_name_label` |
| 2 | Start | No | — | short | existing `event.wizard_start_label` |
| 3 | Suggestion duration | No | — | short | existing `event.wizard_suggestion_duration_label` |
| 4 | Voting duration | Yes | — | short | existing `event.wizard_vote_duration_label` |

Discord modals allow up to 5 `TextInput`s, so 4 fits. Name goes first because it's the most semantically distinctive field — "what is this event called?" naturally precedes "when does it run?".

On submit:

- Read `name_input.value`, normalize per the rules above.
- Pass to `EventCreateConfirmView` and downstream to `_finalize_event_creation()` (bot.py:4401–4449).
- Stored on the event via `db.build_default_event()` keyword override before `db.create_event()`.

**New i18n keys:**

```python
"event.wizard_name_label": {
    "de": "Event-Name (optional)",
    "en": "Event name (optional)",
},
"event.wizard_name_placeholder": {
    "de": "z.B. Friday Night Fights — leer = Standardname",
    "en": "e.g. Friday Night Fights — empty = default name",
},
```

## DM edit — new `event_name` property

Two changes inside the DM edit dialog.

### New property entry

Add to `_EDIT_PROPERTIES` (bot.py:2787–2809):

- **key:** `event_name`
- **storage target:** event root (not `event["config"]`)
- **type:** `string` (new type — see below)
- **label:** `edit.prop.event_name`
- **always editable** — no `edit.locked_phase` guard. Renaming is a display-label change, safe in any phase including `completed`.

### New `string` property type

Add a `string` branch to the DM edit follow-up view dispatch, parallel to the existing `int` / `duration` / `datetime` branches (bot.py:3304+, 3341+, 3378+):

1. Property selector → dispatches to the `string` follow-up view.
2. Embed description shows: current value (or fallback), the 100-char limit, and the hint "Leave empty → reverts to `Event #{db_id}`."
3. Single ⌨️ button labeled `edit.open_input`.
4. Button opens a one-field modal (`max_length=100`, required=False).
5. On submit: normalize per rules above, write back to event, update the JSON blob, return to the property selector with the `edit.updated_inline` toast.

The `string` type is added now because `event_name` needs it. It is designed so future string-typed properties (e.g., a future description field) can reuse it, but no second consumer is being added in this work.

### DM edit dialog header

Update the DM dialog's embed assembly (bot.py:2840–2870) so the embed **title** becomes `display_name(event)` and the existing `edit.title` ("Edit Event Configuration") becomes the first line of the embed description, immediately above the existing `edit.select_property` prompt. The property selector and per-property follow-up views are unchanged.

Update the role-picker description in the in-guild allow-list flow (i18n.py:495–506): replace the trailing `(Event #{db_id})` literal with `({display_name(event)})` so the picker also reflects the current name.

**New i18n keys:**

```python
"edit.prop.event_name": {
    "de": "Event-Name",
    "en": "Event Name",
},
"edit.string_prompt": {
    "de": "Aktuell: `{current}`. Klicke ⌨️, um einen neuen Namen einzugeben (max. {max} Zeichen). Leer lassen, um auf `{fallback}` zurückzusetzen.",
    "en": "Current: `{current}`. Click ⌨️ to enter a new name (max {max} chars). Leave empty to reset to `{fallback}`.",
},
```

Updated i18n keys (rewrite to interpolate `display_name`):

- `roles.picker_desc.de` and `roles.picker_desc.en`: replace the trailing `(Event #{db_id})` with `({event_label})` and pass `event_label=display_name(event)` at the call sites.

## Display surfaces — wiring `display_name(event)`

All four surfaces in scope are wired to the same helper. None of them touches the JSON blob directly.

### Embed title (utils.py:387–546)

Replace the four phase-specific title lookups (`embed.title_suggestion`, `embed.title_voting`, `embed.title_completed`, `embed.title_created`) with `display_name(event, lang=lang)`. Phase context remains in the existing `Status` field (driven by `embed.status_*` keys), which is unchanged.

**Removed i18n keys** (now unused):

- `embed.title_suggestion`
- `embed.title_voting`
- `embed.title_completed`
- `embed.title_created`

These are deleted outright rather than left as dead entries.

### Voting thread name

`thread.voting_name` (i18n.py:481) is currently `"Voting — {period}"`. Replace with `"Voting — {event_label}"`, passing `event_label=display_name(event)`.

Rationale: the period information is already encoded in the surrounding embed and in Discord's thread metadata (creation timestamp). The thread name needs to disambiguate concurrent threads, which the event name does directly.

**Updated i18n key:**

```python
"thread.voting_name": {
    "de": "Abstimmung — {event_label}",
    "en": "Voting — {event_label}",
},
```

### Log channel messages

Every log-channel post for an event (bot.py:4445–4449 for creation, plus all phase-change and admin-action posts that reference the event) is prefixed with `**{display_name(event)}** — ` before the existing content (channel mention, organizer name, action description).

This is implemented as a single helper inside the log-post path so every call site picks up the prefix without each one being touched individually.

### Admin panel embed (bot.py:1632–1633)

The admin panel embed's title becomes `display_name(event)`. The current phase line and suggestion count remain as description/fields, unchanged.

## Validation & edge cases

- **Empty / whitespace-only input** → stored as `None`, display reverts to fallback.
- **Newlines in input** → collapsed to spaces.
- **Over-length input** → impossible to submit (modal `max_length=100`).
- **Markdown in the name** → allowed as-is. Discord embed titles render a subset of markdown verbatim. No sanitization beyond newline collapse.
- **Duplicate names across events** → allowed. The fallback `Event #{db_id}` is the actual identifier; `event_name` is a pure display label.
- **Pre-existing events without the key** → `.get("event_name")` returns `None` → fallback path. No migration script needed.
- **Renaming during voting or after completion** → permitted. All surfaces (embed, thread name, log prefix) update on the next render; Discord thread names update live when patched via the API.

## Out of scope

- **Voting poll question** — unchanged. The embed sitting directly above the poll already shows the event's identity.
- **`/history` entries** — unchanged. History rows are dated; adding the name expands a long-term data shape without a clear benefit.
- **Server-wide event naming defaults** (e.g., "always prefix with weekday") — not in this work; would be a follow-up if requested.
- **Auto-generated name suggestions** — not in this work.

## File-level change summary

| File | Change |
|------|--------|
| `DebugScriptHelper/bot.py` | Add 4th `TextInput` to `EventScheduleModal`; thread the name through `EventCreateConfirmView` and `_finalize_event_creation`; add `string` type to DM edit dispatch + follow-up view; add `event_name` to `_EDIT_PROPERTIES`; update admin panel embed title; update voting thread name builder; update log-post helper to prefix with `display_name`; update DM edit header (`_active_edit_sessions` embed builder). |
| `DebugScriptHelper/database.py` | Add `event_name` kwarg + `"event_name": None` slot to `build_default_event()` (database.py:671). JSON blob storage needs no schema change. |
| `DebugScriptHelper/utils.py` | Add `display_name(event, *, lang)`; replace four `embed.title_*` lookups in the embed builder with the helper. |
| `DebugScriptHelper/i18n.py` | Add `event.fallback_name`, `event.wizard_name_label`, `event.wizard_name_placeholder`, `edit.prop.event_name`, `edit.string_prompt`. Update `thread.voting_name` and `roles.picker_desc.*` to interpolate `{event_label}`. Remove `embed.title_suggestion`, `embed.title_voting`, `embed.title_completed`, `embed.title_created`. |

## Testing notes

Cases the implementation plan should exercise:

1. Create event with a name → embed title, thread name, log prefix, admin panel all show the name.
2. Create event without a name → all surfaces show `Event #{db_id}`.
3. Rename via DM edit in each phase (`created`, `suggestions_open`, `suggestions_closed`, `voting`, `completed`) → all surfaces update on next render; no `edit.locked_phase` block.
4. Clear the name via DM edit (submit empty) → reverts to fallback.
5. Submit name with leading/trailing whitespace and embedded newlines → stored normalized.
6. Pre-existing event in the DB (no `event_name` key) renders correctly via fallback.
7. Two concurrent events with the same name → both render the same title; the fallback `Event #{id}` differs.
8. Name of exactly 100 chars round-trips through embed, thread name, and log prefix without truncation.
