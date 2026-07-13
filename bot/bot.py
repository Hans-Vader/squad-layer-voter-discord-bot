#!/usr/bin/env python3
"""
Layer Vote Bot — Main bot file.

Handles slash commands, interactive views (buttons, dropdowns),
background tasks for scheduled events, and layer cache management.
"""

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands

import database as db
from config import TOKEN, ADMIN_IDS, EVENT_CHECK_INTERVAL, EVENT_CHECK_INTERVAL_FAST, EVENT_CRITICAL_WINDOW, LAYERS_JSON_SOURCES, DEBUG_MODE, is_excluded_layer
from i18n import t, phase_name
from utils import (
    has_organizer_role, is_guild_admin,
    check_role_gate,
    format_layer_short, format_layer_poll_option, suggestion_matches,
    format_vehicle_list, build_ping_messages,
    build_event_embed, build_squadcalc_url, fit_lines_to_field,
    build_winner_copy_text,
    set_log_channel, send_to_log_channel,
    normalize_event_name,
    EVENT_NAME_MAX_LENGTH,
    display_name,
    truncate_thread_name,
)

logger = logging.getLogger("layer_vote")

if DEBUG_MODE:
    logging.getLogger().setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Token check
# ---------------------------------------------------------------------------
if not TOKEN:
    logger.critical("DISCORD_BOT_TOKEN not set. Exiting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Duration parsing ("60" -> 3600s, "2h" -> 7200s, "1d" -> 86400s)
# ---------------------------------------------------------------------------

_DEFAULT_DURATION_MIN_SECONDS = 60
_DEFAULT_DURATION_MAX_SECONDS = 30 * 86400  # 30 days

# Standard placeholder/example string used wherever a duration is entered.
# Keep in sync with parse_duration_to_seconds — bare number = minutes,
# suffixes m/h/d/w supported.
DURATION_HINT = "60m, 2h, 1d, 1w"


def parse_duration_to_seconds(value: str,
                              min_seconds: int = _DEFAULT_DURATION_MIN_SECONDS,
                              max_seconds: int = _DEFAULT_DURATION_MAX_SECONDS,
                              ) -> Optional[int]:
    """Parse a duration string into seconds. Bare numbers are minutes.

    Suffixes: m (minutes), h (hours), d (days), w (weeks). Result clamped
    to [min_seconds, max_seconds]. Returns None for empty/unparseable
    input or non-positive values.

    Single source of truth for duration input across the bot — every
    slash command, modal field, and edit-dialog validator routes here so
    that "60" / "2h" / "1d" all mean the same thing wherever they appear.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    mult = 60  # default: minutes
    if v.endswith("m"):
        v, mult = v[:-1], 60
    elif v.endswith("h"):
        v, mult = v[:-1], 3600
    elif v.endswith("d"):
        v, mult = v[:-1], 86400
    elif v.endswith("w"):
        v, mult = v[:-1], 7 * 86400
    try:
        seconds = int(float(v) * mult)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return max(min_seconds, min(seconds, max_seconds))


# Max voting duration: two weeks, in hours.
MAX_VOTING_DURATION_HOURS = 2 * 7 * 24  # 336h
_VOTING_MIN_SECONDS = 3600  # 1h — voting field stores hours, smaller values would round to 0
_VOTING_MAX_SECONDS = MAX_VOTING_DURATION_HOURS * 3600


def parse_voting_duration_input(value: str) -> Optional[int]:
    """Parse a voting-duration input into hours, using the unified duration parser.

    Bare numbers are minutes (matching every other duration field in the bot).
    Returns hours (the storage unit for `voting_duration_hours`), clamped to
    [1, MAX_VOTING_DURATION_HOURS]. Inputs below 1h round up to 1h.
    """
    seconds = parse_duration_to_seconds(value, min_seconds=_VOTING_MIN_SECONDS,
                                        max_seconds=_VOTING_MAX_SECONDS)
    if seconds is None:
        return None
    # round to nearest hour, min 1
    return max(1, round(seconds / 3600))


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


# ---------------------------------------------------------------------------
# Map name overrides (shorten long names at import time)
# ---------------------------------------------------------------------------
_MAP_NAME_OVERRIDES = {
    "Kamdesh Highlands": "Kamdesh",
    "Pacific Proving Grounds": "Pacific",
    "Tallil Outskirts": "Tallil",
    "Sanxian Islands": "Sanxian",
    "Lashkar Valley": "Lashkar",
    "Logar Valley": "Logar",
    "Sumari Bala": "Sumari"
}

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True


class LayerVoteBot(commands.Bot):
    async def setup_hook(self):
        # Each active event owns its own message-bound view whose buttons carry
        # the event's db_id. Re-bind one view per active event on startup so
        # button clicks survive bot restarts.
        try:
            for record in db.get_all_active_events_global():
                msg_id = record["event"].get("event_message_id")
                if not msg_id:
                    continue
                lang = db.get_guild_language(record["guild_id"])
                phase = record["event"].get("phase", "created")
                view = _view_for_phase(record["db_id"], phase, lang)
                if view is None:
                    continue
                self.add_view(view, message_id=msg_id)
        except Exception as e:
            logger.warning(f"Failed to re-attach event views on startup: {e}")
        await self.tree.sync()
        logger.info("Slash commands synced and event views re-attached.")


bot = LayerVoteBot(command_prefix="!", intents=intents)

# Per-guild locks for concurrency safety
_guild_locks: dict[int, asyncio.Lock] = {}


def _get_guild_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in _guild_locks:
        _guild_locks[guild_id] = asyncio.Lock()
    return _guild_locks[guild_id]


# ---------------------------------------------------------------------------
# Helpers — precondition checks
# ---------------------------------------------------------------------------

async def check_guild_configured(interaction: discord.Interaction) -> Optional[dict]:
    """Check guild is configured, respond with error if not. Returns settings or None."""
    settings = db.get_guild_settings(interaction.guild_id)
    if settings is None:
        lang = db.get_guild_language(interaction.guild_id)
        await interaction.response.send_message(t("general.guild_not_configured", lang), ephemeral=True)
        return None
    return settings


async def check_organizer(interaction: discord.Interaction, settings: dict) -> bool:
    """Check user has organizer role. Responds with error if not. Returns True if OK."""
    lang = settings.get("language", "en")
    if not has_organizer_role(interaction.user, settings.get("organizer_role_id", 0)):
        await interaction.response.send_message(t("general.requires_organizer", lang), ephemeral=True)
        return False
    return True


async def _resolve_channel_event(interaction: discord.Interaction,
                                 lang: str) -> Optional[int]:
    """For slash commands acting on "the event in this channel": return the
    db_id when exactly one active event lives here. Replies with an error and
    returns None when there are zero (or multiple) — multi-event channels
    must be addressed via the embed buttons, which carry db_id explicitly.
    """
    events = db.get_active_events_in_channel(interaction.guild_id, interaction.channel_id)
    if not events:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return None
    if len(events) > 1:
        await interaction.response.send_message(t("event.multiple_in_channel", lang), ephemeral=True)
        return None
    return events[0]["db_id"]


async def check_admin(interaction: discord.Interaction) -> bool:
    """Check user is a Discord admin. Responds with error if not."""
    if not is_guild_admin(interaction.user):
        lang = db.get_guild_language(interaction.guild_id)
        await interaction.response.send_message(t("general.requires_admin", lang), ephemeral=True)
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# LAYER CACHE — fetch & parse layers.json
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_and_cache_layers() -> int:
    """Fetch layers.json from each configured source URL and populate layer_cache.

    Each source is stored independently in the cache, tagged with its derived
    name (the path segment immediately before /layers.json — e.g. "main",
    "supermod"). Sources that fail (network error, non-200, malformed JSON) are
    logged and skipped. Within a single source, layers with duplicate rawName
    are deduped (last wins).

    Returns the total number of cached layer rows across all sources.
    Raises if no source returned data — leaves the existing cache untouched.
    """
    fetched: list[tuple[str, str, object]] = []  # (source_name, url, payload)
    async with aiohttp.ClientSession() as session:
        for source_name, url in LAYERS_JSON_SOURCES:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "layers.json HTTP %s from source '%s' (%s) — skipping",
                            resp.status, source_name, url,
                        )
                        continue
                    fetched.append((source_name, url, await resp.json(content_type=None)))
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "layers.json fetch failed from source '%s' (%s): %s — skipping",
                    source_name, url, e,
                )

    if not fetched:
        raise RuntimeError("No layer sources returned data — cache not refreshed")

    db.clear_layer_cache()
    db.clear_source_units()
    count = 0

    for source_name, source_url, data in fetched:
        layers_list = data.get("Maps", data) if isinstance(data, dict) else data
        if not isinstance(layers_list, list):
            logger.warning(
                "layers.json from source '%s' (%s) did not contain a list — skipping",
                source_name, source_url,
            )
            continue

        # Build factionID → {alliance, factionName} from the source's Units block —
        # this is the source of truth and covers SuperMod factions (SU_*) the
        # hardcoded ALLIANCE_FACTIONS map doesn't know about.
        faction_meta = _build_faction_meta_map(data)

        # Per-unit vehicle layouts from the same Units block, cached per source
        # for the vehicle-info display (resolved at confirm/info/vote-end time).
        db.upsert_source_units(source_name, _build_units_vehicle_map(data))

        # Within a single source, dedupe by rawName (last wins).
        unique: dict[str, dict] = {}
        for layer in layers_list:
            if not isinstance(layer, dict):
                continue
            raw_name = layer.get("rawName") or layer.get("Name", "")
            if raw_name:
                unique[raw_name] = layer

        count += await _cache_source_layers(source_name, unique.values(), faction_meta)

    return count


# Vehicle fields kept for display (the JSON carries many more per vehicle:
# icon/classNames/tags/spawnCommands — dropped to keep the cached blob small).
# spawnerSize is kept so the formatter can tell boats (spawnerSize "BOAT")
# from other ULTVs (e.g. the Minsk 400 quad bike) — they share vehType "ULTV".
_VEHICLE_DISPLAY_FIELDS = ("name", "vehType", "spawnerSize", "count", "delay", "respawnTime")


def _build_units_vehicle_map(data: object) -> dict:
    """Extract {unitObjectName: [trimmed_vehicle, ...]} from the Units block.

    Only units with a non-empty vehicle list are kept; each vehicle is trimmed
    to the display fields. Keys are the unit-object names (e.g.
    "USMC_LD_Armored", "FRA10_LO_AirAssault_8RPIMA_Boats").
    """
    if not isinstance(data, dict):
        return {}
    units = data.get("Units")
    if not isinstance(units, dict):
        return {}
    result: dict[str, list] = {}
    for key, unit in units.items():
        if not isinstance(unit, dict):
            continue
        vehicles = unit.get("vehicles")
        if not isinstance(vehicles, list) or not vehicles:
            continue
        trimmed = [
            {f: v.get(f) for f in _VEHICLE_DISPLAY_FIELDS}
            for v in vehicles if isinstance(v, dict)
        ]
        if trimmed:
            result[key] = trimmed
    return result


def _build_faction_meta_map(data: object) -> dict[str, dict]:
    """Extract {factionID: {"alliance", "factionName"}} from the source's Units block."""
    if not isinstance(data, dict):
        return {}
    units = data.get("Units")
    if not isinstance(units, dict):
        return {}
    result: dict[str, dict] = {}
    for unit in units.values():
        if not isinstance(unit, dict):
            continue
        fid = unit.get("factionID")
        if not fid:
            continue
        entry = result.setdefault(fid, {"alliance": "", "factionName": ""})
        if not entry["alliance"]:
            alliance = unit.get("alliance") or ""
            if alliance:
                entry["alliance"] = alliance
        if not entry["factionName"]:
            faction_name = unit.get("factionName") or ""
            if faction_name:
                entry["factionName"] = faction_name
    return result


_MAP_SIZE_RE = re.compile(r"(-?[\d.]+)\s*x\s*(-?[\d.]+)", re.IGNORECASE)


def _parse_map_size_km(raw: str) -> Optional[float]:
    """Parse '4.0x4.0 km' / '1.2x1.2 km' → max(width, height) in km.

    Returns None for unparseable, zero, or otherwise unusable values. Negative
    components (one source has '-4.1x4.1 km') are treated as their absolute
    value — same magnitude, just a sign typo upstream.
    """
    if not raw:
        return None
    m = _MAP_SIZE_RE.search(raw)
    if not m:
        return None
    try:
        w, h = abs(float(m.group(1))), abs(float(m.group(2)))
    except ValueError:
        return None
    if w == 0 or h == 0:
        return None
    return max(w, h)


async def _cache_source_layers(source_name: str, layers,
                               faction_meta: dict[str, dict] = None) -> int:
    """Parse and upsert each layer for a single source. Returns count cached."""
    faction_meta = faction_meta or {}
    count = 0
    for layer in layers:
        raw_name = layer.get("rawName") or layer.get("Name", "")
        map_name = _MAP_NAME_OVERRIDES.get(
            layer.get("mapName") or layer.get("Map", ""),
            layer.get("mapName") or layer.get("Map", ""),
        )
        map_id = layer.get("mapId") or ""
        gamemode = layer.get("gamemode") or ""
        layer_version = layer.get("layerVersion") or None
        # Parse version from rawName when layerVersion is missing (e.g. AlBasrah_AAS_v3_CL)
        if not layer_version and raw_name:
            m = re.search(r"_v(\d+)", raw_name)
            if m:
                layer_version = f"v{m.group(1)}"

        if not raw_name or not map_name or not gamemode:
            continue

        if is_excluded_layer(map_id, map_name, gamemode):
            continue

        # Extract factions with their unit types, default unit, and team availability.
        # Entries are kept verbatim (no dedup) because the same factionId can appear
        # once per team with a different defaultUnit (e.g. ADF_LO_* for team1,
        # ADF_LD_* for team2 on Invasion layers).
        factions_data = []
        raw_factions = layer.get("factions") or []
        for fac in raw_factions:
            if isinstance(fac, dict):
                fac_id = fac.get("factionId", "")
                default_unit = fac.get("defaultUnit", "") or ""
                available_on_teams = fac.get("availableOnTeams") or []
                unit_types = []
                # Prepend the default unit (e.g. "CombinedArms") — it's never
                # listed in `types` but is always a valid selection.
                default_type = _extract_default_unit_type(default_unit, fac_id)
                if default_type:
                    unit_types.append({"type": default_type, "name": default_type})
                for ut in fac.get("types", []):
                    if isinstance(ut, str):
                        if ut != default_type:
                            unit_types.append({"type": ut, "name": ut})
                    elif isinstance(ut, dict):
                        ut_type = ut.get("type", "")
                        if ut_type != default_type:
                            unit_types.append({
                                "type": ut_type,
                                "name": ut.get("name", ut_type),
                            })
                if fac_id:
                    meta = faction_meta.get(fac_id, {})
                    factions_data.append({
                        "factionId": fac_id,
                        "factionName": meta.get("factionName", ""),
                        "defaultUnit": default_unit,
                        "availableOnTeams": available_on_teams,
                        "unitTypes": unit_types,
                        "alliance": meta.get("alliance", ""),
                    })
            elif isinstance(fac, str):
                meta = faction_meta.get(fac, {})
                factions_data.append({
                    "factionId": fac,
                    "factionName": meta.get("factionName", ""),
                    "defaultUnit": "",
                    "availableOnTeams": [],
                    "unitTypes": [],
                    "alliance": meta.get("alliance", ""),
                })

        # Extract team alliance restrictions
        team_configs = layer.get("teamConfigs", {})
        t1_alliances = []
        t2_alliances = []
        if isinstance(team_configs, dict):
            t1 = team_configs.get("team1") or team_configs.get("Team1") or {}
            t2 = team_configs.get("team2") or team_configs.get("Team2") or {}
            if isinstance(t1, dict):
                t1_alliances = t1.get("allowedAlliances", [])
            if isinstance(t2, dict):
                t2_alliances = t2.get("allowedAlliances", [])

        db.upsert_layer(
            raw_name=raw_name,
            source=source_name,
            map_name=map_name,
            map_id=map_id,
            gamemode=gamemode,
            layer_version=layer_version,
            factions=factions_data,
            team1_alliances=t1_alliances,
            team2_alliances=t2_alliances,
            map_size_km=_parse_map_size_km(layer.get("mapSize", "")),
        )
        count += 1

    return count


def get_factions_for_team(layer_data: dict, team: int,
                          blacklisted_factions: list[str] = None,
                          blacklisted_units: list[str] = None,
                          exclude_faction: str = None) -> list[dict]:
    """Get available factions for a team, respecting alliance restrictions and blacklists.

    Returns list of dicts: {factionId, unitTypes: [{type, name}]}
    """
    alliances_key = f"team{team}_allowed_alliances"
    allowed_alliances = layer_data.get(alliances_key, [])
    allowed_alliance_set = set(allowed_alliances) if allowed_alliances else set()

    # Fallback alliance → faction mapping for cached rows that predate the
    # per-faction alliance field. New caches store `alliance` directly on each
    # faction (sourced from the JSON's Units block), which covers SuperMod
    # (SU_*) and any other modded factions this map doesn't list.
    ALLIANCE_FACTIONS = {
        "BLUFOR": {"USA", "USMC", "BAF", "CAF", "ADF"},
        "REDFOR": {"RGF", "VDV", "PLA", "PLANMC", "PLAAGF"},
        "INDEPENDENT": {"IMF", "MEI", "TLF", "CRF", "GFI"},
        "PAC": {"PLA", "PLANMC", "PLAAGF"},
    }

    fallback_faction_ids = set()
    if allowed_alliances:
        for alliance in allowed_alliances:
            fallback_faction_ids |= ALLIANCE_FACTIONS.get(alliance, set())

    factions = layer_data.get("factions", [])
    seen_ids = set()
    result = []
    for fac in factions:
        fac_id = fac.get("factionId", "") if isinstance(fac, dict) else fac
        if not fac_id:
            continue
        # Filter by availableOnTeams when present — on layers like Invasion the
        # same factionId appears twice, once per team, with different defaultUnits.
        if isinstance(fac, dict):
            available = fac.get("availableOnTeams") or []
            if available and team not in available:
                continue
        if fac_id in seen_ids:
            continue
        if allowed_alliances:
            fac_alliance = fac.get("alliance", "") if isinstance(fac, dict) else ""
            if fac_alliance:
                if fac_alliance not in allowed_alliance_set:
                    continue
            elif fac_id not in fallback_faction_ids:
                continue
        if blacklisted_factions and fac_id in blacklisted_factions:
            continue
        if exclude_faction and fac_id == exclude_faction:
            continue

        seen_ids.add(fac_id)
        unit_types = []
        default_unit = ""
        faction_name = ""
        if isinstance(fac, dict):
            default_unit = fac.get("defaultUnit", "") or ""
            faction_name = fac.get("factionName", "") or ""
            for ut in fac.get("unitTypes", fac.get("types", [])):
                ut_type = ut.get("type", "") if isinstance(ut, dict) else ut
                if blacklisted_units and ut_type in blacklisted_units:
                    continue
                if ut_type:
                    unit_types.append(ut if isinstance(ut, dict) else {"type": ut, "name": ut})

        result.append({
            "factionId": fac_id,
            "factionName": faction_name,
            "defaultUnit": default_unit,
            "unitTypes": unit_types,
        })
    return result


# Game modes whose two teams have asymmetric unit-type pools (attacker/defender,
# e.g. AirAssault is attacker-only). A "mirror match" — both teams the same unit
# type — is frequently impossible there, so Mirror Match never applies to them.
MIRROR_INCOMPATIBLE_GAMEMODES = {"Invasion", "Insurgency", "Destruction", "Frontline"}


def _layer_is_separated(layer_data: dict) -> bool:
    """True when the layer keeps factions split per team (Invasion-style).

    On separated layers each faction entry is restricted to a single team
    (availableOnTeams == [1] or [2]); symmetric layers list every faction as
    [1, 2]. Used as a safety net so any asymmetric layer is exempt from Mirror
    Match even if its gamemode isn't in MIRROR_INCOMPATIBLE_GAMEMODES.
    """
    for fac in layer_data.get("factions", []):
        if isinstance(fac, dict) and len(fac.get("availableOnTeams") or [1, 2]) == 1:
            return True
    return False


def _is_mirror_compatible(layer_data: dict) -> bool:
    """Whether Mirror Match can be enforced on this layer (symmetric teams)."""
    if (layer_data.get("gamemode") or "") in MIRROR_INCOMPATIBLE_GAMEMODES:
        return False
    return not _layer_is_separated(layer_data)


def _mirror_team2_unit_pool(layer_data: dict, team1_faction: str,
                            blacklisted_factions: list[str] = None,
                            blacklisted_units: list[str] = None) -> set:
    """Unit types at least one *other* faction can field on team 2.

    Excludes team1_faction (Team 2 may not reuse Team 1's faction), so a unit
    type in this pool is guaranteed to have a valid Team 2 mirror faction.
    """
    pool = set()
    for fac in get_factions_for_team(layer_data, 2, blacklisted_factions,
                                     blacklisted_units, exclude_faction=team1_faction):
        for unit in fac["unitTypes"]:
            pool.add(unit["type"])
    return pool


def _resolve_unit_object_key(units_map: dict, default_unit: str,
                             default_type: Optional[str], target_type: str) -> Optional[str]:
    """Resolve the Units-block key for a faction's loadout of `target_type`.

    Verified recipe (see docs/layer_files_reference.md): the faction's
    `defaultUnit` is the exact key for the default loadout; for other types
    substitute the type token, and fall back to matching the same base+prefix
    stem + type token among all keys (handles SuperMod regiment codes like
    FRA10_LO_AirAssault_8RPIMA_Boats and alias factions). NOT filtered on
    factionID — alias factions point defaultUnit at a base faction's objects.
    """
    if not default_unit:
        return None
    if target_type == default_type:
        return default_unit if default_unit in units_map else None
    if not (default_type and default_type in default_unit):
        return None
    sub = default_unit.replace(default_type, target_type, 1)
    if sub in units_map:
        return sub
    idx = default_unit.index(default_type)
    base_prefix = default_unit[:idx]                       # e.g. "FRA10_LO_"
    tail = default_unit[idx + len(default_type):]          # e.g. "_2BB_Boats"
    feat = tail.split("_")[-1] if tail else ""             # map-feature, e.g. "Boats"
    cands = [k for k in units_map
             if k.startswith(base_prefix)
             and k[len(base_prefix):].startswith(target_type)]
    if not cands:
        return None
    best = [k for k in cands if feat and k.endswith(feat)] or cands
    return best[0]


def get_team_vehicles(layer_data: dict, faction: str, team: int,
                      unit_type: Optional[str], units_map: dict) -> list:
    """Return the (trimmed) vehicle list for a faction's chosen loadout on a team.

    Returns [] when the layer/faction/units data is missing or the unit has no
    vehicles. `unit_type` may be the friendly type, None, or "Default" (the
    no-unit-selection path) — all of which fall back to the default loadout.
    """
    if not layer_data or not faction or not units_map:
        return []
    entry = get_faction_entry_for_team(layer_data.get("factions", []), faction, team)
    if not entry:
        return []
    default_unit = entry.get("defaultUnit", "")
    default_type = _extract_default_unit_type(default_unit, faction)
    if unit_type in (None, "", "Default"):
        unit_type = default_type
    key = _resolve_unit_object_key(units_map, default_unit, default_type, unit_type)
    if not key:
        return []
    return units_map.get(key) or []


def _attach_winner_vehicles(winner: dict) -> None:
    """Resolve & store both teams' vehicle lists on a winning suggestion dict so
    the completed-event embed can render them without a live lookup. No-op when
    the layer/units data is unavailable (the embed then omits vehicle fields)."""
    if not isinstance(winner, dict):
        return
    raw_name = winner.get("raw_name")
    if not raw_name:
        return
    source = winner.get("source") or ""
    layer_data = db.get_layer_by_raw_name(
        raw_name, allowed_sources=[source] if source else None)
    if not layer_data:
        return
    # get_team_vehicles handles an empty units_map gracefully ([] = no fields
    # rendered), so no early-return needed when the source has no cached units.
    units_map = db.get_source_units(source)
    winner["team1_vehicles"] = get_team_vehicles(
        layer_data, winner.get("team1_faction"), 1, winner.get("team1_unit"), units_map)
    winner["team2_vehicles"] = get_team_vehicles(
        layer_data, winner.get("team2_faction"), 2, winner.get("team2_unit"), units_map)


def get_unit_types_for_faction(factions: list[dict], faction_id: str,
                               blacklisted_units: list[str] = None,
                               team: int = None) -> list[dict]:
    """Get available unit types for a specific faction.

    When `team` is given, prefers the faction entry whose availableOnTeams
    matches — Invasion-style layers keep separate entries per team and they
    can expose different unit types.
    """
    fallback = None
    for fac in factions:
        fac_id = fac.get("factionId", "") if isinstance(fac, dict) else fac
        if fac_id != faction_id:
            continue
        units = (fac.get("unitTypes", fac.get("types", []))) if isinstance(fac, dict) else []
        if blacklisted_units:
            units = [u for u in units if (u.get("type", "") if isinstance(u, dict) else u) not in blacklisted_units]
        if team is not None and isinstance(fac, dict):
            available = fac.get("availableOnTeams") or []
            if available and team not in available:
                if fallback is None:
                    fallback = units
                continue
        return units
    return fallback or []


def get_faction_entry_for_team(factions: list[dict], faction_id: str,
                               team: int) -> Optional[dict]:
    """Return the faction entry for (factionId, team), or None.

    Prefers an entry listing the team in availableOnTeams; falls back to the
    first matching factionId when no team info is stored (older cache rows).
    """
    fallback = None
    for fac in factions:
        if not isinstance(fac, dict) or fac.get("factionId") != faction_id:
            continue
        available = fac.get("availableOnTeams") or []
        if not available:
            fallback = fallback or fac
            continue
        if team in available:
            return fac
    return fallback


def _resolve_unit_prefix(layer_data: dict, faction_id: str, team: int) -> Optional[str]:
    """Look up the unit prefix (LO, LD, MO, S, …) for a faction on a team."""
    if not layer_data or not faction_id:
        return None
    entry = get_faction_entry_for_team(layer_data.get("factions", []), faction_id, team)
    if not entry:
        return None
    return extract_unit_prefix(entry.get("defaultUnit", ""), faction_id)


def _resolve_faction_name(layer_data: dict, faction_id: str, team: int) -> str:
    """Look up the human-readable factionName for a faction on a team.

    Falls back to "" when the layer data has no entry for that faction.
    """
    if not layer_data or not faction_id:
        return ""
    entry = get_faction_entry_for_team(layer_data.get("factions", []), faction_id, team)
    if not entry:
        return ""
    return entry.get("factionName", "") or ""


def _faction_select_options(factions: list[dict]) -> list[discord.SelectOption]:
    """Build dropdown options for a list of factions (label=factionId,
    description=factionName). Caps at Discord's 25-option limit; description
    is truncated to the 100-char limit and omitted when empty."""
    return [
        discord.SelectOption(
            label=f["factionId"],
            value=f["factionId"],
            description=(f.get("factionName") or "")[:100] or None,
        )
        for f in factions[:25]
    ]


# Loadout-prefix tokens that appear between the faction marker and the unit
# type in a defaultUnit string (e.g. "ADF_LO_CombinedArms"). Discovered by
# scanning both layers.json and spm_layers.json — same set in both, the SPM
# variant just adds extra suffixes (-Boats, _SuperMod, …) after the type.
_UNIT_PREFIX_MARKERS = ("LO", "LD", "MO", "MD", "S", "Seed")

# Canonical unit-type names as the user expects to see them. The SPM source
# decorates the type with extra qualifiers ("CombinedArms-Boats_SuperMod",
# "CombinedArms_2BB_Boats", …) — we collapse those back to the canonical name
# so the dropdown stays clean and dedup against the `types` array works.
_KNOWN_UNIT_TYPES = (
    "CombinedArms", "AirAssault", "Mechanized", "Armored",
    "Motorized", "LightInfantry", "Support", "SpecialForces",
    "AntiTank", "AmphibiousAssault",
)


def extract_unit_prefix(default_unit: str, faction_id: Optional[str] = None) -> Optional[str]:
    """Extract the loadout-prefix token (LO, LD, MO, MD, S, Seed) from a defaultUnit.

    Scans for the marker directly so it doesn't matter whether factionId is
    a prefix of defaultUnit — handles the SPM cases where factionId is
    "SU_ADF" but defaultUnit is "ADF_LO_..." or even "PLAGF_2010_LO_..." with
    an era qualifier between the faction marker and the loadout token.

    `faction_id` is accepted for backward compatibility but is no longer used.
    """
    if not default_unit:
        return None
    for part in default_unit.split("_"):
        if part in _UNIT_PREFIX_MARKERS:
            return part
    return None


def _extract_default_unit_type(default_unit: str, faction_id: Optional[str] = None) -> Optional[str]:
    """Extract the canonical unit-type name (e.g. ``CombinedArms``) from a defaultUnit.

    Generic across both layers.json and spm_layers.json:

      ``ADF_LO_CombinedArms``                -> ``CombinedArms``
      ``ADF_LO_CombinedArms-Boats_SuperMod`` -> ``CombinedArms``  (SPM suffix)
      ``PLAGF_2010_LO_CombinedArms-Boats``   -> ``CombinedArms``  (era qualifier)
      ``UKSF_LO_SpecialForces_Boats``        -> ``SpecialForces``
      ``ADF_S_CombinedArms_Seed``            -> ``CombinedArms``
      ``FSTemplate_IMF``                     -> None  (no marker)

    Strategy: locate the loadout-prefix marker, then match the remainder
    against the canonical type list. `faction_id` is accepted for backward
    compatibility but not used — the SPM source has factionIds like
    ``SU_ADF`` that are not prefixes of ``ADF_LO_...``, which broke the old
    string-prefix approach.
    """
    if not default_unit:
        return None
    parts = default_unit.split("_")
    for i, part in enumerate(parts):
        if part not in _UNIT_PREFIX_MARKERS or i + 1 >= len(parts):
            continue
        tail = "_".join(parts[i + 1:])
        for known in _KNOWN_UNIT_TYPES:
            if tail == known:
                return known
            # Boundary check: the char after the canonical name must be a
            # separator so we don't match "Combined" inside a longer token.
            if tail.startswith(known) and not tail[len(known)].isalpha():
                return known
        # No canonical match — return the first sub-token (split on _ or -).
        return re.split(r"[-_]", tail, maxsplit=1)[0] or None
    return None


def build_admin_change_layer(winner: Optional[dict]) -> Optional[str]:
    """Build the Squad RCON ``AdminChangeLayer`` command for a winning suggestion.

    Format: ``AdminChangeLayer <raw_name> <fac1>+<unit1> <fac2>+<unit2>``.

    Faction tokens are taken verbatim from the suggestion — they already carry
    the ``SU_`` prefix for supermod layers. A unit suffix is always emitted: a
    specific pick is used as-is; a ``Default`` pick resolves to the faction's
    real default loadout from the layer cache (per team, since Invasion layers
    reuse a factionId across teams), falling back to ``CombinedArms``.

    Returns None when there is no winner or no raw layer name to target.
    """
    if not winner:
        return None
    raw_name = winner.get("raw_name")
    if not raw_name:
        return None

    source = winner.get("source") or ""
    layer = db.get_layer_by_raw_name(raw_name, [source] if source else None)
    factions = layer.get("factions", []) if layer else []

    def team_token(faction: Optional[str], unit: Optional[str], team: int) -> Optional[str]:
        if not faction:
            return None
        if unit and unit not in ("Default", "?"):
            return f"{faction}+{unit}"
        resolved = None
        entry = get_faction_entry_for_team(factions, faction, team)
        if entry:
            resolved = _extract_default_unit_type(entry.get("defaultUnit", ""))
        return f"{faction}+{resolved or 'CombinedArms'}"

    t1 = team_token(winner.get("team1_faction"), winner.get("team1_unit"), 1)
    t2 = team_token(winner.get("team2_faction"), winner.get("team2_unit"), 2)
    if not t1 or not t2:
        return None
    return f"AdminChangeLayer {raw_name} {t1} {t2}"


# ═══════════════════════════════════════════════════════════════════════════
# BASE VIEW — grey out components when a dialog goes stale
# ═══════════════════════════════════════════════════════════════════════════

class AutoDisableView(ui.View):
    """A view whose components grey out when it times out.

    `_origin` is the interaction that last rendered this view. On timeout we
    edit that interaction's original (ephemeral) response to push the now
    disabled components, so the user sees the dialog expired instead of getting
    a silent "This interaction failed" on click.

    Multi-step flows reuse one ephemeral message, leaving behind older views
    whose timers keep running. A superseded view must be retired with `.stop()`
    by the callback that navigates away from it (advance to a new step, or
    finalize to `view=None`) so its timer can't later clobber the live message.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._origin: Optional[discord.Interaction] = None

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self._origin is None:
            return
        try:
            await self._origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


def _bind(view: ui.View, interaction: discord.Interaction) -> ui.View:
    """Bind a view to the interaction that renders it so it can grey itself out
    on timeout. Returns the view so it can wrap a constructor inline:
    ``view = _bind(SomeView(...), interaction)``.

    Binding is synchronous (no extra HTTP on the hot path); the only timeout
    cost is one edit when a screen actually expires. Callbacks that navigate
    away from their own view are responsible for retiring it via `.stop()` so a
    superseded timer can't later clobber the live message.
    """
    if isinstance(view, AutoDisableView):
        view._origin = interaction
    return view


# ═══════════════════════════════════════════════════════════════════════════
# PERSISTENT VIEW — Event embed buttons
# ═══════════════════════════════════════════════════════════════════════════

def _add_admin_button(view: ui.View, db_id: int, lang: str) -> None:
    """Attach the standard ⚙️ admin button (routes to the admin panel).

    Shared by every phase view so the label / style / custom_id can't drift.
    """
    btn = ui.Button(
        label=t("button.admin", lang),
        style=discord.ButtonStyle.danger,
        custom_id=f"event_action:admin:{db_id}",
        emoji="⚙️",
    )

    async def _cb(interaction: discord.Interaction):
        await handle_admin_panel(interaction, db_id)

    btn.callback = _cb
    view.add_item(btn)


class EventActionView(ui.View):
    """View attached to a specific event's embed. Buttons: Suggest, Info, Admin.

    Each instance is bound to a single event via db_id, so multiple events
    can coexist in the same channel without their buttons colliding. Views
    are re-attached on bot startup (see LayerVoteBot.setup_hook) by walking
    all active events and binding each view to its event_message_id.
    """

    def __init__(self, db_id: int, lang: str = "en", phase: str = "created"):
        super().__init__(timeout=None)
        self.db_id = db_id

        suggest = ui.Button(
            label=t("button.suggest", lang),
            style=discord.ButtonStyle.primary,
            custom_id=f"event_action:suggest:{db_id}",
            emoji="🗺️",
        )
        suggest.callback = self._suggest
        self.add_item(suggest)

        # Self-removal is only meaningful while suggestions are open, so the
        # button only exists in that phase — once suggestions close it's gone
        # rather than visible-but-rejecting.
        if phase == "suggestions_open":
            remove = ui.Button(
                label=t("button.remove_own", lang),
                style=discord.ButtonStyle.secondary,
                custom_id=f"event_action:remove_own:{db_id}",
                emoji="🗑️",
            )
            remove.callback = self._remove_own
            self.add_item(remove)

        info = ui.Button(
            label=t("button.info", lang),
            style=discord.ButtonStyle.secondary,
            custom_id=f"event_action:info:{db_id}",
            emoji="ℹ️",
        )
        info.callback = self._info
        self.add_item(info)

        _add_admin_button(self, db_id, lang)

    async def _suggest(self, interaction: discord.Interaction):
        await handle_suggest_start(interaction, self.db_id)

    async def _remove_own(self, interaction: discord.Interaction):
        await handle_remove_own_suggestion(interaction, self.db_id)

    async def _info(self, interaction: discord.Interaction):
        await handle_info(interaction, self.db_id)


class VotingPhaseView(ui.View):
    """View attached to a gated event's embed during the voting phase.

    Suggest Layer is gone (suggestion phase ended) and Info is gone
    (suggestions are visible as poll options inside the thread). Replaced
    by Join Voting, which runs the role gate and adds the user to the
    private voting thread on success — also covers the late-joiner case
    where someone gets the allowed role *after* the thread was created.
    """

    def __init__(self, db_id: int, lang: str = "en"):
        super().__init__(timeout=None)
        self.db_id = db_id

        join = ui.Button(
            label=t("button.join_vote", lang),
            style=discord.ButtonStyle.success,
            custom_id=f"event_action:join_vote:{db_id}",
            emoji="🗳️",
        )
        join.callback = self._join
        self.add_item(join)

        _add_admin_button(self, db_id, lang)

    async def _join(self, interaction: discord.Interaction):
        await handle_join_vote(interaction, self.db_id)


class CompletedPhaseView(ui.View):
    """View attached to a completed event's embed.

    Only the Admin button — vote ended, the user-facing actions are all
    behind us. The button still routes to the standard admin panel so the
    organizer can edit metadata or delete the event from the embed
    without touching slash commands.
    """

    def __init__(self, db_id: int, lang: str = "en"):
        super().__init__(timeout=None)
        self.db_id = db_id
        _add_admin_button(self, db_id, lang)


class DrawPendingView(ui.View):
    """View attached to a drawn event's embed while it awaits resolution.

    Three organizer-only buttons — runoff / random / pick manually — bound to the
    event's db_id. Persistent (timeout=None) so `setup_hook` re-attaches it across
    restarts, exactly like the other phase views.
    """

    _BUTTONS = (
        ("draw_runoff", "button.draw_runoff", discord.ButtonStyle.primary, "🔁"),
        ("draw_random", "button.draw_random", discord.ButtonStyle.secondary, "🎲"),
        ("draw_pick", "button.draw_pick", discord.ButtonStyle.secondary, "☝️"),
    )

    def __init__(self, db_id: int, lang: str = "en"):
        super().__init__(timeout=None)
        self.db_id = db_id
        for action, label_key, style, emoji in self._BUTTONS:
            btn = ui.Button(
                label=t(label_key, lang),
                style=style,
                custom_id=f"event_action:{action}:{db_id}",
                emoji=emoji,
            )
            btn.callback = self._make_cb(action)
            self.add_item(btn)

        _add_admin_button(self, db_id, lang)

    def _make_cb(self, action: str):
        async def cb(interaction: discord.Interaction):
            await handle_draw_action(interaction, self.db_id, action)
        return cb


def _view_for_phase(db_id: int, phase: str, lang: str) -> Optional[ui.View]:
    """Return the persistent View for an event in the given phase."""
    if phase == "completed":
        return CompletedPhaseView(db_id, lang)
    if phase == "draw_pending":
        return DrawPendingView(db_id, lang)
    if phase == "voting":
        return VotingPhaseView(db_id, lang)
    return EventActionView(db_id, lang, phase)


# ═══════════════════════════════════════════════════════════════════════════
# SUGGESTION FLOW — Sequential dropdowns in ephemeral messages
# ═══════════════════════════════════════════════════════════════════════════

class SuggestState:
    """Tracks the state of a suggestion flow for a user."""
    __slots__ = ("guild_id", "channel_id", "db_id", "source", "map_name",
                 "mode_raw_name", "gamemode", "layer_version",
                 "team1_faction", "team1_unit", "team2_faction", "team2_unit",
                 "layer_data", "flow", "mirror_match", "mirror_effective")

    def __init__(self, guild_id: int, channel_id: int, flow: str = "suggest",
                 db_id: int = 0):
        self.guild_id = guild_id
        self.channel_id = channel_id
        # db_id of the event this suggestion targets. 0 for the history_add
        # flow, which doesn't bind to an event.
        self.db_id = db_id
        # The layer source the user is suggesting from (e.g. "main", "supermod").
        # Empty string acts as "no source filter" — used for legacy events that
        # predate per-source caching.
        self.source = ""
        self.map_name = None
        self.mode_raw_name = None
        self.gamemode = None
        self.layer_version = None
        self.team1_faction = None
        self.team1_unit = None
        self.team2_faction = None
        self.team2_unit = None
        self.layer_data = None
        # "suggest" = normal event suggestion; "history_add" = manual
        # insertion into voting_history via /history_add.
        self.flow = flow
        # Mirror Match: the event-level toggle (snapshotted at flow start) and
        # the per-suggestion "effective" flag (toggle AND a mirror-compatible
        # layer). Only mirror_effective drives the placeholder/skip/confirm.
        self.mirror_match = False
        self.mirror_effective = False


# Active suggestion sessions: user_id -> SuggestState
_suggest_sessions: dict[int, SuggestState] = {}


def _event_settings(event: dict, settings: dict) -> dict:
    """Merge per-event config with guild settings.

    Per-event keys (see db.EVENT_CONFIG_KEYS) win over guild values, so admins
    editing an event via the DM dialog only affect that event. Falls back to
    guild settings for any key the event doesn't carry (legacy events that
    predate Phase 2, or settings outside the snapshot list like language and
    organizer_role_id).
    """
    merged = dict(settings or {})
    cfg = (event or {}).get("config") or {}
    for key, value in cfg.items():
        merged[key] = value
    return merged


def _state_event_settings(state: "SuggestState") -> dict:
    """Per-suggest-flow settings: merged event config + guild fallbacks.

    SuggestState's mid-flow callbacks don't keep the event in scope, so each
    one re-merges from a fresh DB read. This keeps suggest filters in sync
    with concurrent DM edits to the event's config and avoids stale snapshots.
    """
    settings = db.get_guild_settings(state.guild_id) or {}
    if not state.db_id:
        return settings
    record = db.get_event_by_db_id(state.guild_id, state.db_id)
    return _event_settings(record["event"] if record else {}, settings)


def _resolve_event_sources(event: dict, settings: dict) -> list[str]:
    """Return the list of source names a user may pick from for this event.

    The event's stored `allowed_sources` (chosen by the admin at creation time)
    is the starting point. The guild's `allowed_sources` setting is then
    applied as a live cap — so changes to /config_layer_sources take effect
    immediately for already-active events, instead of being frozen at the
    moment the event was created.

    Falls back to all distinct sources currently in the cache when the event
    has no explicit selection (legacy events that predate this feature).
    """
    explicit = event.get("allowed_sources") or []
    candidate = list(explicit) if explicit else db.get_unique_sources()

    guild_allowed = settings.get("allowed_sources") or []
    if guild_allowed:
        candidate = [s for s in candidate if s in guild_allowed]

    return candidate


async def handle_suggest_start(interaction: discord.Interaction, db_id: int):
    """Start the suggestion flow when user clicks the Suggest button on a
    specific event's embed. The button carries db_id so the right event is
    targeted even when multiple events live in the channel."""
    settings = db.get_guild_settings(interaction.guild_id)
    if not settings:
        await interaction.response.send_message(
            t("general.guild_not_configured", "en"), ephemeral=True)
        return

    lang = settings.get("language", "en")

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    if event.get("phase") != "suggestions_open":
        await interaction.response.send_message(t("suggest.not_open", lang), ephemeral=True)
        return

    # Per-event role/user gate. Same allow-list controls both suggesting
    # and voting — empty list = open.
    if not check_role_gate(event, interaction.user):
        await interaction.response.send_message(t("gate.denied", lang), ephemeral=True)
        return

    # Check max suggestions — read from per-event config first, falling back
    # to guild defaults for legacy events created before Phase 2.
    event_settings = _event_settings(event, settings)
    max_suggestions = event_settings.get("max_suggestions_per_user", 2)
    user_suggestions = [s for s in event.get("suggestions", [])
                        if str(s.get("user_id")) == str(interaction.user.id)]
    if len(user_suggestions) >= max_suggestions:
        await interaction.response.send_message(
            t("suggest.max_reached", lang, max=max_suggestions), ephemeral=True)
        return

    # Check layer cache
    if db.get_layer_cache_count() == 0:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    # Start suggestion flow
    state = SuggestState(interaction.guild_id, interaction.channel_id, db_id=db_id)
    state.mirror_match = bool(event.get("mirror_match", False))
    _suggest_sessions[interaction.user.id] = state

    sources = _resolve_event_sources(event, settings)
    if len(sources) > 1:
        # Show source picker first; the map step runs after the user picks one.
        options = [discord.SelectOption(label=s, value=s) for s in sources[:25]]
        view = _bind(SourceSelectView(options, lang), interaction)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=t("suggest.select_source", lang),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return

    # Single source (or none recorded → no filter): skip the picker.
    state.source = sources[0] if sources else ""
    await _suggest_show_map_step(interaction, state, settings, lang, edit=False)


# Map-size buckets in km (max layer size per map, since skirmish layers reuse
# the mapId with a smaller area). Thresholds chosen so each bucket stays under
# Discord's 25-option Select cap for both layers.json and spm_layers.json.
_SIZE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("small", 3.0),    # < 3.0 km — skirmish / CQB
    ("medium", 4.5),   # 3.0 ≤ size < 4.5 km — standard AAS
    ("large", float("inf")),  # ≥ 4.5 km — full RAAS / big maps
)
_SIZE_BUCKET_KEYS = {
    "small": "suggest.size_small",
    "medium": "suggest.size_medium",
    "large": "suggest.size_large",
}


def _bucket_for_size(size_km: Optional[float]) -> str:
    """Return the bucket key ('small'/'medium'/'large') for a map size in km.
    Maps without a size fall into 'medium' as a safe default."""
    if size_km is None:
        return "medium"
    for key, upper in _SIZE_BUCKETS:
        if size_km < upper:
            return key
    return "large"


def _group_maps_by_size(maps: list[str], sizes: "dict[str, float]") -> "dict[str, list[str]]":
    """Group map names by size bucket. Insertion order is small → medium → large
    so the dropdowns appear in size order regardless of dict iteration."""
    groups: dict[str, list[str]] = {key: [] for key, _ in _SIZE_BUCKETS}
    for m in maps:
        groups[_bucket_for_size(sizes.get(m))].append(m)
    return groups


def _build_map_picker_view(maps: list[str], lang: str,
                           sizes: "dict[str, float]") -> ui.View:
    """Build the map-select view: always split into Small/Medium/Large
    dropdowns by canonical (largest-layer) size, with map counts in every
    placeholder. Falls back to a single flat dropdown only when grouping
    collapses to a single non-empty bucket (e.g. tiny custom sources).
    """
    groups = _group_maps_by_size(maps, sizes)
    non_empty = [(k, v) for k, v in groups.items() if v]
    if len(non_empty) <= 1:
        options = [discord.SelectOption(label=m, value=m) for m in maps]
        placeholder = f"{t('suggest.select_map', lang).rstrip('.')} ({len(maps)})"
        return MapSelectView(options, lang, placeholder=placeholder)
    return GroupedMapSelectView(groups, lang)


async def _suggest_show_map_step(interaction: discord.Interaction, state: SuggestState,
                                 settings: dict, lang: str, edit: bool):
    """Render the map-select dropdown. Used after source pick or when only one source exists."""
    event_settings = _state_event_settings(state) if state.db_id else (settings or {})
    blacklisted_maps = event_settings.get("blacklisted_maps", [])
    source_filter = [state.source] if state.source else None
    maps = db.get_unique_maps(excluded_maps=blacklisted_maps, allowed_sources=source_filter)

    if not maps:
        msg = t("general.error", lang, error="No maps available")
        if edit:
            await interaction.response.edit_message(
                embed=discord.Embed(description=msg, color=discord.Color.red()),
                view=None,
            )
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    sizes = db.get_map_sizes(allowed_sources=source_filter)
    view = _bind(_build_map_picker_view(maps, lang, sizes), interaction)
    desc = t("suggest.select_map", lang)
    if state.source:
        desc = f"**{t('suggest.source_label', lang)}:** {state.source}\n{desc}"
    embed = discord.Embed(
        title=t("suggest.phase_title", lang),
        description=desc,
        color=discord.Color.green(),
    )
    if edit:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SourceSelectView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=600)
        self.add_item(SourceSelect(options, lang))


class SourceSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_source", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        self.view.stop()  # retire this step so its timer can't clobber later steps
        state.source = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        await _suggest_show_map_step(interaction, state, settings or {}, lang, edit=True)


class MapSelectView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], lang: str,
                 placeholder: Optional[str] = None):
        super().__init__(timeout=600)
        self.lang = lang
        select = MapSelect(options, lang, placeholder=placeholder)
        self.add_item(select)


class GroupedMapSelectView(AutoDisableView):
    """Map picker with one MapSelect per size bucket. Used when the map list
    exceeds Discord's 25-option-per-Select cap (typical for the supermod source,
    which has 43+ playable maps).

    Buckets are always Small/Medium/Large (3 dropdowns), comfortably under the
    5-component View cap.
    """

    def __init__(self, groups: "dict[str, list[str]]", lang: str):
        super().__init__(timeout=600)
        self.lang = lang
        for bucket_key, group_maps in groups.items():
            if not group_maps:
                continue
            label = t(_SIZE_BUCKET_KEYS[bucket_key], lang)
            if len(group_maps) > 25:
                logger.warning(
                    "Map size bucket '%s' has %d maps; truncating to 25 (Discord Select limit).",
                    bucket_key, len(group_maps),
                )
            options = [discord.SelectOption(label=m, value=m) for m in group_maps[:25]]
            self.add_item(MapSelect(options, lang, placeholder=f"{label} ({len(group_maps)})"))


class MapSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str,
                 placeholder: Optional[str] = None):
        super().__init__(placeholder=placeholder or t("suggest.select_map", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        self.view.stop()  # retire this step so its timer can't clobber later steps
        state.map_name = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        event_settings = _state_event_settings(state) if state.db_id else (settings or {})
        source_filter = [state.source] if state.source else None

        # Get available modes for this map (within the chosen source, if any)
        modes = db.get_modes_for_map(
            state.map_name,
            allowed_gamemodes=event_settings.get("allowed_gamemodes", []),
            allowed_sources=source_filter,
        )

        if not modes:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=t("suggest.phase_title", lang),
                    description=t("general.error", lang, error="No modes available for this map"),
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        options = [
            discord.SelectOption(label=m["display"], value=m["raw_name"])
            for m in modes[:25]
        ]

        view = _bind(ModeSelectView(options, lang), interaction)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=f"**Map:** {state.map_name}\n{t('suggest.select_mode', lang)}",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class ModeSelectView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=600)
        self.add_item(ModeSelect(options, lang))


class ModeSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_mode", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        self.view.stop()  # retire this step so its timer can't clobber later steps
        raw_name = self.values[0]
        source_filter = [state.source] if state.source else None
        layer_data = db.get_layer_by_raw_name(raw_name, allowed_sources=source_filter)
        if not layer_data:
            await interaction.response.edit_message(
                embed=discord.Embed(description="Layer not found.", color=discord.Color.red()),
                view=None,
            )
            return

        state.mode_raw_name = raw_name
        state.gamemode = layer_data["gamemode"]
        state.layer_version = layer_data["layer_version"]
        state.layer_data = layer_data
        # Mirror Match only applies to symmetric layers; on asymmetric modes the
        # normal independent flow runs (with a notice at the team 1 step).
        state.mirror_effective = state.mirror_match and _is_mirror_compatible(layer_data)

        settings = db.get_guild_settings(state.guild_id)
        await _show_team1_faction_select(interaction, state, settings)


async def _show_team1_faction_select(interaction: discord.Interaction,
                                     state: SuggestState, settings: dict,
                                     notice: str = ""):
    """Render the Team 1 faction dropdown.

    Also the re-entry point when a Mirror Match faction turns out to have no
    mirrorable unit type (`notice` carries the explanation in that case).
    """
    lang = settings.get("language", "en") if settings else "en"
    event_settings = _state_event_settings(state) if state.db_id else (settings or {})
    bl_factions = event_settings.get("blacklisted_factions", [])
    bl_units = event_settings.get("blacklisted_units", [])

    factions = get_factions_for_team(state.layer_data, 1, bl_factions, bl_units)

    # Mirror Match: drop Team 1 factions with no mirrorable unit type so the user
    # can't pick a dead end. A unit type is mirrorable when ≥2 factions field it
    # (Team 2 may not reuse Team 1's faction). Factions with no unit types stay —
    # they take the "Default" path where mirror is a no-op.
    if state.mirror_effective:
        type_count: dict = {}
        for fac in get_factions_for_team(state.layer_data, 2, bl_factions, bl_units):
            for unit in fac["unitTypes"]:
                type_count[unit["type"]] = type_count.get(unit["type"], 0) + 1
        mirrorable = {tp for tp, count in type_count.items() if count >= 2}
        factions = [f for f in factions
                    if not f["unitTypes"]
                    or any(u["type"] in mirrorable for u in f["unitTypes"])]

    if not factions:
        await interaction.response.edit_message(
            embed=discord.Embed(description="No factions available.", color=discord.Color.red()),
            view=None,
        )
        return

    options = _faction_select_options(factions)

    prefix = ""
    if notice:
        prefix = f"{notice}\n\n"
    elif state.mirror_match and not state.mirror_effective:
        prefix = f"{t('suggest.mirror_mode_excluded', lang, mode=state.gamemode)}\n\n"

    mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
    view = _bind(Team1FactionSelectView(options, lang), interaction)
    embed = discord.Embed(
        title=t("suggest.phase_title", lang),
        description=(
            f"{prefix}"
            f"**Map:** {state.map_name}\n"
            f"**Mode:** {mode_str}\n"
            f"{t('suggest.select_team1_faction', lang)}"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class Team1FactionSelectView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=600)
        self.add_item(Team1FactionSelect(options, lang))


class Team1FactionSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_team1_faction", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        self.view.stop()  # retire this step so its timer can't clobber later steps
        state.team1_faction = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        event_settings = _state_event_settings(state) if state.db_id else (settings or {})
        bl_factions = event_settings.get("blacklisted_factions", [])
        bl_units = event_settings.get("blacklisted_units", [])

        # Get unit types for team 1 faction
        units = get_unit_types_for_faction(
            state.layer_data.get("factions", []), state.team1_faction, bl_units, team=1)

        # Mirror Match: only offer unit types that some other faction can also
        # field on team 2, so the user can never reach a dead end on team 2.
        if state.mirror_effective and units:
            pool = _mirror_team2_unit_pool(
                state.layer_data, state.team1_faction, bl_factions, bl_units)
            units = [u for u in units if u.get("type") in pool]
            if not units:
                # This faction has no mirrorable unit type — bounce back to the
                # team 1 faction step with an explanation. (Pre-filtering in
                # _show_team1_faction_select makes this defensive.)
                await _show_team1_faction_select(
                    interaction, state, settings, notice=t("suggest.mirror_no_unit", lang))
                return

        if not units:
            # No unit types — skip to team 2
            state.team1_unit = "Default"
            await _show_team2_faction_select(interaction, state, settings)
            return

        options = [
            discord.SelectOption(label=u.get("type", "?"), value=u.get("type", "?"))
            for u in units[:25]
        ]

        unit_placeholder = t(
            "suggest.select_team1_unit_mirror" if state.mirror_effective
            else "suggest.select_team1_unit", lang)
        hint = f"{t('suggest.mirror_hint', lang)}\n\n" if state.mirror_effective else ""
        mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
        view = _bind(Team1UnitSelectView(options, lang, placeholder=unit_placeholder), interaction)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=(
                f"{hint}"
                f"**Map:** {state.map_name}\n"
                f"**Mode:** {mode_str}\n"
                f"**Team 1:** {state.team1_faction}\n"
                f"{unit_placeholder}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class Team1UnitSelectView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], lang: str,
                 placeholder: str = None):
        super().__init__(timeout=600)
        self.add_item(Team1UnitSelect(options, lang, placeholder=placeholder))


class Team1UnitSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str,
                 placeholder: str = None):
        super().__init__(placeholder=placeholder or t("suggest.select_team1_unit", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        self.view.stop()  # retire this step so its timer can't clobber later steps
        state.team1_unit = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        await _show_team2_faction_select(interaction, state, settings)


async def _show_team2_faction_select(interaction: discord.Interaction,
                                     state: SuggestState, settings: dict):
    """Show team 2 faction dropdown."""
    lang = settings.get("language", "en") if settings else "en"
    event_settings = _state_event_settings(state) if state.db_id else (settings or {})
    bl_factions = event_settings.get("blacklisted_factions", [])
    bl_units = event_settings.get("blacklisted_units", [])

    factions = get_factions_for_team(
        state.layer_data, 2, bl_factions, bl_units,
        exclude_faction=state.team1_faction)

    # Mirror Match: Team 2 must field Team 1's unit type, so keep only the
    # factions that support it (skipped for the "Default" / no-unit path).
    if state.mirror_effective and state.team1_unit not in (None, "Default"):
        factions = [f for f in factions
                    if any(u.get("type") == state.team1_unit for u in f["unitTypes"])]

    if not factions:
        await interaction.response.edit_message(
            embed=discord.Embed(description="No factions available for Team 2.", color=discord.Color.red()),
            view=None,
        )
        return

    options = _faction_select_options(factions)

    mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
    view = _bind(Team2FactionSelectView(options, lang), interaction)
    embed = discord.Embed(
        title=t("suggest.phase_title", lang),
        description=(
            f"**Map:** {state.map_name}\n"
            f"**Mode:** {mode_str}\n"
            f"**Team 1:** {state.team1_faction} / {state.team1_unit}\n"
            f"{t('suggest.select_team2_faction', lang)}"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class Team2FactionSelectView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=600)
        self.add_item(Team2FactionSelect(options, lang))


class Team2FactionSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_team2_faction", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        self.view.stop()  # retire this step so its timer can't clobber later steps
        state.team2_faction = self.values[0]
        settings = db.get_guild_settings(state.guild_id)

        # Mirror Match: Team 2 uses the same unit type as Team 1 — skip the
        # Team 2 unit-type step entirely and go straight to confirmation.
        if state.mirror_effective:
            state.team2_unit = state.team1_unit
            await _show_confirm(interaction, state, settings)
            return

        lang = settings.get("language", "en") if settings else "en"
        event_settings = _state_event_settings(state) if state.db_id else (settings or {})
        bl_units = event_settings.get("blacklisted_units", [])

        # Get unit types for team 2 faction
        units = get_unit_types_for_faction(
            state.layer_data.get("factions", []), state.team2_faction, bl_units, team=2)

        if not units:
            state.team2_unit = "Default"
            await _show_confirm(interaction, state, settings)
            return

        options = [
            discord.SelectOption(label=u.get("type", "?"), value=u.get("type", "?"))
            for u in units[:25]
        ]

        mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
        view = _bind(Team2UnitSelectView(options, lang), interaction)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=(
                f"**Map:** {state.map_name}\n"
                f"**Mode:** {mode_str}\n"
                f"**Team 1:** {state.team1_faction} / {state.team1_unit}\n"
                f"**Team 2:** {state.team2_faction}\n"
                f"{t('suggest.select_team2_unit', lang)}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class Team2UnitSelectView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=600)
        self.add_item(Team2UnitSelect(options, lang))


class Team2UnitSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_team2_unit", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        self.view.stop()  # retire this step so its timer can't clobber the confirm screen
        state.team2_unit = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        await _show_confirm(interaction, state, settings)


def _squadcalc_link_line(suggestion: dict, lang: str) -> str:
    """Markdown 'Open in SquadCalc' link line for a suggestion.

    Returns "" when no SquadCalc URL applies (integration disabled, or a
    non-main layer source that SquadCalc can't resolve).
    """
    url = build_squadcalc_url(suggestion)
    if not url:
        return ""
    return f"\n\n🗺️ [{t('squadcalc.open', lang)}]({url})"


async def _show_confirm(interaction: discord.Interaction, state: SuggestState, settings: dict):
    """Show the confirmation step with Submit/Cancel buttons."""
    lang = settings.get("language", "en") if settings else "en"
    mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode

    # Preview-shaped suggestion for the SquadCalc link (mirrors the fields
    # build_squadcalc_url reads from a stored suggestion).
    preview = {
        "source": state.source,
        "map_name": state.map_name,
        "gamemode": state.gamemode,
        "layer_version": state.layer_version,
        "team1_faction": state.team1_faction,
        "team2_faction": state.team2_faction,
        "team1_unit": state.team1_unit,
        "team2_unit": state.team2_unit,
        "team1_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team1_faction, 1),
        "team2_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team2_faction, 2),
    }

    mirror_line = f"\n**Mirror Match:** {t('suggest.mirror_on', lang)}" if state.mirror_effective else ""

    # Per-team vehicle layout for the chosen loadouts.
    units_map = db.get_source_units(state.source or "")
    t1_veh = format_vehicle_list(
        get_team_vehicles(state.layer_data, state.team1_faction, 1, state.team1_unit, units_map), lang)
    t2_veh = format_vehicle_list(
        get_team_vehicles(state.layer_data, state.team2_faction, 2, state.team2_unit, units_map), lang)

    view = _bind(ConfirmSuggestionView(lang), interaction)
    embed = discord.Embed(
        title=t("suggest.confirm_title", lang),
        description=(
            f"**Map:** {state.map_name}\n"
            f"**Mode:** {mode_str}\n"
            f"**Team 1:** {state.team1_faction} / {state.team1_unit}\n"
            f"{t1_veh}\n"
            f"**Team 2:** {state.team2_faction} / {state.team2_unit}\n"
            f"{t2_veh}"
            f"{mirror_line}"
            f"{_squadcalc_link_line(preview, lang)}"
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class ConfirmSuggestionView(AutoDisableView):
    def __init__(self, lang: str):
        super().__init__(timeout=600)
        self.lang = lang

    @ui.button(label="Submit", style=discord.ButtonStyle.success, emoji="✅")
    async def submit_button(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()  # terminal: retire so the timer can't grey out the result message
        await handle_suggest_submit(interaction, self.lang)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()  # terminal: retire so the timer can't grey out the result message
        _suggest_sessions.pop(interaction.user.id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.cancelled", self.lang), color=discord.Color.greyple()),
            view=None,
        )


async def handle_suggest_submit(interaction: discord.Interaction, lang: str):
    """Process the final suggestion submission."""
    state = _suggest_sessions.pop(interaction.user.id, None)
    if not state:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.timeout", lang), color=discord.Color.red()),
            view=None,
        )
        return

    if state.flow == "history_add":
        await _handle_history_add_submit(interaction, state, lang)
        return

    lock = _get_guild_lock(state.guild_id)
    async with lock:
        record = db.get_event_by_db_id(state.guild_id, state.db_id)
        if not record:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
                view=None,
            )
            return

        event = record["event"]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else lang
        event_settings = _event_settings(event, settings or {})

        if event.get("phase") != "suggestions_open":
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("suggest.not_open", lang), color=discord.Color.red()),
                view=None,
            )
            return

        # Check total suggestion limit (hard cap 25 due to Discord select menu limit)
        max_total = min(event_settings.get("max_total_suggestions", 25), 25)
        if len(event.get("suggestions", [])) >= max_total:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    description=t("suggest.max_total_reached", lang, max=max_total),
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        # Build suggestion dict
        suggestion = {
            "id": str(uuid.uuid4())[:8],
            "user_id": str(interaction.user.id),
            "user_name": interaction.user.display_name,
            "map_name": state.map_name,
            "gamemode": state.gamemode,
            "layer_version": state.layer_version,
            "team1_faction": state.team1_faction,
            "team1_faction_name": _resolve_faction_name(state.layer_data, state.team1_faction, 1),
            "team1_unit": state.team1_unit,
            "team2_faction": state.team2_faction,
            "team2_faction_name": _resolve_faction_name(state.layer_data, state.team2_faction, 2),
            "team2_unit": state.team2_unit,
            "team1_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team1_faction, 1),
            "team2_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team2_faction, 2),
            "raw_name": state.mode_raw_name,
            "source": state.source,
            "suggested_at": datetime.now().isoformat(),
        }

        # Check duplicate in current event
        for existing in event.get("suggestions", []):
            if suggestion_matches(suggestion, existing):
                await interaction.response.edit_message(
                    embed=discord.Embed(description=t("suggest.duplicate", lang), color=discord.Color.red()),
                    view=None,
                )
                return

        # Check history blocking
        lookback = event_settings.get("history_lookback_events", 12)
        if lookback > 0:
            blocked = db.get_blocked_suggestions(state.guild_id, state.channel_id, lookback)
            for bl in blocked:
                if suggestion_matches(suggestion, bl):
                    await interaction.response.edit_message(
                        embed=discord.Embed(
                            description=t("suggest.blocked_history", lang, count=lookback),
                            color=discord.Color.red(),
                        ),
                        view=None,
                    )
                    return

        # Add suggestion
        event.setdefault("suggestions", []).append(suggestion)
        db.save_event(record["db_id"], event)

    # Confirm to user
    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('suggest.submitted', lang)}\n{format_layer_short(suggestion)}",
            color=discord.Color.green(),
        ),
        view=None,
    )

    # Update the main event embed
    await _update_event_embed(state.db_id)

    await send_event_log(
        event, state.db_id,
        f"New suggestion by {interaction.user.display_name}: {format_layer_short(suggestion)}",
        guild_id=state.guild_id,
        lang=lang,
    )


# ═══════════════════════════════════════════════════════════════════════════
# INFO BUTTON handler
# ═══════════════════════════════════════════════════════════════════════════

def _build_info_embed(interaction: discord.Interaction, event: dict,
                      settings: dict, channel_id: int, lang: str) -> discord.Embed:
    """Build the Info panel embed (phase/budget, the user's suggestions, recent
    winners). Factored out so the vehicle-detail Back button can re-render it."""
    user_suggestions = [s for s in event.get("suggestions", [])
                        if str(s.get("user_id")) == str(interaction.user.id)]

    event_settings = _event_settings(event, settings or {})
    max_suggestions = event_settings.get("max_suggestions_per_user", 2)
    embed = discord.Embed(
        title=t("button.info", lang),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name=t("admin.phase", lang, phase=phase_name(event.get("phase", "?"), lang)),
        value=t("info.suggestions_used", lang,
                used=len(user_suggestions), max=max_suggestions),
        inline=False,
    )

    # Show the player's remaining self-removal budget, but only when the
    # "Remove Suggestion" feature is enabled for this event (limit > 0).
    removal_limit = _self_removal_limit(event, settings or {})
    if removal_limit > 0:
        removals_used = _self_removals_used(event, interaction.user.id)
        embed.add_field(
            name=t("info.removals_label", lang),
            value=t("info.removals_value", lang,
                    remaining=max(0, removal_limit - removals_used),
                    max=removal_limit),
            inline=False,
        )

    if user_suggestions:
        lines = [f"• {format_layer_short(s)}" for s in user_suggestions]
        embed.add_field(name=t("info.your_suggestions", lang),
                        value="\n".join(lines), inline=False)

    # Recent winners for this channel — same source/formatting as /history.
    # Show as many as the re-suggestion blocking window (history_lookback_events),
    # so the panel reflects exactly which winners are still blocked from being
    # re-suggested. lookback 0 (blocking disabled) falls back to the default 12.
    lookback = event_settings.get("history_lookback_events", 12) or 12
    history = db.get_recent_history(interaction.guild_id, channel_id, limit=lookback)
    winners = []
    for h in history:
        winner = h.get("winning_layer")
        if not winner:
            continue
        # completed_at is "YYYY-MM-DD HH:MM:SS" (or ISO) — keep the date part.
        date = (h.get("completed_at") or "")[:10]
        line = f"• {format_layer_short(winner)}"
        if date:
            line += f" — *{date}*"
        winners.append(line)
    if winners:
        # A field value is capped at 1024 chars; with lookback up to 50 the list
        # can overflow, so trim to fit and note how many were dropped.
        value = fit_lines_to_field(
            winners,
            lambda dropped: t("info.recent_winners_more", lang, count=dropped),
        )
        embed.add_field(name=t("info.recent_winners", lang),
                        value=value, inline=False)

    return embed


async def _render_info(interaction: discord.Interaction, db_id: int, *, edit: bool):
    """Render the Info panel — as a fresh ephemeral message (edit=False) or by
    editing the current one (edit=True, used by the vehicle-detail Back button).
    Attaches a layer-pick select for vehicle details when suggestions exist."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        if edit:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang),
                                    color=discord.Color.red()),
                view=None)
        else:
            await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    embed = _build_info_embed(interaction, event, settings, record["channel_id"], lang)
    suggestions = event.get("suggestions", [])
    view = (_bind(VehicleInfoSelectView(suggestions, lang, db_id), interaction)
            if suggestions else None)
    if edit:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def handle_info(interaction: discord.Interaction, db_id: int):
    """Info panel: the user's suggestions, budgets and recent winners, plus a
    select to drill into any suggested layer's full vehicle layout."""
    await _render_info(interaction, db_id, edit=False)


