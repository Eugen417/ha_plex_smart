"""Button platform for SmartPlex."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SIGNAL_ROULETTE_UPDATED
from .models import PlexSmartRuntimeData
from .roulette import async_spin_roulette
from .sensor import build_service_device_info, build_ui_entry_meta


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SmartPlex button entities."""
    async_add_entities(
        [
            SmartPlexRefreshCacheButton(entry),
            SmartPlexSpinRouletteButton(entry),
        ]
    )


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


class SmartPlexSpinRouletteButton(ButtonEntity):
    """Button to spin Movie Roulette and expose live picks for the Lovelace card."""

    _attr_has_entity_name = True
    _attr_translation_key = "spin_roulette"
    _attr_icon = "mdi:dice-5"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_spin_roulette"
        self._attr_device_info = build_service_device_info(entry)

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_roulette_updated() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_ROULETTE_UPDATED}_{self._entry.entry_id}",
                _on_roulette_updated,
            )
        )
        runtime: PlexSmartRuntimeData | None = getattr(
            self._entry, "runtime_data", None
        )
        if (
            runtime is not None
            and runtime.last_roulette_result is None
            and runtime.coordinator.data
            and runtime.coordinator.data.items_by_type.get("movie")
        ):
            self.hass.async_create_task(
                async_spin_roulette(self.hass, self._entry, {})
            )

    async def async_press(self) -> None:
        """Spin roulette using the default preset."""
        await async_spin_roulette(self.hass, self._entry, {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meta = build_ui_entry_meta(self._entry)
        runtime: PlexSmartRuntimeData | None = getattr(
            self._entry, "runtime_data", None
        )
        res = getattr(runtime, "last_roulette_result", None) if runtime else None
        if not isinstance(res, dict):
            return {**meta, "ui_card_kind": "roulette", "items": []}
        return {
            **meta,
            "ui_card_kind": "roulette",
            "zone": res.get("zone") or meta["default_zone"],
            "preset": res.get("preset", ""),
            "pick_count": res.get("pick_count", 0),
            "items": res.get("items", []),
        }

