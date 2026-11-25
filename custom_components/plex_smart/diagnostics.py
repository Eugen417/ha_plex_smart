"""Diagnostics support for SmartPlex with strict privacy & media redaction."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AI_ENTITY,
    CONF_ALIASES,
    CONF_DEFAULT_ZONE,
    CONF_DISPLAY_DEVICE_ID,
    CONF_DISPLAY_ENTITY,
    CONF_DISPLAY_SOURCE,
    CONF_EXCLUDE_GENRES,
    CONF_HARDWARE_DEVICE_ID,
    CONF_HARDWARE_ENTITY,
    CONF_INCLUDE_GENRES,
    CONF_LIBRARIES,
    CONF_PLEX_CLIENT,
    CONF_PLEX_DEVICE_ID,
    CONF_REMOTE_ENTITY,
    CONF_ROULETTE_NAME,
    CONF_SCAN_BUTTON,
    CONF_SERVER_ID,
    CONF_SERVER_NAME,
    CONF_TOKEN,
    CONF_URL,
    CONF_USER_TITLE,
    CONF_ZONE_NAME,
    DOMAIN,
)
from .models import PlexSmartRuntimeData

TO_REDACT = {
    CONF_TOKEN,
    CONF_URL,
    CONF_USER_TITLE,
    CONF_SERVER_ID,
    CONF_SERVER_NAME,
    CONF_PLEX_DEVICE_ID,
    CONF_HARDWARE_DEVICE_ID,
    CONF_DISPLAY_DEVICE_ID,
    CONF_DISPLAY_ENTITY,
    CONF_DISPLAY_SOURCE,
    CONF_PLEX_CLIENT,
    CONF_HARDWARE_ENTITY,
    CONF_REMOTE_ENTITY,
    CONF_SCAN_BUTTON,
    CONF_AI_ENTITY,
    CONF_ZONE_NAME,
    CONF_ALIASES,
    CONF_ROULETTE_NAME,
    CONF_LIBRARIES,
    CONF_INCLUDE_GENRES,
    CONF_EXCLUDE_GENRES,
    CONF_DEFAULT_ZONE,
    "area_id",
    "matched_title",
    "title",
    "display_title",
    "player_title",
    "summary",
    "thumb",
    "token",
    "x-plex-token",
    "secret",
    "password",
    "ip",
    "host",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return fully redacted diagnostics without tokens, IPs, names, or media titles."""
    runtime: PlexSmartRuntimeData | None = getattr(entry, "runtime_data", None)
    index = runtime.coordinator.data if runtime else None

    subentries_diag = [
        {
            "index": idx,
            "subentry_type": sub.subentry_type,
            "data": async_redact_data(dict(sub.data), TO_REDACT),
        }
        for idx, sub in enumerate(entry.subentries.values(), start=1)
    ]

    return {
        "integration": {
            "domain": DOMAIN,
            "subentries_count": len(subentries_diag),
        },
        "config_entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "subentries": subentries_diag,
        },
        "index_stats": {
            "sections_count": len(index.sections) if index else 0,
            "movies_count": len(index.items_by_type.get("movie", [])) if index else 0,
            "shows_count": len(index.items_by_type.get("show", [])) if index else 0,
            "music_count": len(index.items_by_type.get("music", [])) if index else 0,
            "playlists_count": len(index.playlists) if index else 0,
            "genres_count": len(index.all_genres) if index else 0,
            "active_sessions_count": len(index.active_sessions) if index else 0,
        },
        "last_launch": (
            {
                "success": runtime.last_launch.success,
                "start_phase": runtime.last_launch.start_phase,
                "media_type": runtime.last_launch.media_type,
                "boot_seconds": runtime.last_launch.boot_seconds,
                "retries_used": runtime.last_launch.retries_used,
                "was_handoff": bool(runtime.last_launch.handoff_from),
            }
            if runtime and runtime.last_launch
            else None
        ),
    }

