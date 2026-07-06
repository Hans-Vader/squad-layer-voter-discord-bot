#!/usr/bin/env python3
"""
Utility functions for the Layer Vote Bot.

Permission checks, embed builders, layer formatting helpers.
"""

import logging
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import urlencode

import discord
from discord import Embed

from i18n import t
from config import ADMIN_IDS, LAYERS_JSON_SOURCES, SQUADCALC_BASE_URL

logger = logging.getLogger("layer_vote")

# ---------------------------------------------------------------------------
# Event name helpers
# ---------------------------------------------------------------------------

EVENT_NAME_MAX_LENGTH = 100


def normalize_event_name(raw: Optional[str]) -> Optional[str]:
    """Normalize a user-supplied event name.

    Strips whitespace, collapses CR/LF to spaces (titles are single-line),
    and returns None for empty input so the display layer falls back to the
    `Event #{db_id}` form.
    """
    if not raw:
        return None
    cleaned = raw.replace("\r", " ").replace("\n", " ").strip()
    return cleaned or None


def display_name(event: dict, db_id: int, *, lang: str = "en") -> str:
    """Render the event's display name with fallback to `Event #{db_id}`.

    `db_id` is the SQLite row id (lives on the wrapper record, not inside
    the event blob — see `database.get_event_by_db_id`). Single source of
    truth for the fallback rule — every embed title, thread name, log
    prefix, and admin-panel title calls this.
    """
    name = (event.get("event_name") or "").strip()
    if name:
        return name
    return t("event.fallback_name", lang, db_id=db_id)


# Discord caps thread (and channel) names at 100 characters; create_thread
# raises HTTPException if exceeded. Since a composed name like
# "Voting — {event_label}" can run up to ~109 chars (event names alone go to
# EVENT_NAME_MAX_LENGTH = 100), truncate defensively so a long event title can
# never break thread creation — which, for a gated event, would otherwise fall
# back to posting the poll in the open channel with no allow-list enforcement.
DISCORD_THREAD_NAME_MAX_LENGTH = 100


def truncate_thread_name(name: str,
                         *, max_length: int = DISCORD_THREAD_NAME_MAX_LENGTH) -> str:
    """Truncate a composed thread name to Discord's length limit.

    Appends an ellipsis when truncation occurs so the name reads as visibly
    shortened rather than abruptly cut. Returns `name` unchanged when it
    already fits. The result never exceeds `max_length`.
    """
    if len(name) <= max_length:
        return name
    return name[:max_length - 1].rstrip() + "…"


# Layer source whose factionIds and map names map cleanly to SquadCalc params.
# Layers from any other source still get a clickable map icon, but the URL
# points at SquadCalc's homepage (since the params would 404) and the
# layer-specific info is surfaced via a hover tooltip on the link instead.
SQUADCALC_COMPATIBLE_SOURCE = "main"

# ---------------------------------------------------------------------------
# Log channel — set per guild at runtime
# ---------------------------------------------------------------------------

_log_channels: dict[int, discord.TextChannel] = {}


def set_log_channel(guild_id: int, channel: discord.TextChannel):
    _log_channels[guild_id] = channel


def get_log_channel(guild_id: int) -> Optional[discord.TextChannel]:
    return _log_channels.get(guild_id)