_VEHICLE_PICKER_OPTIONS_PER_SELECT = 25


def _vehicle_option_label(s: dict) -> str:
    """Discord-safe (≤100 char) select-option label for a suggestion."""
    label = format_layer_short(s)
    return f"{label[:97]}..." if len(label) > 100 else label


class VehicleInfoSelectView(AutoDisableView):
    """Picker listing every suggested layer; selecting one shows its vehicles.

    Discord caps a Select at 25 options; the suggestion total is itself capped
    at 25, so this is normally a single select (chunked defensively anyway)."""

    def __init__(self, suggestions: list[dict], lang: str, db_id: int):
        super().__init__(timeout=600)
        self.db_id = db_id
        valid = [s for s in suggestions if s.get("id")]
        chunks = [valid[i:i + _VEHICLE_PICKER_OPTIONS_PER_SELECT]
                  for i in range(0, len(valid), _VEHICLE_PICKER_OPTIONS_PER_SELECT)]
        for chunk in chunks[:5]:
            self.add_item(VehicleInfoSelect(chunk, lang))


class VehicleInfoSelect(ui.Select):
    def __init__(self, chunk: list[dict], lang: str):
        options = [
            discord.SelectOption(
                label=_vehicle_option_label(s),
                value=s["id"],
                description=(s.get("user_name") or "")[:100] or None,
            )
            for s in chunk if s.get("id")
        ]
        super().__init__(placeholder=t("info.vehicle_select_placeholder", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        self.view.stop()  # retire the picker so its timer can't clobber the detail view
        await _show_vehicle_detail(interaction, self.view.db_id, self.values[0], self.lang)


class VehicleBackView(AutoDisableView):
    """A single Back button that returns from the vehicle detail to the Info panel."""

    def __init__(self, db_id: int, lang: str):
        super().__init__(timeout=600)
        self.db_id = db_id
        back = ui.Button(label=t("button.back", lang),
                         style=discord.ButtonStyle.secondary, emoji="⬅️")
        back.callback = self._back
        self.add_item(back)

    async def _back(self, interaction: discord.Interaction):
        self.stop()  # retire this view; _render_info attaches a fresh picker
        await _render_info(interaction, self.db_id, edit=True)


async def _show_vehicle_detail(interaction: discord.Interaction, db_id: int,
                               suggestion_id: str, lang: str):
    """Render the per-team vehicle breakdown for a selected suggestion."""
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    suggestion = None
    if record:
        suggestion = next((s for s in record["event"].get("suggestions", [])
                           if s.get("id") == suggestion_id), None)
    if suggestion is None:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang),
                                color=discord.Color.red()),
            view=None)
        return

    source = suggestion.get("source") or ""
    layer_data = db.get_layer_by_raw_name(
        suggestion.get("raw_name", ""), allowed_sources=[source] if source else None)
    units_map = db.get_source_units(source)

    embed = discord.Embed(
        title=t("info.vehicle_detail_title", lang, layer=format_layer_short(suggestion)),
        color=discord.Color.blurple(),
    )
    for team, fac_key, unit_key in ((1, "team1_faction", "team1_unit"),
                                    (2, "team2_faction", "team2_unit")):
        faction = suggestion.get(fac_key, "?")
        unit = suggestion.get(unit_key, "?")
        vehicles = (get_team_vehicles(layer_data, faction, team, unit, units_map)
                    if layer_data else [])
        embed.add_field(
            name=f"🚛 Team {team} — {faction} / {unit}"[:256],
            value=format_vehicle_list(vehicles, lang),
            inline=False,
        )
    await interaction.response.edit_message(
        embed=embed, view=_bind(VehicleBackView(db_id, lang), interaction))


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════════

