## File overview

> Updated 2026-06-19 for the refreshed reference data. The SuperMod file was renamed
> `spm_layers.json` → **`supermod_layers.json`** (and `spm_layers_minimal.json` removed).

| File | Total layers | Independent maps |
|------|-------------|-----------------|
| `layers.json` | 271 | **27** |
| `supermod_layers.json` | 248 | **46** |

Both files share the same JSON schema: `{ "DefaultGameSettings": {...}, "Maps": [...], "Units": ..., "Roles": ..., "MeleeWeapons": ... }`. Each entry in `Maps` is a layer. Independent maps are identified by the `mapName` field (or `mapId` as fallback). These files are **reference/analysis only — not used at runtime** (the bot fetches its own copy from the configured `LAYERS_JSON_URL` sources and caches it in the `layer_cache` table).

---

## `layers.json` — 27 independent maps

Al Basrah, Anvil, Black Coast, Chora, Combat Outpost Summit, Fallujah, Fool's Road, Goose Bay, Gorodok, Harju, Jensen's Range, Kamdesh Highlands, Kohat Toi, Kokan, Lashkar Valley, Logar Valley, Manicouagan, Mestia, Mutaha, Narva, Pacific Proving Grounds, Sanxian Islands, Skorpo, Sumari Bala, Tallil Outskirts, Tutorials, Yehorivka

Gamemodes: AAS 44, Invasion 44, RAAS 39, Skirmish 33, Training 32, Fireteam 31, TerritoryControl 18, Seed 14, Insurgency 9, Destruction 4, Tutorial 2, Lobby 1.

---

## `supermod_layers.json` — 46 independent maps

`mapName` uses an `SPM | <Map>` (and `SPM | GoingDark | <Map>`) prefix for 39 maps, plus 7 plain names.

**SPM-prefixed (24, standard SuperMod):** Al Basrah, Anvil, Black Coast, Black Coast | Half Map, Chora, Fallujah, Fool's Road, Goose Bay, Gorodok, Gorodok | Half Map, Harju, Kamdesh Highlands, Kohat Toi, Kokan, Lashkar Valley, Manicouagan, Mestia, Mutaha, Narva, Sanxian Islands, Skorpo, Sumari Bala, Tallil Outskirts, Yehorivka

**SPM | GoingDark | … (15):** Anvil, Chora, Fallujah, Gorodok, Harju, Kamdesh Highlands, Kohat Toi, Kokan, Mestia, Mutaha, Narva, Skorpo, Sumari Bala, Tallil Outskirts, Yehorivka

**Plain mapName (7):** Chora, Douentza, Johat Boi, Mekong, Mogadishu, SU_KhoKhan, Supermod_JensensRange

Gamemodes: Invasion 49, AAS 48, RAAS 47, RVAAS 22, Frontline 21, Seed 13, RAAS_PreCap 8, AAS_PreCap 7, Skirmish 7, Training 24, RVAAS_PreCap 2. **New since the previous version:** `AAS_PreCap`, `RAAS_PreCap`, `RVAAS_PreCap` (PreCap variants, symmetric); `CAH` is gone.

---

## Schema fields the bot actually consumes

These are the only parts of the JSON read by `_cache_source_layers()` (bot/bot.py) into the `layer_cache` table; everything else is ignored (see next section).

