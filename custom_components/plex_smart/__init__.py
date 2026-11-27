"""The SmartPlex integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    llm,
)

from .api import PlexApiClient
from .const import (
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    FRONTEND_CARD_VERSION,
    FRONTEND_JS_URL_PATH,
    PLATFORMS,
)
from .coordinator import SmartPlexCoordinator
from .llm_api import SmartPlexLLMAPI
from .models import PlexSmartRuntimeData
from .roulette import (
    RouletteHistoryStore,
    SmartPlexPosterProxyView,
    SmartPlexRouletteImageView,
)
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload SmartPlex when options or subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_cleanup_legacy_zone_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove orphaned legacy entities (media_player, image) and zone devices."""
    ent_reg = er.async_get(hass)
    for ent in list(er.async_entries_for_config_entry(ent_reg, entry.entry_id)):
        if ent.domain in ("media_player", "image"):
            ent_reg.async_remove(ent.entity_id)

    dev_reg = dr.async_get(hass)
    for dev in list(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)):
        is_legacy_zone_dev = any(
            dom == DOMAIN and str(ident).startswith("zone_")
            for dom, ident in getattr(dev, "identifiers", set())
        )
        sub_links = getattr(dev, "config_entries_subentries", {}).get(entry.entry_id)
        has_only_subentry_links = (
            bool(sub_links)
            and any(sub_id is not None for sub_id in sub_links)
            and None not in sub_links
        )
        if is_legacy_zone_dev or has_only_subentry_links:
            dev_reg.async_remove_device(dev.id)


async def _async_register_frontend_cards(
    hass: HomeAssistant, domain_data: dict[str, object]
) -> None:
    """Register bundled SmartPlex Lovelace cards without writing to /config/www."""
    if domain_data.get("frontend_registered"):
        return
    js_file = Path(__file__).parent / "frontend" / "smartplex-cards.js"
    if not js_file.is_file():
        return
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    FRONTEND_JS_URL_PATH,
                    str(js_file),
                    cache_headers=False,
                )
            ]
        )
        add_extra_js_url(hass, f"{FRONTEND_JS_URL_PATH}?v={FRONTEND_CARD_VERSION}")
        domain_data["frontend_registered"] = True
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not register SmartPlex Lovelace cards: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartPlex from a config entry."""
    _async_cleanup_legacy_zone_devices(hass, entry)
    domain_data = hass.data.setdefault(DOMAIN, {})

    if "roulette_view" not in domain_data:
        view = SmartPlexRouletteImageView()
        hass.http.register_view(view)
        domain_data["roulette_view"] = view

    if "poster_view" not in domain_data:
        poster_view = SmartPlexPosterProxyView()
        hass.http.register_view(poster_view)
        domain_data["poster_view"] = poster_view

    await _async_register_frontend_cards(hass, domain_data)

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
