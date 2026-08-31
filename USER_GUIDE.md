# User Guide — Squad Layer Vote Bot

## For Players

### Suggesting a Layer

1. Find the channel with the active Layer Vote event
2. Click the **"Suggest Layer"** button on the event embed
3. Follow the dropdown steps:
   - **Step 1**: Select a map
   - **Step 2**: Select a game mode (e.g., AAS v1, RAAS v2, Invasion)
   - **Step 3**: Select Team 1 faction and unit type
   - **Step 4**: Select Team 2 faction and unit type
   - **Step 5**: Confirm your suggestion — the preview shows each team's **vehicle layout** plus an **🗺️ Open in SquadCalc** link to inspect the layer
4. Your suggestion appears in the event embed

> **Mirror Match events:** if the organizer enabled Mirror Match, the Team 1 unit-type dropdown is labelled **"(Mirror Match!)"** and Team 2 automatically uses the **same unit type** — you only pick Team 2's faction. It does not apply to Invasion, Insurgency, Destruction or Frontline layers (those are suggested normally, with a notice).

### Viewing Your Suggestions

Click the **"Info"** button on the event embed to see:
- Current event phase
- How many suggestions you've used
- Your submitted suggestions
- Recent winners in this channel (with the date each was decided)

It also has a **dropdown to pick any suggested layer** and view its full per-team **vehicle layout** (counts and types for both teams).

### Removing Your Suggestion

While suggestions are open, click the **"Remove Suggestion"** button on the event embed to take back one of your own picks:

1. Pick the suggestion to remove from the dropdown (only your own are shown)
2. Confirm

Removing frees the slot, so you can suggest again (up to the per-user limit). There is a per-event cap on how many of your own suggestions you may remove — once you hit it, the button stops working for you. The organizer sets this cap (it can be `0` to disable removal entirely).

### Voting

When the admin starts a vote, a Discord poll appears in a dedicated **voting thread** for the event (public for open events, private for restricted ones). Use the **Join Voting** button on the event embed to jump to it, then vote for your preferred layer.

### Viewing Past Winners

Use `/history` to see the winners of previous events.

---

## For Organizers

### Initial Setup

1. Run `/setup` and select:
   - **Organizer Role**: The role that can manage events
   - **Log Channel**: Where bot logs are sent
   - **Language**: English or German
2. Run `/refresh_layers` to load layer data (happens automatically on first start)

### Configuring the Bot