async def handle_admin_panel(interaction: discord.Interaction, db_id: int):
    """Show admin action buttons for a specific event."""
    settings = db.get_guild_settings(interaction.guild_id)
    if not settings:
        await interaction.response.send_message(
            t("general.guild_not_configured", "en"), ephemeral=True)
        return

    lang = settings.get("language", "en")
    if not has_organizer_role(interaction.user, settings.get("organizer_role_id", 0)):
        await interaction.response.send_message(t("general.requires_organizer", lang), ephemeral=True)
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    phase = event.get("phase", "created")

    embed = discord.Embed(
        title=display_name(event, record["db_id"], lang=lang),
        description=t("admin.phase", lang, phase=phase_name(phase, lang)) + "\n" +
                    t("admin.suggestions_count", lang, count=len(event.get("suggestions", []))),
        color=discord.Color.dark_red(),
    )

    suggestion_count = len(event.get("suggestions", []))
    has_winner = bool(event.get("winning_layer"))
    view = _bind(AdminPanelView(phase, lang, record["db_id"], suggestion_count,
                                has_winner=has_winner), interaction)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class AdminPanelView(AutoDisableView):
    def __init__(self, phase: str, lang: str, db_id: int, suggestion_count: int = 0,
                 has_winner: bool = False):
        super().__init__(timeout=120)
        self.lang = lang
        self.db_id = db_id

        if phase == "created":
            self.add_item(AdminButton("open_suggestions", t("admin.open_suggestions", lang), discord.ButtonStyle.success, "▶️"))
        elif phase == "suggestions_open":
            self.add_item(AdminButton("close_suggestions", t("admin.close_suggestions", lang), discord.ButtonStyle.secondary, "⏹️"))
        elif phase == "suggestions_closed":
            self.add_item(AdminButton("select_for_vote", t("admin.select_for_vote", lang), discord.ButtonStyle.primary, "🗳️"))
            self.add_item(AdminButton("reopen_suggestions", t("admin.reopen_suggestions", lang), discord.ButtonStyle.secondary, "🔄"))
        elif phase == "voting":
            self.add_item(AdminButton("end_vote", t("admin.end_vote", lang), discord.ButtonStyle.danger, "🏁"))
        elif phase == "completed" and has_winner:
            # Copy-friendly plain-text version of the winner block.
            self.add_item(AdminButton("copy_result", t("admin.copy_result", lang), discord.ButtonStyle.secondary, "📋"))

        # Removing a suggestion only makes sense before the poll is live.
        if phase in ("suggestions_open", "suggestions_closed") and suggestion_count > 0:
            self.add_item(AdminButton("remove_suggestion",
                                      t("admin.remove_suggestion", lang),
                                      discord.ButtonStyle.secondary, "✂️"))

        # Edit opens a DM dialog for changing this event's per-event config
        # (gamemodes, blacklists, limits, voting params) without touching the
        # guild defaults. Available at any phase.
        self.add_item(AdminButton("edit_event", t("admin.edit_event", lang),
                                  discord.ButtonStyle.primary, "✏️"))

        # Allow-list picker — opens an ephemeral MentionableSelect (multi-select)
        # right here in the guild, since auto-populated role/user pickers don't
        # work in DMs (which is why `edit_event` redirects here for that field).
        self.add_item(AdminButton("set_event_roles",
                                  t("admin.set_event_roles", lang),
                                  discord.ButtonStyle.primary, "🔐"))

        self.add_item(AdminButton("delete_event", t("admin.delete_event", lang), discord.ButtonStyle.danger, "🗑️"))


