# Squad Layer Vote Bot

A Discord bot for collecting Squad layer suggestions from users and running votes on them using Discord's native poll feature.

## Features

- **Layer Suggestions**: Users suggest layers via interactive dropdown menus (Map > Mode > Factions > Units)
- **Layer Data**: Pulls from [SquadLayerList](https://github.com/fantinodavide/SquadLayerList) and caches locally
- **Vehicle Info**: Shows each team's vehicle layout (named vehicles + counts) on the suggestion confirm screen, via the **Info** button (pick any suggested layer), and on the winning embed
- **Mirror Match** *(per-event)*: Optionally force both teams to use the same unit type (factions may differ); exempts asymmetric modes (Invasion, Insurgency, Destruction, Frontline)
- **Admin Blacklists**: Block maps, factions, unit types, or gamemodes
- **Configurable Gamemodes**: Admin selects which gamemodes are available (AAS, RAAS, Invasion, TC, Destruction, Insurgency)
- **Discord Native Polls**: Voting uses Discord's built-in poll system (max 10 options)
- **History Blocking**: Prevents re-suggesting layers from recent events
- **Multi-Language**: English and German (i18n)
- **Persistent Embeds**: Buttons survive bot restarts

## Event Cycle

Events are driven from the **Admin panel** (the `Admin` button on the event embed), not slash commands. The background scheduler can also advance phases automatically based on the event's timers.

1. Organizer creates an event with `/create_layer_suggestion` (creation wizard)
2. Suggestion phase opens — automatically at the scheduled start time, or manually via **Admin → Open Suggestions**
3. Users suggest layers by clicking the **Suggest Layer** button
4. Suggestions close — automatically at the deadline, or manually via **Admin → Close Suggestions** (an organizer can reopen with **Reopen Suggestions**)
5. Organizer picks or randomizes the layers for voting via **Admin → Select for Vote** (skipped automatically when suggestions ≤ vote slots)
6. Voting runs as a Discord native poll (gated events get a private thread)
7. Poll ends — automatically at its duration, or manually via **Admin → End Vote** — and the winner (plus its `AdminChangeLayer` command) is saved to history
8. Repeat

## Commands

discord.py registers **13 slash commands**, in three permission tiers. Per-event configuration (gamemodes, blacklists, suggestion limits, voting parameters) and phase transitions are **not** slash commands — they live in the **Admin panel** and the **Edit Event** DM dialog (see [Interactive Buttons](#interactive-buttons-on-event-embed)).

### Setup (Discord Admin)

| Command | Description |
|---------|-------------|
| `/setup` | Initial server setup (organizer role, log channel, language) |
| `/set_organizer_role` | Change the organizer role |
| `/set_language` | Change bot language (en/de); also refreshes active embeds |
| `/set_log_channel` | Change the log channel |
| `/sync` | Force sync slash commands |

### Event & History Management (Organizer)

| Command | Description |
|---------|-------------|
| `/create_layer_suggestion` | Create a new layer vote event in the channel (creation wizard) |
| `/delete_event` | Delete the current event in the channel |
| `/update` | Refresh all event embeds in this server |
| `/refresh_layers` | Re-fetch layer data from GitHub |
| `/history_add` | Manually add a previously played layer to the history |
| `/history_remove` | Remove an entry from the voting history |
| `/config_defaults` | Edit the guild-wide defaults new events start from (opens the same DM dialog as Admin → Edit Event); changes affect **only newly created events** |

### User

| Command | Description |
|---------|-------------|
| `/history` | View past winning layers |

### Interactive Buttons (on event embed)

| Button | Description |
|--------|-------------|
| Suggest Layer | Start the layer suggestion dropdown flow |
| Info | View event info, your suggestions and recent winners — and pick any suggested layer to see its full per-team vehicle layout |
| Admin | Open the admin panel (organizer only) |

The **Admin panel** holds the per-event actions that used to be slash commands: **Open / Close / Reopen Suggestions**, **Select for Vote**, **End Vote**, **Edit Event** (a DM dialog for gamemodes, blacklists, suggestion limits, voting parameters, and the Mirror Match toggle), **Edit Allow-list** (gate the event to specific roles/users), and **Delete Event**. Gated events also surface a **Join Voting** button on the poll.

## Installation

### Docker (Recommended)

```bash
cp .env.dist .env
# Edit .env and set DISCORD_BOT_TOKEN
docker-compose up -d
```

### Manual

```bash
pip install -r requirements.txt
cp .env.dist .env
# Edit .env and set DISCORD_BOT_TOKEN
cd bot
python bot.py
```

> Note: a local `python bot.py` from inside `bot/` writes the SQLite DB to `bot/data/layer_vote.db` (cwd-relative). Set `DB_PATH` to point at the repo-root `data/` dir if you want to share the Docker DB locally, e.g. `DB_PATH=../data/layer_vote.db python bot.py`.

## First-Time Setup (in Discord)

1. Run `/setup` with organizer role, log channel, and language
2. Run `/refresh_layers` to populate the layer cache (auto-fetched on first start)
3. Optionally run `/config_defaults` to set the guild-wide defaults that new events will start from (gamemodes, blacklists, voting parameters, etc.)
4. Create an event with `/create_layer_suggestion`
5. Open the event's **Admin → Edit Event** dialog to tweak that event's config if needed (optional — sensible defaults apply)
6. Optionally gate the event to specific roles/users via **Admin → Edit Allow-list**

## Configuration

### Environment Variables (.env)

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | Discord bot token |
| `ADMIN_IDS` | No | Comma-separated bot superadmin user IDs |
| `DEBUG_MODE` | No | Enable debug logging (true/false) |
| `LAYERS_JSON_URL` | No | Custom URL for layer data |
| `PUID` | No | Docker user ID (default: 1000) |
| `PGID` | No | Docker group ID (default: 1000) |

### Guild Settings (via `/setup`, `/config_defaults`, and the Admin → Edit Event dialog)

| Setting | Default | Description |
|---------|---------|-------------|
| Organizer Role | — | Required during `/setup` |
| Log Channel | — | Required during `/setup` |
| Language | en | en or de |
| Allowed Gamemodes | AAS, RAAS, Invasion, TC, Destruction, Insurgency | Which modes appear in suggestions |
| Blacklisted Maps | — | Jensen's Range, Tutorial, and Training maps are excluded at import |
| Blacklisted Factions | — | Factions excluded from suggestions |
| Blacklisted Units | — | Unit types excluded from suggestions |
| Max Suggestions/User | 2 | 1-10 |
| Max Total Suggestions | 25 | 1-25 (hard cap due to Discord dropdown limit) |
| History Lookback | 3 | Block layers from last N events |

## Data Structure

### Suggestion Object

```json
{
  "id": "abc12345",
  "user_id": "123456789",
  "user_name": "PlayerName",
  "map_name": "Al Basrah",
  "gamemode": "AAS",
  "layer_version": "v1",
  "team1_faction": "USMC",
  "team1_unit": "CombinedArms",
  "team2_faction": "RGF",
  "team2_unit": "Mechanized",
  "raw_name": "AlBasrah_AAS_v1",
  "suggested_at": "2026-03-30T12:00:00"
}
```

## Project Structure

```
squad-event-map-layer-vote/
├── bot/
│   ├── bot.py              # Main bot: commands, views, background tasks
│   ├── config.py           # .env loading, constants
│   ├── database.py         # SQLite with JSON blobs
│   ├── i18n.py             # Translation strings (de/en)
│   └── utils.py            # Embed builders, formatting helpers
├── data/                   # SQLite DB (Docker volume)
├── reference/              # Local reference layer data (not used at runtime)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.dist
├── .gitignore
├── README.md
└── USER_GUIDE.md
```
