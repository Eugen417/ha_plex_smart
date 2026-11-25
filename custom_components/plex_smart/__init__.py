"""The SmartPlex integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, llm

from .api import PlexApiClient
from .const import (
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import SmartPlexCoordinator
from .llm_api import SmartPlexLLMAPI
from .models import PlexSmartRuntimeData
from .roulette import RouletteHistoryStore, SmartPlexRouletteImageView
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload SmartPlex when options or subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartPlex from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})

    if "roulette_view" not in domain_data:
        view = SmartPlexRouletteImageView()
        hass.http.register_view(view)
        domain_data["roulette_view"] = view

    api = PlexApiClient(
        hass=hass,
        base_url=str(entry.data[CONF_URL]),
        token=str(entry.data[CONF_TOKEN]),
        verify_ssl=bool(entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
    )
    coordinator = SmartPlexCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    roulette_store = RouletteHistoryStore(hass, entry.entry_id)
    await roulette_store.async_load()

    entry.runtime_data = PlexSmartRuntimeData(
        coordinator=coordinator,
        api=api,
        roulette_store=roulette_store,
    )
    domain_data.setdefault("runtimes", {})[entry.entry_id] = entry.runtime_data

    old_unreg = domain_data.pop("llm_unreg", None)
    if callable(old_unreg):
        old_unreg()
    try:
        domain_data["llm_unreg"] = llm.async_register_api(
            hass, SmartPlexLLMAPI(hass)
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not register SmartPlex LLM API: %s", err)

    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a SmartPlex config entry and clean up shared resources when last."""
    from .hardware import get_plex_client_candidates
    from .search import get_zone_subentries

    runtime = getattr(entry, "runtime_data", None)
    active_tasks = getattr(runtime, "active_zone_tasks", None) if runtime else None
    if isinstance(active_tasks, dict) and active_tasks:
        pending = [t for t in active_tasks.values() if not t.done()]
        active_tasks.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    domain_data = hass.data.get(DOMAIN, {})
    runtimes_map = domain_data.get("runtimes")
    if isinstance(runtimes_map, dict):
        runtimes_map.pop(entry.entry_id, None)

    seek_tasks = domain_data.get("seek_tasks")
    if isinstance(seek_tasks, dict) and seek_tasks:
        entry_clients: set[str] = set()
        for sub in get_zone_subentries(entry):
            entry_clients.update(get_plex_client_candidates(hass, sub.data))
        pending_entry_seeks: list[asyncio.Task[object]] = []
        for cid in entry_clients:
            t_sk = seek_tasks.pop(cid, None)
            if t_sk is not None and not t_sk.done():
                t_sk.cancel()
                pending_entry_seeks.append(t_sk)
        if pending_entry_seeks:
            await asyncio.gather(*pending_entry_seeks, return_exceptions=True)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and getattr(e, "runtime_data", None) is not None
    ]
    if not remaining:
        seek_tasks_all = domain_data.pop("seek_tasks", None)
        if isinstance(seek_tasks_all, dict) and seek_tasks_all:
            pending_seeks = [t for t in seek_tasks_all.values() if not t.done()]
            seek_tasks_all.clear()
            for task in pending_seeks:
                task.cancel()
            if pending_seeks:
                await asyncio.gather(*pending_seeks, return_exceptions=True)
        unreg = domain_data.pop("llm_unreg", None)
        if callable(unreg):
            unreg()
        await async_unregister_services(hass)

    return True