class AdminButton(ui.Button):
    def __init__(self, action: str, label: str, style: discord.ButtonStyle, emoji: str):
        # Custom_ids are scoped to the action only; the per-event db_id lives
        # on the view (AdminPanelView), which is non-persistent (panel is
        # ephemeral, reopens via the Admin button each time).
        super().__init__(label=label, style=style, emoji=emoji, custom_id=f"admin:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        db_id = self.view.db_id
        if self.action == "open_suggestions":
            # Open with whatever duration the wizard stored on the event.
            # To override, edit the event's suggestion_duration_seconds via
            # the Admin → Edit DM dialog before clicking Open.
            await admin_open_suggestions(interaction, db_id)
        elif self.action == "close_suggestions":
            await admin_close_suggestions(interaction, db_id)
        elif self.action == "reopen_suggestions":
            await admin_reopen_suggestions(interaction, db_id)
        elif self.action == "select_for_vote":
            await admin_select_for_vote(interaction, db_id)
        elif self.action == "end_vote":
            await admin_end_vote(interaction, db_id)
        elif self.action == "copy_result":
            await admin_copy_result(interaction, db_id)
        elif self.action == "remove_suggestion":
            await admin_remove_suggestion(interaction, db_id)
        elif self.action == "edit_event":
            await admin_edit_event(interaction, db_id)
        elif self.action == "set_event_roles":
            await admin_set_event_roles(interaction, db_id)
        elif self.action == "delete_event":
            await admin_delete_event(interaction, db_id)

        # Every action except those that post a separate ephemeral ack
        # replaces the panel message with a sub-dialog or result, so retire the
        # panel's timer — otherwise its 120s timeout would later grey out
        # whatever now occupies that message.
        if self.action not in ("open_suggestions", "reopen_suggestions", "copy_result"):
            self.view.stop()


class ConfirmActionView(AutoDisableView):
    """Generic confirmation dialog with Confirm and Cancel buttons.

    The confirm_callback is invoked with (interaction, db_id) so admin flows
    can route the action back to the originating event without relying on
    channel-scoped lookups.
    """

    def __init__(self, lang: str, confirm_callback, db_id: int = 0):
        super().__init__(timeout=60)
        self.lang = lang
        self.db_id = db_id
        self._confirm_callback = confirm_callback
        self.confirm_button.label = t("general.confirm", lang)
        self.cancel_button.label = t("general.cancel", lang)

    @ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()  # terminal: the callback replaces the message with a result
        await self._confirm_callback(interaction, self.db_id)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()  # terminal: retire so the timer can't grey out the result
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=t("general.cancelled", self.lang),
                color=discord.Color.greyple()),
            view=None,
        )


async def admin_open_suggestions(interaction: discord.Interaction, db_id: int,
                                 auto_close_seconds: Optional[int] = None):
    """Open the suggestion phase for a specific event.

    `auto_close_seconds` is an explicit override. When None, fall back to
    the value the wizard stored at event-creation time — so the Admin →
    Open Suggestions button respects what the admin already chose, instead
    of resetting it.
    """
    lock = _get_guild_lock(interaction.guild_id)
    end_time = None
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            return
        event = record["event"]
        settings = db.get_guild_settings(interaction.guild_id)
        lang = settings.get("language", "en") if settings else "en"

        if event.get("phase") not in ("created",):
            await interaction.response.send_message(
                embed=discord.Embed(description=t("phase.already_open", lang), color=discord.Color.orange()),
                ephemeral=True,
            )
            return

        if auto_close_seconds is None:
            auto_close_seconds = event.get("suggestion_duration_seconds")

        event["phase"] = "suggestions_open"
        end_time = (datetime.now() + timedelta(seconds=auto_close_seconds)) if auto_close_seconds else None
        event["suggestion_end_time"] = end_time
        event["suggestion_duration_seconds"] = auto_close_seconds
        db.save_event(record["db_id"], event)

    if end_time:
        ts = int(end_time.timestamp())
        ack_text = t("phase.suggestions_opened_until", lang, ts=ts)
    else:
        ack_text = t("phase.suggestions_opened", lang)

    await interaction.response.send_message(
        embed=discord.Embed(description=f"✅ {ack_text}", color=discord.Color.green()),
        ephemeral=True,
    )
    await _update_event_embed(db_id)
    await send_event_log(event, db_id, ack_text, guild_id=interaction.guild_id, lang=lang)


async def admin_close_suggestions(interaction: discord.Interaction, db_id: int):
    """Show confirmation before closing the suggestion phase."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        return
    event = record["event"]
    if event.get("phase") != "suggestions_open":
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("phase.not_open", lang), color=discord.Color.orange()),
            view=None,
        )
        return

    view = _bind(ConfirmActionView(lang, _do_close_suggestions, db_id=db_id), interaction)
    await interaction.response.edit_message(
        embed=discord.Embed(description=t("confirm.close_suggestions", lang), color=discord.Color.orange()),
        view=view,
    )


async def _do_close_suggestions(interaction: discord.Interaction, db_id: int):
    """Actually close the suggestion phase after confirmation."""
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            return
        event = record["event"]
        settings = db.get_guild_settings(interaction.guild_id)
        lang = settings.get("language", "en") if settings else "en"

        if event.get("phase") != "suggestions_open":
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("phase.not_open", lang), color=discord.Color.orange()),
                view=None,
            )
            return

        event["phase"] = "suggestions_closed"
        db.save_event(record["db_id"], event)

    count = len(event.get("suggestions", []))
    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('phase.suggestions_closed', lang, count=count)}",
            color=discord.Color.green(),
        ),
        view=None,
    )
    await _update_event_embed(db_id)
    await send_event_log(
        event, db_id,
        f"Suggestion phase closed. {count} suggestions.",
        guild_id=interaction.guild_id,
        lang=lang,
    )


async def admin_reopen_suggestions(interaction: discord.Interaction, db_id: int):
    """Reopen a closed suggestion phase: suggestions_closed → suggestions_open.

    Mirror of admin_open_suggestions but for the inverse transition. No
    auto-close timer is set (suggestion_end_time = None) so the phase stays
    open until the organizer closes it again — restoring a stale past
    end_time would make the scheduler close it again immediately.
    """
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            return
        event = record["event"]
        settings = db.get_guild_settings(interaction.guild_id)
        lang = settings.get("language", "en") if settings else "en"

        if event.get("phase") != "suggestions_closed":
            await interaction.response.send_message(
                embed=discord.Embed(description=t("phase.not_closed", lang),
                                    color=discord.Color.orange()),
                ephemeral=True,
            )
            return

        event["phase"] = "suggestions_open"
        event["suggestion_end_time"] = None
        db.save_event(record["db_id"], event)

    ack_text = t("phase.suggestions_reopened", lang)
    await interaction.response.send_message(
        embed=discord.Embed(description=f"✅ {ack_text}", color=discord.Color.green()),
        ephemeral=True,
    )
    await _update_event_embed(db_id)
    await send_event_log(event, db_id, ack_text, guild_id=interaction.guild_id, lang=lang)


async def admin_select_for_vote(interaction: discord.Interaction, db_id: int):
    """Show layer selection view for admin to pick layers for voting."""
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        return

    event = record["event"]
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"
    suggestions = event.get("suggestions", [])

    if not suggestions:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("vote.no_suggestions", lang), color=discord.Color.orange()),
            view=None,
        )
        return

    max_voting = min(event.get("max_voting_layers", 10), 10)

    # Build selection options
    options = []
    for s in suggestions[:25]:
        label = format_layer_poll_option(s)
        options.append(discord.SelectOption(label=label, value=s["id"]))

    view = _bind(VoteSelectionView(options, max_voting, lang, record["db_id"]), interaction)
    embed = discord.Embed(
        title=t("admin.select_for_vote", lang),
        description=t("vote.select_layers", lang, max=max_voting),
        color=discord.Color.blue(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class VoteSelectionView(AutoDisableView):
    def __init__(self, options: list[discord.SelectOption], max_values: int,
                 lang: str, db_id: int):
        super().__init__(timeout=120)
        self.lang = lang
        self.db_id = db_id
        self.max_values = max_values

        select = VoteLayerSelect(options, max_values, lang)
        self.add_item(select)
        self.add_item(RandomButton(min(len(options), max_values), lang))
        self.add_item(ConfirmVoteButton(lang))

    selected_ids: list[str] = []


class VoteLayerSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], max_values: int, lang: str):
        super().__init__(
            placeholder=t("vote.select_layers", lang, max=max_values),
            options=options,
            min_values=1,
            max_values=min(max_values, len(options)),
        )
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_ids = self.values
        await interaction.response.defer()


class RandomButton(ui.Button):
    def __init__(self, count: int, lang: str):
        super().__init__(
            label=t("button.random", lang, count=count),
            style=discord.ButtonStyle.secondary,
            emoji="🎲",
        )
        self.count = count
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        import random
        record = db.get_event_by_db_id(interaction.guild_id, self.view.db_id)
        if not record:
            return

        suggestions = record["event"].get("suggestions", [])
        count = min(self.count, len(suggestions))
        selected = random.sample(suggestions, count)
        self.view.selected_ids = [s["id"] for s in selected]

        names = [format_layer_poll_option(s) for s in selected]
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=t("admin.select_for_vote", self.lang),
                description="**Selected (random):**\n" + "\n".join(f"• {n}" for n in names),
                color=discord.Color.blue(),
            ),
        )


class ConfirmVoteButton(ui.Button):
    def __init__(self, lang: str):
        super().__init__(
            label=t("button.confirm_selection", lang),
            style=discord.ButtonStyle.success,
            emoji="✅",
        )
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        selected_ids = self.view.selected_ids
        if not selected_ids:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    description=t("vote.no_layers_selected", self.lang),
                    color=discord.Color.orange(),
                ),
            )
            return

        captured_ids = list(selected_ids)
        lang = self.lang
        db_id = self.view.db_id

        async def _do_start_vote(confirm_interaction: discord.Interaction, _db_id: int):
            lock = _get_guild_lock(confirm_interaction.guild_id)
            async with lock:
                record = db.get_event_by_db_id(confirm_interaction.guild_id, _db_id)
                if not record:
                    return
                event = record["event"]
                event["selected_for_vote"] = captured_ids
                event["phase"] = "voting"
                db.save_event(record["db_id"], event)
            await _start_poll(confirm_interaction, _db_id, captured_ids)

        view = _bind(ConfirmActionView(lang, _do_start_vote, db_id=db_id), interaction)
        self.view.stop()  # retire the selection view; replaced by the confirm dialog
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=t("confirm.start_vote", lang),
                color=discord.Color.orange()),
            view=view,
        )


# ═══════════════════════════════════════════════════════════════════════════
# VOTING THREAD — private thread for role-gated events
#
# Discord native polls can't be filtered per-user — anyone who can see the
# message can vote. For events with a non-empty allow-list, we sidestep that
# by posting the poll inside a *private thread* and letting Discord enforce
# membership at the platform level.
#
# Order on /start_vote (gated event):
#   1. Create private thread off the event's channel
#   2. Send a welcome message that mentions the allowed role(s) — Discord
#      auto-invites members of mentioned roles to private threads (much
#      faster than iterating add_user across a large role)
#   3. Add explicit users from allowed_user_ids
#   4. Post the poll inside the thread
#   5. Lock the thread (members can still vote — voting is an interaction,
#      not a message send — but they can't chat, only vote)
#
# Late joiners (someone gets the role after the thread was created) click
# the "Join Voting" button on the public event embed; the role gate runs
# again and they get added.
# ═══════════════════════════════════════════════════════════════════════════

async def _resolve_thread(guild: discord.Guild, thread_id: int) -> Optional[discord.Thread]:
    """Best-effort lookup of a thread by id, falling back to fetch_channel
    so archived private threads are still reachable."""
    if not thread_id:
        return None
    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread
    try:
        fetched = await bot.fetch_channel(thread_id)
        if isinstance(fetched, discord.Thread):
            return fetched
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return None


async def _resolve_poll_target(channel: discord.abc.Messageable, event: dict) -> discord.abc.Messageable:
    """Return the messageable that holds this event's poll message.

    Gated events post their poll into a private thread (event.vote_thread_id);
    open events post directly in the parent channel. Falls back to `channel`
    if the thread is unreachable so callers don't have to special-case errors.
    """
    thread_id = event.get("vote_thread_id")
    if not thread_id:
        return channel
    guild = getattr(channel, "guild", None)
    if guild is None:
        return channel
    thread = await _resolve_thread(guild, thread_id)
    return thread or channel


async def _fetch_event_message(channel: discord.abc.Messageable,
                               event: dict) -> Optional[discord.Message]:
    """Best-effort fetch of an event's embed message from its channel."""
    msg_id = event.get("event_message_id")
    if not msg_id:
        return None
    try:
        return await channel.fetch_message(msg_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _create_voting_thread(channel: discord.TextChannel, event: dict,
                                 db_id: int, lang: str) -> Optional[discord.Thread]:
    """Create the voting thread for an event.

    Open events (no allow-list) get a PUBLIC thread attached to the event's
    embed message, so it shows up under the embed; anyone in the channel can
    open it and vote. Gated events (with an allow-list) get a standalone
    PRIVATE thread whose members are pre-populated via role pings + explicit
    adds — Discord can't attach a private thread to a message, and the privacy
    is what gates the poll, so those stay unbound.

    Returns the thread, or None if thread creation failed (caller then falls
    back to posting the poll directly in `channel`). Errors during the
    optional member auto-invite are non-fatal — eligible users can always use
    the Join Voting button to opt in afterwards.
    """
    role_ids = event.get("allowed_role_ids") or []
    user_ids = event.get("allowed_user_ids") or []
    gated = bool(role_ids or user_ids)

    thread_name = truncate_thread_name(
        t("thread.voting_name", lang,
          event_label=display_name(event, db_id, lang=lang)))

    try:
        if gated:
            # Private threads can't attach to a message (Discord limitation);
            # privacy is what gates the poll, so keep them standalone.
            thread = await channel.create_thread(
                name=thread_name,
                auto_archive_duration=10080,  # 7 days
                type=discord.ChannelType.private_thread,
                invitable=False,  # only the bot/mods can add others
            )
        else:
            # Open event: attach the public voting thread to the embed message
            # so it shows up under the embed.
            parent = await _fetch_event_message(channel, event)
            if parent is not None:
                thread = parent.thread or await parent.create_thread(
                    name=thread_name,
                    auto_archive_duration=10080,  # 7 days
                )
            else:
                # Embed message unreachable — fall back to a standalone public
                # thread (legacy behavior) so voting still proceeds.
                thread = await channel.create_thread(
                    name=thread_name,
                    auto_archive_duration=10080,  # 7 days
                    type=discord.ChannelType.public_thread,
                )
    except discord.HTTPException as e:
        logger.error(f"Failed to create voting thread in #{channel.id}: {e}")
        return None

    if not gated:
        # Open event: public thread, no allow-list to ping. Post a neutral
        # welcome so the thread isn't empty until the poll lands.
        try:
            await thread.send(t("thread.voting_welcome_open", lang))
        except discord.HTTPException as e:
            logger.warning(f"Failed to send welcome in voting thread {thread.id}: {e}")
        return thread

    # Welcome message + role pings → Discord auto-adds role members to the
    # private thread. allowed_mentions is set so the pings actually fire.
    parts = [t("thread.voting_welcome", lang)]
    if role_ids:
        parts.append(" ".join(f"<@&{rid}>" for rid in role_ids))
    if user_ids:
        parts.append(" ".join(f"<@{uid}>" for uid in user_ids))
    try:
        await thread.send(
            "\n".join(parts),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )
    except discord.HTTPException as e:
        logger.warning(f"Failed to send welcome ping in voting thread {thread.id}: {e}")

    # Belt-and-suspenders: explicit add_user for the user-id allow-list.
    # (Role mention covers role members; explicit users may not be in any
    # mentioned role.)
    for uid in user_ids:
        try:
            user = await bot.fetch_user(int(uid))
            await thread.add_user(user)
        except (ValueError, discord.HTTPException) as e:
            logger.warning(f"Could not add user {uid} to voting thread: {e}")

    return thread


async def handle_join_vote(interaction: discord.Interaction, db_id: int):
    """Join Voting button — gate-check the user and add them to the thread.

    Also handles the late-joiner case: someone who gets the allowed role
    after the thread was created can opt in here without an organizer
    having to add them manually.
    """
    settings = db.get_guild_settings(interaction.guild_id)
    if not settings:
        await interaction.response.send_message(
            t("general.guild_not_configured", "en"), ephemeral=True)
        return
    lang = settings.get("language", "en")

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    if event.get("phase") != "voting":
        await interaction.response.send_message(t("vote.not_in_voting_phase", lang), ephemeral=True)
        return

    if not check_role_gate(event, interaction.user):
        await interaction.response.send_message(t("gate.denied", lang), ephemeral=True)
        return

    thread_id = event.get("vote_thread_id")
    if not thread_id:
        # Open event — poll lives directly in the parent channel; nothing to join.
        await interaction.response.send_message(t("gate.no_thread", lang), ephemeral=True)
        return

    thread = await _resolve_thread(interaction.guild, thread_id)
    if thread is None:
        await interaction.response.send_message(t("gate.thread_missing", lang), ephemeral=True)
        return

    # Only gated events use a PRIVATE thread, where membership is required for
    # access — add the user so late-joiners (who got the role after creation)
    # can see it. Open events use a PUBLIC thread reachable via the link, so we
    # skip add_user there: adding to a thread notifies the user, and we just
    # want to point them at the voting, not ping them.
    gated = bool(event.get("allowed_role_ids") or event.get("allowed_user_ids"))
    if gated:
        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException as e:
            logger.warning(f"Failed to add {interaction.user.id} to voting thread {thread_id}: {e}")

    await interaction.response.send_message(
        t("gate.joined", lang, thread=thread.mention), ephemeral=True)


async def _start_poll(interaction: discord.Interaction, db_id: int,
                      selected_ids: list[str], reuse_thread: bool = False,
                      ping_user_ids: Optional[list[int]] = None,
                      ping_role_ids: Optional[list[int]] = None):
    """Create a Discord native poll for the selected layers.

    ``reuse_thread=True`` (runoff): post into the event's existing voting thread
    instead of creating a new one, and leave ``vote_thread_id`` untouched — avoids
    a second thread and re-pinging a gated role. In that case the interaction has
    already been responded to (by the runoff modal), so the ack edit is skipped.

    ``ping_user_ids`` / ``ping_role_ids``: if given, post a follow-up message that
    mentions them after the poll (runoff opt-in re-ping). Empty/None → no ping.
    """
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        return

    event = record["event"]
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"
    suggestions = event.get("suggestions", [])
    duration_hours = event.get("voting_duration_hours", 24)

    # Get selected suggestions
    selected = [s for s in suggestions if s.get("id") in selected_ids]
    if not selected:
        return

    # Build poll
    poll = discord.Poll(
        question=t("vote.poll_question", lang),
        duration=timedelta(hours=duration_hours),
        multiple=bool(event.get("allow_multiple_votes", False)),
    )
    for s in selected[:10]:
        poll.add_answer(text=format_layer_poll_option(s))

    # Gated events post the poll inside a private thread so Discord enforces
    # the allow-list. Open events keep the legacy in-channel behavior. A runoff
    # reuses the existing thread rather than spawning a new one.
    if reuse_thread:
        voting_thread = None
        target = await _resolve_poll_target(interaction.channel, event)
        gated = bool(event.get("allowed_role_ids") or event.get("allowed_user_ids"))
        if gated and not isinstance(target, discord.Thread):
            # The gated event's private thread is gone; recreate it rather than
            # leak the restricted poll into the public parent channel.
            voting_thread = await _create_voting_thread(interaction.channel, event, db_id, lang)
            target = voting_thread if voting_thread is not None else interaction.channel
    else:
        voting_thread = await _create_voting_thread(interaction.channel, event, db_id, lang)
        target = voting_thread if voting_thread is not None else interaction.channel
    poll_message = await target.send(poll=poll)

    # Runoff opt-in: re-ping the chosen recipients into the same thread as the poll.
    for content in build_ping_messages(ping_role_ids or [], ping_user_ids or [],
                                       t("runoff.ping_message", lang)):
        try:
            await target.send(
                content,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )
        except discord.HTTPException as e:
            logger.warning(f"Failed to send runoff ping for event {db_id}: {e}")

    poll_end_time = (
        getattr(poll_message.poll, "expires_at", None)
        or datetime.now() + timedelta(hours=duration_hours)
    )

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if record:
            event = record["event"]
            event["poll_message_id"] = poll_message.id
            event["voting_end_time"] = poll_end_time
            if voting_thread is not None:
                event["vote_thread_id"] = voting_thread.id
            db.save_event(record["db_id"], event)

    if not interaction.response.is_done():
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"✅ {t('vote.started', lang, hours=duration_hours)}",
                color=discord.Color.green(),
            ),
            view=None,
        )
    await _update_event_embed(db_id)
    await send_event_log(
        event, db_id,
        f"Voting started with {len(selected)} layers for {duration_hours}h",
        guild_id=interaction.guild_id,
        lang=lang,
    )


async def _auto_start_poll(db_id: int, selected_ids: list[str]) -> bool:
    """Background variant of _start_poll — creates the poll without an
    interaction. Returns True on success.

    Assumes the event phase has already been set to "voting" under lock."""
    record = db.get_active_event_unsafe(db_id)
    if not record:
        return False
    guild_id = record["guild_id"]
    channel_id = record["channel_id"]
    event = record["event"]

    guild = bot.get_guild(guild_id)
    if not guild:
        return False
    channel = guild.get_channel(channel_id)
    if not channel:
        return False

    settings = db.get_guild_settings(guild_id)
    lang = settings.get("language", "en") if settings else "en"
    suggestions = event.get("suggestions", [])
    duration_hours = event.get("voting_duration_hours", 24)

    selected = [s for s in suggestions if s.get("id") in selected_ids]
    if not selected:
        return False

    poll = discord.Poll(
        question=t("vote.poll_question", lang),
        duration=timedelta(hours=duration_hours),
        multiple=bool(event.get("allow_multiple_votes", False)),
    )
    for s in selected[:10]:
        poll.add_answer(text=format_layer_poll_option(s))

    voting_thread = await _create_voting_thread(channel, event, db_id, lang)
    target = voting_thread if voting_thread is not None else channel

    try:
        poll_message = await target.send(poll=poll)
    except Exception as e:
        logger.error(f"Failed to send auto-poll: {e}")
        return False

    poll_end_time = (
        getattr(poll_message.poll, "expires_at", None)
        or datetime.now() + timedelta(hours=duration_hours)
    )

    lock = _get_guild_lock(guild_id)
    async with lock:
        rec = db.get_active_event_unsafe(db_id)
        if rec:
            rec["event"]["poll_message_id"] = poll_message.id
            rec["event"]["voting_end_time"] = poll_end_time
            if voting_thread is not None:
                rec["event"]["vote_thread_id"] = voting_thread.id
            db.save_event(rec["db_id"], rec["event"])

    await _update_event_embed(db_id)
    return True


def _complete_event_with_winner(event: dict, winner: Optional[dict]) -> None:
    """Mark an event completed with the given winner (may be None = no winner).

    In-memory mutation only — caller saves. Shared by every path that finalizes a
    vote so the manual and automatic ends can't drift apart.
    """
    event["phase"] = "completed"
    event["winning_layer"] = winner
    event["winning_layer_command"] = build_admin_change_layer(winner)
    event.pop("draw_tied_ids", None)
    _attach_winner_vehicles(winner)


def _enter_draw_pending(event: dict, tied: list[dict]) -> None:
    """Park a drawn vote in the draw-pending phase, remembering the tied ballot ids.

    In-memory mutation only — caller saves.
    """
    event["phase"] = "draw_pending"
    event["draw_tied_ids"] = [s["id"] for s in tied if s.get("id")]