async def send_to_log_channel(message: str, guild: discord.Guild = None,
                              guild_id: int = None, level: str = "INFO",
                              mention_role_id: int = 0):
    """Send a formatted message to the guild's log channel.

    When `mention_role_id` is given and non-zero, the role is pinged via
    `AllowedMentions(roles=True)` — used when organizers need to act (e.g.
    too many suggestions to auto-start voting).
    """
    gid = guild_id or (guild.id if guild else None)
    if not gid:
        return False

    getattr(logger, level.lower(), logger.info)(message)

    channel = _log_channels.get(gid)
    if not channel:
        return False

    icons = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🚨"}
    labels = {"INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}
    icon = icons.get(level, "ℹ️")
    label = labels.get(level, "INFO")
    formatted = f"{icon} **{label}**: {message}"

    try:
        if mention_role_id:
            await channel.send(
                formatted,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        else:
            await channel.send(formatted)
        return True
    except Exception as e:
        logger.error(f"Failed to send to log channel: {e}")
        return False


# ---------------------------------------------------------------------------
# Role / permission checks
# ---------------------------------------------------------------------------

def has_organizer_role(user, organizer_role_id: int) -> bool:
    """Check if user has the guild's organizer role or is a bot-level admin."""
    if hasattr(user, "id") and str(user.id) in ADMIN_IDS:
        return True
    if not hasattr(user, "roles"):
        return False
    if organizer_role_id == 0:
        return False
    return any(role.id == organizer_role_id for role in user.roles)


def is_guild_admin(user) -> bool:
    """Check if user has Discord administrator permission or is bot-level admin."""
    if hasattr(user, "id") and str(user.id) in ADMIN_IDS:
        return True
    if hasattr(user, "guild_permissions"):
        return user.guild_permissions.administrator
    return False


def check_role_gate(event: dict, user) -> bool:
    """Check if a user is allowed to participate in a gated event.

    The event's `allowed_role_ids` and `allowed_user_ids` form the allow-list.
    Both empty = no gate (anyone allowed). Bot-level admins always pass so
    operators can test restricted events without being on the allow-list.
    Organizers do NOT auto-bypass: the gate is about participation, not
    moderation, and an organizer running the event isn't automatically on
    the team that's supposed to vote.
    """
    if hasattr(user, "id") and str(user.id) in ADMIN_IDS:
        return True
    role_ids = event.get("allowed_role_ids") or []
    user_ids = event.get("allowed_user_ids") or []
    if not role_ids and not user_ids:
        return True
    if hasattr(user, "id") and str(user.id) in [str(uid) for uid in user_ids]:
        return True
    if hasattr(user, "roles"):
        user_role_ids = {r.id for r in user.roles}
        if any(rid in user_role_ids for rid in role_ids):
            return True
    return False


# ---------------------------------------------------------------------------
# Layer formatting
# ---------------------------------------------------------------------------

def format_layer_short(suggestion: dict) -> str:
    """Format a layer suggestion as a short one-line string.

    Example: "Al Basrah AAS v1 — USMC/CombinedArms vs RGF/Mechanized"
    """
    map_name = suggestion.get("map_name", "?")
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = suggestion.get("team1_unit", "?")
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = suggestion.get("team2_unit", "?")

    mode_str = f"{gamemode} {version}".strip() if version else gamemode
    return f"{map_name} {mode_str} — {t1_faction}/{t1_unit} vs {t2_faction}/{t2_unit}"


# Vehicle-class display: class token -> (emoji, label, sort rank). Ranked so
# combat assets list before logistics/transport. Tokens come from the layers
# JSON `Units[].vehicles[].vehType`, EXCEPT "BOAT" which is derived from
# spawnerSize (see _vehicle_class) — boats span several vehTypes (ULTV RHIBs,
# "RHIB Logistics" is LOGI) so spawnerSize is the reliable signal. Unknown
# tokens fall back to a generic car.
_VEHTYPE = {
    "MBT":  ("⚔️", "MBT", 1),
    "IFV":  ("🛡️", "IFV", 2),
    "APC":  ("🚐", "APC", 3),
    "TD":   ("🎯", "ATGM", 4),
    "MGS":  ("💥", "MGS", 5),
    "SPA":  ("💣", "Artillery", 6),
    "SPAA": ("🛰️", "AA", 7),
    "RSV":  ("🔭", "Recon", 8),
    "AH":   ("🚁", "Attack Heli", 9),
    "UH":   ("🚁", "Heli", 10),
    "MRAP": ("🚙", "MRAP", 11),
    "LTV":  ("🛻", "LTV", 12),
    "ULTV": ("🏍️", "Light", 13),
    "MSV":  ("🚜", "MSV", 14),
    "TRAN": ("🚚", "Transport", 15),
    "LOGI": ("📦", "Logistics", 16),
    "BOAT": ("🛥️", "Boat", 17),
}


def _vehicle_class(v: dict) -> str:
    """Effective display class for a vehicle. Boats (spawnerSize "BOAT", e.g.
    RHIBs) get a dedicated "BOAT" class regardless of their vehType; everything
    else uses its vehType token."""
    if v.get("spawnerSize") == "BOAT":
        return "BOAT"
    return v.get("vehType", "")


def _vehtype_info(token: str) -> tuple:
    return _VEHTYPE.get(token, ("🚗", token or "?", 99))


def format_vehicle_list(vehicles: list, lang: str = "en") -> str:
    """Render a unit's vehicle list as embed text, combat classes first.

    One line per vehicle: "{emoji} {count}× {name} [{class}]". Capped to a
    Discord field's 1024 chars via fit_lines_to_field (with a localized
    "+N more" tail). Returns the localized "no vehicles" string when empty.
    """
    if not vehicles:
        return t("vehicles.none", lang)
    ordered = sorted(
        vehicles,
        key=lambda v: (_vehtype_info(_vehicle_class(v))[2], v.get("name", "")),
    )
    lines = []
    for v in ordered:
        emoji, label, _ = _vehtype_info(_vehicle_class(v))
        count = v.get("count", 1)
        lines.append(f"{emoji} {count}× {v.get('name', '?')} [{label}]")
    return fit_lines_to_field(lines, lambda n: t("vehicles.more", lang, count=n))


def build_squadcalc_url(suggestion: dict) -> Optional[str]:
    """Build a SquadCalc URL for the given suggestion, or None if disabled.

    Only returns a parameterized URL for layers from the SquadCalc-compatible
    source ("main"); SPM/SU layers don't round-trip into SquadCalc params, so
    callers should handle non-main sources via build_map_icon_markdown.
    """
    if not SQUADCALC_BASE_URL:
        return None

    source = suggestion.get("source") or ""
    if source and source != SQUADCALC_COMPATIBLE_SOURCE:
        return None

    map_name = suggestion.get("map_name", "")
    if not map_name:
        return None

    sc_map = map_name.replace(" ", "").replace("'", "")

    gamemode = suggestion.get("gamemode", "")
    version = suggestion.get("layer_version", "")
    sc_layer = f"{gamemode}{version}" if version else gamemode

    params = {"map": sc_map, "layer": sc_layer}

    t1 = suggestion.get("team1_faction")
    t2 = suggestion.get("team2_faction")
    if t1:
        params["team1"] = t1
    if t2:
        params["team2"] = t2

    t1u = suggestion.get("team1_unit")
    t2u = suggestion.get("team2_unit")
    # Prefix (LO, LD, MO, S, …) is layer + team dependent. Stored on the
    # suggestion at submit time; fall back to LO for legacy rows.
    t1_prefix = suggestion.get("team1_unit_prefix") or "LO"
    t2_prefix = suggestion.get("team2_unit_prefix") or "LO"
    if t1u and t1u != "Default":
        params["team1unit"] = f"{t1}_{t1_prefix}_{t1u}"
    if t2u and t2u != "Default":
        params["team2unit"] = f"{t2}_{t2_prefix}_{t2u}"

    return f"{SQUADCALC_BASE_URL}/?{urlencode(params)}"


def _build_layer_tooltip(suggestion: dict) -> str:
    """One-line tooltip text: map + mode + full faction names."""
    map_name = suggestion.get("map_name", "?")
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    mode_str = f"{gamemode} {version}".strip() if version else gamemode

    t1_name = suggestion.get("team1_faction_name") or suggestion.get("team1_faction") or "?"
    t2_name = suggestion.get("team2_faction_name") or suggestion.get("team2_faction") or "?"

    text = f"{map_name} {mode_str} — {t1_name} vs {t2_name}"
    # Markdown link titles are quoted with `"`; replace any embedded quotes so
    # the link doesn't break (e.g. SU_IRGC's name contains "Saberin Unit").
    return text.replace('"', "'")


# No-op masked-link target used to attach a hover tooltip to the 🗺 icon for
# non-main-source layers — Discord requires a URL on masked links, but we
# don't want to send users to SquadCalc since it doesn't recognize SPM/SU
# maps or factions. discord.com keeps the click inside the user's Discord.
_TOOLTIP_NOOP_URL = SQUADCALC_BASE_URL


def build_map_icon_markdown(suggestion: dict) -> str:
    """Render the 🗺️ map icon for embeds.

    Both main and SPM/SU layers go through the same link template; only the
    URL target differs (SquadCalc when usable, a no-op Discord URL otherwise).
    The hover tooltip — map + version + full faction names — is identical
    across sources. Falls back to a plain emoji when no URL is available
    (e.g. SquadCalc disabled and main source).
    """
    url = build_squadcalc_url(suggestion) or _fallback_icon_url(suggestion)
    if not url:
        return "🗺️"
    return f'[🗺️]({url} "{_build_layer_tooltip(suggestion)}")'


def _fallback_icon_url(suggestion: dict) -> Optional[str]:
    """No-op masked-link target for non-main sources, or None if main."""
    source = suggestion.get("source") or ""
    if source and source != SQUADCALC_COMPATIBLE_SOURCE:
        return _TOOLTIP_NOOP_URL
    return None


def format_suggestion_entry(index: int, suggestion: dict,
                            vote_count: Optional[int] = None) -> str:
    """Format a suggestion as a two-line embed entry.

    When ``vote_count`` is given, prepend a 🗳️ vote-count indicator before
    the map icon. Pass ``None`` (the default) during phases where a poll
    doesn't exist yet to render without the prefix. The submitter's name
    is rendered on a second line below the suggestion.
    """
    gamemode = suggestion.get("gamemode", "?")
    gm_short = _GAMEMODE_ABBREV.get(gamemode, gamemode)

    map_name = _MAP_NAME_ABBREV.get(suggestion.get("map_name", "?"), suggestion.get("map_name", "?"))
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = _UNIT_ABBREV.get(suggestion.get("team1_unit", "?"), suggestion.get("team1_unit", "?"))
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = _UNIT_ABBREV.get(suggestion.get("team2_unit", "?"), suggestion.get("team2_unit", "?"))

    version = suggestion.get("layer_version", "")
    user_name = suggestion.get("user_name", "?")

    mode_str = f"{gm_short} {version}".strip() if version else gm_short

    map_icon = build_map_icon_markdown(suggestion)

    prefix = f"🗳️ **{vote_count}** | " if vote_count is not None else ""
    return (
        f"{prefix}{map_icon} **{index}. {map_name}**: {mode_str} "
        f"⚔️ {t1_faction}/{t1_unit} vs {t2_faction}/{t2_unit}\n"
        f"👤 {user_name}"
    )


_RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def format_vote_bar(count: int, total: int, width: int = 10) -> str:
    """Unicode block bar whose length is the count's share of ``total``.

    Scaling to the total (not the leader) keeps the bar and the printed
    percentage consistent. All empty when ``total`` is 0 (no votes yet); at
    least one filled block when ``count > 0`` so an option with real votes is
    never invisible.
    """
    if total <= 0:
        filled = 0
    else:
        # Half-up rounding (int(x + 0.5)); Python's round() is half-to-even and
        # would render a block short at exact .5 boundaries.
        filled = min(int(count / total * width + 0.5), width)
        if count > 0 and filled == 0:
            filled = 1
    return "█" * filled + "░" * (width - filled)


def _format_vote_result_entry(rank: int, suggestion: dict, count: int,
                              total: int) -> str:
    """Render one ranked voting entry: a bar line above the layer detail.

    The bar line carries a 🥇/🥈/🥉 medal for the top three (only when that
    option has votes — no medals on a fresh all-zero poll), a block bar sized
    to the option's share of ``total``, the raw count and — when any votes
    exist — that share as a percentage. The detail lines reuse
    ``format_suggestion_entry`` (with ``vote_count=None`` to drop the inline
    🗳️ prefix; ``rank`` becomes the "N." label).
    """
    prefix = f"{_RANK_MEDALS[rank]} " if rank in _RANK_MEDALS and count > 0 else ""
    line = f"{prefix}`{format_vote_bar(count, total)}` {count}"
    if total > 0:
        # Half-up, and floor a real vote to 1% so a filled bar block is never
        # labelled 0% (mirrors format_vote_bar's min-one-block guard).
        pct = max(1, int(count / total * 100 + 0.5)) if count > 0 else 0
        line += f" · {pct}%"
    return f"{line}\n{format_suggestion_entry(rank, suggestion, vote_count=None)}"


_GAMEMODE_ABBREV = {
    "TerritoryControl": "TC",
    "Invasion": "INV",
}

_MAP_NAME_ABBREV = {
    "Kamdesh Highlands": "Kamdesh",
    "Pacific Proving Grounds": "Pacific",
}

_UNIT_ABBREV = {
    "LightInfantry": "LightInf",
    "CombinedArms": "CombArms"
}


def format_layer_poll_option(suggestion: dict) -> str:
    """Format a layer for use in a Discord poll option (max 55 chars for poll answers)."""
    map_name = _MAP_NAME_ABBREV.get(suggestion.get("map_name", "?"), suggestion.get("map_name", "?"))
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = _UNIT_ABBREV.get(suggestion.get("team1_unit", "?"), suggestion.get("team1_unit", "?"))
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = _UNIT_ABBREV.get(suggestion.get("team2_unit", "?"), suggestion.get("team2_unit", "?"))

    gm_short = _GAMEMODE_ABBREV.get(gamemode, gamemode)
    mode_str = f"{gm_short}{version}".strip() if version else gm_short
    text = f"{map_name} {mode_str} {t1_faction}({t1_unit}) vs {t2_faction}({t2_unit})"
    if len(text) > 55:
        text = text[:52] + "..."
    return text


def suggestion_matches(s1: dict, s2: dict) -> bool:
    """Check if two suggestions represent the exact same layer combination."""
    keys = ("map_name", "gamemode", "layer_version",
            "team1_faction", "team1_unit", "team2_faction", "team2_unit")
    return all(s1.get(k) == s2.get(k) for k in keys)


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

_SUPERMOD_SOURCE = "supermod"


def _event_uses_supermod(event: dict, settings: dict) -> bool:
    """Whether the supermod layer source is among this event's active sources.

    Mirrors the precedence used by bot._resolve_event_sources without needing
    a database lookup: explicit event sources first, then the guild cap, then
    the full configured source list as a legacy fallback.
    """
    explicit = event.get("allowed_sources") or []
    guild_allowed = settings.get("allowed_sources") or []

    if explicit:
        candidate = list(explicit)
    elif guild_allowed:
        candidate = list(guild_allowed)
    else:
        candidate = [name for name, _ in LAYERS_JSON_SOURCES]

    if explicit and guild_allowed:
        candidate = [s for s in candidate if s in guild_allowed]

    return _SUPERMOD_SOURCE in candidate


def fit_lines_to_field(lines: list[str],
                       more_label: Callable[[int], str],
                       max_len: int = 1024) -> str:
    """Join ``lines`` with newlines so the result fits a Discord embed field.

    A field value is capped at ``max_len`` (1024) characters. If every line
    fits, returns them joined as-is. Otherwise keeps the longest leading run of
    lines that still fits alongside a trailing summary produced by
    ``more_label(dropped)`` — a callable given the number of omitted lines that
    returns the localized "… and N more" string.
    """
    joined = "\n".join(lines)
    if len(joined) <= max_len:
        return joined
    for keep in range(len(lines) - 1, -1, -1):
        candidate = "\n".join(lines[:keep] + [more_label(len(lines) - keep)])
        if len(candidate) <= max_len:
            return candidate
    # Pathological: even the summary line alone exceeds max_len.
    return more_label(len(lines))[:max_len]


def _embed_total_chars(embed: Embed) -> int:
    """Return total character count of an embed (Discord limit: 6000)."""
    total = len(embed.title or "") + len(embed.description or "")
    total += len(embed.footer.text) if embed.footer else 0
    total += len(embed.author.name) if embed.author else 0
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total


def build_event_embed(event: dict, settings: dict, db_id: int,
                      vote_counts: Optional[dict] = None) -> Embed:
    """Build the main event embed displayed in the channel.

    During the voting phase, callers can pass `vote_counts` (mapping
    suggestion id → live vote_count from the poll) to render per-layer
    counts inline. Suggestions not in `selected_for_vote` aren't part of
    the poll and stay countless.
    """
    phase = event.get("phase", "created")
    lang = settings.get("language", "en")

    title = display_name(event, db_id, lang=lang)

    color_map = {
        "created": discord.Color.greyple(),
        "suggestions_open": discord.Color.green(),
        "suggestions_closed": discord.Color.orange(),
        "voting": discord.Color.blue(),
        "completed": discord.Color.gold(),
    }
    color = color_map.get(phase, discord.Color.greyple())

    embed = Embed(title=title, color=color)

    # Status field
    if phase == "created":
        sst = event.get("suggestion_start_time")
        if sst and isinstance(sst, datetime):
            ts = int(sst.timestamp())
            status_text = t("embed.status_created", lang, ts=ts)
        else:
            status_text = t("embed.status_created_manual", lang)
    elif phase == "suggestions_open":
        end_time = event.get("suggestion_end_time")
        if end_time and isinstance(end_time, datetime):
            ts = int(end_time.timestamp())
            status_text = t("embed.status_suggestions_open_until", lang, ts=ts)
        else:
            status_text = t("embed.status_suggestions_open", lang)
    elif phase == "suggestions_closed":
        count = len(event.get("suggestions", []))
        status_text = t("embed.status_suggestions_closed", lang, count=count)
    elif phase == "voting":
        end_time = event.get("voting_end_time")
        if end_time and isinstance(end_time, datetime):
            ts = int(end_time.timestamp())
            status_text = t("embed.status_voting_until", lang, ts=ts)
        else:
            status_text = t("embed.status_voting", lang)
    elif phase == "completed":
        status_text = t("embed.status_completed", lang)
    else:
        status_text = phase

    embed.add_field(name=t("embed.status", lang), value=status_text, inline=False)

    # Suggestions
    suggestions = event.get("suggestions", [])

    # Per-event config wins over the guild default; fall back to 25
    # (DEFAULT_GUILD_SETTINGS["max_total_suggestions"]).
    max_total = (event.get("config") or {}).get(
        "max_total_suggestions", settings.get("max_total_suggestions", 25))

    if phase in ("suggestions_open", "suggestions_closed", "voting"):
        if suggestions:
            # A non-empty vote_counts dict means the poll is live and we have
            # per-layer counts → show a sorted bar-chart of the ballot only.
            # None (non-voting) or {} (poll fetch failed) falls back to the
            # plain, submission-order listing.
            use_bars = phase == "voting" and vote_counts

            if use_bars:
                # The ballot = the layers _start_poll actually put on the poll:
                # those in selected_for_vote, capped to Discord's 10-answer
                # limit, in suggestion order. Deriving it from selected_for_vote
                # (not from vote_counts keys) keeps a still-votable layer visible
                # even when _fetch_vote_counts couldn't read its count — e.g. its
                # recomputed poll-option text no longer matches the frozen poll
                # answer — so it shows 0 rather than vanishing from the board.
                selected_ids = set(event.get("selected_for_vote") or [])
                ballot = [s for s in suggestions if s.get("id") in selected_ids][:10]
                counts = {s["id"]: vote_counts.get(s["id"], 0) for s in ballot}
                # Descending by votes; a stable sort keeps submission order
                # among ties.
                ballot.sort(key=lambda s: -counts[s["id"]])
                total = sum(counts.values())
                entries = [
                    _format_vote_result_entry(rank, s, counts[s["id"]], total)
                    for rank, s in enumerate(ballot, 1)
                ]
                header = f"📋 {t('embed.live_results_header', lang)}"
            else:
                entries = [
                    format_suggestion_entry(i, s)
                    for i, s in enumerate(suggestions, 1)
                ]
                header = f"📋 {t('embed.suggestions_header', lang)} ({len(suggestions)}/{max_total})"

            # One embed field per entry so Discord spaces them uniformly — a
            # shared field that packs entries together gaps only at
            # its boundaries, which looks uneven. Guarded against Discord's
            # 25-field and 6000-char embed caps: on overflow the tail collapses
            # into a single "… and N more" field. The Status field already holds
            # one of the 25 slots, leaving 24 for entries + that tail.
            for idx, entry in enumerate(entries):
                name = header if idx == 0 else "\u200b"
                remaining = len(entries) - idx
                out_of_fields = len(embed.fields) >= 24 and remaining > 1
                out_of_chars = (idx > 0
                                and _embed_total_chars(embed) + len(name) + len(entry) > 6000)
                if idx > 0 and (out_of_fields or out_of_chars):
                    embed.add_field(
                        name="\u200b",
                        value=t("embed.suggestions_more", lang, count=remaining),
                        inline=False,
                    )
                    break
                embed.add_field(name=name, value=entry, inline=False)

        else:
            embed.add_field(
                name=f"📋 {t('embed.suggestions_header', lang)} ({len(suggestions)}/{max_total})",
                value=t("embed.no_suggestions", lang),
                inline=False,
            )

    # Winner (completed phase)
    if phase == "completed":
        winner = event.get("winning_layer")
        if winner:
            map_name = winner.get("map_name", "?")
            gamemode = winner.get("gamemode", "?")
            version = winner.get("layer_version", "")
            t1_id = winner.get("team1_faction", "?")
            t2_id = winner.get("team2_faction", "?")
            # Prefer the human-readable factionName captured at submit time;
            # fall back to the factionId for legacy history rows.
            t1 = winner.get("team1_faction_name") or t1_id
            t2 = winner.get("team2_faction_name") or t2_id
            t1u = winner.get("team1_unit", "?")
            t2u = winner.get("team2_unit", "?")

            mode_str = f"{gamemode} {version}".strip() if version else gamemode

            map_icon = build_map_icon_markdown(winner)

            winner_text = (
                f"{map_icon} **{map_name}** — {mode_str}\n"
                f"⚔️ {t1}/{t1u} vs {t2}/{t2u}"
            )

            embed.add_field(
                name=f"🏆 {t('embed.winner_header', lang)}",
                value=winner_text,
                inline=False,
            )

            # Per-team vehicle layout, resolved & stored on the winner at vote
            # completion. Absent on legacy/history winners → skip silently.
            t1_veh = winner.get("team1_vehicles")
            t2_veh = winner.get("team2_vehicles")
            if t1_veh:
                embed.add_field(
                    name=f"🚛 Team 1 — {t1}/{t1u} {t('vehicles.label', lang)}"[:256],
                    value=format_vehicle_list(t1_veh, lang),
                    inline=False,
                )
            if t2_veh:
                embed.add_field(
                    name=f"🚛 Team 2 — {t2}/{t2u} {t('vehicles.label', lang)}"[:256],
                    value=format_vehicle_list(t2_veh, lang),
                    inline=False,
                )

            # Ready-to-copy Squad RCON command to set the winning layer. The
            # string is built at vote completion (bot.build_admin_change_layer)
            # and stored on the event; a fenced code block renders as a
            # one-tap-copy block on mobile.
            command = event.get("winning_layer_command")
            if command:
                embed.add_field(
                    name=f"⚙️ {t('embed.admin_command_header', lang)}",
                    value=f"```\n{command}\n```",
                    inline=False,
                )

    # Footer: when the supermod source is active, the legend takes the slot
    # so users can decode SPM/SU and GoingDark prefixes (the SquadCalc hint
    # is suppressed since SquadCalc has no supermod map data anyway).
    # Otherwise we fall back to the SquadCalc hint, which only makes sense
    # when clickable map icons are visible (during/after suggestions, or on
    # a completed winner).
    if _event_uses_supermod(event, settings):
        embed.set_footer(text=t("embed.footer_legend_supermod", lang))
    else:
        show_squadcalc_hint = (
            phase in ("suggestions_open", "suggestions_closed", "voting")
            and event.get("suggestions")
        ) or (phase == "completed" and event.get("winning_layer"))
        if show_squadcalc_hint:
            embed.set_footer(text=t("embed.footer", lang))
    return embed
