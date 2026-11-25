"""DataUpdateCoordinator for SmartPlex library cache and active sessions."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PlexApiClient, PlexApiError, PlexAuthError
from .const import CONF_CACHE_INTERVAL, DEFAULT_CACHE_INTERVAL, DOMAIN
from .models import PlexIndexData
from .privacy import redact_sensitive_text

_LOGGER = logging.getLogger(__name__)


class SmartPlexCoordinator(DataUpdateCoordinator[PlexIndexData]):
    """Coordinates periodic caching of all Plex libraries, genres, playlists, and sessions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: PlexApiClient,
    ) -> None:
        """Initialize the coordinator."""
        interval_min = max(
            5, int(entry.data.get(CONF_CACHE_INTERVAL, DEFAULT_CACHE_INTERVAL))
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=interval_min),
            config_entry=entry,
        )
        self.api = api

    async def _async_update_data(self) -> PlexIndexData:
        """Fetch full library index and current sessions from Plex."""
        try:
            index = await self.api.async_fetch_full_index()
            try:
                index.active_sessions = await self.api.async_get_active_sessions()
            except PlexApiError as err:
                _LOGGER.debug(
                    "Could not refresh active sessions: %s",
                    redact_sensitive_text(err),
                )
            return index
        except PlexAuthError as err:
            raise ConfigEntryAuthFailed(
                "Plex authentication failed; please re-authenticate"
            ) from err
        except PlexApiError as err:
            raise UpdateFailed(
                f"Error communicating with Plex server: {redact_sensitive_text(err)}"
            ) from err
