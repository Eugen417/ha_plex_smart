"""Button platform for SmartPlex."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .models import PlexSmartRuntimeData
from .sensor import build_service_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SmartPlex button entities."""
    async_add_entities([SmartPlexRefreshCacheButton(entry)])


class SmartPlexRefreshCacheButton(ButtonEntity):
    """Button to trigger an immediate Plex library cache refresh."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh_cache"
    _attr_icon = "mdi:database-refresh"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_refresh_cache"
        self._attr_device_info = build_service_device_info(entry)

    async def async_press(self) -> None:
        """Refresh the Plex media index immediately."""
        runtime: PlexSmartRuntimeData = self._entry.runtime_data
        await runtime.coordinator.async_request_refresh()