- `Maps[]` — the list of layers (the only top-level block iterated).
- `Units` (top-level dict) — **only** `factionName`, `factionID`, `alliance` per entry are used, to attach a display name + alliance to each faction.
- Per layer:
  - `rawName` (falls back to `Name`/`Map`) — the unique cache key.
  - `mapName` (falls back to `Map`) — primary map-grouping key; `mapId` is the fallback grouping key.
  - `gamemode` — AAS, RAAS, RVAAS, Invasion, Insurgency, Destruction, Frontline, TerritoryControl, Skirmish, Seed, Training, Fireteam, Tutorial, Lobby, AAS_PreCap, RAAS_PreCap, RVAAS_PreCap.
  - `layerVersion` — **optional** (present on 198/271 `layers.json` and only 35/248 `supermod_layers.json`); when absent the bot parses `_v<N>` out of `rawName`.
  - `mapSize` — string like `"4.0x4.0 km"`; parsed to km for the suggest map-size buckets (small/medium/large).
  - `factions[]` — only `factionId`, `defaultUnit`, `availableOnTeams`, `types` are read. The cache renames `types` → `unitTypes: [{type, name}]`, prepends the `defaultUnit`'s type (e.g. `CombinedArms`), and adds the `alliance`/`factionName` from `Units`.
  - `teamConfigs.team1`/`team2` — **only `allowedAlliances`** is read.

> `Name` is **not unique** — `layers.json` has two distinct "Fallujah AAS v1" entries (a 16-faction standard one and a 2-faction variant). The bot keys by the unique `rawName`.

---

## Other data present in the files but NOT used by the bot

Everything below is in the JSON (so it is available for future features) but is currently ignored. Listed by location.

### Unused layer-level fields (each entry in `Maps`)

- `levelName`, `fName` — duplicate internal identifiers (normally equal to `rawName`).
- `commander` — bool; whether the Commander role is enabled on the layer. Not read or cached.
- `separatedFactionsList` — bool, present on **every** layer; flags Invasion-style asymmetric team rosters. **Not stored in the runtime cache** — the Mirror Match code re-derives the same thing from single-team `availableOnTeams` (`_layer_is_separated`), an exact proxy (0 disagreements across all 519 layers).
- `biome` — environment tag, e.g. `DESERT`, `FOREST`.
- `persistentLightingType` — time-of-day/lighting variant, e.g. `Daytime`, `Night`, `Dusk`.
- `lightingLevel` — string, frequently empty.
- `minimapTexture` — asset name of the minimap image (e.g. `T_AlBasrah_Minimap`).
- `minimapCornersPosition` — `{ min, max }` world-coordinate bounds of the minimap.
- `mapTextureCorners` — list of 2 corner dicts geo-referencing the map texture.
- `objectives` — dict of capture points / flags keyed by ordered name (e.g. `00-Team1Main`, `01-Shu'Aiba`, … `100-Team2Main`); each value is `{ name, objectName, pointPosition, location_x, location_y, location_z }`. The full flag/lattice layout.

### `teamConfigs.team1` / `teamConfigs.team2` (only `allowedAlliances` is used)

Each team object also carries:

- `defaultFactionUnit` — default unit object for the team (e.g. `USMC_LO_CombinedArms`).
- `index` — team number (1 / 2).
- `playerPercent` — player-count split (e.g. 50).
- `tickets` — starting tickets. Symmetric on AAS (250/250), asymmetric on Invasion (e.g. 250/800).
- `disabledVeh` — bool, vehicles disabled.
- `isAttackingTeam` / `isDefendingTeam` — attacker/defender flags. On symmetric modes both teams are `attacking=true`; on Invasion/Insurgency team 2 is `defending=true`. **A direct per-layer asymmetry signal** that could complement (or replace) `separatedFactionsList`/`availableOnTeams` detection.
- `allowedFactionUnitTypes` — per-team whitelist (~15–17) of allowed unit types, in a **more granular, upper-snake token format** than `factions[].types`: e.g. `COMBINED_ARMS`, `MECHANIZED`, `MECHANIZED_WHEELED`, `ARMORED`, `ARMORED_RECON_WHEELED`, `INFANTRY_AIR_MOBILE`, `SUPPORT`, `SPECIAL_FORCES`. The bot instead derives selectable unit types from `factions[].types`; this field is the authoritative per-team source if finer control is ever needed.

### Top-level `Units` (dict keyed by unit object name, e.g. `USMC_LO_CombinedArms`)

The bot reads only `factionName`, `factionID`, `alliance`. Each entry also has:

- `unitObjectName`, `shortName`, `type` (unit category token, e.g. `COMBINED_ARMS`), `displayName` (flavour name, e.g. "31st Marine Expeditionary Unit"), `description`, `unitBadge`.
- `actions`, `intelOnEnemy`, `useCommanderActionNearVehicle`, `hasBuddyRally` — gameplay attributes.
- `roles` — list of role `rowName`s available to the unit.
- `vehicles` — full vehicle layout; each `{ name, rowName, type, count, delay, respawnTime, vehType, spawnerSize, icon, classNames, tags, spawnCommands }`.
- `characteristics` — list of `{ key, description }` tags (e.g. `NoSpecial`).

#### Resolving a vehicle list from a suggestion (if a vehicle-info feature is built)

A suggestion records `team{N}_faction` + `team{N}_unit` (friendly type). To reach the right `Units` entry:
1. **Default loadout** → the layer's `factions[].defaultUnit` (per team) **is** the exact `Units` key.
2. **Other unit types** → substitute the type token in that `defaultUnit` key (works for vanilla).
3. **SuperMod fallback** (required): SuperMod keys carry per-type regiment codes (`FRA10_LO_AirAssault_8RPIMA_Boats`) and some **alias factions** (FAF10/USAF/RAF) point `defaultUnit` at a *base* faction's objects (FRA10/USA/RGF). Match on the `defaultUnit`'s **base+prefix stem + type token**, and do **not** filter on the Units `factionID`.

The prefix encodes team role (`LO`=attacker, `LD`=defender), so attacker/defender get different vehicle layouts. Verified coverage with this resolver: **100% of real vote-mode combos** (AAS/RAAS/RVAAS/Invasion/Insurgency/Destruction/Frontline/TC/PreCap) resolve in both files, 0 wrong-type resolutions across 52,422 combos. A naïve substitute-only resolver only reaches ~81% on SuperMod. Gaps exist only for LightInfantry on Skirmish/Fireteam/Training (no such unit object upstream). Each vehicle: `name`, `vehType` (TRAN/LOGI/MRAP/APC/IFV/MBT/TD/RSV/AH/UH/boat), `count`, `delay` (spawn min), `respawnTime` (min).

### Other top-level blocks (entirely unused)

- `Roles` — list (~605) of every kit/role across all factions: `{ rowName, displayName, inventory: [ … ] }`, where `inventory` is the role's weapon/equipment loadout.
- `MeleeWeapons` — list (~8) of melee-weapon blueprint name strings.
- `DefaultGameSettings` — `{ ProjectName: "Squad", ProjectVersion }` build/metadata.

---

## Team symmetry & Mirror Match (relevant to the per-event Mirror Match toggle)

Unit-type availability is a function of **(layer, faction, team)**: `factions[].types` is the unit-type pool for that faction, gated by `availableOnTeams`.

- **Asymmetric gamemodes** (the two teams have different unit-type pools — e.g. `AirAssault` is attacker-only): **`Invasion`, `Insurgency`, `Destruction`, `Frontline`**. A symmetric "mirror" (both teams same unit type) is frequently impossible there, so Mirror Match exempts them (`MIRROR_INCOMPATIBLE_GAMEMODES` in `bot/bot.py`).
- `separatedFactionsList == true` ⟺ at least one faction has single-team `availableOnTeams` (verified: **0 disagreements across all 519 layers**). The runtime cache does **not** store `separatedFactionsList`, so `_layer_is_separated()` detects separation via single-team `availableOnTeams` instead — an exact proxy.
- **Symmetric (mirror-compatible) gamemodes:** AAS, RAAS, RVAAS, TerritoryControl, AAS_PreCap, RAAS_PreCap, RVAAS_PreCap, etc. On these, every faction is `availableOnTeams: [1,2]`. A few SuperMod layers still have a unit type fielded by a single faction (e.g. `SU_TLF / CombinedArms`); Mirror Match drops those from the Team 1 options so Team 2 always has a mirror faction.
