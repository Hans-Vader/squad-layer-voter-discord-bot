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
    and any surrounding whitespace is ignored.
    """
    seen: dict[str, None] = {}
    for line in (text or "").splitlines():
        name = _BULLET_RE.sub("", line.strip()).strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


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