async def admin_end_vote(interaction: discord.Interaction, db_id: int):
    """End the voting phase and determine the winner."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            return

        event = record["event"]
        if event.get("phase") != "voting":
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("vote.not_in_voting_phase", lang), color=discord.Color.orange()),
                view=None,
            )
            return

        # Try to end the poll and get results
        winner, tied = await _resolve_poll_result(interaction.channel, event)

        if tied:
            # Draw: park the event and let an organizer pick how to resolve it.
            _enter_draw_pending(event, tied)
            db.save_event(record["db_id"], event)
        else:
            _complete_event_with_winner(event, winner)
            db.save_event(record["db_id"], event)
            # Only record events that actually produced a winner.
            if winner:
                db.save_voting_history(
                    interaction.guild_id,
                    interaction.channel_id,
                    event.get("suggestions", []),
                    winner,
                )

    if tied:
        tied_names = ", ".join(format_layer_short(s) for s in tied)
        desc = f"⚖️ {t('vote.draw_detected', lang, layers=tied_names)}"
        log_msg = f"Voting ended in a draw between: {tied_names}"
    elif winner:
        desc = f"✅ {t('vote.ended', lang)}\n{t('vote.winner', lang, layer=format_layer_short(winner))}"
        log_msg = f"Voting ended. Winner: {format_layer_short(winner)}"
    else:
        desc = f"✅ {t('vote.ended', lang)}\n{t('vote.no_winner', lang)}"
        log_msg = "Voting ended. Winner: None"

    await interaction.response.edit_message(
        embed=discord.Embed(description=desc, color=discord.Color.gold()),
        view=None,
    )
    await _update_event_embed(db_id)
    await send_event_log(
        event, db_id, log_msg,
        guild_id=interaction.guild_id,
        lang=lang,
    )


async def admin_copy_result(interaction: discord.Interaction, db_id: int):
    """Send the winner block as a copy-friendly ephemeral plain-text message."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return
    text = build_winner_copy_text(record["event"], lang)
    if not text:
        await interaction.response.send_message(t("vote.no_winner", lang), ephemeral=True)
        return

    # Discord caps message content at 2000 chars; split on section breaks
    # when the two vehicle lists push the text over the limit.
    chunks, current = [], ""
    for part in text.split("\n\n"):
        candidate = f"{current}\n\n{part}" if current else part
        if len(candidate) > 2000 and current:
            chunks.append(current)
            candidate = part
        current = candidate
    chunks.append(current)

    await interaction.response.send_message(
        chunks[0], ephemeral=True, suppress_embeds=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True, suppress_embeds=True)


def _tally_poll(answer_counts: list[tuple[str, int]],
                selected: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """Decide a poll's outcome from (answer_text, vote_count) pairs.

    Returns (winner, tied):
    - one suggestion holds the top count (> 0 votes) -> (winner, [])
    - 2+ suggestions share the top count (> 0)       -> (None, [tied...]) in ballot order
    - no votes / empty poll                          -> (None, [])

    Answer text is matched back to a suggestion with ``format_layer_poll_option``,
    the same formatter used to build the poll. If a lone top answer matches no
    ballot entry, fall back to the first selected suggestion (legacy behaviour).
    """
    if not answer_counts:
        return None, []
    best = max(c for _, c in answer_counts)
    if best <= 0:
        return None, []
    # A draw is 2+ distinct top *answers*, not 2+ matched suggestions. Two ballot
    # layers whose truncated poll-option text collides share a single poll answer,
    # so they must not manufacture a false draw out of one outright winner.
    winning_texts = {text for text, c in answer_counts if c == best}
    tied = []
    seen: set = set()
    for s in selected:
        txt = format_layer_poll_option(s)
        if txt in winning_texts and txt not in seen:
            seen.add(txt)
            tied.append(s)
    if len(tied) >= 2:
        return None, tied
    if tied:
        return tied[0], []
    return (selected[0] if selected else None), []


async def _resolve_poll_result(channel: discord.TextChannel,
                               event: dict) -> tuple[Optional[dict], list[dict]]:
    """Fetch + end the poll and tally it into (winner, tied) via ``_tally_poll``.

    A non-empty ``tied`` list means the vote ended in a draw. ``(None, [])`` means
    no votes or a poll/fetch failure — not a draw.
    """
    poll_msg_id = event.get("poll_message_id")
    if not poll_msg_id:
        return None, []

    target = await _resolve_poll_target(channel, event)

    try:
        message = await target.fetch_message(poll_msg_id)
        if not message.poll:
            return None, []

        # Try to end the poll if it's still active
        try:
            message = await message.end_poll()
        except discord.HTTPException:
            pass

        answer_counts = [(a.text, a.vote_count) for a in message.poll.answers]
        selected_ids = event.get("selected_for_vote", [])
        selected = [s for s in event.get("suggestions", []) if s.get("id") in selected_ids]
        return _tally_poll(answer_counts, selected)
    except discord.NotFound:
        logger.warning(f"Poll message {poll_msg_id} not found")
    except Exception as e:
        logger.error(f"Error resolving poll winner: {e}")

    return None, []


async def _fetch_poll_voter_ids(channel: discord.abc.Messageable, event: dict,
                                poll_msg_id: int) -> list[int]:
    """Fetch the distinct user ids that voted on the (finalised) poll.

    Discord native polls don't expose voters to us until we ask: this pulls
    them live from the poll message via ``PollAnswer.voters()``. With
    ``allow_multiple_votes`` the same user appears under several answers, so a
    set dedupes them. Best-effort — returns [] if the poll message is gone.
    """
    ids: set[int] = set()
    try:
        target = await _resolve_poll_target(channel, event)
        message = await target.fetch_message(poll_msg_id)
        if not message.poll:
            return []
        for answer in message.poll.answers:
            async for user in answer.voters():
                ids.add(user.id)
    except discord.HTTPException as e:
        logger.warning(f"Could not fetch poll voters for {poll_msg_id}: {e}")
    return sorted(ids)


# ═══════════════════════════════════════════════════════════════════════════
# DRAW RESOLUTION — organizer picks how to break a tied vote
# ═══════════════════════════════════════════════════════════════════════════

def _draw_tied_suggestions(event: dict) -> list[dict]:
    """The tied suggestions recorded when the event entered draw_pending."""
    by_id = {s["id"]: s for s in event.get("suggestions", []) if s.get("id")}
    return [by_id[i] for i in event.get("draw_tied_ids", []) if i in by_id]


async def _apply_draw_winner(guild_id: int, db_id: int, winner: dict,
                             channel_id: int) -> Optional[dict]:
    """Complete a drawn event with the chosen winner, under the guild lock.

    Returns the updated event dict, or None if the draw was already resolved by
    someone else (lost the race). Records voting history like a normal win.
    """
    lock = _get_guild_lock(guild_id)
    async with lock:
        record = db.get_event_by_db_id(guild_id, db_id)
        if not record or record["event"].get("phase") != "draw_pending":
            return None
        event = record["event"]
        _complete_event_with_winner(event, winner)
        db.save_event(record["db_id"], event)
        db.save_voting_history(guild_id, channel_id, event.get("suggestions", []), winner)
        return event


async def _commit_draw_winner(interaction: discord.Interaction, db_id: int, winner: dict,
                              lang: str, ack_key: str, log_tag: str) -> None:
    """Finalize a drawn event with ``winner`` after the organizer confirmed.

    Called from a Confirm button whose interaction has not yet been responded to.
    """
    await interaction.response.defer()
    event = await _apply_draw_winner(interaction.guild_id, db_id, winner, interaction.channel_id)
    if event is None:
        await interaction.edit_original_response(
            embed=discord.Embed(description=t("draw.already_resolved", lang),
                                color=discord.Color.greyple()),
            view=None)
        return
    await interaction.edit_original_response(
        embed=discord.Embed(
            description=t(ack_key, lang, layer=format_layer_short(winner)),
            color=discord.Color.green()),
        view=None)
    await _update_event_embed(db_id)
    await send_event_log(
        event, db_id, f"Draw resolved ({log_tag}). Winner: {format_layer_short(winner)}",
        guild_id=interaction.guild_id, lang=lang)


async def _confirm_draw_random(interaction: discord.Interaction, db_id: int) -> None:
    """Confirm callback for the Random tie-break: pick a tied layer at random now."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    tied = (_draw_tied_suggestions(record["event"])
            if record and record["event"].get("phase") == "draw_pending" else [])
    if not tied:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("draw.already_resolved", lang),
                                color=discord.Color.greyple()),
            view=None)
        return
    import random
    await _commit_draw_winner(interaction, db_id, random.choice(tied), lang,
                              "draw.resolved_random", "random")


class RunoffPingChoiceView(AutoDisableView):
    """Ephemeral step before a runoff: let the organizer pick who gets re-pinged.

    Each choice opens the duration modal (the "confirm" step); the ``roles``
    button is omitted for open events (no allow-list to ping).
    """

    def __init__(self, db_id: int, lang: str, tied_ids: list[str],
                 default_hours: int, gated: bool):
        super().__init__(timeout=120)
        self.db_id = db_id
        self.lang = lang
        self.tied_ids = tied_ids
        self.default_hours = default_hours

        self._add_choice("voters", "button.runoff_ping_voters",
                         discord.ButtonStyle.primary, "🔁")
        if gated:
            self._add_choice("roles", "button.runoff_ping_roles",
                             discord.ButtonStyle.secondary, "🔔")
        self._add_choice("none", "button.runoff_ping_none",
                         discord.ButtonStyle.secondary, "🚫")

        cancel = ui.Button(label=t("general.cancel", lang),
                           style=discord.ButtonStyle.secondary, emoji="❌")
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _add_choice(self, mode: str, label_key: str,
                    style: discord.ButtonStyle, emoji: str) -> None:
        btn = ui.Button(label=t(label_key, self.lang), style=style, emoji=emoji)

        async def _cb(interaction: discord.Interaction):
            self.stop()  # retire: the modal takes over from here
            await interaction.response.send_modal(
                RunoffDurationModal(self.db_id, self.lang, self.tied_ids,
                                    self.default_hours, ping_mode=mode))

        btn.callback = _cb
        self.add_item(btn)

    async def _cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.cancelled", self.lang),
                                color=discord.Color.greyple()),
            view=None)


class RunoffDurationModal(ui.Modal):
    """Ask the organizer for a runoff duration, then re-poll only the tied layers."""

    def __init__(self, db_id: int, lang: str, tied_ids: list[str], default_hours: int,
                 ping_mode: str = "none"):
        super().__init__(title=t("runoff.duration_label", lang)[:45])
        self.db_id = db_id
        self.lang = lang
        self.tied_ids = tied_ids
        self.ping_mode = ping_mode  # "voters" | "roles" | "none"
        self.duration_input = ui.TextInput(
            label=t("runoff.duration_label", lang)[:45],
            placeholder=DURATION_HINT,
            default=f"{default_hours}h",
            required=True,
            max_length=20,
        )
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.duration_input.value or "").strip()
        hours = parse_voting_duration_input(raw)
        if hours is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
            return

        lock = _get_guild_lock(interaction.guild_id)
        async with lock:
            record = db.get_event_by_db_id(interaction.guild_id, self.db_id)
            if not record:
                await interaction.response.send_message(
                    t("event.no_event", self.lang), ephemeral=True)
                return
            event = record["event"]
            if event.get("phase") != "draw_pending":
                await interaction.response.send_message(
                    t("draw.already_resolved", self.lang), ephemeral=True)
                return
            # Capture what we need to re-ping before we clear the poll id below.
            old_poll_msg_id = event.get("poll_message_id")
            allowed_role_ids: list[int] = []
            allowed_user_ids: list[int] = []
            if self.ping_mode == "roles":  # only this branch pings the allow-list
                allowed_role_ids = list(event.get("allowed_role_ids") or [])
                allowed_user_ids = [int(u) for u in (event.get("allowed_user_ids") or [])]
            event["voting_duration_hours"] = hours
            event["selected_for_vote"] = list(self.tied_ids)
            # Drop the finalised poll id so a mid-runoff failure can't leave the
            # background loop re-tallying the old poll and bouncing back here.
            event["poll_message_id"] = None
            event["phase"] = "voting"
            event.pop("draw_tied_ids", None)
            db.save_event(record["db_id"], event)

        # Ack right away: the voter fetch below is network I/O that can blow the
        # 3s interaction window. Everything that can fail (fetch + poll start)
        # runs under the try so any error rolls the event back to draw_pending.
        await interaction.response.send_message(
            t("runoff.started", self.lang, hours=hours), ephemeral=True)
        try:
            ping_user_ids: list[int] = []
            ping_role_ids: list[int] = []
            if self.ping_mode == "voters" and old_poll_msg_id:
                ping_user_ids = await _fetch_poll_voter_ids(
                    interaction.channel, event, old_poll_msg_id)
            elif self.ping_mode == "roles":
                ping_role_ids = allowed_role_ids
                ping_user_ids = allowed_user_ids
            await _start_poll(interaction, self.db_id, list(self.tied_ids),
                              reuse_thread=True,
                              ping_user_ids=ping_user_ids, ping_role_ids=ping_role_ids)
        except Exception as e:
            logger.error(f"Runoff poll failed to start for event {self.db_id}: {e}")
            # Roll the event back to the draw so an organizer can retry instead of
            # being stranded in a voting phase with no poll.
            lock2 = _get_guild_lock(interaction.guild_id)
            async with lock2:
                rec = db.get_event_by_db_id(interaction.guild_id, self.db_id)
                if rec and rec["event"].get("phase") == "voting":
                    rec["event"]["phase"] = "draw_pending"
                    rec["event"]["draw_tied_ids"] = list(self.tied_ids)
                    db.save_event(rec["db_id"], rec["event"])
            await interaction.followup.send(t("runoff.failed", self.lang), ephemeral=True)
            await _update_event_embed(self.db_id)


class DrawPickSelect(ui.Select):
    """Dropdown of the tied layers for the manual-pick tie-break."""

    def __init__(self, tied: list[dict], lang: str):
        options = [
            discord.SelectOption(label=format_layer_poll_option(s)[:100], value=s["id"])
            for s in tied if s.get("id")
        ]
        super().__init__(placeholder=t("draw.pick_placeholder", lang),
                         options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.stop()  # retire the picker; replaced by the confirm dialog
        lang = self.view.lang
        db_id = self.view.db_id
        winner = next((s for s in self.view.tied if s.get("id") == self.values[0]), None)
        if winner is None:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("draw.already_resolved", lang),
                                    color=discord.Color.greyple()),
                view=None)
            return

        # Confirm the pick before finalizing so a dropdown misclick doesn't end
        # the vote outright.
        async def _confirm(inter: discord.Interaction, _db_id: int):
            await _commit_draw_winner(inter, db_id, winner, lang, "draw.resolved_pick", "pick")

        view = _bind(ConfirmActionView(lang, _confirm, db_id=db_id), interaction)
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=t("draw.confirm_pick", lang, layer=format_layer_short(winner)),
                color=discord.Color.orange()),
            view=view)


class DrawPickView(AutoDisableView):
    """Ephemeral picker for the manual tie-break."""

    def __init__(self, db_id: int, lang: str, tied: list[dict]):
        super().__init__(timeout=120)
        self.db_id = db_id
        self.lang = lang
        self.tied = tied
        self.add_item(DrawPickSelect(tied, lang))


async def handle_draw_action(interaction: discord.Interaction, db_id: int, action: str):
    """Organizer resolves a drawn vote: runoff / random / pick manually."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"
    if not has_organizer_role(interaction.user, (settings or {}).get("organizer_role_id", 0)):
        await interaction.response.send_message(
            t("general.requires_organizer", lang), ephemeral=True)
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return
    event = record["event"]
    if event.get("phase") != "draw_pending":
        await interaction.response.send_message(t("draw.already_resolved", lang), ephemeral=True)
        return

    tied = _draw_tied_suggestions(event)
    if not tied:
        await interaction.response.send_message(t("draw.already_resolved", lang), ephemeral=True)
        return

    if action == "draw_runoff":
        gated = bool(event.get("allowed_role_ids") or event.get("allowed_user_ids"))
        view = _bind(RunoffPingChoiceView(
            db_id, lang, [s["id"] for s in tied],
            event.get("voting_duration_hours", 24), gated), interaction)
        await interaction.response.send_message(
            embed=discord.Embed(description=t("runoff.ping_prompt", lang),
                                color=discord.Color.blurple()),
            view=view, ephemeral=True)
        return

    if action == "draw_pick":
        view = _bind(DrawPickView(db_id, lang, tied), interaction)
        await interaction.response.send_message(
            embed=discord.Embed(title=t("draw.pick_prompt", lang), color=discord.Color.gold()),
            view=view, ephemeral=True)
        return

    # draw_random — confirm before rolling, so a misclick doesn't end the vote.
    view = _bind(ConfirmActionView(lang, _confirm_draw_random, db_id=db_id), interaction)
    await interaction.response.send_message(
        embed=discord.Embed(
            description=t("draw.confirm_random", lang, count=len(tied)),
            color=discord.Color.orange()),
        view=view, ephemeral=True)


async def admin_delete_event(interaction: discord.Interaction, db_id: int):
    """Show confirmation before deleting a specific event."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
            view=None,
        )
        return

    view = _bind(ConfirmActionView(lang, _do_delete_event, db_id=db_id), interaction)
    await interaction.response.edit_message(
        embed=discord.Embed(description=t("confirm.delete_event", lang), color=discord.Color.orange()),
        view=view,
    )


async def _do_delete_event(interaction: discord.Interaction, db_id: int):
    """Actually delete the event after confirmation."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
                view=None,
            )
            return

        event = record["event"]

        # Delete the event embed message
        msg_id = event.get("event_message_id")
        if msg_id:
            try:
                msg = await interaction.channel.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass

        # Clean up the poll only when it's directly in the parent channel.
        # Gated events vote inside a private thread, which we leave intact
        # as a permanent record of the vote — deleting just the poll there
        # would leave an empty thread with only the welcome message.
        poll_msg_id = event.get("poll_message_id")
        thread_id = event.get("vote_thread_id")
        if poll_msg_id and not thread_id:
            try:
                async for msg in interaction.channel.history(
                    after=discord.Object(id=poll_msg_id), limit=15
                ):
                    if msg.type.value == 46:  # MessageType.poll_result
                        await msg.delete()
                        break
            except Exception:
                pass

            try:
                msg = await interaction.channel.fetch_message(poll_msg_id)
                await msg.delete()
            except discord.NotFound:
                pass

        db.delete_event(record["db_id"])

    await interaction.response.edit_message(
        embed=discord.Embed(description=f"✅ {t('event.deleted', lang)}", color=discord.Color.green()),
        view=None,
    )
    await send_event_log(event, db_id, "Event deleted", guild_id=interaction.guild_id, lang=lang)


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN: Remove a single suggestion
# ═══════════════════════════════════════════════════════════════════════════

# Discord caps each Select at 25 options and a View at 5 action rows, so a
# single picker view holds up to 5 × 25 = 125 suggestions.
_REMOVE_PICKER_OPTIONS_PER_SELECT = 25
_REMOVE_PICKER_MAX_SUGGESTIONS = 5 * _REMOVE_PICKER_OPTIONS_PER_SELECT


def _remove_option_label(s: dict) -> str:
    """Build a Discord-safe (≤100 char) option label for a suggestion."""
    user = s.get("user_name", "?") or "?"
    layer = format_layer_short(s)
    label = f"{user} — {layer}"
    if len(label) > 100:
        label = label[:97] + "..."
    return label


class RemoveSuggestionView(AutoDisableView):
    """Picker view that chunks suggestions across multiple Select dropdowns.

    Discord caps each Select at 25 options, so we split the suggestion list
    into 25-sized chunks. Each chunk becomes its own Select on its own row.
    """

    def __init__(self, suggestions: list[dict], lang: str, db_id: int):
        super().__init__(timeout=120)
        self.db_id = db_id
        chunks = [
            suggestions[i:i + _REMOVE_PICKER_OPTIONS_PER_SELECT]
            for i in range(0, len(suggestions), _REMOVE_PICKER_OPTIONS_PER_SELECT)
        ]
        for idx, chunk in enumerate(chunks[:5]):
            self.add_item(RemoveSuggestionSelect(chunk, lang, idx, len(chunks)))


class RemoveSuggestionSelect(ui.Select):
    def __init__(self, chunk: list[dict], lang: str, idx: int, total: int):
        if total > 1:
            placeholder = t("admin.remove_select_chunk", lang,
                            current=idx + 1, total=total)
        else:
            placeholder = t("admin.remove_select", lang)

        options = [
            discord.SelectOption(
                label=_remove_option_label(s),
                value=s["id"],
                description=(s.get("user_name") or "")[:100] or None,
            )
            for s in chunk if s.get("id")
        ]
        super().__init__(placeholder=placeholder, options=options,
                         min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        self.view.stop()  # retire the picker; replaced by the confirm dialog
        await _confirm_admin_remove_suggestion(
            interaction, self.view.db_id, self.values[0], self.lang)


async def _confirm_admin_remove_suggestion(interaction: discord.Interaction,
                                            db_id: int, suggestion_id: str,
                                            lang: str) -> None:
    """Two-step delete: show a confirmation embed, then route Confirm to the
    actual removal. Cancel falls through to the existing ConfirmActionView's
    "cancelled" message — admin reopens the picker via the Admin → Remove
    button if they want to try again."""
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang),
                                color=discord.Color.red()),
            view=None,
        )
        return
    suggestion = next(
        (s for s in record["event"].get("suggestions", [])
         if s.get("id") == suggestion_id),
        None,
    )
    if suggestion is None:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("admin.remove_not_found", lang),
                                color=discord.Color.orange()),
            view=None,
        )
        return

    embed = discord.Embed(
        title=t("admin.confirm_remove_title", lang),
        description=t("admin.confirm_remove_prompt", lang,
                      user=suggestion.get("user_name", "?"),
                      layer=format_layer_short(suggestion)),
        color=discord.Color.orange(),
    )

    async def confirm_cb(inter: discord.Interaction, _db_id: int):
        await admin_do_remove_suggestion(inter, _db_id, suggestion_id)

    await interaction.response.edit_message(
        embed=embed,
        view=_bind(ConfirmActionView(lang, confirm_cb, db_id), interaction),
    )


