"""Sensor platform for SmartPlex (library stats, active sessions, last launch)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_IS_DEFAULT,
    CONF_ROULETTE_NAME,
    CONF_SERVER_ID,
    CONF_SERVER_NAME,
    CONF_ZONE_NAME,
    DOMAIN,
    SIGNAL_LAST_LAUNCH_UPDATED,
)
from .coordinator import SmartPlexCoordinator
from .models import PlexMediaItem, PlexSmartRuntimeData
from .search import get_roulette_subentries, get_zone_subentries


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SmartPlex sensor entities."""
    runtime: PlexSmartRuntimeData = entry.runtime_data
    async_add_entities(
        [
            SmartPlexLibraryStatsSensor(entry, runtime.coordinator),
            SmartPlexActiveSessionsSensor(entry, runtime.coordinator),
            SmartPlexRecentlyAddedSensor(entry, runtime.coordinator),
            SmartPlexLastLaunchSensor(entry),
        ]
    )


def build_service_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build shared DeviceInfo for the SmartPlex controller service device."""
    server_id = str(entry.data.get(CONF_SERVER_ID) or entry.entry_id)
    server_name = str(entry.data.get(CONF_SERVER_NAME) or entry.title)
    return DeviceInfo(
        identifiers={(DOMAIN, server_id)},
        name=f"SmartPlex ({server_name})",
        manufacturer="Plex / SmartPlex",
        model="SmartPlex Controller",
        entry_type=DeviceEntryType.SERVICE,
    )


def build_ui_entry_meta(entry: ConfigEntry) -> dict[str, Any]:
    """Build common metadata for bundled SmartPlex Lovelace cards."""
    zones_subs = get_zone_subentries(entry)
    zones: list[str] = []
    default_zone = ""
    for z in zones_subs:
        z_name = str(z.data.get(CONF_ZONE_NAME) or z.title)
        if z_name and z_name not in zones:
            zones.append(z_name)
        if not default_zone and z.data.get(CONF_IS_DEFAULT):
            default_zone = z_name
    if not default_zone and zones:
        default_zone = zones[0]

    presets = [
        str(p.data.get(CONF_ROULETTE_NAME) or p.title)
        for p in get_roulette_subentries(entry)
    ]
    return {
        "entry_id": entry.entry_id,
        "server_name": str(entry.data.get(CONF_SERVER_NAME) or entry.title),
        "zones": zones,
        "default_zone": default_zone,
        "presets": presets,
    }


def serialize_ui_media_items(
    items: list[PlexMediaItem], limit: int = 10
) -> list[dict[str, Any]]:
    """Serialize PlexMediaItems for bundled Lovelace cards."""
    return [
        {
            "index": idx,
            "rating_key": it.rating_key,
            "media_type": it.media_type,
            "title": it.title,
            "year": it.year,
            "rating": it.rating,
            "library": it.library_title,
            "genres": list(it.genres[:3]),
            "duration_min": int(round(it.duration_ms / 60000.0))
            if it.duration_ms
            else 0,
            "summary": (it.summary or "")[:220],
        }
        for idx, it in enumerate(items[:limit], start=1)
    ]


class SmartPlexLibraryStatsSensor(
    CoordinatorEntity[SmartPlexCoordinator], SensorEntity
):
    """Total indexed media items with breakdown by library and type."""

    _attr_has_entity_name = True
    _attr_translation_key = "library_stats"
    _attr_icon = "mdi:filmstrip-box-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, coordinator: SmartPlexCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_library_stats"
        self._attr_device_info = build_service_device_info(entry)

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        if not data:
            return 0
        return (
            len(data.items_by_type.get("movie", []))
            + len(data.items_by_type.get("show", []))
            + len(data.items_by_type.get("music", []))
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        return {
            "movies": len(data.items_by_type.get("movie", [])),
            "shows": len(data.items_by_type.get("show", [])),
            "music_artists": len(data.items_by_type.get("music", [])),
            "playlists": len(data.playlists),
            "sections": [
                {"id": s.section_id, "title": s.title, "type": s.lib_type}
                for s in data.sections
            ],
            "genres_count": len(data.all_genres),
            "configured_zones": len(get_zone_subentries(self._entry)),
            "roulette_presets": len(get_roulette_subentries(self._entry)),
        }


class SmartPlexActiveSessionsSensor(
    CoordinatorEntity[SmartPlexCoordinator], SensorEntity
):
    """Count of currently active Plex playback sessions."""

    _attr_has_entity_name = True
    _attr_translation_key = "active_sessions"
    _attr_icon = "mdi:play-network"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, coordinator: SmartPlexCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_active_sessions"
        self._attr_device_info = build_service_device_info(entry)

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        return len(data.active_sessions) if data else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        return {
            "sessions": [
                {
                    "title": s.display_title,
                    "player": s.player_title,
                    "state": s.player_state,
                    "media_type": s.media_type,
                }
                for s in data.active_sessions
            ]
        }


class SmartPlexLastLaunchSensor(SensorEntity):
    """Diagnostic sensor recording the last launch result and TV boot timing."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_launch"
    _attr_icon = "mdi:television-play"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_launch"
        self._attr_device_info = build_service_device_info(entry)

    async def async_added_to_hass(self) -> None:
        @callback
        def _handle_update() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_LAST_LAUNCH_UPDATED}_{self._entry.entry_id}",
                _handle_update,
            )
        )

    @property
    def native_value(self) -> str:
        runtime: PlexSmartRuntimeData | None = getattr(
            self._entry, "runtime_data", None
        )
        if runtime is None or runtime.last_launch is None:
            return "idle"
        res = runtime.last_launch
        if not res.success:
            return "not_found"
        return (res.matched_title or "ok")[:250]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        runtime: PlexSmartRuntimeData | None = getattr(
            self._entry, "runtime_data", None
        )
        if runtime is None or runtime.last_launch is None:
            return {}
        res = runtime.last_launch
        return {
            "success": res.success,
            "start_phase": res.start_phase,
            "zone": res.zone_name,
            "rating_key": res.rating_key,
            "media_type": res.media_type,
            "boot_seconds": res.boot_seconds,
            "retries_used": res.retries_used,
            "handoff_from": res.handoff_from,
            "message": res.message,
            "suggestions": res.suggestions,
        }


class SmartPlexRecentlyAddedSensor(
    CoordinatorEntity[SmartPlexCoordinator], SensorEntity
):
    """Latest added movie/show in Plex with top-8 items list for Lovelace UI cards."""

    _attr_has_entity_name = True
    _attr_translation_key = "recently_added"
    _attr_icon = "mdi:movie-open-plus"

    def __init__(self, entry: ConfigEntry, coordinator: SmartPlexCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_recently_added"
        self._attr_device_info = build_service_device_info(entry)

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        recent = data.recently_added if data else []
        if not recent:
            return "empty"
        top = recent[0]
        label = f"{top.title} ({top.year})" if top.year else top.title
        return label[:250]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        recent = data.recently_added if data else []
        return {
            **build_ui_entry_meta(self._entry),
            "ui_card_kind": "recent",
            "count": len(recent),
            "items": serialize_ui_media_items(recent, limit=10),
        }