Per-event settings live in the **Admin → Edit Event** DM dialog (open the event embed's **Admin** panel, then **Edit Event**). From there you can adjust:

| Setting | What it does |
|---------|-------------|
| Game modes | Choose which game modes are available (AAS, RAAS, etc.) |
| Blacklists | Block maps, factions, or unit types from being suggested |
| Suggestion limits | Max suggestions per user / total, and history blocking |
| Voting parameters | Voting duration, multiple-choice voting, etc. |
| Mirror Match | Require both teams to use the same unit type — factions may differ (symmetric modes only) |

These are snapshotted per event when it is created, so changing one event never affects another. To gate **who** may participate, use **Admin → Edit Allow-list**.

### Setting Guild-Wide Defaults (`/config_defaults`)

`/config_defaults` (organizer-only) opens a DM dialog that is **identical to Admin → Edit Event**, but it edits the **guild-wide defaults** that every new event starts from instead of a specific event.

Fields available in the dialog:

| Setting | What it does |
|---------|-------------|
| Game modes | Which modes are enabled by default (AAS, RAAS, etc.) |
| Blacklisted maps | Maps blocked from suggestions in new events |
| Blacklisted factions | Factions excluded from suggestions |
| Blacklisted units | Unit types excluded from suggestions |
| Max suggestions per user | Default per-user suggestion limit (1–10) |
| Max total suggestions | Default total suggestion cap (1–25) |
| Max self-removals per user | Default limit for how many times each user may remove their own suggestions (0 disables the Remove Suggestion button) |
| History lookback | How many past events to block winning layers from |
| Layer sources | Which layer data sets to draw from (standard, supermod, etc.) |
| Voting duration | Default poll duration |
| Max voting layers | Default maximum number of layers in the poll |
| Multiple-choice voting | Whether the poll allows multiple votes by default |
| Mirror Match | Whether Mirror Match is on by default |
| Suggestion duration | Default suggestion phase length |
| Suggestion start offset | Default offset before suggestions open |

> **Important:** changes made here apply only to **newly created events**. Existing events keep the snapshot they captured at creation and are not affected. The one exception is the **layer-source cap**: the bot applies that live across all events.

### Running an Event

**Step 1: Create**
```
/create_layer_suggestion
  event_name: Friday Night Fights        (optional — embed title; defaults to "Event #ID")
  suggestion_start: 05.04.2026 18:00     (optional — auto-opens at this time)
  voting_duration_hours: 24              (optional — how long the poll runs)
```

The event name appears as the embed title, in the voting thread name (for gated events), in the log channel, and in the admin panel. Leave it empty to fall back to `Event #ID`. You can rename the event later via the **Edit Event** DM dialog (see Admin Panel below).

**Step 2: Open Suggestions**
- Wait for the scheduled time, or
- Click **Admin → Open Suggestions** on the event embed to open immediately

**Step 3: Collect Suggestions**
Users click "Suggest Layer" and submit their picks.

**Step 4: Close Suggestions**
- Click **Admin → Manage Suggestions → Close Suggestions** (suggestions can also auto-close at the deadline when there are more suggestions than vote slots)
- Closed too early? Click **Admin → Reopen Suggestions** to accept picks again. It stays open until you close it again (no auto-close timer).

**Step 5: Select Layers & Start Voting**
- Click **Admin → Select for Vote**
- Pick specific layers from the dropdown, or click **Random** to select random layers
- **Confirm** your selection — this creates the Discord poll, which runs for the configured duration

  (When there are no more suggestions than vote slots, the bot can skip this step and start the poll automatically at the suggestion deadline.)

**Step 6: End & Results**
- Wait for the poll to expire naturally, or click **Admin → End Vote** to end early
- The winner — and the `AdminChangeLayer` command to set it — is saved to history automatically

### Admin Panel

Click the **"Admin"** button on the event embed for quick actions:
- Open suggestions
- **Manage Suggestions** — while suggestions are open, this sub-panel holds **Close Suggestions** and **Remove Suggestion** (remove someone else's pick); **Back** returns to the panel
- Reopen suggestions, and remove a suggestion, once suggestions are closed
- Select layers for voting (confirming starts the poll)
- End voting
- Delete the event
- **Edit Event** — opens a DM dialog where you can rename the event and tweak per-event config (blacklists, voting duration, max suggestions, **Max Self-Removals per User**, etc.). Pick **Event Name** from the dropdown; submit empty to revert to `Event #ID`. **Max Self-Removals per User** controls how many times each player may remove their own suggestion (`0` disables the player-facing **Remove Suggestion** button).
- **Edit Allow-list** — set which roles/users may participate. This lives on the event embed's Admin panel (not in the Edit Event DM dialog), because Discord's role/user pickers don't work inside DMs.

### Viewing Settings

There is no `/settings` command. Server-wide values (organizer role, log channel, language) are set with `/setup` (and `/set_organizer_role`, `/set_language`, `/set_log_channel`); guild-wide event defaults are managed with `/config_defaults`; per-event configuration is shown and edited in the **Admin → Edit Event** dialog.

---

## FAQ

**Q: How many layers can be in the vote?**
A: Maximum 10 (Discord poll limit).

**Q: What if no factions are available for a map?**
A: Check your blacklist settings — you may have blocked too many factions.

**Q: Can I have multiple events in one server?**
A: Yes, one event per channel.

**Q: How does history blocking work?**
A: If a layer was suggested in one of the last N events (configurable), it cannot be suggested again. The exact combination (map + mode + factions + units) must match.

**Q: What maps are excluded by default?**
A: Jensen's Range, Tutorial, and Training maps are automatically excluded during layer import and never appear in suggestions.

**Q: What is Mirror Match?**
A: An optional per-event setting (organizer enables it in **Admin → Edit Event**) that requires both teams to field the **same unit type** — e.g. both Mechanized — though the factions can differ. Team 2's unit type is set automatically to match Team 1's. It applies only to symmetric modes; Invasion, Insurgency, Destruction and Frontline are exempt (those layers are suggested normally).

**Q: Where does the vehicle list come from?**
A: It's read from the same SquadLayerList data, per faction/unit/team. It reflects the layer's assigned loadout, so an attacking and defending team can have different vehicles. If a layer's loadout has no vehicles for a unit, none are shown.