async def admin_remove_suggestion(interaction: discord.Interaction, db_id: int):
    """Render the picker view for choosing a suggestion to remove."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang),
                                color=discord.Color.red()),
            view=None,
        )
        return

    suggestions = record["event"].get("suggestions", [])
    if not suggestions:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("admin.no_suggestions", lang),
                                color=discord.Color.orange()),
            view=None,
        )
        return

    visible = suggestions[:_REMOVE_PICKER_MAX_SUGGESTIONS]
    embed = discord.Embed(
        title=t("admin.remove_suggestion", lang),
        description=t("admin.remove_prompt", lang, count=len(visible)),
        color=discord.Color.dark_red(),
    )
    await interaction.response.edit_message(
        embed=embed,
        view=_bind(RemoveSuggestionView(visible, lang, db_id), interaction),
    )


async def admin_do_remove_suggestion(interaction: discord.Interaction,
                                     db_id: int, suggestion_id: str):
    """Remove the chosen suggestion, refresh the event embed, and re-render
    the picker so the admin can remove more without reopening the panel.
    """
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    removed: Optional[dict] = None
    remaining: list[dict] = []
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang),
                                    color=discord.Color.red()),
                view=None,
            )
            return

        event = record["event"]
        new_list: list[dict] = []
        for s in event.get("suggestions", []):
            if removed is None and s.get("id") == suggestion_id:
                removed = s
                continue
            new_list.append(s)

        if removed is None:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("admin.remove_not_found", lang),
                                    color=discord.Color.orange()),
                view=None,
            )
            return

        event["suggestions"] = new_list
        remaining = new_list
        db.save_event(record["db_id"], event)

    # Refresh the public event embed. The per-user suggestion limit is
    # computed live from event["suggestions"], so removal automatically frees
    # the slot for the original suggester.
    await _update_event_embed(db_id)

    await send_event_log(
        event, db_id,
        f"Suggestion removed by {interaction.user.display_name}: "
        f"{format_layer_short(removed)} (originally by {removed.get('user_name', '?')})",
        guild_id=interaction.guild_id,
        lang=lang,
    )

    removed_line = t("admin.suggestion_removed", lang,
                     layer=format_layer_short(removed))
    if remaining:
        visible = remaining[:_REMOVE_PICKER_MAX_SUGGESTIONS]
        embed = discord.Embed(
            title=t("admin.remove_suggestion", lang),
            description=(
                f"✅ {removed_line}\n\n"
                f"{t('admin.remove_prompt', lang, count=len(visible))}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=_bind(RemoveSuggestionView(visible, lang, db_id), interaction),
        )
    else:
        await interaction.response.edit_message(
            embed=discord.Embed(description=f"✅ {removed_line}",
                                color=discord.Color.green()),
            view=None,
        )


# ═══════════════════════════════════════════════════════════════════════════
# USER SELF-REMOVAL — players remove their own suggestions (capped per event)
# ═══════════════════════════════════════════════════════════════════════════

def _self_removal_limit(event: dict, settings: dict) -> int:
    """How many self-removals this event allows per user (0 = disabled)."""
    return _event_settings(event, settings).get("max_self_removals_per_user", 1)


def _self_removals_used(event: dict, user_id) -> int:
    """How many self-removals the given user has already spent this event."""
    return (event.get("user_removal_counts") or {}).get(str(user_id), 0)


class SelfRemoveSuggestionView(AutoDisableView):
    """Ephemeral picker listing only the caller's own suggestions.

    A user can hold at most `max_suggestions_per_user` (≤10) suggestions, so a
    single Select always fits Discord's 25-option cap — no chunking needed,
    unlike the admin RemoveSuggestionView.
    """

    def __init__(self, suggestions: list[dict], lang: str, db_id: int):
        super().__init__(timeout=120)
        self.db_id = db_id
        self.add_item(SelfRemoveSuggestionSelect(suggestions, lang))


class SelfRemoveSuggestionSelect(ui.Select):
    def __init__(self, suggestions: list[dict], lang: str):
        options = [
            discord.SelectOption(
                label=_remove_option_label(s),
                value=s["id"],
                description=format_layer_short(s)[:100] or None,
            )
            for s in suggestions if s.get("id")
        ]
        super().__init__(placeholder=t("self_remove.select", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        self.view.stop()  # retire the picker; replaced by the confirm dialog
        await _confirm_self_remove_suggestion(
            interaction, self.view.db_id, self.values[0], self.lang)


async def handle_remove_own_suggestion(interaction: discord.Interaction, db_id: int):
    """Let a user remove one of their own suggestions during the open phase,
    bounded by the per-event self-removal limit. Mirrors handle_suggest_start's
    validation order so behaviour stays consistent."""
    settings = db.get_guild_settings(interaction.guild_id)
    if not settings:
        await interaction.response.send_message(
            t("general.guild_not_configured", "en"), ephemeral=True)
        return

    lang = settings.get("language", "en")

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    if event.get("phase") != "suggestions_open":
        await interaction.response.send_message(t("suggest.not_open", lang), ephemeral=True)
        return

    limit = _self_removal_limit(event, settings)
    if limit <= 0:
        await interaction.response.send_message(
            t("self_remove.disabled", lang), ephemeral=True)
        return

    own = [s for s in event.get("suggestions", [])
           if str(s.get("user_id")) == str(interaction.user.id)]
    if not own:
        await interaction.response.send_message(
            t("self_remove.none", lang), ephemeral=True)
        return

    if _self_removals_used(event, interaction.user.id) >= limit:
        await interaction.response.send_message(
            t("self_remove.limit_reached", lang, max=limit), ephemeral=True)
        return

    embed = discord.Embed(
        title=t("self_remove.title", lang),
        description=t("self_remove.prompt", lang, count=len(own)),
        color=discord.Color.dark_red(),
    )
    await interaction.response.send_message(
        embed=embed, view=_bind(SelfRemoveSuggestionView(own, lang, db_id), interaction),
        ephemeral=True)


async def _confirm_self_remove_suggestion(interaction: discord.Interaction,
                                          db_id: int, suggestion_id: str,
                                          lang: str) -> None:
    """Two-step delete: show a confirmation embed, route Confirm to the actual
    removal. Re-validates the suggestion is still present and owned by the
    caller before prompting."""
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang),
                                color=discord.Color.red()),
            view=None,
        )
        return
    suggestion = next(
        (s for s in record["event"].get("suggestions", [])
         if s.get("id") == suggestion_id
         and str(s.get("user_id")) == str(interaction.user.id)),
        None,
    )
    if suggestion is None:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("self_remove.not_found", lang),
                                color=discord.Color.orange()),
            view=None,
        )
        return

    embed = discord.Embed(
        title=t("self_remove.confirm_title", lang),
        description=t("self_remove.confirm_prompt", lang,
                      layer=format_layer_short(suggestion))
        + _squadcalc_link_line(suggestion, lang),
        color=discord.Color.orange(),
    )

    async def confirm_cb(inter: discord.Interaction, _db_id: int):
        await do_remove_own_suggestion(inter, _db_id, suggestion_id)

    await interaction.response.edit_message(
        embed=embed,
        view=_bind(ConfirmActionView(lang, confirm_cb, db_id), interaction),
    )


async def do_remove_own_suggestion(interaction: discord.Interaction,
                                   db_id: int, suggestion_id: str):
    """Remove the caller's own suggestion and spend one self-removal. All
    ownership and limit checks are re-run inside the guild lock to guard
    against double-clicks and races."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    removed: Optional[dict] = None
    remaining_uses = 0
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang),
                                    color=discord.Color.red()),
                view=None,
            )
            return

        event = record["event"]
        if event.get("phase") != "suggestions_open":
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("suggest.not_open", lang),
                                    color=discord.Color.orange()),
                view=None,
            )
            return

        limit = _self_removal_limit(event, settings)
        used = _self_removals_used(event, interaction.user.id)
        if limit <= 0 or used >= limit:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    description=t("self_remove.limit_reached", lang, max=limit),
                    color=discord.Color.orange()),
                view=None,
            )
            return

        new_list: list[dict] = []
        for s in event.get("suggestions", []):
            if (removed is None and s.get("id") == suggestion_id
                    and str(s.get("user_id")) == str(interaction.user.id)):
                removed = s
                continue
            new_list.append(s)

        if removed is None:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("self_remove.not_found", lang),
                                    color=discord.Color.orange()),
                view=None,
            )
            return

        event["suggestions"] = new_list
        event.setdefault("user_removal_counts", {})[str(interaction.user.id)] = used + 1
        remaining_uses = limit - (used + 1)
        db.save_event(record["db_id"], event)

    # The per-user *suggestion* limit is computed live from event["suggestions"]
    # (see handle_suggest_start), so removal automatically frees a slot for the
    # user to suggest again.
    await _update_event_embed(db_id)

    await send_event_log(
        event, db_id,
        f"Suggestion self-removed by {interaction.user.display_name}: "
        f"{format_layer_short(removed)}",
        guild_id=interaction.guild_id,
        lang=lang,
    )

    await interaction.response.edit_message(
        embed=discord.Embed(
            description=t("self_remove.removed", lang,
                          layer=format_layer_short(removed),
                          remaining=remaining_uses),
            color=discord.Color.green()),
        view=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# EVENT EDIT DIALOG (DM)
# ═══════════════════════════════════════════════════════════════════════════
#
# Admin → Edit on the event embed opens a private DM dialog where the
# organizer can change this event's per-event configuration (gamemodes,
# blacklists, suggestion limits, voting params) without affecting any other
# event or the guild defaults. Replaces the old /config_gamemodes,
# /config_layer_sources, /config_blacklist, /config_suggestions slash
# commands; their picker UIs are reborn as views inside this dialog and
# write to event["config"] / event[...] instead of guild_settings.
#
# Session model: a single DM message (kept on the in-memory session dict) is
# re-rendered with different views for the property selector, list pickers,
# bool toggles, and scalar modals. Component interactions edit that message
# in place; modal submits edit it via the stored Message reference.

# user_id -> {db_id, guild_id, lang, dm_message, active_view, last_activity}
_active_edit_sessions: dict[int, dict] = {}

# Sessions older than this with no interaction are treated as stuck — the
# view's on_timeout (600s) should have cleared them by now, so a longer gap
# means something went wrong (bot disconnect, dropped on_timeout, etc.).
# Recovering automatically prevents the user from being permanently locked
# out by "Du hast bereits eine offene Bearbeitungssitzung".
SESSION_STALE_AFTER_SECONDS = 660


def _format_duration_seconds(seconds: int) -> str:
    """Render seconds as '60', '2h', '1d' style — round-trip via parse_duration_to_seconds."""
    s = int(seconds)
    if s <= 0:
        return "0"
    if s % 86400 == 0:
        return f"{s // 86400}d"
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def _format_property_value(value, kind: str) -> str:
    """Compact one-line display of a property's current value."""
    if kind == "string":
        if not value:
            return "—"
        s = str(value)
        return (s[:40] + "…") if len(s) > 40 else s
    if kind == "list":
        if not value:
            return "—"
        joined = ", ".join(value[:5])
        return joined + ("…" if len(value) > 5 else "")
    if kind == "bool":
        return "✅" if value else "❌"
    if kind == "duration":
        if not value:
            return "—"
        return _format_duration_seconds(int(value))
    if kind == "vote_duration":
        if not value:
            return "—"
        # value is stored as hours; reuse the seconds formatter for consistency.
        return _format_duration_seconds(int(value) * 3600)
    if kind == "duration_str":
        return value if value else "—"
    if kind == "datetime":
        if not value:
            return "—"
        try:
            return value.strftime("%d.%m.%Y %H:%M")
        except AttributeError:
            return str(value)
    if value is None:
        return "—"
    return str(value)


def _read_event_property(event: dict, key: str, target: str):
    """Read a property's current value from event[key] or event["config"][key]."""
    if target == "config":
        return (event.get("config") or {}).get(key)
    return event.get(key)


def _write_event_property(event: dict, key: str, target: str, value) -> None:
    """Write a property's value into event[key] or event["config"][key]."""
    if target == "config":
        event.setdefault("config", {})[key] = value
    else:
        event[key] = value


# Properties exposed by the DM edit dialog. Keys live on either event["config"]
# (the per-event snapshot of guild settings — Phase 2) or directly on the
# event (sources, voting params, suggestion timer). `source` is a callable
# returning the available choices for "list" kinds.
_EDIT_PROPERTIES: list[dict] = [
    {"key": "event_name",                "label_key": "edit.prop.event_name",            "kind": "string",   "target": "event"},
    {"key": "allowed_gamemodes",         "label_key": "edit.prop.allowed_gamemodes",     "kind": "list",     "target": "config", "source": db.get_unique_gamemodes},
    {"key": "blacklisted_maps",          "label_key": "edit.prop.blacklisted_maps",      "kind": "list",     "target": "config", "source": db.get_unique_maps},
    {"key": "blacklisted_factions",      "label_key": "edit.prop.blacklisted_factions",  "kind": "list",     "target": "config", "source": db.get_unique_factions},
    {"key": "blacklisted_units",         "label_key": "edit.prop.blacklisted_units",     "kind": "list",     "target": "config", "source": db.get_unique_unit_types},
    {"key": "max_suggestions_per_user",  "label_key": "edit.prop.max_per_user",          "kind": "int",      "target": "config", "min": 1,  "max": 10},
    {"key": "max_total_suggestions",     "label_key": "edit.prop.max_total",             "kind": "int",      "target": "config", "min": 1,  "max": 25},
    {"key": "max_self_removals_per_user","label_key": "edit.prop.max_self_removals",     "kind": "int",      "target": "config", "min": 0,  "max": 10},
    {"key": "history_lookback_events",   "label_key": "edit.prop.history_lookback",      "kind": "int",      "target": "config", "min": 0,  "max": 50},
    {"key": "allowed_sources",           "label_key": "edit.prop.allowed_sources",       "kind": "list",     "target": "event",  "source": db.get_unique_sources},
    {"key": "voting_duration_hours",     "label_key": "edit.prop.voting_duration",       "kind": "vote_duration", "target": "event"},
    {"key": "max_voting_layers",         "label_key": "edit.prop.max_voting_layers",     "kind": "int",      "target": "event",  "min": 1,  "max": 10},
    {"key": "allow_multiple_votes",      "label_key": "edit.prop.allow_multiple_votes",  "kind": "bool",     "target": "event"},
    {"key": "mirror_match",              "label_key": "edit.prop.mirror_match",          "kind": "bool",     "target": "event",  "note_key": "edit.prop.mirror_match_note"},
    {"key": "suggestion_duration_seconds", "label_key": "edit.prop.suggestion_duration", "kind": "duration", "target": "event"},
    {"key": "suggestion_start_time",     "label_key": "edit.prop.suggestion_start_time", "kind": "datetime", "target": "event"},
]


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

    kind = ""
    properties: list[dict] = []
    has_phase_lock = False

    def finish_link(self, guild_id, db_id, channel_id, lang) -> Optional[str]:
        """Markdown `[label](url)` appended to the Done message, or None."""
        return None

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

    def finish_link(self, guild_id, db_id, channel_id, lang):
        url = _event_message_url(guild_id, db_id)
        return f"[{t('edit.event_link', lang)}]({url})" if url else None

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

    def finish_link(self, guild_id, db_id, channel_id, lang):
        # No event message to point at — link back to the channel where
        # /config_defaults was run, the analog of the per-event "Go to event".
        if not channel_id:
            return None
        url = f"https://discord.com/channels/{guild_id}/{channel_id}"
        return f"[{t('edit.config_defaults_link', lang)}]({url})"

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


def _build_edit_main_embed(obj: dict, db_id, guild_id: int, lang: str,
                           updated_label: Optional[str] = None, *,
                           target: "EditTarget" = _EVENT_TARGET) -> discord.Embed:
    """Property overview embed shown at the top of every DM dialog state."""
    embed = discord.Embed(
        title=target.overview_title(obj, db_id, guild_id, lang),
        description=target.overview_description(lang),
        color=discord.Color.blurple(),
    )
    for num, prop in enumerate(target.properties, 1):
        value = target.read(obj, prop)
        formatted = _format_property_value(value, prop["kind"])
        embed.add_field(
            name=f"{num}. {t(prop['label_key'], lang)}",
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


async def admin_set_event_roles(interaction: discord.Interaction, db_id: int):
    """Open the role/user allow-list picker ephemerally for this event.

    Triggered by the Admin panel's "Edit Allow-list" button — the only entry
    point now that the standalone slash commands are gone. The Admin panel
    is itself organizer-gated (check_organizer at bot.py:1648), so this
    inherits the same auth.
    """
    settings = db.get_guild_settings(interaction.guild_id) or {}
    lang = settings.get("language", "en")

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang),
                                color=discord.Color.red()),
            view=None,
        )
        return

    event = record["event"]
    role_ids = list(event.get("allowed_role_ids") or [])
    user_ids = list(event.get("allowed_user_ids") or [])

    view = _bind(EventGateEditView(db_id, role_ids, user_ids, lang), interaction)
    embed = discord.Embed(
        title=t("roles.picker_title", lang),
        description=t("roles.picker_desc", lang,
                      event_label=display_name(event, db_id, lang=lang)),
        color=discord.Color.blurple(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


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
        "origin_channel_id": getattr(interaction, "channel_id", None),
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

    link = f"\n[{t('edit.dm_open_link', lang)}]({dm_msg.jump_url})"
    await _respond(discord.Embed(description=f"📨 {t('edit.dm_sent', lang)}{link}",
                                 color=discord.Color.green()))


async def admin_edit_event(interaction: discord.Interaction, db_id: int):
    """Kick off a DM edit session for this event. Triggered by Admin → Edit."""
    settings = db.get_guild_settings(interaction.guild_id) or {}
    lang = settings.get("language", "en")
    await _open_edit_session(interaction, target=_EVENT_TARGET, db_id=db_id,
                             guild_id=interaction.guild_id, lang=lang,
                             via_component=True)


def _close_session(user_id: int) -> None:
    _active_edit_sessions.pop(user_id, None)


def _event_message_url(guild_id: int, db_id: int) -> Optional[str]:
    """Build a Discord deep-link to the event's public message, or None.

    Returns None if the event is gone or its embed message hasn't been
    posted yet.
    """
    record = db.get_event_by_db_id(guild_id, db_id)
    if not record:
        return None
    msg_id = record["event"].get("event_message_id")
    if not msg_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{record['channel_id']}/{msg_id}"


def _set_active_view(user_id: int, view: ui.View) -> None:
    """Mark `view` as the currently displayed dialog view for this session.

    Also refreshes `last_activity`, which `admin_edit_event` consults to
    detect stuck sessions. _handle_edit_timeout consults `active_view` to
    ignore stale on_timeout callbacks from views the user has already
    navigated away from.
    """
    session = _active_edit_sessions.get(user_id)
    if session is not None:
        session["active_view"] = view
        session["last_activity"] = time.monotonic()


async def _force_close_stale_session(user_id: int) -> None:
    """Tear down a stuck edit session and disable its DM dialog if any.

    Called when `admin_edit_event` finds a leftover session that the view's
    on_timeout never cleared. Best-effort: HTTP errors editing the old DM
    are swallowed — the in-memory pop is the part that unblocks the user.
    """
    session = _active_edit_sessions.pop(user_id, None)
    if not session:
        return
    dm_msg = session.get("dm_message")
    if dm_msg is None:
        return
    try:
        await dm_msg.edit(view=None)
    except discord.HTTPException:
        pass


async def _handle_edit_timeout(view: ui.View, user_id: int) -> None:
    """Inform the user we gave up waiting and disable the dialog.

    Called from the on_timeout of any edit view. No-op if the session was
    already closed (e.g. the user pressed Done before the timer fired) or
    if `view` is a stale view the user has already navigated away from —
    each navigation creates a fresh view whose timer supersedes the old one.
    """
    session = _active_edit_sessions.get(user_id)
    if not session or session.get("active_view") is not view:
        return
    _active_edit_sessions.pop(user_id, None)
    lang = session.get("lang", "en")
    dm_msg = session.get("dm_message")
    if dm_msg is None:
        return
    try:
        await dm_msg.edit(view=None)
    except discord.HTTPException:
        pass
    try:
        await dm_msg.channel.send(t("edit.timeout", lang))
    except discord.HTTPException:
        pass


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
            discord.SelectOption(label=f"{num}. {t(prop['label_key'], lang)}"[:100], value=prop["key"])
            for num, prop in enumerate(target.properties, 1)
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
        session = _active_edit_sessions.get(self.user_id) or {}
        origin_channel_id = session.get("origin_channel_id")
        _close_session(self.user_id)
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass
        text = t("edit.finished", self.lang)
        link = self.target.finish_link(self.guild_id, self.db_id,
                                       origin_channel_id, self.lang)
        if link:
            text = f"{text} {link}"
        try:
            await interaction.channel.send(text)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


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


def _edit_list_embed(prop: dict, lang: str) -> discord.Embed:
    return discord.Embed(
        title=t(prop["label_key"], lang),
        description=t("edit.list_prompt", lang),
        color=discord.Color.blurple(),
    )


class EditListView(ui.View):
    """Buffered multi-select editor for list-typed properties.

    Selections accumulate in `self.selected`; nothing is written to the DB
    until the user presses Fertig. Pressing Abbrechen discards the buffer and
    the original value stays. Re-renders on each Select interaction so the
    dropdown's checkmarks reflect the in-progress state when reopened.
    """

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, choices: list[str], selected: set, *,
                 target: "EditTarget" = _EVENT_TARGET):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.choices = choices  # already truncated to <=25 by caller
        self.selected = set(selected)
        self.target = target

        options = [
            discord.SelectOption(label=c[:100], value=c, default=(c in self.selected))
            for c in choices
        ]
        select = ui.Select(
            placeholder=t("edit.list_placeholder", lang),
            options=options,
            min_values=0, max_values=len(options) or 1,
        )
        select.callback = self._on_select
        self.add_item(select)

        done = ui.Button(
            label=t("edit.done", lang),
            style=discord.ButtonStyle.success, emoji="✅",
        )
        done.callback = self._on_done
        self.add_item(done)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = set(interaction.data.get("values", []))
        new_view = EditListView(
            self.user_id, self.db_id, self.guild_id, self.lang,
            self.prop, self.choices, self.selected, target=self.target)
        _set_active_view(self.user_id, new_view)
        await interaction.response.edit_message(
            embed=_edit_list_embed(self.prop, self.lang), view=new_view)

    async def _on_done(self, interaction: discord.Interaction):
        new_value: list = sorted(self.selected)
        # `allowed_sources` uses [] as the canonical "all sources" form
        # (resolved dynamically via _resolve_event_sources). If the user
        # confirms with every visible source still selected, save [] so
        # future cache changes still auto-include new sources rather than
        # the event being frozen to the explicit list.
        if (self.prop["key"] == "allowed_sources"
                and self.selected == set(self.choices)):
            new_value = []
        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, new_value, target=self.target)

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class EditBoolView(ui.View):
    """Two-button toggle for bool properties."""

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, current_value: bool, *,
                 target: "EditTarget" = _EVENT_TARGET):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.target = target

        yes = ui.Button(
            label=t("edit.bool_yes", lang),
            style=discord.ButtonStyle.success if current_value else discord.ButtonStyle.secondary,
            emoji="✅",
        )
        yes.callback = self._make_setter(True)
        self.add_item(yes)

        no = ui.Button(
            label=t("edit.bool_no", lang),
            style=discord.ButtonStyle.danger if not current_value else discord.ButtonStyle.secondary,
            emoji="❌",
        )
        no.callback = self._make_setter(False)
        self.add_item(no)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    def _make_setter(self, value: bool):
        async def cb(interaction: discord.Interaction):
            await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                              self.lang, self.prop, value, target=self.target)
        return cb

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class EditScalarView(ui.View):
    """Wrapper for int/duration properties — opens a Modal on click.

    Modals can only be opened from a component interaction, so we have to
    chain Component → Modal rather than putting the TextInput in the view.
    """

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, *, target: "EditTarget" = _EVENT_TARGET):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.target = target

        edit = ui.Button(
            label=t("edit.open_input", lang),
            style=discord.ButtonStyle.primary, emoji="⌨️",
        )
        edit.callback = self._on_edit
        self.add_item(edit)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_edit(self, interaction: discord.Interaction):
        if self.prop["kind"] == "datetime":
            modal_cls = EditDateTimeModal
        elif self.prop["kind"] == "string":
            modal_cls = EditStringModal
        else:
            modal_cls = EditScalarModal
        modal = modal_cls(self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, target=self.target)
        await interaction.response.send_modal(modal)

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class EditDateTimeModal(ui.Modal):
    """Modal for the suggestion_start_time datetime property.

    Empty input clears the timestamp — that's the existing "manual phase"
    semantics from the creation wizard (bot.py — EventScheduleModal).
    """

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, *, target: "EditTarget" = _EVENT_TARGET):
        super().__init__(title=t(prop["label_key"], lang)[:45])
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.target = target

        self.value_input = ui.TextInput(
            label=t("edit.input_label", lang)[:45],
            placeholder="DD.MM.YYYY HH:MM",
            required=False,
            max_length=20,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.value_input.value or "").strip()
        if not raw:
            value = None
        else:
            try:
                value = datetime.strptime(raw, "%d.%m.%Y %H:%M")
            except ValueError:
                await interaction.response.send_message(
                    t("edit.invalid_datetime", self.lang, value=raw),
                    ephemeral=True,
                )
                return
        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, value, via_modal=True, target=self.target)


class EditScalarModal(ui.Modal):
    """Text-input modal for int / duration properties."""

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, *, target: "EditTarget" = _EVENT_TARGET):
        super().__init__(title=t(prop["label_key"], lang)[:45])
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.target = target

        if prop["kind"] == "int":
            placeholder = f"{prop.get('min', 0)}–{prop.get('max', '?')}"
        else:
            placeholder = DURATION_HINT
        self.value_input = ui.TextInput(
            label=t("edit.input_label", lang)[:45],
            placeholder=placeholder,
            required=prop["kind"] != "duration_str",
            max_length=20,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.value_input.value or "").strip()
        if self.prop["kind"] == "int":
            try:
                value = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    t("edit.invalid_int", self.lang, value=raw), ephemeral=True)
                return
            mn = self.prop.get("min")
            mx = self.prop.get("max")
            if (mn is not None and value < mn) or (mx is not None and value > mx):
                await interaction.response.send_message(
                    t("edit.out_of_range", self.lang, value=value, min=mn, max=mx),
                    ephemeral=True,
                )
                return
        elif self.prop["kind"] == "vote_duration":
            value = parse_voting_duration_input(raw)
            if value is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
                return
        elif self.prop["kind"] == "duration_str":
            ok, value = validate_duration_str(raw)
            if not ok:
                await interaction.response.send_message(
                    t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
                return
        else:  # duration
            value = parse_duration_to_seconds(raw)
            if value is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
                return

        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, value, via_modal=True, target=self.target)


class EditStringModal(ui.Modal):
    """Text-input modal for string properties (currently just `event_name`).

    Empty submission clears the value — for `event_name` this reverts the
    display to the `Event #{db_id}` fallback.
    """

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, *, target: "EditTarget" = _EVENT_TARGET):
        super().__init__(title=t(prop["label_key"], lang)[:45])
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.target = target

        self.value_input = ui.TextInput(
            label=t("edit.input_label", lang)[:45],
            required=False,
            max_length=EVENT_NAME_MAX_LENGTH,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        value = normalize_event_name(self.value_input.value)
        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, value, via_modal=True, target=self.target)


# ───────────────────────────────────────────────────────────────────────────
# Scoped blacklist editors (blacklisted_maps, blacklisted_factions)
#
# A flat multi-select is unusable when a source has dozens of maps/factions.
# Mirroring the suggest flow, the user first picks a source, then sees a
# bucketed picker (Small/Medium/Large for maps, single list for factions).
# Saves are scope-aware: only entries within the visible bucket(s) are
# replaced, so blacklisted items from other sources/buckets stay intact.
# ───────────────────────────────────────────────────────────────────────────

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


def _scoped_blacklist_embed(prop: dict, source: str, lang: str) -> discord.Embed:
    desc = (f"**{t('suggest.source_label', lang)}:** {source}\n{t('edit.list_prompt', lang)}"
            if source else t("edit.list_prompt", lang))
    return discord.Embed(
        title=t(prop["label_key"], lang),
        description=desc,
        color=discord.Color.blurple(),
    )


