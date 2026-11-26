"""Config flow for SmartPlex (Plex Server entry + Zone & Roulette subentries)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, override

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    ActionSelector,
    AreaSelector,
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import PlexApiClient, PlexAuthError, PlexConnectionError
from .const import (
    COLLAGE_LAYOUTS,
    CONF_AI_ENTITY,
    CONF_ALIASES,
    CONF_ANTI_REPEAT_DAYS,
    CONF_APP_SOURCE,
    CONF_AREA_ID,
    CONF_ATV_AUTO_SELECT,
    CONF_CACHE_INTERVAL,
    CONF_COLD_APP_DELAY,
    CONF_COLD_BOOT_TIMEOUT,
    CONF_COLD_OS_DELAY,
    CONF_COLLAGE_LAYOUT,
    CONF_CUSTOM_BOOT_ACTIONS,
    CONF_CUSTOM_LAUNCH_ACTIONS,
    CONF_CUSTOM_OFF_ACTIONS,
    CONF_DEFAULT_ZONE,
    CONF_DISPLAY_DEVICE_ID,
    CONF_DISPLAY_ENTITY,
    CONF_DISPLAY_SOURCE,
    CONF_EXCLUDE_GENRES,
    CONF_HANDOFF_REWIND,
    CONF_HARDWARE_DEVICE_ID,
    CONF_HARDWARE_ENTITY,
    CONF_HOT_DELAY,
    CONF_INCLUDE_GENRES,
    CONF_IS_DEFAULT,
    CONF_LIBRARIES,
    CONF_MAX_DURATION_MIN,
    CONF_MIN_RATING,
    CONF_OPTIMISTIC_MODE,
    CONF_PICK_COUNT,
    CONF_PLAY_RETRY_COUNT,
    CONF_PLAY_RETRY_DELAY,
    CONF_PLEX_CLIENT,
    CONF_PLEX_DEVICE_ID,
    CONF_POWER_PROFILE,
    CONF_REMOTE_ENTITY,
    CONF_ROULETTE_NAME,
    CONF_SCAN_BUTTON,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_TIMEOUT,
    CONF_SERVER_ID,
    CONF_SERVER_NAME,
    CONF_TOKEN,
    CONF_TURN_OFF_ON_HANDOFF,
    CONF_URL,
    CONF_USER_TITLE,
    CONF_VERIFY_SSL,
    CONF_WARM_APP_DELAY,
    CONF_WATCH_FILTER,
    CONF_YEAR_FROM,
    CONF_ZONE_NAME,
    DEFAULT_ANTI_REPEAT_DAYS,
    DEFAULT_APP_SOURCE,
    DEFAULT_ATV_AUTO_SELECT,
    DEFAULT_CACHE_INTERVAL,
    DEFAULT_COLLAGE_LAYOUT,
    DEFAULT_HANDOFF_REWIND,
    DEFAULT_IS_DEFAULT,
    DEFAULT_MAX_DURATION_MIN,
    DEFAULT_MIN_RATING,
    DEFAULT_OPTIMISTIC_MODE,
    DEFAULT_PICK_COUNT,
    DEFAULT_TURN_OFF_ON_HANDOFF,
    DEFAULT_USER_TITLE,
    DEFAULT_VERIFY_SSL,
    DEFAULT_WATCH_FILTER,
    DEFAULT_YEAR_FROM,
    DOMAIN,
    POWER_PROFILES,
    PROFILE_APPLE_TV,
    PROFILE_CUSTOM_ACTIONS,
    PROFILE_STANDARD_MEDIA_PLAYER,
    PROFILE_TIMING_DEFAULTS,
    SUBENTRY_TYPE_ROULETTE,
    SUBENTRY_TYPE_ZONE,
    WATCH_FILTERS,
)
from .search import get_zone_subentries

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _discover_default_ai_entity(hass: HomeAssistant) -> str:
    """Return the first enabled ai_task.* entity in Home Assistant (if any)."""
    ent_reg = er.async_get(hass)
    for ent in ent_reg.entities.values():
        if ent.domain == "ai_task" and not ent.disabled:
            return ent.entity_id
    for st in hass.states.async_all("ai_task"):
        return st.entity_id
    return ""


def _discover_ha_plex_servers(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Inspect existing built-in 'plex' entries to prefill URL, SSL, and scan button (never tokens)."""
    discovered: dict[str, dict[str, Any]] = {}
    ent_reg = er.async_get(hass)

    for plex_entry in hass.config_entries.async_entries("plex"):
        cfg = plex_entry.data.get("server_config") or plex_entry.data
        url = str(cfg.get("url") or "").strip()
        if not url:
            continue
        verify_ssl = bool(cfg.get("verify_ssl", DEFAULT_VERIFY_SSL))
        server_name = str(
            plex_entry.data.get("server") or plex_entry.title or url
        ).strip()

        scan_btn = ""
        for ent in er.async_entries_for_config_entry(ent_reg, plex_entry.entry_id):
            if ent.domain == "button" and "scan" in ent.entity_id:
                scan_btn = ent.entity_id
                break

        discovered[plex_entry.entry_id] = {
            "label": f"{server_name} ({url})",
            CONF_URL: url,
            CONF_VERIFY_SSL: verify_ssl,
            CONF_SCAN_BUTTON: scan_btn,
        }
    return discovered


class SmartPlexConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SmartPlex (Plex Server)."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, dict[str, Any]] = {}
        self._prefill: dict[str, Any] = {}

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentry flow handlers (media zones & movie roulette presets)."""
        return {
            SUBENTRY_TYPE_ZONE: PlexZoneSubentryFlowHandler,
            SUBENTRY_TYPE_ROULETTE: PlexRouletteSubentryFlowHandler,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a built-in HA Plex server or choose manual URL/token entry."""
        self._discovered = _discover_ha_plex_servers(self.hass)
        if not self._discovered:
            return await self.async_step_manual()

        if user_input is not None:
            choice = user_input["server_source"]
            if choice != "manual" and choice in self._discovered:
                self._prefill = dict(self._discovered[choice])
            else:
                self._prefill = {}
            return await self.async_step_manual()

        options = [
            {"value": eid, "label": info["label"]}
            for eid, info in self._discovered.items()
        ]
        options.append({"value": "manual", "label": "Manual URL & Token"})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "server_source", default=options[0]["value"]
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure Plex server URL, token, scan button, and optional AI Task."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = str(user_input[CONF_URL]).strip().rstrip("/")
            token = str(user_input[CONF_TOKEN]).strip()
            verify_ssl = bool(user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))

            client = PlexApiClient(self.hass, url, token, verify_ssl=verify_ssl)
            try:
                server_id, server_name = await client.async_validate_connection()
            except PlexAuthError:
                errors["base"] = "invalid_auth"
            except PlexConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(server_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=server_name,
                    data={
                        CONF_URL: url,
                        CONF_TOKEN: token,
                        CONF_VERIFY_SSL: verify_ssl,
                        CONF_SERVER_ID: server_id,
                        CONF_SERVER_NAME: server_name,
                        CONF_SCAN_BUTTON: user_input.get(CONF_SCAN_BUTTON) or "",
                        CONF_AI_ENTITY: user_input.get(CONF_AI_ENTITY) or "",
                        CONF_CACHE_INTERVAL: int(
                            user_input.get(CONF_CACHE_INTERVAL, DEFAULT_CACHE_INTERVAL)
                        ),
                        CONF_USER_TITLE: str(
                            user_input.get(CONF_USER_TITLE) or ""
                        ).strip(),
                    },
                )

        src = user_input if user_input is not None else self._prefill
        def_url = src.get(CONF_URL, "")
        def_ssl = src.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
        def_scan = src.get(CONF_SCAN_BUTTON, "")
        def_ai = (
            src.get(CONF_AI_ENTITY, "")
            if user_input is not None
            else _discover_default_ai_entity(self.hass)
        )
        def_cache = int(src.get(CONF_CACHE_INTERVAL, DEFAULT_CACHE_INTERVAL))
        def_title = str(src.get(CONF_USER_TITLE, DEFAULT_USER_TITLE))

        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_URL, default=def_url): str,
            vol.Required(CONF_TOKEN): PASSWORD_SELECTOR,
            vol.Optional(CONF_VERIFY_SSL, default=def_ssl): BooleanSelector(),
            _opt_key(CONF_SCAN_BUTTON, str(def_scan or "")): EntitySelector(
                EntitySelectorConfig(domain="button")
            ),
            _opt_key(CONF_AI_ENTITY, str(def_ai or "")): EntitySelector(
                EntitySelectorConfig(domain="ai_task")
            ),
            vol.Optional(
                CONF_CACHE_INTERVAL, default=def_cache
            ): NumberSelector(
                NumberSelectorConfig(
                    min=5, max=1440, step=5, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(CONF_USER_TITLE, default=def_title): str,
        }
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure Plex server settings."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            url = str(user_input[CONF_URL]).strip().rstrip("/")
            token = (
                str(user_input.get(CONF_TOKEN) or entry.data[CONF_TOKEN]).strip()
            )
            verify_ssl = bool(user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
            client = PlexApiClient(self.hass, url, token, verify_ssl=verify_ssl)
            try:
                server_id, server_name = await client.async_validate_connection()
            except PlexAuthError:
                errors["base"] = "invalid_auth"
            except PlexConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    title=server_name,
                    data={
                        **entry.data,
                        CONF_URL: url,
                        CONF_TOKEN: token,
                        CONF_VERIFY_SSL: verify_ssl,
                        CONF_SERVER_ID: server_id,
                        CONF_SERVER_NAME: server_name,
                        CONF_SCAN_BUTTON: user_input.get(CONF_SCAN_BUTTON) or "",
                        CONF_AI_ENTITY: user_input.get(CONF_AI_ENTITY) or "",
                        CONF_CACHE_INTERVAL: int(
                            user_input.get(CONF_CACHE_INTERVAL, DEFAULT_CACHE_INTERVAL)
                        ),
                        CONF_USER_TITLE: str(
                            user_input.get(CONF_USER_TITLE) or ""
                        ).strip(),
                    },
                )

        cur_scan = entry.data.get(CONF_SCAN_BUTTON) or ""
        cur_ai = entry.data.get(CONF_AI_ENTITY) or ""
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=entry.data.get(CONF_URL, "")): str,
                    vol.Required(
                        CONF_TOKEN, default=entry.data.get(CONF_TOKEN, "")
                    ): PASSWORD_SELECTOR,
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        default=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                    ): BooleanSelector(),
                    _opt_key(CONF_SCAN_BUTTON, cur_scan): EntitySelector(
                        EntitySelectorConfig(domain="button")
                    ),
                    _opt_key(CONF_AI_ENTITY, cur_ai): EntitySelector(
                        EntitySelectorConfig(domain="ai_task")
                    ),
                    vol.Optional(
                        CONF_CACHE_INTERVAL,
                        default=entry.data.get(
                            CONF_CACHE_INTERVAL, DEFAULT_CACHE_INTERVAL
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=5, max=1440, step=5, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(
                        CONF_USER_TITLE,
                        default=entry.data.get(CONF_USER_TITLE, DEFAULT_USER_TITLE),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Initiate re-authentication when Plex returns HTTP 401."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update expired or revoked Plex token."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            new_token = str(user_input[CONF_TOKEN]).strip()
            client = PlexApiClient(
                self.hass,
                str(reauth_entry.data[CONF_URL]),
                new_token,
                verify_ssl=bool(
                    reauth_entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
                ),
            )
            try:
                await client.async_validate_connection()
            except PlexAuthError:
                errors["base"] = "invalid_auth"
            except PlexConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_TOKEN: new_token},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): PASSWORD_SELECTOR}),
            errors=errors,
        )


