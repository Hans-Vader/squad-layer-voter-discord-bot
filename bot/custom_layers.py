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