async def _show_scoped_blacklist_editor(
        interaction: discord.Interaction, user_id: int, db_id,
        guild_id: int, lang: str, prop: dict, source: str, *,
        target: "EditTarget" = _EVENT_TARGET) -> None:
    """Fresh entry into the bucketed picker.

    Builds the buckets from current DB state and snapshots the existing
    blacklist into each bucket's `selected` set. The user's subsequent
    Select interactions mutate that buffer in place; nothing is written to
    the DB until they press Fertig.
    """
    obj = target.load(guild_id, db_id)
    if obj is None:
        await _notify_event_gone(interaction, user_id, lang)
        return
    blacklist = target.read(obj, prop) or []
    bl_set = set(blacklist)
    source_filter = [source] if source else None

    if prop["key"] == "blacklisted_maps":
        maps = db.get_unique_maps(allowed_sources=source_filter)
        if not maps:
            await _bounce_to_main(interaction, user_id, db_id, guild_id, lang,
                                  t("cache.empty", lang), target=target)
            return
        sizes = db.get_map_sizes(allowed_sources=source_filter)
        groups = _group_maps_by_size(maps, sizes)
        buckets = [
            {"placeholder": f"{t(_SIZE_BUCKET_KEYS[k], lang)} ({len(items)})",
             "items": items,
             "selected": {m for m in items[:25] if m in bl_set}}
            for k, items in groups.items() if items
        ]
    else:  # blacklisted_factions
        factions = db.get_unique_factions(allowed_sources=source_filter)
        if not factions:
            await _bounce_to_main(interaction, user_id, db_id, guild_id, lang,
                                  t("cache.empty", lang), target=target)
            return
        buckets = [{
            "placeholder": t("edit.list_placeholder", lang),
            "items": factions,
            "selected": {f for f in factions[:25] if f in bl_set},
        }]

    view = ScopedBlacklistView(user_id, db_id, guild_id, lang, prop, source, buckets, target=target)
    _set_active_view(user_id, view)
    await interaction.response.edit_message(
        embed=_scoped_blacklist_embed(prop, source, lang), view=view)


class ScopedBlacklistSourceView(ui.View):
    """Source picker for blacklisted_maps / blacklisted_factions edits."""

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, sources: list[str], *,
                 target: "EditTarget" = _EVENT_TARGET):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.target = target

        options = [discord.SelectOption(label=s[:100], value=s) for s in sources[:25]]
        select = ui.Select(
            placeholder=t("suggest.select_source", lang),
            options=options, min_values=1, max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_select(self, interaction: discord.Interaction):
        source = interaction.data["values"][0]
        await _show_scoped_blacklist_editor(
            interaction, self.user_id, self.db_id, self.guild_id, self.lang,
            self.prop, source, target=self.target)

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class ScopedBlacklistView(ui.View):
    """Buffered multi-select editor for blacklisted_maps / blacklisted_factions.

    `buckets` is a list of `{placeholder, items, selected}` dicts — one Select
    per bucket. Maps get 1-3 size-grouped buckets; factions get a single
    bucket. The list is shared by reference across re-renders so each Select
    interaction mutates `selected` in place and the next view sees the latest
    state. Nothing is written to the DB until the user presses Fertig.
    Pressing Abbrechen discards the buffer; the original blacklist stays.
    """

    def __init__(self, user_id: int, db_id, guild_id: int, lang: str,
                 prop: dict, source: str, buckets: list, *,
                 target: "EditTarget" = _EVENT_TARGET):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.source = source
        self.buckets = buckets
        self.target = target

        for i, bucket in enumerate(buckets):
            items = bucket["items"]
            if not items:
                continue
            visible = items[:25]
            if len(items) > 25:
                logger.warning(
                    "scoped blacklist '%s' bucket %r in source '%s' has %d items; truncating to 25.",
                    prop["key"], bucket["placeholder"], source, len(items))
            sel = bucket["selected"]
            options = [
                discord.SelectOption(label=v[:100], value=v, default=(v in sel))
                for v in visible
            ]
            select = ui.Select(
                placeholder=bucket["placeholder"],
                options=options,
                min_values=0, max_values=len(options),
            )
            select.callback = self._make_callback(i)
            self.add_item(select)

        done = ui.Button(
            label=t("edit.done", lang),
            style=discord.ButtonStyle.success, emoji="✅",
        )
        done.callback = self._on_done
        self.add_item(done)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    def _make_callback(self, bucket_index: int):
        async def cb(interaction: discord.Interaction):
            # Buffer the selection for this bucket. The mutation propagates
            # across re-renders because `self.buckets` is the same list
            # object passed to the next ScopedBlacklistView instance below.
            self.buckets[bucket_index]["selected"] = set(
                interaction.data.get("values", []))

            new_view = ScopedBlacklistView(
                self.user_id, self.db_id, self.guild_id, self.lang,
                self.prop, self.source, self.buckets, target=self.target)
            _set_active_view(self.user_id, new_view)
            await interaction.response.edit_message(
                embed=_scoped_blacklist_embed(self.prop, self.source, self.lang),
                view=new_view,
            )
        return cb

    async def _on_done(self, interaction: discord.Interaction):
        # Commit: replace the visible-scope slice of the blacklist with the
        # buffered selection. Items outside the visible scope (other sources,
        # other buckets, or anything past the 25-item Select cap) stay
        # untouched. Runs inside the guild lock for atomicity.
        scope: set = set()
        selected: set = set()
        for bucket in self.buckets:
            scope.update(bucket["items"][:25])
            selected.update(bucket["selected"])

        def transform(current):
            return sorted((set(current or []) - scope) | selected)

        ok = await _persist_property_value(
            self.guild_id, self.db_id, self.prop, transform, target=self.target)
        if not ok:
            await _notify_event_gone(interaction, self.user_id, self.lang)
            return
        label = t(self.prop["label_key"], self.lang)
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, updated_label=label,
                                 target=self.target)

    async def _on_cancel(self, interaction: discord.Interaction):
        # Buffer is discarded simply by navigating away — it lives on this
        # view and is not persisted anywhere else.
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang, target=self.target)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


async def _persist_property_value(guild_id: int, db_id, prop: dict,
                                   value_or_transform, *,
                                   target: "EditTarget" = _EVENT_TARGET) -> bool:
    """Persist a property value via the target (lock + write + optional refresh).

    `value_or_transform` may be a value or a callable receiving the current
    value (invoked inside the lock for atomicity). Returns False when the
    underlying object is gone (event deleted); guild persist always returns True.
    """
    return await target.persist(guild_id, db_id, prop, value_or_transform)


async def _notify_event_gone(interaction: discord.Interaction, user_id: int,
                              lang: str, *, via_modal: bool = False) -> None:
    """Close the session and tell the user their event no longer exists."""
    _close_session(user_id)
    if via_modal:
        try:
            await interaction.response.send_message(
                t("event.no_event", lang), ephemeral=True)
        except discord.InteractionResponded:
            pass
    else:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang),
                                color=discord.Color.red()),
            view=None,
        )


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


# ═══════════════════════════════════════════════════════════════════════════
# EVENT EMBED UPDATE
# ═══════════════════════════════════════════════════════════════════════════

# Keyed by event db_id rather than (guild,channel) since multiple events may
# share a channel; debouncing must be per-event.
_display_update_tasks: dict[int, asyncio.Task] = {}

# Live vote-count refresh cadence. Short enough that the embed feels live
# during a 24h vote; long enough to stay well clear of Discord's per-channel
# message-edit rate limits even with multiple concurrent voting events.
LIVE_VOTE_REFRESH_SECONDS = 60

# db_id -> datetime of last live-vote embed refresh; throttles the periodic
# refresh in check_events_loop so we don't burn API calls every loop tick.
_last_vote_embed_refresh: dict[int, datetime] = {}


async def _update_event_embed(db_id: int):
    """Debounced update of a specific event's embed message."""
    task = _display_update_tasks.get(db_id)
    if task and not task.done():
        task.cancel()
    _display_update_tasks[db_id] = asyncio.create_task(_do_update_embed(db_id))


async def send_event_log(event: dict, db_id: int, message: str, *,
                         guild_id: int, level: str = "INFO",
                         mention_role_id: int = 0,
                         lang: str = "en") -> bool:
    """Send a log message that begins with the event's display name.

    Thin wrapper over `send_to_log_channel` for event-scoped log lines so
    organizers can tell which event a log entry refers to.
    """
    prefix = display_name(event, db_id, lang=lang)
    return await send_to_log_channel(
        f"**{prefix}** — {message}",
        guild_id=guild_id,
        level=level,
        mention_role_id=mention_role_id,
    )


async def _fetch_vote_counts(target: discord.abc.Messageable, event: dict) -> dict:
    """Read live per-suggestion vote counts from the poll message.

    Returns {suggestion_id: vote_count} for layers in the poll. Empty dict
    on any error — callers should treat that as "live counts unavailable"
    and fall back to a count-less embed.
    """
    poll_msg_id = event.get("poll_message_id")
    if not poll_msg_id:
        return {}
    try:
        msg = await target.fetch_message(poll_msg_id)
    except (discord.NotFound, discord.HTTPException):
        return {}
    if not getattr(msg, "poll", None):
        return {}

    text_to_count = {a.text: a.vote_count for a in msg.poll.answers}
    selected_ids = set(event.get("selected_for_vote") or [])
    counts: dict = {}
    for s in event.get("suggestions", []):
        sid = s.get("id")
        if sid not in selected_ids:
            continue
        text = format_layer_poll_option(s)
        if text in text_to_count:
            counts[sid] = text_to_count[text]
    return counts


async def _do_update_embed(db_id: int):
    """Actually update the event embed after a short delay."""
    await asyncio.sleep(2)

    record = db.get_active_event_unsafe(db_id)
    if not record:
        return
    guild_id = record["guild_id"]
    channel_id = record["channel_id"]
    event = record["event"]

    settings = db.get_guild_settings(guild_id)
    if not settings:
        return

    msg_id = event.get("event_message_id")
    if not msg_id:
        return

    try:
        guild = bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        # Fetch live vote counts during voting so the embed shows running
        # totals next to each polled layer. Failures degrade gracefully to
        # a count-less embed.
        vote_counts = None
        if event.get("phase") == "voting":
            target = await _resolve_poll_target(channel, event)
            vote_counts = await _fetch_vote_counts(target, event)

        embed = build_event_embed(event, settings, db_id, vote_counts=vote_counts)
        message = await channel.fetch_message(msg_id)

        lang = settings.get("language", "en")
        phase = event.get("phase", "created")
        view = _view_for_phase(db_id, phase, lang)
        await message.edit(embed=embed, view=view)
    except discord.NotFound:
        logger.warning(f"Event message {msg_id} not found in {channel_id}")
    except Exception as e:
        logger.error(f"Error updating event embed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — Setup & Config
# ═══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setup", description="Initial server setup for the Layer Vote Bot")
@app_commands.describe(
    organizer_role="The role that can manage events",
    log_channel="Channel for bot log messages",
    language="Bot language",
)
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Deutsch", value="de"),
])
async def cmd_setup(interaction: discord.Interaction, organizer_role: discord.Role,
                    log_channel: discord.TextChannel,
                    language: app_commands.Choice[str] = None):
    if not await check_admin(interaction):
        return

    lang_value = language.value if language else "en"

    settings = db.get_guild_settings(interaction.guild_id) or dict(db.DEFAULT_GUILD_SETTINGS)
    settings["organizer_role_id"] = organizer_role.id
    settings["log_channel_id"] = log_channel.id
    settings["language"] = lang_value
    db.save_guild_settings(interaction.guild_id, settings)

    set_log_channel(interaction.guild_id, log_channel)

    msg = t("setup.welcome", lang_value, role=organizer_role.mention,
            channel=log_channel.mention, language=lang_value.upper())
    await interaction.response.send_message(msg, ephemeral=True)
    await send_to_log_channel(f"Server setup by {interaction.user.display_name}", guild_id=interaction.guild_id)


@bot.tree.command(name="set_organizer_role", description="Change the organizer role")
@app_commands.describe(role="The new organizer role")
async def cmd_set_organizer_role(interaction: discord.Interaction, role: discord.Role):
    if not await check_admin(interaction):
        return
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    settings["organizer_role_id"] = role.id
    db.save_guild_settings(interaction.guild_id, settings)
    lang = settings.get("language", "en")
    await interaction.response.send_message(
        t("setup.organizer_role_updated", lang, role=role.mention), ephemeral=True)


@bot.tree.command(name="set_language", description="Change the bot language")
@app_commands.describe(language="Language (en/de)")
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Deutsch", value="de"),
])
async def cmd_set_language(interaction: discord.Interaction, language: app_commands.Choice[str]):
    if not await check_admin(interaction):
        return
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    settings["language"] = language.value
    db.save_guild_settings(interaction.guild_id, settings)
    await interaction.response.send_message(
        t("setup.language_updated", language.value, language=language.value.upper()), ephemeral=True)

    # Refresh all active event embeds in this guild so the language change takes effect
    for ev in db.get_all_active_events_global():
        if ev["guild_id"] == interaction.guild_id:
            await _update_event_embed(ev["db_id"])


@bot.tree.command(name="set_log_channel", description="Change the log channel")
@app_commands.describe(channel="The new log channel")
async def cmd_set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await check_admin(interaction):
        return
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    settings["log_channel_id"] = channel.id
    db.save_guild_settings(interaction.guild_id, settings)
    set_log_channel(interaction.guild_id, channel)
    lang = settings.get("language", "en")
    await interaction.response.send_message(
        t("setup.log_channel_updated", lang, channel=channel.mention), ephemeral=True)


@bot.tree.command(name="sync", description="Force sync slash commands")
async def cmd_sync(interaction: discord.Interaction):
    if not await check_admin(interaction):
        return
    await bot.tree.sync()
    await interaction.response.send_message("Commands synced.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# Per-event configuration is edited via the Admin → Edit DM dialog (see the
# `EVENT EDIT DIALOG` section). The previous /config_gamemodes,
# /config_layer_sources, /config_blacklist, and /config_suggestions slash
# commands wrote to guild-wide settings, which leaked across every event in
# the channel — they were removed in Phase 3 along with their picker views
# (BlacklistConfigView / GamemodeConfigView / SourceConfigView), whose logic
# now lives inside the DM dialog and writes to event["config"] instead.
# ═══════════════════════════════════════════════════════════════════════════


@bot.tree.command(name="refresh_layers", description="Re-fetch layer data from GitHub")
async def cmd_refresh_layers(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")
    await interaction.response.defer(ephemeral=True)

    try:
        count = await fetch_and_cache_layers()
        await interaction.followup.send(t("cache.refreshed", lang, count=count), ephemeral=True)
        await send_to_log_channel(f"Layer cache refreshed: {count} layers", guild_id=interaction.guild_id)
    except Exception as e:
        logger.error(f"Error refreshing layers: {e}")
        await interaction.followup.send(t("cache.error", lang, error=str(e)), ephemeral=True)


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


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — Event Management
# ═══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="create_layer_suggestion",
                  description="Create a new layer vote event in this channel")
async def cmd_create_event(interaction: discord.Interaction):
    """Open the event-creation wizard (modal → confirm view).

    All previous parameters moved into a wizard for a streamlined UX
    that mirrors the squad-event-discord-bot's create_event flow.
    """
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    if db.get_layer_cache_count() == 0:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    # Resolve which sources will be offered. Same logic as before — the
    # universe of cache sources, intersected with the guild's allowed list.
    offered = _resolve_offered_sources(settings)
    if not offered:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    await interaction.response.send_modal(EventScheduleModal(settings, lang, offered))


def _resolve_offered_sources(settings: dict) -> list[str]:
    """Sources to expose to event creators: cache ∩ guild default (or all if no default)."""
    cache_sources = db.get_unique_sources()
    guild_default = settings.get("allowed_sources") or []
    if guild_default:
        return [s for s in cache_sources if s in guild_default]
    return list(cache_sources)


class EventScheduleModal(ui.Modal):
    """Wizard step 1: collect schedule + duration text inputs.

    Modals only support TextInputs, so role/user gate, source picking,
    and the multi-vote toggle live on the follow-up EventCreateConfirmView.
    Suggestion start is strictly absolute (DD.MM.YYYY + HH:MM); the legacy
    "+offset" syntax was removed. If the guild has a default offset, we
    pre-fill the date/time fields with `now + offset` so admins can accept
    or edit the resolved time instead of writing it from scratch.
    """

    def __init__(self, settings: dict, lang: str, offered_sources: list[str]):
        super().__init__(title=t("event.wizard_title", lang), timeout=600)
        self.settings = settings
        self.lang = lang
        self.offered_sources = list(offered_sources)

        # Pre-fill start datetime from guild default offset, if any.
        prefill_start = ""
        default_offset = settings.get("default_suggestion_start") or ""
        if default_offset:
            offset_secs = parse_duration_to_seconds(default_offset)
            if offset_secs is not None:
                target = datetime.now() + timedelta(seconds=offset_secs)
                prefill_start = target.strftime("%d.%m.%Y %H:%M")

        sug_default = settings.get("default_suggestion_duration") or ""
        vote_hours = int(settings.get("default_voting_duration_hours", 24) or 24)
        vote_default = f"{vote_hours}h"

        self.event_name_input = ui.TextInput(
            label=t("event.wizard_name_label", lang),
            placeholder=t("event.wizard_name_placeholder", lang),
            required=False, max_length=EVENT_NAME_MAX_LENGTH,
        )
        self.add_item(self.event_name_input)

        self.start = ui.TextInput(
            label=t("event.wizard_start_label", lang),
            placeholder="DD.MM.YYYY HH:MM",
            required=False, max_length=16, default=prefill_start,
        )
        self.sug_duration = ui.TextInput(
            label=t("event.wizard_suggestion_duration_label", lang),
            placeholder=DURATION_HINT,
            required=False, max_length=10, default=sug_default,
        )
        self.vote_duration = ui.TextInput(
            label=t("event.wizard_vote_duration_label", lang),
            placeholder=DURATION_HINT,
            required=True, max_length=10, default=vote_default,
        )
        self.add_item(self.start)
        self.add_item(self.sug_duration)
        self.add_item(self.vote_duration)

    async def on_submit(self, interaction: discord.Interaction):
        lang = self.lang

        # Single combined "DD.MM.YYYY HH:MM" field; empty = manual phase.
        start_raw = self.start.value.strip()
        sst: Optional[datetime] = None
        if start_raw:
            try:
                sst = datetime.strptime(start_raw, "%d.%m.%Y %H:%M")
            except ValueError:
                await interaction.response.send_message(
                    t("event.wizard_invalid_date_time", lang, value=start_raw),
                    ephemeral=True)
                return

        suggestion_duration_seconds = None
        if self.sug_duration.value.strip():
            suggestion_duration_seconds = parse_duration_to_seconds(self.sug_duration.value)
            if suggestion_duration_seconds is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", lang, value=self.sug_duration.value),
                    ephemeral=True)
                return

        voting_duration_hours = parse_voting_duration_input(self.vote_duration.value)
        if voting_duration_hours is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", lang, value=self.vote_duration.value),
                ephemeral=True)
            return

        event_name = normalize_event_name(self.event_name_input.value)
        view = EventCreateConfirmView(
            lang=lang,
            sst=sst,
            suggestion_duration_seconds=suggestion_duration_seconds,
            voting_duration_hours=voting_duration_hours,
            offered_sources=self.offered_sources,
            allow_multiple_votes=bool(self.settings.get("default_allow_multiple_votes", False)),
            mirror_match=bool(self.settings.get("default_mirror_match", False)),
            event_name=event_name,
        )
        view = _bind(view, interaction)
        embed = discord.Embed(
            title=t("event.wizard_confirm_title", lang),
            description=t("event.wizard_confirm_desc", lang),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class EventCreateConfirmView(AutoDisableView):
    """Wizard step 2: gate selection, source picker, multi-vote toggle, Confirm.

    The schedule fields collected by EventScheduleModal are stashed on
    this view; the admin uses the visual selectors here for the things
    a modal can't host (Role/User pickers, multi-selects, toggles).
    """

    def __init__(self, lang, sst, suggestion_duration_seconds, voting_duration_hours,
                 offered_sources, allow_multiple_votes, event_name, mirror_match=False):
        super().__init__(timeout=300)
        self.lang = lang
        self.sst = sst
        self.suggestion_duration_seconds = suggestion_duration_seconds
        self.voting_duration_hours = voting_duration_hours
        self.offered_sources = list(offered_sources)
        self.allow_multiple_votes = bool(allow_multiple_votes)
        self.mirror_match = bool(mirror_match)
        self.event_name = event_name
        self.selected_role_ids: list[int] = []
        self.selected_user_ids: list[int] = []
        self.selected_sources: list[str] = list(offered_sources)

        # Row 0 — gate (mentionable: roles + members in one picker; optional)
        self.gate_select = ui.MentionableSelect(
            placeholder=t("event.wizard_gate_placeholder", lang),
            min_values=0,
            max_values=10,
            row=0,
        )
        self.gate_select.callback = self._gate_changed
        self.add_item(self.gate_select)

        # Row 1 — source select, only when there's a real choice
        self.source_select: Optional[ui.Select] = None
        if len(offered_sources) > 1:
            options = [
                discord.SelectOption(label=s, value=s, default=True)
                for s in offered_sources[:25]
            ]
            self.source_select = ui.Select(
                placeholder=t("event.select_sources_placeholder", lang),
                options=options,
                min_values=1,
                max_values=len(options),
                row=1,
            )
            self.source_select.callback = self._sources_changed
            self.add_item(self.source_select)

        # Row 2 — multi-vote + mirror-match toggles + Confirm
        self.multi_button = ui.Button(
            label=self._multi_label(),
            style=self._multi_style(),
            row=2,
        )
        self.multi_button.callback = self._multi_toggled
        self.add_item(self.multi_button)

        self.mirror_button = ui.Button(
            label=self._mirror_label(),
            style=self._mirror_style(),
            row=2,
        )
        self.mirror_button.callback = self._mirror_toggled
        self.add_item(self.mirror_button)

        self.confirm_button = ui.Button(
            label=t("button.confirm_selection", lang),
            style=discord.ButtonStyle.success,
            emoji="✅",
            row=2,
        )
        self.confirm_button.callback = self._confirm
        self.add_item(self.confirm_button)

    def _multi_label(self) -> str:
        key = "event.wizard_multi_on" if self.allow_multiple_votes else "event.wizard_multi_off"
        return t(key, self.lang)

    def _multi_style(self) -> discord.ButtonStyle:
        return discord.ButtonStyle.success if self.allow_multiple_votes else discord.ButtonStyle.secondary

    def _mirror_label(self) -> str:
        key = "event.wizard_mirror_on" if self.mirror_match else "event.wizard_mirror_off"
        return t(key, self.lang)

    def _mirror_style(self) -> discord.ButtonStyle:
        return discord.ButtonStyle.success if self.mirror_match else discord.ButtonStyle.secondary

    async def _gate_changed(self, interaction: discord.Interaction):
        roles: list[int] = []
        users: list[int] = []
        for v in self.gate_select.values:
            if isinstance(v, discord.Role):
                roles.append(v.id)
            else:
                users.append(v.id)
        self.selected_role_ids = roles
        self.selected_user_ids = users
        await interaction.response.defer()

    async def _sources_changed(self, interaction: discord.Interaction):
        self.selected_sources = list(self.source_select.values)
        await interaction.response.defer()

    async def _multi_toggled(self, interaction: discord.Interaction):
        self.allow_multiple_votes = not self.allow_multiple_votes
        self.multi_button.label = self._multi_label()
        self.multi_button.style = self._multi_style()
        await interaction.response.edit_message(view=self)

    async def _mirror_toggled(self, interaction: discord.Interaction):
        self.mirror_match = not self.mirror_match
        self.mirror_button.label = self._mirror_label()
        self.mirror_button.style = self._mirror_style()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction):
        if not self.selected_sources:
            await interaction.response.send_message(
                t("event.select_sources_required", self.lang), ephemeral=True)
            return
        self.stop()  # terminal: the wizard message is replaced by the created-event ack
        settings = db.get_guild_settings(interaction.guild_id) or {}
        await _finalize_event_creation(
            interaction, settings, self.lang,
            allowed_sources=self.selected_sources,
            sst=self.sst,
            suggestion_duration_seconds=self.suggestion_duration_seconds,
            voting_duration_hours=self.voting_duration_hours,
            allow_multiple_votes=self.allow_multiple_votes,
            mirror_match=self.mirror_match,
            allowed_role_ids=self.selected_role_ids,
            allowed_user_ids=self.selected_user_ids,
            ack_via_followup=True,
            event_name=self.event_name,
        )