def _get_plex_client_options(hass: HomeAssistant) -> list[dict[str, str]]:
    """Return ALL registered Plex media_player entities (including sleeping/offline ones)."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    options: list[dict[str, str]] = []
    seen: set[str] = set()

    for ent in ent_reg.entities.values():
        if ent.platform != "plex" or ent.domain != "media_player" or ent.disabled:
            continue
        dev_name = ""
        if ent.device_id:
            dev = dev_reg.async_get(ent.device_id)
            if dev:
                dev_name = str(dev.name_by_user or dev.name or "").strip()
        ent_name = str(ent.name or ent.original_name or "").strip()
        label = (
            f"{dev_name} ({ent.entity_id})"
            if dev_name
            else (f"{ent_name} ({ent.entity_id})" if ent_name else ent.entity_id)
        )
        options.append({"value": ent.entity_id, "label": label})
        seen.add(ent.entity_id)

    for st in hass.states.async_all("media_player"):
        if st.entity_id not in seen and st.entity_id.startswith("media_player.plex_"):
            friendly = str(st.attributes.get("friendly_name") or "").strip()
            label = f"{friendly} ({st.entity_id})" if friendly else st.entity_id
            options.append({"value": st.entity_id, "label": label})
            seen.add(st.entity_id)

    options.sort(key=lambda item: item["label"].lower())
    return options


def _resolve_hardware_entities_from_device(
    hass: HomeAssistant, device_id: str | None
) -> tuple[str, str]:
    """Resolve primary non-Plex media_player and remote entities belonging to a device_id."""
    if not device_id:
        return "", ""
    ent_reg = er.async_get(hass)
    hw_entity = ""
    remote_entity = ""
    for ent in er.async_entries_for_device(ent_reg, device_id):
        if ent.disabled:
            continue
        if ent.domain == "media_player" and ent.platform != "plex" and not hw_entity:
            hw_entity = ent.entity_id
        elif ent.domain == "remote" and not remote_entity:
            remote_entity = ent.entity_id
    return hw_entity, remote_entity


def _opt_key(key: str, suggested: str) -> vol.Optional:
    """Create vol.Optional without passing description=None."""
    if suggested:
        return vol.Optional(key, description={"suggested_value": suggested})
    return vol.Optional(key)


class PlexZoneSubentryFlowHandler(ConfigSubentryFlow):
    """Two-step subentry flow for adding or reconfiguring a TV/Media Player zone."""

    def __init__(self) -> None:
        super().__init__()
        self._zone_draft: dict[str, Any] = {}

    @property
    def _is_new(self) -> bool:
        return self.source == "user"

    async def async_step_set_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 1: Zone name, HA Area, aliases, Plex client, and physical TV device."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        existing: Mapping[str, Any] = (
            {} if self._is_new else self._get_reconfigure_subentry().data
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            ent_reg = er.async_get(self.hass)
            adv = user_input.get("advanced_options") or {}
            plex_client = str(user_input[CONF_PLEX_CLIENT]).strip()
            plex_ent = ent_reg.async_get(plex_client)

            if plex_ent is not None and plex_ent.platform != "plex":
                errors[CONF_PLEX_CLIENT] = "not_a_plex_client"
            else:
                hw_device_id = str(
                    user_input.get(CONF_HARDWARE_DEVICE_ID) or ""
                ).strip()
                auto_hw, auto_rem = _resolve_hardware_entities_from_device(
                    self.hass, hw_device_id
                )
                hw_entity = (
                    str(adv.get(CONF_HARDWARE_ENTITY) or "").strip() or auto_hw
                )
                remote_entity = (
                    str(adv.get(CONF_REMOTE_ENTITY) or "").strip() or auto_rem
                )
                if not hw_device_id and hw_entity:
                    hw_ent = ent_reg.async_get(hw_entity)
                    if hw_ent and hw_ent.device_id:
                        hw_device_id = hw_ent.device_id

                disp_device_id = str(
                    adv.get(CONF_DISPLAY_DEVICE_ID) or ""
                ).strip()
                disp_entity, _ = _resolve_hardware_entities_from_device(
                    self.hass, disp_device_id
                )

                self._zone_draft = {
                    CONF_ZONE_NAME: str(user_input[CONF_ZONE_NAME]).strip(),
                    CONF_AREA_ID: user_input.get(CONF_AREA_ID) or "",
                    CONF_IS_DEFAULT: bool(
                        user_input.get(CONF_IS_DEFAULT, DEFAULT_IS_DEFAULT)
                    ),
                    CONF_ALIASES: str(adv.get(CONF_ALIASES) or "").strip(),
                    CONF_PLEX_CLIENT: plex_client,
                    CONF_PLEX_DEVICE_ID: (
                        plex_ent.device_id
                        if plex_ent and plex_ent.device_id
                        else existing.get(CONF_PLEX_DEVICE_ID, "")
                    ),
                    CONF_POWER_PROFILE: user_input[CONF_POWER_PROFILE],
                    CONF_HARDWARE_DEVICE_ID: hw_device_id,
                    CONF_HARDWARE_ENTITY: hw_entity,
                    CONF_REMOTE_ENTITY: remote_entity,
                    CONF_DISPLAY_DEVICE_ID: disp_device_id,
                    CONF_DISPLAY_ENTITY: disp_entity,
                    CONF_DISPLAY_SOURCE: str(
                        adv.get(CONF_DISPLAY_SOURCE) or ""
                    ).strip(),
                    CONF_APP_SOURCE: str(
                        adv.get(CONF_APP_SOURCE) or DEFAULT_APP_SOURCE
                    ).strip(),
                    CONF_TURN_OFF_ON_HANDOFF: bool(
                        adv.get(
                            CONF_TURN_OFF_ON_HANDOFF, DEFAULT_TURN_OFF_ON_HANDOFF
                        )
                    ),
                }
                return await self.async_step_zone_timings()

        plex_options = _get_plex_client_options(self.hass)
        cur_area = str(existing.get(CONF_AREA_ID) or "")
        cur_plex = str(existing.get(CONF_PLEX_CLIENT) or "")
        cur_hw_dev = str(existing.get(CONF_HARDWARE_DEVICE_ID) or "")
        cur_hw = str(existing.get(CONF_HARDWARE_ENTITY) or "")
        cur_rem = str(existing.get(CONF_REMOTE_ENTITY) or "")
        cur_disp_dev = str(existing.get(CONF_DISPLAY_DEVICE_ID) or "")
        is_first_zone = self._is_new and not get_zone_subentries(entry)
        cur_is_default = bool(
            existing.get(
                CONF_IS_DEFAULT, True if is_first_zone else DEFAULT_IS_DEFAULT
            )
        )

        if cur_plex and not any(o["value"] == cur_plex for o in plex_options):
            plex_options.insert(0, {"value": cur_plex, "label": cur_plex})

        plex_key = (
            vol.Required(CONF_PLEX_CLIENT, default=cur_plex)
            if cur_plex
            else vol.Required(CONF_PLEX_CLIENT)
        )

        advanced_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ALIASES, default=existing.get(CONF_ALIASES, "")
                ): str,
                _opt_key(CONF_DISPLAY_DEVICE_ID, cur_disp_dev): DeviceSelector(
                    DeviceSelectorConfig(
                        entity=[
                            EntityFilterSelectorConfig(
                                domain=["media_player", "remote", "switch"]
                            )
                        ]
                    )
                ),
                vol.Optional(
                    CONF_DISPLAY_SOURCE,
                    default=existing.get(CONF_DISPLAY_SOURCE, ""),
                ): str,
                vol.Optional(
                    CONF_APP_SOURCE,
                    default=existing.get(CONF_APP_SOURCE, DEFAULT_APP_SOURCE),
                ): str,
                vol.Optional(
                    CONF_TURN_OFF_ON_HANDOFF,
                    default=existing.get(
                        CONF_TURN_OFF_ON_HANDOFF, DEFAULT_TURN_OFF_ON_HANDOFF
                    ),
                ): BooleanSelector(),
                _opt_key(CONF_HARDWARE_ENTITY, cur_hw): EntitySelector(
                    EntitySelectorConfig(domain="media_player")
                ),
                _opt_key(CONF_REMOTE_ENTITY, cur_rem): EntitySelector(
                    EntitySelectorConfig(domain="remote")
                ),
            }
        )

        schema_dict: dict[Any, Any] = {
            vol.Required(
                CONF_ZONE_NAME, default=existing.get(CONF_ZONE_NAME, "")
            ): str,
            _opt_key(CONF_AREA_ID, cur_area): AreaSelector(),
            vol.Optional(CONF_IS_DEFAULT, default=cur_is_default): BooleanSelector(),
            plex_key: SelectSelector(
                SelectSelectorConfig(
                    options=plex_options,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_POWER_PROFILE,
                default=existing.get(
                    CONF_POWER_PROFILE, PROFILE_STANDARD_MEDIA_PLAYER
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=POWER_PROFILES,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="power_profile",
                )
            ),
            _opt_key(CONF_HARDWARE_DEVICE_ID, cur_hw_dev): DeviceSelector(
                DeviceSelectorConfig(
                    entity=[
                        EntityFilterSelectorConfig(domain=["media_player", "remote"])
                    ]
                )
            ),
            vol.Required("advanced_options"): section(
                advanced_schema, {"collapsed": True}
            ),
        }
        return self.async_show_form(
            step_id="set_zone",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_zone_timings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 2: Editable TV boot/app/scan/retry timings (+ custom actions if chosen)."""
        entry = self._get_entry()
        existing: Mapping[str, Any] = (
            {} if self._is_new else self._get_reconfigure_subentry().data
        )
        profile = str(
            self._zone_draft.get(CONF_POWER_PROFILE, PROFILE_STANDARD_MEDIA_PLAYER)
        )
        defaults = PROFILE_TIMING_DEFAULTS.get(
            profile, PROFILE_TIMING_DEFAULTS[PROFILE_STANDARD_MEDIA_PLAYER]
        )
        same_profile = (not self._is_new) and (
            existing.get(CONF_POWER_PROFILE) == profile
        )

        if user_input is not None:
            final_data: dict[str, Any] = {
                **self._zone_draft,
                CONF_COLD_BOOT_TIMEOUT: int(user_input[CONF_COLD_BOOT_TIMEOUT]),
                CONF_COLD_OS_DELAY: int(user_input[CONF_COLD_OS_DELAY]),
                CONF_COLD_APP_DELAY: int(user_input[CONF_COLD_APP_DELAY]),
                CONF_WARM_APP_DELAY: int(user_input[CONF_WARM_APP_DELAY]),
                CONF_HOT_DELAY: int(user_input[CONF_HOT_DELAY]),
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                CONF_SCAN_TIMEOUT: int(user_input[CONF_SCAN_TIMEOUT]),
                CONF_PLAY_RETRY_COUNT: int(user_input[CONF_PLAY_RETRY_COUNT]),
                CONF_PLAY_RETRY_DELAY: int(user_input[CONF_PLAY_RETRY_DELAY]),
                CONF_HANDOFF_REWIND: int(user_input[CONF_HANDOFF_REWIND]),
                CONF_OPTIMISTIC_MODE: bool(
                    user_input.get(CONF_OPTIMISTIC_MODE, DEFAULT_OPTIMISTIC_MODE)
                ),
            }
            if profile == PROFILE_APPLE_TV:
                final_data[CONF_ATV_AUTO_SELECT] = bool(
                    user_input.get(CONF_ATV_AUTO_SELECT, DEFAULT_ATV_AUTO_SELECT)
                )
            if profile == PROFILE_CUSTOM_ACTIONS:
                final_data[CONF_CUSTOM_BOOT_ACTIONS] = user_input.get(
                    CONF_CUSTOM_BOOT_ACTIONS, []
                )
                final_data[CONF_CUSTOM_LAUNCH_ACTIONS] = user_input.get(
                    CONF_CUSTOM_LAUNCH_ACTIONS, []
                )
                final_data[CONF_CUSTOM_OFF_ACTIONS] = user_input.get(
                    CONF_CUSTOM_OFF_ACTIONS, []
                )

            title = final_data[CONF_ZONE_NAME]
            if self._is_new:
                return self.async_create_entry(title=title, data=final_data)
            subentry = self._get_reconfigure_subentry()
            return self.async_update_and_abort(
                entry, subentry, title=title, data=final_data
            )

        def _t_val(key: str, legacy_key: str | None = None) -> int:
            if same_profile:
                if key in existing:
                    return int(existing[key])
                if legacy_key and legacy_key in existing:
                    return int(existing[legacy_key])
            return int(defaults.get(key, 3))

        def _num(min_v: int, max_v: int) -> NumberSelector:
            return NumberSelector(
                NumberSelectorConfig(
                    min=min_v, max=max_v, step=1, mode=NumberSelectorMode.BOX
                )
            )

        schema_dict: dict[Any, Any] = {
            vol.Required(
                CONF_COLD_BOOT_TIMEOUT,
                default=_t_val(CONF_COLD_BOOT_TIMEOUT, "boot_delay"),
            ): _num(0, 60),
            vol.Required(
                CONF_COLD_OS_DELAY, default=_t_val(CONF_COLD_OS_DELAY)
            ): _num(0, 30),
            vol.Required(
                CONF_COLD_APP_DELAY,
                default=_t_val(CONF_COLD_APP_DELAY, "app_load_delay"),
            ): _num(0, 60),
            vol.Required(
                CONF_WARM_APP_DELAY, default=_t_val(CONF_WARM_APP_DELAY)
            ): _num(0, 30),
            vol.Required(
                CONF_HOT_DELAY, default=_t_val(CONF_HOT_DELAY)
            ): _num(0, 15),
            vol.Required(
                CONF_SCAN_INTERVAL, default=_t_val(CONF_SCAN_INTERVAL)
            ): _num(1, 15),
            vol.Required(
                CONF_SCAN_TIMEOUT, default=_t_val(CONF_SCAN_TIMEOUT)
            ): _num(5, 120),
            vol.Required(
                CONF_PLAY_RETRY_COUNT, default=_t_val(CONF_PLAY_RETRY_COUNT)
            ): _num(0, 10),
            vol.Required(
                CONF_PLAY_RETRY_DELAY, default=_t_val(CONF_PLAY_RETRY_DELAY)
            ): _num(1, 15),
            vol.Required(
                CONF_HANDOFF_REWIND,
                default=int(existing.get(CONF_HANDOFF_REWIND, DEFAULT_HANDOFF_REWIND)),
            ): _num(0, 30),
            vol.Optional(
                CONF_OPTIMISTIC_MODE,
                default=bool(
                    existing.get(CONF_OPTIMISTIC_MODE, DEFAULT_OPTIMISTIC_MODE)
                ),
            ): BooleanSelector(),
        }

        if profile == PROFILE_APPLE_TV:
            schema_dict[
                vol.Optional(
                    CONF_ATV_AUTO_SELECT,
                    default=bool(
                        existing.get(CONF_ATV_AUTO_SELECT, DEFAULT_ATV_AUTO_SELECT)
                    ),
                )
            ] = BooleanSelector()

        if profile == PROFILE_CUSTOM_ACTIONS:
            schema_dict[
                vol.Optional(
                    CONF_CUSTOM_BOOT_ACTIONS,
                    default=existing.get(CONF_CUSTOM_BOOT_ACTIONS, []),
                )
            ] = ActionSelector()
            schema_dict[
                vol.Optional(
                    CONF_CUSTOM_LAUNCH_ACTIONS,
                    default=existing.get(CONF_CUSTOM_LAUNCH_ACTIONS, []),
                )
            ] = ActionSelector()
            schema_dict[
                vol.Optional(
                    CONF_CUSTOM_OFF_ACTIONS,
                    default=existing.get(CONF_CUSTOM_OFF_ACTIONS, []),
                )
            ] = ActionSelector()

        return self.async_show_form(
            step_id="zone_timings",
            data_schema=vol.Schema(schema_dict),
        )

    async_step_user = async_step_set_zone
    async_step_reconfigure = async_step_set_zone


