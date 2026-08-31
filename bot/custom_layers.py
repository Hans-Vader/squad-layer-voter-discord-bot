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

import database as db
from config import LAYERS_JSON_SOURCES
from utils import SQUADCALC_COMPATIBLE_SOURCE

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
    and any surrounding whitespace is ignored. Deduplication is case-insensitive;
    first spelling of each unique layer is preserved.
    """
    seen: dict[str, str] = {}  # lowercased name → first spelling seen
    for line in (text or "").splitlines():
        name = _BULLET_RE.sub("", line.strip()).strip()
        if name:
            seen.setdefault(name.lower(), name)
    return list(seen.values())


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

    # Reject layers with no gamemode token (e.g., "Belaya_v1")
    for n in names:
        if n not in invalid:  # only check if not already invalid from regex
            _, mode_token, _ = split_raw_name(n)
            if not mode_token:
                invalid.append(n)

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


def resolve_reference_source() -> Optional[str]:
    """The cached source whose faction and unit data custom layers borrow.

    Prefers the SquadCalc-compatible main-game source, then the first
    configured source that is actually cached, then whatever is there. Returns
    None when no fetched layers are cached at all — custom layers cannot be
    materialized in that state, since there is no faction metadata to copy.
    """
    cached = db.get_fetched_sources()
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


def materialize_custom_layers(guild_id: Optional[int] = None,
                               map_name: Optional[str] = None) -> int:
    """Expand the stored custom maps into layer_cache rows. Returns rows written.

    Idempotent — upsert_layer keys on (raw_name, source) — so it is safe to run
    after a refresh, on boot and after every save. A no-op when no fetched
    source is cached, since there would be no faction metadata to borrow.

    `map_name` narrows the work to that one map of `guild_id` (pass both
    together). This exists because a save answers a Discord interaction and
    cannot afford to rewrite every map of the guild: measured against a
    1260-row cache, materializing one map takes ~0.7s but a 25-map guild takes
    ~13s — well past the ~3s window before Discord invalidates the
    interaction token. `guild_id` alone still materializes every map of that
    guild, and no arguments still materializes every guild — both existing
    callers (on_ready, /refresh_layers) legitimately want that.
    """
    if map_name is not None:
        if guild_id is None:
            raise ValueError("map_name requires guild_id")
        entries = [e for e in db.get_custom_maps(guild_id)
                   if e["map_name"] == map_name]
    else:
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
    """Store one custom map and materialize it. Returns layers actually cached.

    The cached rows are dropped first so a re-save that removes a layer doesn't
    leave the old one behind — upsert alone would never delete it.
    """
    db.upsert_custom_map(guild_id, map_name, {
        "layers": list(raw_names),
        "factions": list(faction_ids),
        "units": list(unit_types),
    })
    db.delete_layers(db.custom_source(guild_id), map_name)
    # Narrowed to this one map — a save answers a Discord interaction and
    # cannot afford to rewrite every map of the guild (see
    # materialize_custom_layers). It also silently writes nothing when no
    # reference source is cached, so report what actually reached the cache.
    materialize_custom_layers(guild_id, map_name)
    return db.count_layers(db.custom_source(guild_id), map_name)


def remove_custom_map(guild_id: int, map_name: str) -> bool:
    """Delete a custom map and its materialized rows."""
    db.delete_layers(db.custom_source(guild_id), map_name)
    return db.delete_custom_map(guild_id, map_name)