async def _finalize_event_creation(interaction: discord.Interaction, settings: dict, lang: str,
                                   *, allowed_sources: list[str],
                                   sst, suggestion_duration_seconds,
                                   voting_duration_hours, allow_multiple_votes,
                                   allowed_role_ids: list[int],
                                   allowed_user_ids: list[int],
                                   ack_via_followup: bool,
                                   event_name: Optional[str] = None,
                                   mirror_match: bool = False):
    """Create the event row and post its embed.

    Sole call site is the EventCreateConfirmView confirm button — the
    wizard now always routes through that view, so there's no separate
    "single-source fast path" anymore (the view just hides the source
    select when there's nothing to pick).
    """
    event_data = db.build_default_event(suggestion_start_time=sst, settings=settings, event_name=event_name)
    event_data["voting_duration_hours"] = max(1, min(MAX_VOTING_DURATION_HOURS, voting_duration_hours))
    event_data["suggestion_duration_seconds"] = suggestion_duration_seconds
    event_data["allow_multiple_votes"] = bool(allow_multiple_votes)
    event_data["mirror_match"] = bool(mirror_match)
    event_data["allowed_sources"] = list(allowed_sources)
    event_data["allowed_role_ids"] = list(allowed_role_ids)
    event_data["allowed_user_ids"] = list(allowed_user_ids)

    # Create event in DB first to get its db_id; the EventActionView and the
    # follow-up update both need it baked into their button custom_ids.
    db_id = db.create_event(interaction.guild_id, interaction.channel_id, event_data)

    embed = build_event_embed(event_data, settings, db_id)
    view = EventActionView(db_id, lang, event_data.get("phase", "created"))
    msg = await interaction.channel.send(embed=embed, view=view)

    # Save message ID
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if record:
            event = record["event"]
            event["event_message_id"] = msg.id
            db.save_event(record["db_id"], event)

    ack_text = f"✅ {t('event.created', lang)}"
    if ack_via_followup:
        await interaction.response.edit_message(content=ack_text, embed=None, view=None)
    else:
        await interaction.response.send_message(ack_text, ephemeral=True)
    await send_event_log(
        event_data, db_id,
        f"Event created in <#{interaction.channel_id}> by {interaction.user.display_name} "
        f"(sources: {', '.join(allowed_sources)})",
        guild_id=interaction.guild_id,
        lang=lang,
    )


@bot.tree.command(name="delete_event", description="Delete the current event in this channel")
async def cmd_delete_event(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    view = _bind(ConfirmActionView(lang, _do_delete_event, db_id=db_id), interaction)
    await interaction.response.send_message(
        embed=discord.Embed(description=t("confirm.delete_event", lang), color=discord.Color.orange()),
        view=view,
        ephemeral=True,
    )


class EventGateEditView(AutoDisableView):
    """Ephemeral multi-select picker for the per-event role/user allow-list.

    Opened by the Admin panel's "Edit Allow-list" button (admin_set_event_roles).
    The MentionableSelect is multi-select; Submit REPLACES the allow-list — it
    is the canonical view of the current state, not an additive form. Empty
    submit clears the allow-list (event becomes open to everyone).
    """

    def __init__(self, db_id: int, role_ids: list[int], user_ids: list[int],
                 lang: str):
        super().__init__(timeout=300)
        self.db_id = db_id
        self.lang = lang
        self.selected_role_ids: list[int] = list(role_ids)
        self.selected_user_ids: list[int] = list(user_ids)

        # Pre-populate the picker with the existing allow-list. discord.py
        # 2.4+ exposes SelectDefaultValue / SelectDefaultValueType for this;
        # the AttributeError fallback keeps older runtimes from blowing up
        # at import — they just open with an empty selection.
        default_values = []
        try:
            for rid in role_ids:
                default_values.append(discord.SelectDefaultValue(
                    id=rid, type=discord.SelectDefaultValueType.role))
            for uid in user_ids:
                default_values.append(discord.SelectDefaultValue(
                    id=uid, type=discord.SelectDefaultValueType.user))
        except AttributeError:
            default_values = []

        gate_kwargs = dict(
            placeholder=t("roles.picker_placeholder", lang),
            min_values=0,
            max_values=25,
            row=0,
        )
        if default_values:
            gate_kwargs["default_values"] = default_values

        self.gate_select = ui.MentionableSelect(**gate_kwargs)
        self.gate_select.callback = self._on_select
        self.add_item(self.gate_select)

        submit = ui.Button(
            label=t("roles.picker_submit", lang),
            style=discord.ButtonStyle.success,
            emoji="✅",
            row=1,
        )
        submit.callback = self._on_submit
        self.add_item(submit)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            row=1,
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_select(self, interaction: discord.Interaction):
        roles: list[int] = []
        users: list[int] = []
        for v in self.gate_select.values:
            if isinstance(v, discord.Role):
                roles.append(v.id)
            else:
                users.append(v.id)
        self.selected_role_ids = roles
        self.selected_user_ids = users
        await interaction.response.defer()

    async def _on_submit(self, interaction: discord.Interaction):
        self.stop()  # terminal: the picker is replaced by the result message
        lock = _get_guild_lock(interaction.guild_id)
        async with lock:
            record = db.get_event_by_db_id(interaction.guild_id, self.db_id)
            if not record:
                await interaction.response.edit_message(
                    content=t("event.no_event", self.lang),
                    embed=None, view=None,
                )
                return
            event = record["event"]
            event["allowed_role_ids"] = list(self.selected_role_ids)
            event["allowed_user_ids"] = list(self.selected_user_ids)
            db.save_event(record["db_id"], event)

        if not self.selected_role_ids and not self.selected_user_ids:
            content = t("roles.cleared", self.lang)
        else:
            mentions = (
                [f"<@&{rid}>" for rid in self.selected_role_ids]
                + [f"<@{uid}>" for uid in self.selected_user_ids]
            )
            content = t("roles.replaced", self.lang, entries=", ".join(mentions))

        await interaction.response.edit_message(
            content=content, embed=None, view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await _update_event_embed(self.db_id)
        await send_event_log(
            event, self.db_id,
            f"Allow-list set by {interaction.user.display_name}: "
            f"{len(self.selected_role_ids)} role(s), {len(self.selected_user_ids)} user(s)",
            guild_id=interaction.guild_id,
            lang=self.lang,
        )

    async def _on_cancel(self, interaction: discord.Interaction):
        self.stop()  # terminal: retire so the timer can't grey out the result
        await interaction.response.edit_message(
            content=t("general.cancelled", self.lang),
            embed=None, view=None,
        )


@bot.tree.command(name="update", description="Refresh all event embeds in this server (organizer only)")
async def cmd_update(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    db_ids = [ev["db_id"] for ev in db.get_all_active_events_global()
              if ev["guild_id"] == interaction.guild_id]
    if not db_ids:
        await interaction.response.send_message(t("update.none", lang), ephemeral=True)
        return

    # The refresh itself is fire-and-forget (each _update_event_embed just
    # schedules a debounced background task), but defer up front so the ack
    # still lands inside Discord's 3s window as the global active-event scan
    # and per-event scheduling grow; reply via followup once they're queued.
    await interaction.response.defer(ephemeral=True)
    for db_id in db_ids:
        await _update_event_embed(db_id)
    await interaction.followup.send(
        t("update.refreshed", lang, count=len(db_ids)), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — User
# ═══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="history", description="View past winning layers")
@app_commands.describe(count="Number of past events to show (default 5)")
async def cmd_history(interaction: discord.Interaction, count: int = 5):
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    lang = settings.get("language", "en")
    history = db.get_recent_history(interaction.guild_id, interaction.channel_id,
                                    limit=min(count, 25))

    if not history:
        await interaction.response.send_message(t("history.empty", lang), ephemeral=True)
        return

    embed = discord.Embed(title=t("history.title", lang), color=discord.Color.gold())

    for entry in history:
        winner = entry.get("winning_layer")
        if not winner:
            continue
        embed.add_field(
            name=format_layer_short(winner),
            value=entry.get("completed_at", "?"),
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — History editing
# ═══════════════════════════════════════════════════════════════════════════

async def _handle_history_add_submit(interaction: discord.Interaction,
                                     state: SuggestState, lang: str):
    """Save the picked layer as a standalone voting_history entry."""
    settings = db.get_guild_settings(state.guild_id)
    lang = settings.get("language", "en") if settings else lang

    layer = {
        "id": str(uuid.uuid4())[:8],
        "user_id": str(interaction.user.id),
        "user_name": interaction.user.display_name,
        "map_name": state.map_name,
        "gamemode": state.gamemode,
        "layer_version": state.layer_version,
        "team1_faction": state.team1_faction,
        "team1_faction_name": _resolve_faction_name(state.layer_data, state.team1_faction, 1),
        "team1_unit": state.team1_unit,
        "team2_faction": state.team2_faction,
        "team2_faction_name": _resolve_faction_name(state.layer_data, state.team2_faction, 2),
        "team2_unit": state.team2_unit,
        "team1_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team1_faction, 1),
        "team2_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team2_faction, 2),
        "raw_name": state.mode_raw_name,
        "source": state.source,
        "suggested_at": datetime.now().isoformat(),
    }

    db.save_voting_history(state.guild_id, state.channel_id, [layer], layer)

    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('history.added', lang)}\n{format_layer_short(layer)}",
            color=discord.Color.green(),
        ),
        view=None,
    )
    await send_to_log_channel(
        f"History entry added by {interaction.user.display_name}: {format_layer_short(layer)}",
        guild_id=state.guild_id,
    )


@bot.tree.command(name="history_add",
                  description="Manually add a previously played layer to the history")
async def cmd_history_add(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    if db.get_layer_cache_count() == 0:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    state = SuggestState(interaction.guild_id, interaction.channel_id, flow="history_add")
    _suggest_sessions[interaction.user.id] = state

    # Source picker first, mirroring the suggest flow. There's no event to
    # resolve sources from, so the candidates are all known sources, capped
    # by guild-level allowed_sources via _resolve_event_sources({}, ...).
    sources = _resolve_event_sources({}, settings)
    if len(sources) > 1:
        options = [discord.SelectOption(label=s[:100], value=s) for s in sources[:25]]
        view = _bind(SourceSelectView(options, lang), interaction)
        embed = discord.Embed(
            title=t("history.add_title", lang),
            description=t("suggest.select_source", lang),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return

    # Single source (or no sources configured): skip the picker. The map
    # step renders the size-bucketed picker via _build_map_picker_view and
    # surfaces its own "no maps" error if applicable.
    state.source = sources[0] if sources else ""
    await _suggest_show_map_step(interaction, state, settings, lang, edit=False)


async def _remove_history_entry(interaction: discord.Interaction,
                                 entry_id_str: str, lang: str) -> None:
    """Delete the history row identified by `entry_id_str` and show the result."""
    try:
        entry_id = int(entry_id_str)
    except (TypeError, ValueError):
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.error", lang, error="bad id"),
                                color=discord.Color.red()),
            view=None,
        )
        return

    removed = db.delete_voting_history_entry(entry_id)
    if not removed:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("history.remove_not_found", lang),
                                color=discord.Color.red()),
            view=None,
        )
        return

    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('history.removed', lang)}",
            color=discord.Color.green(),
        ),
        view=None,
    )
    await send_to_log_channel(
        f"History entry {entry_id} removed by {interaction.user.display_name}",
        guild_id=interaction.guild_id,
    )


def _history_remove_bucketed(entries: list, lang: str) -> tuple:
    """Build the size-bucketed pick-one view for the given history entries.

    Returns (embed, view). Entries are grouped into Small/Medium/Large by
    their winning layer's map size — the same buckets used by the suggest
    and scoped-blacklist flows. Each non-empty bucket gets its own Select.
    """
    sizes = db.get_map_sizes()
    groups: dict[str, list] = {key: [] for key, _ in _SIZE_BUCKETS}
    for entry in entries:
        winner = entry.get("winning_layer") or {}
        groups[_bucket_for_size(sizes.get(winner.get("map_name", "")))].append(entry)

    view = HistoryRemoveBucketedView(groups, lang)
    embed = discord.Embed(
        title=t("history.remove_title", lang),
        description=t("history.remove_prompt", lang),
        color=discord.Color.blurple(),
    )
    return embed, view


class HistoryRemoveSourceView(AutoDisableView):
    """Source picker for /history_remove. Skipped when only one source is
    represented in the recent history."""

    def __init__(self, by_source: dict, lang: str):
        super().__init__(timeout=120)
        self.by_source = by_source
        self.lang = lang

        sources = sorted(by_source.keys())
        options = [
            discord.SelectOption(
                label=f"{'—' if s == '<unknown>' else s} ({len(by_source[s])})"[:100],
                value=s,
            )
            for s in sources[:25]
        ]
        select = ui.Select(
            placeholder=t("suggest.select_source", lang),
            options=options, min_values=1, max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_select(self, interaction: discord.Interaction):
        self.stop()  # retire the source picker; replaced by the bucket picker
        source = interaction.data["values"][0]
        embed, view = _history_remove_bucketed(
            self.by_source.get(source, []), self.lang)
        view = _bind(view, interaction)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _on_cancel(self, interaction: discord.Interaction):
        self.stop()  # terminal: retire so the timer can't grey out the result
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.cancelled", self.lang),
                                color=discord.Color.greyple()),
            view=None,
        )


class HistoryRemoveBucketedView(AutoDisableView):
    """Multi-bucket Selects of history entries. Picking one shows a
    confirmation dialog before the actual delete fires."""

    def __init__(self, groups: dict, lang: str):
        super().__init__(timeout=120)
        self.lang = lang
        # Keyed by str(entry_id) so the confirm step can format the layer
        # without re-querying the DB.
        self._entries_by_id: dict[str, dict] = {}

        for bucket_key, entries in groups.items():
            if not entries:
                continue
            label = t(_SIZE_BUCKET_KEYS[bucket_key], lang)
            options = []
            for entry in entries[:25]:
                self._entries_by_id[str(entry["id"])] = entry
                winner = entry.get("winning_layer") or {}
                opt_label = format_layer_poll_option(winner)
                date = str(entry.get("completed_at", ""))[:16]
                options.append(discord.SelectOption(
                    label=opt_label[:100],
                    value=str(entry["id"]),
                    description=date[:100] or None,
                ))
            select = ui.Select(
                placeholder=f"{label} ({len(entries)})",
                options=options, min_values=1, max_values=1,
            )
            select.callback = self._on_pick
            self.add_item(select)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_pick(self, interaction: discord.Interaction):
        self.stop()  # retire the bucket picker; replaced by the confirm dialog
        entry_id_str = interaction.data["values"][0]
        entry = self._entries_by_id.get(entry_id_str)
        await _confirm_history_remove(interaction, entry_id_str, entry, self.lang)

    async def _on_cancel(self, interaction: discord.Interaction):
        self.stop()  # terminal: retire so the timer can't grey out the result
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.cancelled", self.lang),
                                color=discord.Color.greyple()),
            view=None,
        )


async def _confirm_history_remove(interaction: discord.Interaction,
                                   entry_id_str: str, entry: Optional[dict],
                                   lang: str) -> None:
    """Show a confirm dialog before deleting a history entry.

    `entry` is the in-memory entry dict (used to render the layer in the
    prompt). It can be None if lookup failed — we still confirm by ID,
    but the prompt is less informative.
    """
    if entry is not None:
        layer_str = format_layer_poll_option(entry.get("winning_layer") or {})
    else:
        layer_str = ""
    embed = discord.Embed(
        title=t("history.confirm_remove_title", lang),
        description=t("history.confirm_remove_prompt", lang, layer=layer_str),
        color=discord.Color.orange(),
    )

    async def confirm_cb(inter: discord.Interaction, _db_id: int):
        await _remove_history_entry(inter, entry_id_str, lang)

    await interaction.response.edit_message(
        embed=embed,
        view=_bind(ConfirmActionView(lang, confirm_cb), interaction),
    )


@bot.tree.command(name="history_remove",
                  description="Remove an entry from the voting history")
async def cmd_history_remove(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")
    history = db.get_recent_history(interaction.guild_id, interaction.channel_id, limit=25)
    if not history:
        await interaction.response.send_message(t("history.empty", lang), ephemeral=True)
        return

    # Group by source so we can show a source picker first when multiple
    # sources are represented (mirroring /history_add and the suggest flow).
    # Legacy entries without a `source` field land under a sentinel key —
    # Discord's SelectOption.value doesn't accept the empty string.
    by_source: dict[str, list] = {}
    for entry in history:
        winner = entry.get("winning_layer")
        if not winner:
            continue
        by_source.setdefault(winner.get("source") or "<unknown>", []).append(entry)

    if not by_source:
        await interaction.response.send_message(t("history.empty", lang), ephemeral=True)
        return

    if len(by_source) == 1:
        # Skip source picker — straight to the bucketed entry list.
        entries = next(iter(by_source.values()))
        embed, view = _history_remove_bucketed(entries, lang)
        view = _bind(view, interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return

    view = _bind(HistoryRemoveSourceView(by_source, lang), interaction)
    embed = discord.Embed(
        title=t("history.remove_title", lang),
        description=t("suggest.select_source", lang),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND TASK — Check events loop
# ═══════════════════════════════════════════════════════════════════════════

async def _handle_suggestion_timeout(db_id: int, guild_id: int, channel_id: int):
    """Fire when a suggestion phase's auto-close timer expires.

    - If suggestions count fits in max_voting_layers: phase -> voting and
      auto-start the poll with every suggestion.
    - Otherwise: phase -> suggestions_closed and ping the organizer role in
      the log channel so they can run manual selection.
    """
    settings = db.get_guild_settings(guild_id) or {}
    lang = settings.get("language", "en")
    organizer_role_id = settings.get("organizer_role_id", 0) or 0

    auto_started_ids: Optional[list[str]] = None
    needs_selection = False
    suggestion_count = 0
    max_voting = 10

    lock = _get_guild_lock(guild_id)
    async with lock:
        rec = db.get_event_by_db_id(guild_id, db_id)
        if not rec:
            return
        event = rec["event"]
        if event.get("phase") != "suggestions_open":
            return

        suggestions = event.get("suggestions", [])
        suggestion_count = len(suggestions)
        max_voting = min(int(event.get("max_voting_layers", 10) or 10), 10)
        # Clear the timer so we don't fire twice.
        event["suggestion_end_time"] = None

        if suggestion_count == 0 or suggestion_count <= max_voting:
            # Auto-start voting with every suggestion (or transition to a
            # no-suggestion completed state if there are none).
            if suggestion_count == 0:
                event["phase"] = "suggestions_closed"
                needs_selection = False
            else:
                selected_ids = [s["id"] for s in suggestions]
                event["selected_for_vote"] = selected_ids
                event["phase"] = "voting"
                auto_started_ids = selected_ids
        else:
            event["phase"] = "suggestions_closed"
            needs_selection = True

        db.save_event(rec["db_id"], event)

    if auto_started_ids:
        ok = await _auto_start_poll(db_id, auto_started_ids)
        if ok:
            await send_event_log(
                event, db_id,
                t("phase.auto_vote_started", lang, count=len(auto_started_ids)),
                guild_id=guild_id,
                lang=lang,
            )
            return
        # Poll creation failed — fall through to manual-selection path so
        # the organizer can still act.
        lock2 = _get_guild_lock(guild_id)
        async with lock2:
            rec = db.get_event_by_db_id(guild_id, db_id)
            if rec and rec["event"].get("phase") == "voting":
                rec["event"]["phase"] = "suggestions_closed"
                rec["event"]["selected_for_vote"] = []
                db.save_event(rec["db_id"], rec["event"])
        needs_selection = True

    await _update_event_embed(db_id)

    if needs_selection:
        mention = f"<@&{organizer_role_id}>" if organizer_role_id else ""
        msg = t(
            "phase.selection_needed", lang,
            mention=mention,
            channel_id=channel_id,
            count=suggestion_count,
            max=max_voting,
        )
        await send_event_log(
            event, db_id,
            msg,
            guild_id=guild_id,
            level="WARNING",
            mention_role_id=organizer_role_id,
            lang=lang,
        )
    elif suggestion_count == 0:
        await send_event_log(
            event, db_id,
            f"Suggestion phase auto-closed with 0 suggestions in <#{channel_id}>",
            guild_id=guild_id,
            level="WARNING",
            lang=lang,
        )


async def check_events_loop():
    """Background loop that checks for scheduled events."""
    await bot.wait_until_ready()
    logger.info("Background event check loop started.")

    while not bot.is_closed():
        sleep_time = EVENT_CHECK_INTERVAL

        try:
            events = db.get_all_active_events_global()
            now = datetime.now()

            for record in events:
                event = record["event"]
                guild_id = record["guild_id"]
                channel_id = record["channel_id"]
                db_id = record["db_id"]
                phase = event.get("phase", "created")

                # Auto-open suggestions
                if phase == "created":
                    sst = event.get("suggestion_start_time")
                    if sst and isinstance(sst, datetime):
                        seconds_until = (sst - now).total_seconds()
                        if seconds_until <= 0:
                            lock = _get_guild_lock(guild_id)
                            async with lock:
                                rec = db.get_event_by_db_id(guild_id, db_id)
                                if rec and rec["event"].get("phase") == "created":
                                    rec["event"]["phase"] = "suggestions_open"
                                    # Propagate the optional auto-close window
                                    # configured at event-creation time.
                                    dur = rec["event"].get("suggestion_duration_seconds")
                                    if dur:
                                        rec["event"]["suggestion_end_time"] = (
                                            now + timedelta(seconds=int(dur))
                                        )
                                    db.save_event(rec["db_id"], rec["event"])
                            await _update_event_embed(db_id)
                            lang = db.get_guild_language(guild_id)
                            await send_event_log(
                                event, db_id,
                                f"Suggestion phase auto-opened in <#{channel_id}>",
                                guild_id=guild_id,
                                lang=lang,
                            )
                        elif seconds_until < EVENT_CRITICAL_WINDOW:
                            sleep_time = EVENT_CHECK_INTERVAL_FAST

                # Auto-close suggestions when their timer expires
                if phase == "suggestions_open":
                    set_end = event.get("suggestion_end_time")
                    if set_end and isinstance(set_end, datetime):
                        seconds_until = (set_end - now).total_seconds()
                        if seconds_until <= 0:
                            await _handle_suggestion_timeout(db_id, guild_id, channel_id)
                        elif seconds_until < EVENT_CRITICAL_WINDOW:
                            sleep_time = EVENT_CHECK_INTERVAL_FAST

                # Refresh the embed periodically so live vote counts stay
                # current while the poll is still running. Throttled to
                # LIVE_VOTE_REFRESH_SECONDS per event.
                if phase == "voting":
                    last_refresh = _last_vote_embed_refresh.get(db_id)
                    if (last_refresh is None or
                            (now - last_refresh).total_seconds() >= LIVE_VOTE_REFRESH_SECONDS):
                        _last_vote_embed_refresh[db_id] = now
                        await _update_event_embed(db_id)

                # Check if poll has ended (voting phase)
                if phase == "voting":
                    poll_msg_id = event.get("poll_message_id")
                    if poll_msg_id:
                        try:
                            guild = bot.get_guild(guild_id)
                            if guild:
                                channel = guild.get_channel(channel_id)
                                if channel:
                                    target = await _resolve_poll_target(channel, event)
                                    message = await target.fetch_message(poll_msg_id)
                                    if message.poll and message.poll.is_finalised():
                                        lock = _get_guild_lock(guild_id)
                                        tied: list[dict] = []
                                        winner = None
                                        finalized = False
                                        async with lock:
                                            rec = db.get_event_by_db_id(guild_id, db_id)
                                            if rec and rec["event"].get("phase") == "voting":
                                                finalized = True
                                                winner, tied = await _resolve_poll_result(channel, rec["event"])
                                                if tied:
                                                    # Draw: park it; an organizer resolves via the embed buttons.
                                                    _enter_draw_pending(rec["event"], tied)
                                                    db.save_event(rec["db_id"], rec["event"])
                                                else:
                                                    _complete_event_with_winner(rec["event"], winner)
                                                    db.save_event(rec["db_id"], rec["event"])
                                                    if winner:
                                                        db.save_voting_history(
                                                            guild_id, channel_id,
                                                            rec["event"].get("suggestions", []),
                                                            winner,
                                                        )
                                        if finalized:
                                            await _update_event_embed(db_id)
                                            lang = db.get_guild_language(guild_id)
                                            if tied:
                                                result_str = "draw between " + ", ".join(
                                                    format_layer_short(s) for s in tied)
                                            else:
                                                result_str = "Winner: " + (
                                                    format_layer_short(winner) if winner else "None")
                                            await send_event_log(
                                                event, db_id,
                                                f"Poll ended in <#{channel_id}>. {result_str}",
                                                guild_id=guild_id,
                                                lang=lang,
                                            )
                        except discord.NotFound:
                            pass
                        except Exception as e:
                            logger.error(f"Error checking poll {poll_msg_id}: {e}")

        except Exception as e:
            logger.error(f"Error in background loop: {e}")

        await asyncio.sleep(sleep_time)


# ═══════════════════════════════════════════════════════════════════════════
# BOT EVENTS
# ═══════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Initialize log channels from saved settings
    for guild in bot.guilds:
        settings = db.get_guild_settings(guild.id)
        if settings and settings.get("log_channel_id"):
            channel = guild.get_channel(settings["log_channel_id"])
            if channel:
                set_log_channel(guild.id, channel)

    # Auto-fetch layers if cache is empty
    if db.get_layer_cache_count() == 0:
        logger.info("Layer cache is empty, fetching...")
        try:
            count = await fetch_and_cache_layers()
            logger.info(f"Cached {count} layers on startup")
        except Exception as e:
            logger.error(f"Failed to fetch layers on startup: {e}")

    # Notify all configured log channels that the bot is online
    for guild in bot.guilds:
        await send_to_log_channel(f"Layer Vote Bot connected as {bot.user}", guild_id=guild.id)

    # Start background loop (only once, even if on_ready fires again on reconnect)
    if not getattr(bot, "_background_loop_started", False):
        bot._background_loop_started = True
        bot.loop.create_task(check_events_loop())


# ═══════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db.init_db()
    bot.run(TOKEN)