def _build_roulette_schema(
    existing: Mapping[str, Any],
    movie_libs: list[str],
    all_genres: list[str],
    zone_names: list[str],
) -> vol.Schema:
    def _multi(opts: list[str]) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(
                options=opts,
                multiple=True,
                custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    def _num(min_v: float, max_v: float, step: float = 1) -> NumberSelector:
        return NumberSelector(
            NumberSelectorConfig(
                min=min_v, max=max_v, step=step, mode=NumberSelectorMode.BOX
            )
        )

    return vol.Schema(
        {
            vol.Required(
                CONF_ROULETTE_NAME, default=existing.get(CONF_ROULETTE_NAME, "")
            ): str,
            vol.Optional(
                CONF_LIBRARIES, default=existing.get(CONF_LIBRARIES, [])
            ): _multi(movie_libs),
            vol.Required(
                CONF_WATCH_FILTER,
                default=existing.get(CONF_WATCH_FILTER, DEFAULT_WATCH_FILTER),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=WATCH_FILTERS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="watch_filter",
                )
            ),
            vol.Optional(
                CONF_INCLUDE_GENRES, default=existing.get(CONF_INCLUDE_GENRES, [])
            ): _multi(all_genres),
            vol.Optional(
                CONF_EXCLUDE_GENRES, default=existing.get(CONF_EXCLUDE_GENRES, [])
            ): _multi(all_genres),
            vol.Optional(
                CONF_MIN_RATING,
                default=float(existing.get(CONF_MIN_RATING, DEFAULT_MIN_RATING)),
            ): _num(0.0, 10.0, 0.1),
            vol.Optional(
                CONF_YEAR_FROM,
                default=int(existing.get(CONF_YEAR_FROM, DEFAULT_YEAR_FROM)),
            ): _num(1900, 2100, 1),
            vol.Optional(
                CONF_MAX_DURATION_MIN,
                default=int(
                    existing.get(CONF_MAX_DURATION_MIN, DEFAULT_MAX_DURATION_MIN)
                ),
            ): _num(0, 400, 5),
            vol.Required(
                CONF_PICK_COUNT,
                default=int(existing.get(CONF_PICK_COUNT, DEFAULT_PICK_COUNT)),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=6, step=1, mode=NumberSelectorMode.SLIDER
                )
            ),
            vol.Required(
                CONF_COLLAGE_LAYOUT,
                default=existing.get(CONF_COLLAGE_LAYOUT, DEFAULT_COLLAGE_LAYOUT),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=COLLAGE_LAYOUTS,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="collage_layout",
                )
            ),
            vol.Optional(
                CONF_ANTI_REPEAT_DAYS,
                default=int(
                    existing.get(CONF_ANTI_REPEAT_DAYS, DEFAULT_ANTI_REPEAT_DAYS)
                ),
            ): _num(0, 365, 1),
            vol.Optional(
                CONF_DEFAULT_ZONE,
                default=existing.get(
                    CONF_DEFAULT_ZONE, zone_names[0] if zone_names else ""
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=zone_names,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class PlexRouletteSubentryFlowHandler(ConfigSubentryFlow):
    """Single-step subentry flow for configurable Movie Roulette presets."""

    @property
    def _is_new(self) -> bool:
        return self.source == "user"

    async def async_step_set_roulette(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create or edit a Movie Roulette preset using real Plex libraries & genres."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        existing: Mapping[str, Any] = (
            {} if self._is_new else self._get_reconfigure_subentry().data
        )
        runtime = getattr(entry, "runtime_data", None)
        index = runtime.coordinator.data if runtime else None

        movie_libs = [
            s.title for s in (index.sections if index else []) if s.lib_type == "movie"
        ]
        all_genres = list(index.all_genres) if index else []
        zone_names = [
            str(z.data.get(CONF_ZONE_NAME) or z.title)
            for z in get_zone_subentries(entry)
        ]

        if user_input is not None:
            name = str(user_input[CONF_ROULETTE_NAME]).strip()
            sub_data: dict[str, Any] = {
                CONF_ROULETTE_NAME: name,
                CONF_LIBRARIES: list(user_input.get(CONF_LIBRARIES) or []),
                CONF_WATCH_FILTER: user_input.get(
                    CONF_WATCH_FILTER, DEFAULT_WATCH_FILTER
                ),
                CONF_INCLUDE_GENRES: list(user_input.get(CONF_INCLUDE_GENRES) or []),
                CONF_EXCLUDE_GENRES: list(user_input.get(CONF_EXCLUDE_GENRES) or []),
                CONF_MIN_RATING: float(
                    user_input.get(CONF_MIN_RATING, DEFAULT_MIN_RATING)
                ),
                CONF_YEAR_FROM: int(user_input.get(CONF_YEAR_FROM, DEFAULT_YEAR_FROM)),
                CONF_MAX_DURATION_MIN: int(
                    user_input.get(CONF_MAX_DURATION_MIN, DEFAULT_MAX_DURATION_MIN)
                ),
                CONF_PICK_COUNT: int(
                    user_input.get(CONF_PICK_COUNT, DEFAULT_PICK_COUNT)
                ),
                CONF_COLLAGE_LAYOUT: user_input.get(
                    CONF_COLLAGE_LAYOUT, DEFAULT_COLLAGE_LAYOUT
                ),
                CONF_ANTI_REPEAT_DAYS: int(
                    user_input.get(CONF_ANTI_REPEAT_DAYS, DEFAULT_ANTI_REPEAT_DAYS)
                ),
                CONF_DEFAULT_ZONE: str(user_input.get(CONF_DEFAULT_ZONE) or "").strip(),
            }
            if self._is_new:
                return self.async_create_entry(title=name, data=sub_data)
            subentry = self._get_reconfigure_subentry()
            return self.async_update_and_abort(
                entry, subentry, title=name, data=sub_data
            )

        return self.async_show_form(
            step_id="set_roulette",
            data_schema=_build_roulette_schema(
                existing, movie_libs, all_genres, zone_names
            ),
        )

    async_step_user = async_step_set_roulette
    async_step_reconfigure = async_step_set_roulette






