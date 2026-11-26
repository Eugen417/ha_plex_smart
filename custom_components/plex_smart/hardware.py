"""Adaptive TV/player boot pipeline, scan loop, retry, and cross-room handoff."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
import json
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.script import Script
from homeassistant.util import dt as dt_util

from .const import (
    ACTIVE_PLAYBACK_STATES,
    APPLE_TV_PLEX_APP_ID,
    CONF_APP_SOURCE,
    CONF_AREA_ID,
    CONF_ATV_AUTO_SELECT,
    CONF_COLD_APP_DELAY,
    CONF_COLD_BOOT_TIMEOUT,
    CONF_COLD_OS_DELAY,
    CONF_CUSTOM_BOOT_ACTIONS,
    CONF_CUSTOM_LAUNCH_ACTIONS,
    CONF_CUSTOM_OFF_ACTIONS,
    CONF_DISPLAY_DEVICE_ID,
    CONF_DISPLAY_ENTITY,
    CONF_DISPLAY_SOURCE,
    CONF_HANDOFF_REWIND,
    CONF_HARDWARE_DEVICE_ID,
    CONF_HARDWARE_ENTITY,
    CONF_HOT_DELAY,
    CONF_OPTIMISTIC_MODE,
    CONF_PLAY_RETRY_COUNT,
    CONF_PLAY_RETRY_DELAY,
    CONF_PLEX_CLIENT,
    CONF_PLEX_DEVICE_ID,
    CONF_POWER_PROFILE,
    CONF_REMOTE_ENTITY,
    CONF_SCAN_BUTTON,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_TIMEOUT,
    CONF_SERVER_ID,
    CONF_SERVER_NAME,
    CONF_TURN_OFF_ON_HANDOFF,
    CONF_WARM_APP_DELAY,
    CONF_ZONE_NAME,
    DEFAULT_APP_SOURCE,
    DEFAULT_ATV_AUTO_SELECT,
    DEFAULT_HANDOFF_REWIND,
    DEFAULT_OPTIMISTIC_MODE,
    DEFAULT_TURN_OFF_ON_HANDOFF,
    DOMAIN,
    LG_COLD_MIN_BOOT_WAIT_SEC,
    LG_MIN_RETRY_COUNT,
    LG_MIN_RETRY_DELAY_SEC,
    LG_WEBOS_RESUME_INITIAL_SETTLE_SEC,
    MEDIA_STOP_TIMEOUT_SEC,
    OFFLINE_CLIENT_STATES,
    PLEX_URI_SCHEME,
    POST_PLAY_SPINUP_WAIT_SEC,
    PROFILE_ANDROID_TV,
    PROFILE_APPLE_TV,
    PROFILE_CUSTOM_ACTIONS,
    PROFILE_LG_WEBOS,
    PROFILE_SAMSUNG_TIZEN,
    PROFILE_STANDARD_MEDIA_PLAYER,
    PROFILE_TIMING_DEFAULTS,
    PROXY_READ_TIMEOUT_GRACE_SEC,
    RESUME_SEEK_CALL_TIMEOUT_SEC,
    RESUME_SEEK_MAX_ATTEMPTS,
    RESUME_SEEK_VERIFY_WAIT_SEC,
    RESUME_SPINUP_TIMEOUT_SEC,
    RESUME_WIPE_GUARD_SEC,
    SAMSUNG_MIN_RETRY_COUNT,
    SAMSUNG_MIN_RETRY_DELAY_SEC,
    SAMSUNG_RESUME_INITIAL_SETTLE_SEC,
    SAMSUNG_TIZEN_PLEX_APP_ID,
    START_PHASE_COLD,
    START_PHASE_HOT,
    START_PHASE_WARM,
)
from .messages import format_response
from .models import LaunchResult, PlexActiveSession
from .payload_templates import (
    build_episode_template,
    build_movie_template,
    build_music_item_template,
)
from .privacy import redact_sensitive_text
from .search import get_zone_subentries, normalize_title, resolve_zone

_LOGGER = logging.getLogger(__name__)
_HW_OFF_STATES = frozenset({"off", "standby", "unavailable", "unknown"})


def _zone_timing(
    zone_data: Mapping[str, Any], key: str, legacy_key: str | None = None
) -> float:
    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    defaults = PROFILE_TIMING_DEFAULTS.get(
        profile, PROFILE_TIMING_DEFAULTS[PROFILE_STANDARD_MEDIA_PLAYER]
    )
    raw = zone_data.get(key)
    if raw is None and legacy_key:
        raw = zone_data.get(legacy_key)
    if raw is None:
        return float(defaults.get(key, 2))
    try:
        return max(0.0, float(raw))
    except (ValueError, TypeError):
        return float(defaults.get(key, 2))


async def _run_custom_actions(
    hass: HomeAssistant, actions: list[dict[str, Any]] | None, name: str
) -> None:
    """Execute user-configured ActionSelector steps via HA Script runner."""
    if not actions:
        return
    try:
        script = Script(hass, actions, name, DOMAIN)
        await script.async_run(context=None)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "SmartPlex custom action '%s' failed: %s",
            name,
            redact_sensitive_text(err),
        )


async def _press_scan_button(hass: HomeAssistant, scan_button: str | None) -> None:
    if not scan_button:
        return
    try:
        await hass.services.async_call(
            "button", "press", {"entity_id": scan_button}, blocking=True
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Scan button press '%s' failed: %s",
            scan_button,
            redact_sensitive_text(err),
        )


_CLIENT_SUFFIX_RE = re.compile(r"^(.+?)_(\d{1,2})$")


def _strip_numeric_entity_suffix(entity_id: str) -> tuple[str, int]:
    """Strip 1-2 digit HA duplicate suffix (_2.._99) while keeping 4-digit model years (_2019)."""
    match = _CLIENT_SUFFIX_RE.match(entity_id)
    if match:
        return match.group(1), int(match.group(2))
    return entity_id, 1


def is_plex_client_ready(hass: HomeAssistant, plex_client: str | None) -> bool:
    """Return True if the Plex media_player entity is online and ready."""
    if not plex_client:
        return False
    st = hass.states.get(plex_client)
    return st is not None and st.state not in OFFLINE_CLIENT_STATES


def get_plex_client_candidates(
    hass: HomeAssistant, zone_data: Mapping[str, Any]
) -> list[str]:
    """Return all candidate Plex media_player entity_ids for a zone, best/newest first."""
    configured = str(zone_data.get(CONF_PLEX_CLIENT) or "").strip()
    if not configured:
        return []

    base_name, cfg_suffix = _strip_numeric_entity_suffix(configured)
    plex_dev_id = str(zone_data.get(CONF_PLEX_DEVICE_ID) or "").strip()
    ent_reg = er.async_get(hass)
    cfg_ent = ent_reg.async_get(configured)
    if not plex_dev_id and cfg_ent and getattr(cfg_ent, "device_id", None):
        plex_dev_id = str(cfg_ent.device_id)

    candidates: dict[str, int] = {configured: cfg_suffix}
    reg_entities = getattr(ent_reg, "entities", None)
    if isinstance(reg_entities, Mapping):
        for ent in reg_entities.values():
            if (
                getattr(ent, "disabled", False)
                or getattr(ent, "domain", "") != "media_player"
                or getattr(ent, "platform", "") != "plex"
            ):
                continue
            cand_base, suffix_num = _strip_numeric_entity_suffix(ent.entity_id)
            same_dev = bool(
                plex_dev_id and getattr(ent, "device_id", None) == plex_dev_id
            )
            if same_dev or cand_base == base_name:
                candidates[ent.entity_id] = suffix_num

    async_all = getattr(hass.states, "async_all", None)
    if callable(async_all):
        for st in async_all("media_player"):
            if st.entity_id in candidates:
                continue
            cand_base, suffix_num = _strip_numeric_entity_suffix(st.entity_id)
            if cand_base == base_name and st.entity_id.startswith("media_player.plex_"):
                candidates[st.entity_id] = suffix_num

    def _rank(item: tuple[str, int]) -> tuple[int, int, int, int]:
        eid, suffix_num = item
        st = hass.states.get(eid)
        is_active = 1 if (st is not None and st.state in ACTIVE_PLAYBACK_STATES) else 0
        is_ready = 1 if is_plex_client_ready(hass, eid) else 0
        non_restored = (
            1
            if (st is not None and not bool(st.attributes.get("restored")))
            else 0
        )
        return (is_active, is_ready, non_restored, suffix_num)

    ordered = sorted(candidates.items(), key=_rank, reverse=True)
    return [eid for eid, _ in ordered]


def resolve_plex_client(hass: HomeAssistant, zone_data: Mapping[str, Any]) -> str:
    """Resolve active Plex media_player entity, auto-following HA _2/_3 suffix recreations."""
    configured = str(zone_data.get(CONF_PLEX_CLIENT) or "").strip()
    if not configured:
        return ""
    candidates = get_plex_client_candidates(hass, zone_data)
    return candidates[0] if candidates else configured


def resolve_zone_hardware(
    hass: HomeAssistant, zone_data: Mapping[str, Any]
) -> tuple[str, str, str]:
    """Return (hw_device_id, hw_entity, remote_entity) with dynamic device_id lookup."""
    hw_device_id = str(zone_data.get(CONF_HARDWARE_DEVICE_ID) or "").strip()
    hw_entity = str(zone_data.get(CONF_HARDWARE_ENTITY) or "").strip()
    remote_entity = str(zone_data.get(CONF_REMOTE_ENTITY) or "").strip()

    ent_reg = er.async_get(hass)
    if hw_device_id and (not hw_entity or not remote_entity):
        for ent in er.async_entries_for_device(ent_reg, hw_device_id):
            if getattr(ent, "disabled", False):
                continue
            if ent.domain == "media_player" and ent.platform != "plex" and not hw_entity:
                hw_entity = ent.entity_id
            elif ent.domain == "remote" and not remote_entity:
                remote_entity = ent.entity_id
    elif not hw_device_id and hw_entity:
        ent = ent_reg.async_get(hw_entity)
        if ent and getattr(ent, "device_id", None):
            hw_device_id = str(ent.device_id)

    return hw_device_id, hw_entity, remote_entity


def resolve_display_hardware(
    hass: HomeAssistant, zone_data: Mapping[str, Any]
) -> tuple[str, str, str]:
    """Return (display_device_id, display_entity, display_source) for paired TV/AVR/projector."""
    disp_dev = str(zone_data.get(CONF_DISPLAY_DEVICE_ID) or "").strip()
    disp_ent = str(zone_data.get(CONF_DISPLAY_ENTITY) or "").strip()
    disp_src = str(zone_data.get(CONF_DISPLAY_SOURCE) or "").strip()

    if disp_dev and not disp_ent:
        ent_reg = er.async_get(hass)
        for ent in er.async_entries_for_device(ent_reg, disp_dev):
            if getattr(ent, "disabled", False):
                continue
            if ent.domain in ("media_player", "switch", "remote") and ent.platform != "plex":
                disp_ent = ent.entity_id
                break
    return disp_dev, disp_ent, disp_src


def build_apple_tv_deeplink(
    server_id: str,
    rating_key: int,
    *,
    is_show_root: bool = False,
) -> str:
    """Build verified Plex 2026.20+ tvOS/React-Native deep link URL."""
    if is_show_root:
        return (
            f"{PLEX_URI_SCHEME}libraries/{server_id}/unmatched/tv/"
            f"%2Flibrary%2Fmetadata%2F{rating_key}"
        )
    return (
        f"{PLEX_URI_SCHEME}libraries/{server_id}/unmatched/"
        f"%2Flibrary%2Fmetadata%2F{rating_key}"
    )


async def _async_ensure_display_ready(
    hass: HomeAssistant, zone_data: Mapping[str, Any]
) -> None:
    """Power on paired TV/projector/AVR and switch HDMI input if configured."""
    disp_dev, disp_ent, disp_src = resolve_display_hardware(hass, zone_data)
    if not disp_dev and not disp_ent:
        return

    disp_st = hass.states.get(disp_ent) if disp_ent else None
    is_off = disp_st is None or disp_st.state in _HW_OFF_STATES
    domain = disp_ent.split(".", 1)[0] if disp_ent and "." in disp_ent else "media_player"

    try:
        if is_off:
            target = {"device_id": disp_dev} if disp_dev else {"entity_id": disp_ent}
            await hass.services.async_call(domain, "turn_on", target, blocking=True)
            wait_sec = min(8.0, _zone_timing(zone_data, CONF_COLD_BOOT_TIMEOUT, "boot_delay"))
            if wait_sec > 0:
                await _async_wait_hardware_on(hass, disp_ent, wait_sec)
            os_delay = _zone_timing(zone_data, CONF_COLD_OS_DELAY)
            if os_delay > 0:
                await asyncio.sleep(os_delay)

        if disp_src and domain == "media_player":
            cur_st = hass.states.get(disp_ent) if disp_ent else None
            cur_src = str(cur_st.attributes.get("source") or "").strip() if cur_st else ""
            if cur_src.lower() != disp_src.lower():
                target = {"device_id": disp_dev} if disp_dev else {"entity_id": disp_ent}
                await hass.services.async_call(
                    "media_player",
                    "select_source",
                    {**target, "source": disp_src},
                    blocking=True,
                )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Paired display boot/source warning: %s", redact_sensitive_text(err)
        )


def _apple_tv_app_status(st: Any, app_source: str = DEFAULT_APP_SOURCE) -> str:
    """Classify Apple TV foreground app as 'plex', 'other', or 'unknown'.

    Home Assistant's built-in `apple_tv` media_player exposes `app_id` and `app_name`
    (never `source`), while custom/TV entities may expose `source`.
    """
    if st is None:
        return "unknown"
    attrs = getattr(st, "attributes", None) or {}
    want = (app_source or DEFAULT_APP_SOURCE).strip().lower()
    app_id = str(attrs.get("app_id") or "").strip().lower()
    app_name = str(attrs.get("app_name") or "").strip()
    cur_src = str(attrs.get("source") or "").strip()
    src_list = attrs.get("source_list") or []

    if (
        app_id == APPLE_TV_PLEX_APP_ID
        or ("plex" in app_id and app_id.startswith("com.plexapp"))
        or (app_name and app_name.lower() == want)
        or (cur_src and cur_src.lower() == want)
    ):
        return "plex"

    if app_id or app_name:
        return "other"
    if cur_src and (
        not src_list or any(str(s).strip().lower() == want for s in src_list)
    ):
        return "other"
    return "unknown"


def _zones_share_physical_display(
    hass: HomeAssistant,
    source_data: Mapping[str, Any],
    target_data: Mapping[str, Any],
) -> bool:
    """Return True when source and target zones share the same physical TV/projector/AVR."""
    s_hw_dev, s_hw_ent, _ = resolve_zone_hardware(hass, source_data)
    s_disp_dev, s_disp_ent, _ = resolve_display_hardware(hass, source_data)
    t_hw_dev, t_hw_ent, _ = resolve_zone_hardware(hass, target_data)
    t_disp_dev, t_disp_ent, _ = resolve_display_hardware(hass, target_data)

    src_devs = {d for d in (s_hw_dev, s_disp_dev) if d}
    tgt_devs = {d for d in (t_hw_dev, t_disp_dev) if d}
    if src_devs & tgt_devs:
        return True

    src_ents = {e for e in (s_hw_ent, s_disp_ent) if e}
    tgt_ents = {e for e in (t_hw_ent, t_disp_ent) if e}
    return bool(src_ents & tgt_ents)


def is_zone_hot_ready(hass: HomeAssistant, zone_data: Mapping[str, Any]) -> bool:
    """Return True only when BOTH the Plex client AND physical TV are awake & on Plex."""
    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    plex_client = resolve_plex_client(hass, zone_data)
    _, hw_entity, _ = resolve_zone_hardware(hass, zone_data)

    # On Apple TV (Plex 2026.20+), we always play via tvOS Deep Link on hw_entity,
    # so readiness depends on Apple TV itself being awake (and not sitting on another app).
    if profile == PROFILE_APPLE_TV and hw_entity:
        hw_st = hass.states.get(hw_entity)
        if hw_st is None or hw_st.state in _HW_OFF_STATES:
            return False
        app_source = str(zone_data.get(CONF_APP_SOURCE) or DEFAULT_APP_SOURCE).strip()
        return _apple_tv_app_status(hw_st, app_source) != "other"

    if not is_plex_client_ready(hass, plex_client):
        return False

    if not hw_entity:
        return True

    hw_st = hass.states.get(hw_entity)
    if hw_st is None or hw_st.state in _HW_OFF_STATES:
        return False

    plex_st = hass.states.get(plex_client)
    if plex_st is not None and plex_st.state in ACTIVE_PLAYBACK_STATES:
        return True

    app_source = str(zone_data.get(CONF_APP_SOURCE) or DEFAULT_APP_SOURCE).strip()
    cur_source = str(hw_st.attributes.get("source") or "").strip()
    source_list = hw_st.attributes.get("source_list") or []

    if profile == PROFILE_SAMSUNG_TIZEN:
        # Built-in HA samsungtv exposes source_list=["TV", "HDMI"] without Tizen apps.
        # Do NOT treat stale idle Plex state on Live TV / Art Mode as HOT unless Plex is explicitly foregrounded.
        app_id = str(hw_st.attributes.get("app_id") or "").strip()
        media_title = str(hw_st.attributes.get("media_title") or "").strip()
        if (
            app_id == SAMSUNG_TIZEN_PLEX_APP_ID
            or (cur_source and cur_source.lower() == app_source.lower())
            or (media_title and media_title.lower() == app_source.lower())
        ):
            return True
        if cur_source or source_list:
            return False
        return True

    if cur_source and source_list and app_source in source_list:
        return cur_source.lower() == app_source.lower()
    return True


def detect_zone_start_phase(
    hass: HomeAssistant, zone_data: Mapping[str, Any]
) -> str:
    """Classify current TV/Plex state as 'hot', 'warm', or 'cold'."""
    if is_zone_hot_ready(hass, zone_data):
        return START_PHASE_HOT
    _, hw_entity, _ = resolve_zone_hardware(hass, zone_data)
    if hw_entity:
        hw_st = hass.states.get(hw_entity)
        if hw_st is not None and hw_st.state not in _HW_OFF_STATES:
            return START_PHASE_WARM
    return START_PHASE_COLD


async def _async_wait_hardware_on(
    hass: HomeAssistant,
    hw_entity: str,
    timeout_sec: float,
    *,
    retry_wake_fn: Callable[[], Coroutine[Any, Any, Any]] | None = None,
) -> bool:
    """Wait reactively for TV to report online up to timeout_sec (with optional 2nd WoL pulse)."""
    if timeout_sec <= 0:
        return True
    if not hw_entity:
        await asyncio.sleep(timeout_sec)
        return False

    cur = hass.states.get(hw_entity)
    if cur is not None and cur.state not in _HW_OFF_STATES:
        return True

    on_event = asyncio.Event()

    @callback
    def _on_hw_state(event: Event) -> None:
        new_st = event.data.get("new_state")
        if new_st is not None and new_st.state not in _HW_OFF_STATES:
            on_event.set()

    unsub = async_track_state_change_event(hass, [hw_entity], _on_hw_state)
    try:
        if retry_wake_fn is not None and timeout_sec > 4.5:
            try:
                await asyncio.wait_for(on_event.wait(), timeout=4.0)
                return True
            except TimeoutError:
                mid = hass.states.get(hw_entity)
                if mid is not None and mid.state not in _HW_OFF_STATES:
                    return True
                await retry_wake_fn()
            await asyncio.wait_for(on_event.wait(), timeout=max(0.1, timeout_sec - 4.0))
            return True

        await asyncio.wait_for(on_event.wait(), timeout=timeout_sec)
        return True
    except TimeoutError:
        return False
    finally:
        unsub()


def build_plex_media_args(
    entry_data: Mapping[str, Any],
    payload: Mapping[str, Any],
    media_type: str,
    *,
    power_profile: str | None = None,
) -> tuple[str, str]:
    """Build native HA Plex (media_content_id, media_content_type) pair.

    Uses native `plex://<server_id>/<rating_key>` for direct items starting at 0:00,
    and `plex://{json}` whenever `offset`, `resume`, `shuffle`, `continuous`,
    playlists, or filter searches are requested so integer `offset` (ms) is passed
    directly to `playMedia` (while post-proxy integer `seekTo` verifies/enforces it).
    """
    _ = power_profile
    norm_type = str(media_type or "movie").strip().lower()
    if norm_type == "show":
        norm_type = "episode"

    server_id = str(entry_data.get(CONF_SERVER_ID) or "").strip()
    server_name = str(entry_data.get(CONF_SERVER_NAME) or "").strip()

    plex_key = payload.get("plex_key")
    offset = int(payload.get("offset") or 0)
    resume = bool(payload.get("resume"))
    shuffle = bool(payload.get("shuffle"))
    continuous = bool(payload.get("continuous"))

    clean_payload = {
        k: v for k, v in payload.items() if not str(k).startswith("_")
    }

    if (
        isinstance(plex_key, int)
        and server_id
        and offset == 0
        and not resume
        and not shuffle
        and not continuous
        and len(clean_payload) <= 3
    ):
        return f"{PLEX_URI_SCHEME}{server_id}/{plex_key}", norm_type

    if server_name and "plex_server" not in clean_payload:
        clean_payload["plex_server"] = server_name
    return (
        f"{PLEX_URI_SCHEME}{json.dumps(clean_payload, ensure_ascii=False)}",
        norm_type,
    )


async def async_power_off_zone(
    hass: HomeAssistant,
    zone_data: Mapping[str, Any],
    *,
    keep_shared_display: bool = False,
) -> None:
    """Stop Plex playback (bounded so proxy never stalls 30s) and turn off the TV/player."""
    plex_client = resolve_plex_client(hass, zone_data)
    if plex_client:
        st = hass.states.get(plex_client)
        if st and st.state in ACTIVE_PLAYBACK_STATES:
            try:
                stop_task = asyncio.create_task(
                    hass.services.async_call(
                        "media_player",
                        "media_stop",
                        {"entity_id": plex_client},
                        blocking=True,
                    )
                )
                stop_task.add_done_callback(
                    lambda t: t.exception() if not t.cancelled() else None
                )
                done, pending = await asyncio.wait(
                    {stop_task}, timeout=MEDIA_STOP_TIMEOUT_SEC
                )
                for p_task in pending:
                    p_task.cancel()
                if stop_task in done and stop_task.exception() is not None:
                    _LOGGER.debug(
                        "media_stop failed for %s: %s",
                        plex_client,
                        redact_sensitive_text(stop_task.exception()),
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "media_stop failed for %s: %s",
                    plex_client,
                    redact_sensitive_text(err),
                )

    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    hw_device_id, hw_entity, remote_entity = resolve_zone_hardware(hass, zone_data)

    if keep_shared_display:
        # Target zone shares the same physical TV screen (e.g. LG built-in Plex <-> Apple TV on same LG).
        # Never call turn_off on either the TV or Apple TV (CEC Standby would turn off the shared TV!).
        if profile == PROFILE_APPLE_TV:
            if hw_entity:
                try:
                    await hass.services.async_call(
                        "media_player",
                        "media_stop",
                        {"entity_id": hw_entity},
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
            if remote_entity:
                try:
                    await hass.services.async_call(
                        "remote",
                        "send_command",
                        {"entity_id": remote_entity, "command": "menu"},
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
        _LOGGER.debug(
            "SmartPlex handoff shares physical display; stopped source player without powering off TV"
        )
        return

    if profile == PROFILE_CUSTOM_ACTIONS and zone_data.get(CONF_CUSTOM_OFF_ACTIONS):
        await _run_custom_actions(
            hass, zone_data.get(CONF_CUSTOM_OFF_ACTIONS), "SmartPlex Power Off"
        )
    else:
        turned_off = False
        if remote_entity and profile in (PROFILE_SAMSUNG_TIZEN, PROFILE_ANDROID_TV):
            try:
                await hass.services.async_call(
                    "remote", "turn_off", {"entity_id": remote_entity}, blocking=True
                )
                turned_off = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Remote turn_off fallback to media_player: %s",
                    redact_sensitive_text(err),
                )

        if not turned_off and hw_entity:
            try:
                await hass.services.async_call(
                    "media_player", "turn_off", {"entity_id": hw_entity}, blocking=True
                )
                turned_off = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Entity turn_off warning (%s): %s",
                    hw_entity,
                    redact_sensitive_text(err),
                )

        if not turned_off and hw_device_id:
            try:
                await hass.services.async_call(
                    "media_player",
                    "turn_off",
                    {"device_id": hw_device_id},
                    blocking=True,
                )
                turned_off = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Device turn_off warning: %s", redact_sensitive_text(err)
                )

        if not turned_off and remote_entity:
            try:
                await hass.services.async_call(
                    "remote", "turn_off", {"entity_id": remote_entity}, blocking=True
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Remote fallback turn_off failed: %s", redact_sensitive_text(err)
                )

    # Also power off paired display / TV / projector when configured
    disp_dev, disp_ent, _ = resolve_display_hardware(hass, zone_data)
    if disp_dev or disp_ent:
        d_domain = (
            disp_ent.split(".", 1)[0]
            if disp_ent and "." in disp_ent
            else "media_player"
        )
        try:
            d_target = {"device_id": disp_dev} if disp_dev else {"entity_id": disp_ent}
            await hass.services.async_call(
                d_domain, "turn_off", d_target, blocking=True
            )
        except Exception as err:  # noqa: BLE001
            if disp_dev and disp_ent:
                try:
                    await hass.services.async_call(
                        d_domain, "turn_off", {"entity_id": disp_ent}, blocking=True
                    )
                    return
                except Exception:  # noqa: BLE001
                    pass
            _LOGGER.debug(
                "Paired display turn_off failed: %s", redact_sensitive_text(err)
            )


async def _async_power_on_hardware(
    hass: HomeAssistant, zone_data: Mapping[str, Any]
) -> bool:
    """Power on the physical TV/player if needed. Returns True if a power-on command ran."""
    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    hw_device_id, hw_entity, remote_entity = resolve_zone_hardware(hass, zone_data)

    hw_state_obj = hass.states.get(hw_entity) if hw_entity else None
    hw_state = hw_state_obj.state if hw_state_obj else None
    hw_is_off = hw_state in (None, "off", "standby", "unavailable", "unknown")

    try:
        if profile == PROFILE_CUSTOM_ACTIONS and zone_data.get(CONF_CUSTOM_BOOT_ACTIONS):
            await _run_custom_actions(
                hass, zone_data.get(CONF_CUSTOM_BOOT_ACTIONS), "SmartPlex Power On"
            )
            return True
        if profile == PROFILE_APPLE_TV:
            ran = False
            if remote_entity:
                try:
                    await hass.services.async_call(
                        "remote", "turn_on", {"entity_id": remote_entity}, blocking=True
                    )
                    ran = True
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Apple TV remote.turn_on warning: %s", redact_sensitive_text(err)
                    )
            target = (
                {"device_id": hw_device_id}
                if hw_device_id
                else ({"entity_id": hw_entity} if hw_entity else {})
            )
            if target:
                await hass.services.async_call(
                    "media_player", "turn_on", target, blocking=True
                )
                ran = True
            return ran
        if profile == PROFILE_SAMSUNG_TIZEN:
            # Built-in HA samsungtv implements Wake-on-LAN in media_player.turn_on,
            # whereas remote.turn_on raises HomeAssistantError unless a trigger automation exists!
            if hw_is_off or not hw_entity:
                target = (
                    {"device_id": hw_device_id}
                    if hw_device_id
                    else ({"entity_id": hw_entity} if hw_entity else {})
                )
                if target:
                    try:
                        await hass.services.async_call(
                            "media_player", "turn_on", target, blocking=True
                        )
                        return True
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "Samsung media_player.turn_on warning: %s",
                            redact_sensitive_text(err),
                        )
                if remote_entity:
                    try:
                        await hass.services.async_call(
                            "remote",
                            "turn_on",
                            {"entity_id": remote_entity},
                            blocking=True,
                        )
                        return True
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "Samsung remote.turn_on warning: %s",
                            redact_sensitive_text(err),
                        )
            return False
        if profile == PROFILE_ANDROID_TV:
            if hw_is_off or not hw_entity:
                if remote_entity:
                    try:
                        await hass.services.async_call(
                            "remote",
                            "turn_on",
                            {"entity_id": remote_entity},
                            blocking=True,
                        )
                        return True
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "Android TV remote.turn_on warning: %s",
                            redact_sensitive_text(err),
                        )
                target = (
                    {"device_id": hw_device_id}
                    if hw_device_id
                    else ({"entity_id": hw_entity} if hw_entity else {})
                )
                if target:
                    await hass.services.async_call(
                        "media_player", "turn_on", target, blocking=True
                    )
                    return True
            return False
        if hw_is_off:
            target = (
                {"device_id": hw_device_id}
                if hw_device_id
                else ({"entity_id": hw_entity} if hw_entity else {})
            )
            if target:
                await hass.services.async_call(
                    "media_player", "turn_on", target, blocking=True
                )
                return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Hardware turn_on warning: %s", redact_sensitive_text(err))
    return False


async def _async_launch_plex_app(
    hass: HomeAssistant, zone_data: Mapping[str, Any]
) -> bool:
    """Launch the Plex app on the TV/player. Returns True if a launch command ran."""
    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    hw_device_id, hw_entity, remote_entity = resolve_zone_hardware(hass, zone_data)
    app_source = str(zone_data.get(CONF_APP_SOURCE) or DEFAULT_APP_SOURCE).strip()

    try:
        current_hw = hass.states.get(hw_entity) if hw_entity else None
        current_source = (
            str(current_hw.attributes.get("source") or "") if current_hw else ""
        )
        source_list = (
            list(current_hw.attributes.get("source_list") or []) if current_hw else []
        )

        if profile == PROFILE_CUSTOM_ACTIONS and zone_data.get(CONF_CUSTOM_LAUNCH_ACTIONS):
            await _run_custom_actions(
                hass,
                zone_data.get(CONF_CUSTOM_LAUNCH_ACTIONS),
                "SmartPlex Launch App",
            )
            return True

        if (
            profile == PROFILE_ANDROID_TV
            and remote_entity
            and (app_source.startswith("plex://") or "." in app_source)
        ):
            await hass.services.async_call(
                "remote",
                "turn_on",
                {"entity_id": remote_entity, "activity": app_source},
                blocking=True,
            )
            return True

        if profile == PROFILE_SAMSUNG_TIZEN:
            matched_src = next(
                (s for s in source_list if str(s).lower() == app_source.lower()),
                None,
            )
            if hw_entity and matched_src:
                await hass.services.async_call(
                    "media_player",
                    "select_source",
                    {"entity_id": hw_entity, "source": matched_src},
                    blocking=True,
                )
                return True
            target = (
                {"entity_id": hw_entity}
                if hw_entity
                else ({"device_id": hw_device_id} if hw_device_id else {})
            )
            if target:
                app_id = (
                    app_source if app_source.isdigit() else SAMSUNG_TIZEN_PLEX_APP_ID
                )
                await hass.services.async_call(
                    "media_player",
                    "play_media",
                    {**target, "media_content_type": "app", "media_content_id": app_id},
                    blocking=True,
                )
                return True
            return False

        if profile == PROFILE_APPLE_TV or current_source != app_source:
            target = (
                {"device_id": hw_device_id}
                if hw_device_id
                else ({"entity_id": hw_entity} if hw_entity else {})
            )
            if target:
                await hass.services.async_call(
                    "media_player",
                    "select_source",
                    {**target, "source": app_source},
                    blocking=True,
                )
                return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Launch Plex app warning: %s", redact_sensitive_text(err))
    return False


async def async_ensure_zone_ready(
    hass: HomeAssistant,
    entry_data: Mapping[str, Any],
    zone_data: Mapping[str, Any],
) -> tuple[str, float]:
    """Ensure TV/player is awake, Plex app is open, and Plex client is online.

    Uses distinct Cold / Warm / Hot phase timings, reactive state waits with an
    optimistic ceiling, and automatic select_source relaunch if the TV swallowed
    the first app-launch command during OS boot.
    Returns (start_phase, elapsed_seconds).
    """
    plex_client = resolve_plex_client(hass, zone_data)
    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    _, hw_entity, _ = resolve_zone_hardware(hass, zone_data)
    app_source = str(zone_data.get(CONF_APP_SOURCE) or DEFAULT_APP_SOURCE).strip()
    phase = detect_zone_start_phase(hass, zone_data)

    if phase == START_PHASE_HOT:
        await _async_ensure_display_ready(hass, zone_data)
        scan_btn_hot = entry_data.get(CONF_SCAN_BUTTON)
        if scan_btn_hot and plex_client and profile != PROFILE_APPLE_TV:
            cur_plex = hass.states.get(plex_client)
            if cur_plex is None or cur_plex.state == "idle":
                try:
                    await hass.services.async_call(
                        "button",
                        "press",
                        {"entity_id": scan_btn_hot},
                        blocking=False,
                    )
                except Exception:  # noqa: BLE001
                    pass
        hot_delay = _zone_timing(zone_data, CONF_HOT_DELAY)
        if hot_delay > 0:
            await asyncio.sleep(hot_delay)
        return START_PHASE_HOT, round(hot_delay, 1)

    started_at = time.monotonic()
    scan_button = entry_data.get(CONF_SCAN_BUTTON)
    cold_boot_timeout = _zone_timing(
        zone_data, CONF_COLD_BOOT_TIMEOUT, "boot_delay"
    )
    cold_os_delay = _zone_timing(zone_data, CONF_COLD_OS_DELAY)
    cold_app_delay = _zone_timing(
        zone_data, CONF_COLD_APP_DELAY, "app_load_delay"
    )
    warm_app_delay = _zone_timing(zone_data, CONF_WARM_APP_DELAY)
    scan_interval = max(1.0, _zone_timing(zone_data, CONF_SCAN_INTERVAL))
    scan_timeout = max(scan_interval, _zone_timing(zone_data, CONF_SCAN_TIMEOUT))

    # Wake paired TV/projector/AVR in parallel with set-top box
    display_task = asyncio.create_task(_async_ensure_display_ready(hass, zone_data))

    try:
        if phase == START_PHASE_COLD:
            powered_on_now = await _async_power_on_hardware(hass, zone_data)
            if powered_on_now:
                eff_boot_timeout = cold_boot_timeout
                if profile == PROFILE_LG_WEBOS and cold_boot_timeout >= 5.0:
                    # Live LG webOS WoL takes ~10.5–12s; guard older zones saved with 10s!
                    eff_boot_timeout = max(cold_boot_timeout, LG_COLD_MIN_BOOT_WAIT_SEC)
                await _async_wait_hardware_on(
                    hass,
                    hw_entity,
                    eff_boot_timeout,
                    retry_wake_fn=(
                        (lambda: _async_power_on_hardware(hass, zone_data))
                        if profile == PROFILE_SAMSUNG_TIZEN
                        else None
                    ),
                )
                if cold_os_delay > 0:
                    await asyncio.sleep(cold_os_delay)

            if is_zone_hot_ready(hass, zone_data):
                await display_task
                return START_PHASE_COLD, round(time.monotonic() - started_at, 1)

            # On Apple TV (Plex 2026.20+), deep link opens Plex directly on the movie card;
            # skip 24s dead scan_clients loop if the client stays unavailable!
            if profile == PROFILE_APPLE_TV and hw_entity:
                await display_task
                return START_PHASE_COLD, round(time.monotonic() - started_at, 1)

            launched_app = await _async_launch_plex_app(hass, zone_data)
            if not launched_app and hw_entity:
                # On LG webOS cold WoL wake, webostv may still be opening its control socket
                hw_mid = hass.states.get(hw_entity)
                if hw_mid is None or hw_mid.state in _HW_OFF_STATES:
                    extra_wait = 0.01 if cold_app_delay <= 0 else 5.0
                    await _async_wait_hardware_on(hass, hw_entity, extra_wait)
                else:
                    retry_os_wait = (
                        0.01 if cold_app_delay <= 0 else min(2.5, cold_app_delay)
                    )
                    await asyncio.sleep(retry_os_wait)
                launched_app = await _async_launch_plex_app(hass, zone_data)
            last_app_launch = time.monotonic()
            if launched_app and cold_app_delay > 0:
                await asyncio.sleep(cold_app_delay)
        else:
            if profile == PROFILE_APPLE_TV and hw_entity:
                await display_task
                return START_PHASE_WARM, round(time.monotonic() - started_at, 1)

            launched_app = await _async_launch_plex_app(hass, zone_data)
            last_app_launch = time.monotonic()
            if launched_app and warm_app_delay > 0:
                await asyncio.sleep(warm_app_delay)

        # Always refresh HA's Plex client state after Cold/Warm app launch so stale
        # overnight "idle" states are cleared if the TV is still on the Plex splash screen,
        # and self.device capabilities ("playback") are refreshed if already online.
        if scan_button:
            await _press_scan_button(hass, scan_button)

        watch_clients = get_plex_client_candidates(hass, zone_data)
        plex_client = resolve_plex_client(hass, zone_data)
        if not is_plex_client_ready(hass, plex_client) and watch_clients:
            ready_event = asyncio.Event()

            @callback
            def _on_client_state(event: Event) -> None:
                new_state = event.data.get("new_state")
                if new_state is not None and new_state.state not in OFFLINE_CLIENT_STATES:
                    ready_event.set()

            unsub = async_track_state_change_event(hass, watch_clients, _on_client_state)
            app_relaunches = 0
            try:
                deadline = time.monotonic() + scan_timeout
                while time.monotonic() < deadline:
                    plex_client = resolve_plex_client(hass, zone_data)
                    if is_plex_client_ready(hass, plex_client):
                        break

                    now = time.monotonic()
                    if (
                        hw_entity
                        and app_relaunches < 2
                        and (now - last_app_launch) >= 5.0
                    ):
                        hw_st = hass.states.get(hw_entity)
                        if hw_st is not None and hw_st.state not in _HW_OFF_STATES:
                            cur_src = str(hw_st.attributes.get("source") or "").strip()
                            src_list = hw_st.attributes.get("source_list") or []
                            source_mismatch = (
                                bool(src_list)
                                and app_source in src_list
                                and cur_src.lower() != app_source.lower()
                            )
                            if source_mismatch or (not src_list and app_relaunches == 0):
                                await _async_launch_plex_app(hass, zone_data)
                                last_app_launch = time.monotonic()
                                app_relaunches += 1

                    await _press_scan_button(hass, scan_button)
                    plex_client = resolve_plex_client(hass, zone_data)
                    if is_plex_client_ready(hass, plex_client):
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(
                            ready_event.wait(), timeout=min(scan_interval, remaining)
                        )
                        break
                    except TimeoutError:
                        continue
            finally:
                unsub()

        await display_task
    finally:
        if not display_task.done():
            display_task.cancel()

    return phase, round(time.monotonic() - started_at, 1)


async def async_play_apple_tv_deeplink(
    hass: HomeAssistant,
    entry_data: Mapping[str, Any],
    zone_data: Mapping[str, Any],
    payload: Mapping[str, Any],
    media_content_type: str,
    *,
    api: Any = None,
) -> tuple[bool, int, str]:
    """Launch media on Apple TV (Plex 2026.20+) via verified deep link + server progress sync."""
    server_id = str(entry_data.get(CONF_SERVER_ID) or "").strip()
    _, hw_entity, remote_entity = resolve_zone_hardware(hass, zone_data)
    plex_client = resolve_plex_client(hass, zone_data)
    plex_key = payload.get("plex_key")

    if not server_id or not hw_entity or not isinstance(plex_key, int):
        return False, 0, "Apple TV deep link requires server_id, Apple TV entity, and rating_key."

    resume = bool(payload.get("resume"))
    offset_sec = int(payload.get("offset") or 0)
    is_show_root = bool(payload.get("_is_show_root"))

    # 1. On cold wake, wait for Apple TV to report awake so pyatv self.atv is connected
    hw_st = hass.states.get(hw_entity)
    if hw_st is None or hw_st.state in _HW_OFF_STATES:
        cold_wait = min(8.0, _zone_timing(zone_data, CONF_COLD_BOOT_TIMEOUT, "boot_delay"))
        if cold_wait > 0:
            await _async_wait_hardware_on(hass, hw_entity, cold_wait)
        os_wait = _zone_timing(zone_data, CONF_COLD_OS_DELAY)
        if os_wait > 0:
            await asyncio.sleep(os_wait)

    # 2. If another video is currently playing/paused, exit the active player cleanly first
    cur_hw = hass.states.get(hw_entity)
    cur_plex = hass.states.get(plex_client) if plex_client else None
    was_playing = (
        (cur_hw is not None and cur_hw.state in ("playing", "paused"))
        or (cur_plex is not None and cur_plex.state in ("playing", "paused"))
    )
    if was_playing and remote_entity:
        try:
            await hass.services.async_call(
                "remote",
                "send_command",
                {"entity_id": remote_entity, "command": "menu"},
                blocking=True,
            )
            await asyncio.sleep(0.8)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Apple TV menu exit warning: %s", redact_sensitive_text(err))

    # 3. Sync viewOffset on Plex server BEFORE opening card (Start=0 vs Resume=offset)
    if api is not None and hasattr(api, "async_set_playback_progress"):
        if not resume:
            await api.async_set_playback_progress(plex_key, 0)
        elif offset_sec > 0:
            await api.async_set_playback_progress(plex_key, offset_sec * 1000)

    deeplink = build_apple_tv_deeplink(server_id, plex_key, is_show_root=is_show_root)
    card_delay = _zone_timing(zone_data, CONF_WARM_APP_DELAY)
    app_source = str(zone_data.get(CONF_APP_SOURCE) or DEFAULT_APP_SOURCE).strip()

    async def _wait_for_plex_foreground(max_wait: float) -> str:
        cur = _apple_tv_app_status(hass.states.get(hw_entity), app_source)
        if cur != "other" or max_wait <= 0:
            return cur
        fg_ev = asyncio.Event()

        @callback
        def _on_hw_fg(event: Event) -> None:
            if _apple_tv_app_status(event.data.get("new_state"), app_source) == "plex":
                fg_ev.set()

        unsub_fg = async_track_state_change_event(hass, [hw_entity], _on_hw_fg)
        try:
            await _async_wait_event_or_delay(fg_ev, max_wait)
        finally:
            unsub_fg()
        return _apple_tv_app_status(hass.states.get(hw_entity), app_source)

    # 4. Send deep link; verify Plex actually opened and fall back to select_source('Plex') + retry
    for dl_attempt in range(2):
        try:
            await hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": hw_entity,
                    "media_content_type": "url",
                    "media_content_id": deeplink,
                },
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            safe = redact_sensitive_text(err)
            _LOGGER.warning("SmartPlex Apple TV deep link failed: %s", safe)
            if dl_attempt == 0:
                await _async_launch_plex_app(hass, zone_data)
                if card_delay > 0:
                    await asyncio.sleep(2.0)
                continue
            return False, dl_attempt, safe

        if card_delay > 0:
            await asyncio.sleep(card_delay)

        fg_wait = max(2.5, card_delay) if card_delay > 0 else 0.0
        fg_status = await _wait_for_plex_foreground(fg_wait)
        if dl_attempt == 0 and fg_status == "other":
            st_after = hass.states.get(hw_entity)
            cur_app = (
                (
                    st_after.attributes.get("app_name")
                    or st_after.attributes.get("app_id")
                    or st_after.attributes.get("source")
                )
                if st_after
                else ""
            )
            _LOGGER.debug(
                "Apple TV did not switch to Plex yet (active=%s); launching Plex app and retrying deep link",
                cur_app,
            )
            await _async_launch_plex_app(hass, zone_data)
            if card_delay > 0:
                await _wait_for_plex_foreground(max(2.5, card_delay))
                await asyncio.sleep(min(1.5, card_delay))
            continue
        break

    if _apple_tv_app_status(hass.states.get(hw_entity), app_source) == "other":
        st_fin = hass.states.get(hw_entity)
        cur_app = (
            (
                st_fin.attributes.get("app_name")
                or st_fin.attributes.get("app_id")
                or st_fin.attributes.get("source")
            )
            if st_fin
            else "unknown"
        )
        msg = (
            f"Apple TV remained on '{cur_app}' instead of '{app_source}'; "
            "aborted auto-select to avoid opening another app."
        )
        _LOGGER.warning("SmartPlex: %s", msg)
        return False, 1, msg

    auto_select = bool(zone_data.get(CONF_ATV_AUTO_SELECT, DEFAULT_ATV_AUTO_SELECT))
    if not auto_select or not remote_entity:
        return True, 0, ""

    def _is_atv_playing() -> bool:
        for eid in (hw_entity, plex_client):
            if eid:
                st = hass.states.get(eid)
                if st is not None and st.state == "playing":
                    return True
        return False

    def _is_atv_paused() -> bool:
        st = hass.states.get(hw_entity)
        return st is not None and st.state == "paused"

    if not _is_atv_playing():
        # Single 'select' clicks Play (0:00) or Resume (at synced viewOffset) on the Plex tvOS card.
        # NEVER send a second 'select' inside the opened tvOS video player, as 'select' toggles Pause!
        try:
            await hass.services.async_call(
                "remote",
                "send_command",
                {"entity_id": remote_entity, "command": "select"},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Apple TV select command warning: %s", redact_sensitive_text(err))

        # Safety net: if stream ever lands in explicit 'paused' state (never while 'idle'/buffering!),
        # issue idempotent media_play which unpauses without ever pausing active video.
        if card_delay > 0 and not _is_atv_playing():
            state_ev = asyncio.Event()
            watch_ids = [eid for eid in (hw_entity, plex_client) if eid]

            @callback
            def _on_atv_state(event: Event) -> None:
                ns = event.data.get("new_state")
                if ns is not None and ns.state in ("playing", "paused"):
                    state_ev.set()

            unsub_st = async_track_state_change_event(hass, watch_ids, _on_atv_state)
            try:
                await _async_wait_event_or_delay(state_ev, 3.5)
            finally:
                unsub_st()

            if _is_atv_paused() and not _is_atv_playing():
                try:
                    await hass.services.async_call(
                        "media_player",
                        "media_play",
                        {"entity_id": hw_entity},
                        blocking=True,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Apple TV media_play unpause warning: %s",
                        redact_sensitive_text(err),
                    )

    return True, 0, ""


def _get_seek_tasks_map(hass: HomeAssistant) -> dict[str, asyncio.Task[Any]]:
    """Return per-client active resume-seek task registry."""
    data = getattr(hass, "data", None)
    if isinstance(data, dict):
        return data.setdefault(DOMAIN, {}).setdefault("seek_tasks", {})
    fallback = getattr(hass, "_smartplex_seek_tasks", None)
    if not isinstance(fallback, dict):
        fallback = {}
        setattr(hass, "_smartplex_seek_tasks", fallback)
    return fallback


def _cancel_pending_seek_task(hass: HomeAssistant, plex_client: str | None) -> None:
    """Cancel any in-flight resume-seek worker on plex_client before starting new media."""
    if not plex_client:
        return
    prev = _get_seek_tasks_map(hass).pop(plex_client, None)
    if prev is not None and not prev.done():
        prev.cancel()


async def _async_wait_event_or_delay(
    event: asyncio.Event, delay_sec: float
) -> bool:
    """Wait reactively for event or up to delay_sec without leaking tasks."""
    if event.is_set():
        return True
    if delay_sec <= 0:
        return False
    ev_task = asyncio.create_task(event.wait())
    sl_task = asyncio.create_task(asyncio.sleep(delay_sec))
    try:
        done, pending = await asyncio.wait(
            {ev_task, sl_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return ev_task in done
    except asyncio.CancelledError:
        ev_task.cancel()
        sl_task.cancel()
        await asyncio.gather(ev_task, sl_task, return_exceptions=True)
        raise


def extract_zone_machine_ids(
    hass: HomeAssistant,
    plex_client: str | None = None,
    zone_data: Mapping[str, Any] | None = None,
) -> set[str]:
    """Return all known Plex machineIdentifiers (lowercase) belonging to a zone."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    client_ids: list[str] = []
    if plex_client:
        client_ids.append(plex_client)
    if zone_data:
        for cand in get_plex_client_candidates(hass, zone_data):
            if cand not in client_ids:
                client_ids.append(cand)
        raw_cfg = str(zone_data.get(CONF_PLEX_CLIENT) or "").strip()
        if raw_cfg and raw_cfg not in client_ids:
            client_ids.append(raw_cfg)

    mids: set[str] = set()
    dev_ids: set[str] = set()
    raw_dev = str((zone_data or {}).get(CONF_PLEX_DEVICE_ID) or "").strip()
    if raw_dev:
        dev_ids.add(raw_dev)

    for cid in client_ids:
        ent = ent_reg.async_get(cid)
        if ent is None:
            continue
        uid = str(getattr(ent, "unique_id", "") or "").strip()
        if uid:
            part = uid.split(":")[-1].strip()
            if part and part.lower() != "plex.tv-clients":
                mids.add(part.lower())
        dev_id = getattr(ent, "device_id", None)
        if dev_id:
            dev_ids.add(str(dev_id))

    for did in dev_ids:
        dev = dev_reg.async_get(did)
        if dev:
            for dom, ident in getattr(dev, "identifiers", ()):
                if dom == "plex" and ident and str(ident).lower() != "plex.tv-clients":
                    mids.add(str(ident).strip().lower())
    return mids


def _session_matches_target_zone(
    hass: HomeAssistant | None,
    sess: PlexActiveSession,
    plex_client: str | None = None,
    zone_data: Mapping[str, Any] | None = None,
    *,
    exclude_machine_ids: set[str] | None = None,
    exclude_player_titles: set[str] | None = None,
) -> bool:
    """Return True if a PMS /status/sessions entry belongs to target zone (never a stale source TV)."""
    sess_mid = str(sess.player_machine_id or "").strip().lower()
    if exclude_machine_ids and sess_mid and sess_mid in exclude_machine_ids:
        return False

    sess_title_norm = normalize_title(sess.player_title)
    target_mids = (
        extract_zone_machine_ids(hass, plex_client, zone_data)
        if hass is not None and (plex_client or zone_data)
        else set()
    )
    if target_mids:
        return bool(sess_mid and sess_mid in target_mids)

    if exclude_player_titles and sess_title_norm and sess_title_norm in exclude_player_titles:
        return False
    return True


def _state_matches_target_key(st: Any, target_key: int | None) -> bool:
    """Return False if HA entity still reports a previous item's media_content_id."""
    if st is None or target_key is None:
        return True
    cid = str(st.attributes.get("media_content_id") or "").strip()
    if not cid:
        return True
    if cid.isdigit():
        return int(cid) == target_key
    return f"/{target_key}" in cid or cid.endswith(str(target_key))


def resolve_protected_offset_sec(
    saved_map: dict[int, int] | None,
    rating_key: int,
    candidate_sec: int,
    *,
    anchors: dict[int, tuple[float, int, str]] | None = None,
    source_zone_name: str | None = None,
    source_was_playing: bool = False,
) -> int:
    """Protect saved resume progress against 0:00..0:64 proxy-start overwrites (zero dead zone)."""
    clean_sec = max(0, int(candidate_sec))
    guard = RESUME_WIPE_GUARD_SEC

    if isinstance(saved_map, dict):
        if clean_sec >= guard:
            saved_map[rating_key] = clean_sec
            return clean_sec
        saved_prev = int(saved_map.get(rating_key, 0) or 0)
        if saved_prev >= guard:
            est = saved_prev
            if isinstance(anchors, dict) and rating_key in anchors:
                anc_ts, anc_off, anc_zone = anchors[rating_key]
                if anc_off >= guard and (
                    not source_zone_name or anc_zone == source_zone_name
                ):
                    est = max(est, int(anc_off))
                    if source_was_playing:
                        elapsed = max(0.0, time.monotonic() - anc_ts)
                        if elapsed <= 7200.0:
                            est = max(est, int(anc_off + elapsed))
            return est
    return clean_sec


def _record_confirmed_playback_position(
    hass: HomeAssistant,
    rating_key: int | None,
    pos_sec: int,
    zone_name: str = "",
) -> None:
    """Persist a verified seek/play position across active SmartPlex runtimes."""
    if not isinstance(rating_key, int) or pos_sec < RESUME_WIPE_GUARD_SEC:
        return
    domain_data = getattr(hass, "data", None)
    if not isinstance(domain_data, dict):
        return
    runtimes = domain_data.get(DOMAIN, {}).get("runtimes")
    if not isinstance(runtimes, dict):
        return
    now_mono = time.monotonic()
    for rt in runtimes.values():
        saved = getattr(rt, "saved_resume_offsets", None)
        if isinstance(saved, dict):
            saved[rating_key] = int(pos_sec)
        anchors = getattr(rt, "playback_anchors", None)
        if isinstance(anchors, dict):
            anchors[rating_key] = (now_mono, int(pos_sec), zone_name)


def _read_ha_client_live_position_sec(
    hass: HomeAssistant,
    zone_data: Mapping[str, Any],
    target_key: int | None = None,
) -> tuple[int, bool]:
    """Read live playback position (sec) and is_playing flag from HA Plex entity states."""
    best_pos = 0
    is_playing = False
    for cid in get_plex_client_candidates(hass, zone_data):
        st = hass.states.get(cid)
        if st is None or st.state not in ACTIVE_PLAYBACK_STATES:
            continue
        if target_key is not None and not _state_matches_target_key(st, target_key):
            continue
        raw_pos = st.attributes.get("media_position")
        if not isinstance(raw_pos, (int, float)):
            continue
        pos = float(raw_pos)
        if st.state == "playing":
            is_playing = True
            updated_at = st.attributes.get("media_position_updated_at")
            if updated_at is not None:
                try:
                    dt_obj = (
                        dt_util.parse_datetime(updated_at)
                        if isinstance(updated_at, str)
                        else updated_at
                    )
                    if dt_obj is not None:
                        delta = (dt_util.utcnow() - dt_obj).total_seconds()
                        if 0.0 < delta < 600.0:
                            pos += delta
                except Exception:  # noqa: BLE001
                    pass
        if int(pos) > best_pos:
            best_pos = int(pos)
    return best_pos, is_playing


async def _async_read_confirmed_position_sec(
    hass: HomeAssistant,
    plex_client: str,
    target_key: int | None,
    target_offset_sec: int,
    tol_sec: float,
    api: Any = None,
    *,
    zone_data: Mapping[str, Any] | None = None,
    exclude_machine_ids: set[str] | None = None,
    exclude_player_titles: set[str] | None = None,
) -> float | None:
    """Read playback position (sec) from HA state and target-zone PMS /status/sessions."""
    st_pos: float | None = None
    st = hass.states.get(plex_client)
    if (
        st is not None
        and st.state in ACTIVE_PLAYBACK_STATES
        and _state_matches_target_key(st, target_key)
    ):
        raw_pos = st.attributes.get("media_position")
        if isinstance(raw_pos, (int, float)):
            st_pos = float(raw_pos)
            if st_pos >= (target_offset_sec - tol_sec):
                return st_pos

    get_sessions = getattr(api, "async_get_active_sessions", None)
    if callable(get_sessions):
        try:
            sessions = await get_sessions()
        except Exception:  # noqa: BLE001
            sessions = []
        for sess in sessions or ():
            if sess.player_state not in ACTIVE_PLAYBACK_STATES:
                continue
            if target_key is not None and sess.rating_key != target_key:
                continue
            if not _session_matches_target_zone(
                hass,
                sess,
                plex_client,
                zone_data,
                exclude_machine_ids=exclude_machine_ids,
                exclude_player_titles=exclude_player_titles,
            ):
                continue
            sess_pos = float(sess.view_offset_ms) / 1000.0
            if sess_pos >= (target_offset_sec - tol_sec):
                return sess_pos
            if st_pos is None or sess_pos > st_pos:
                st_pos = sess_pos
    return st_pos


async def _async_resolve_client_machine_id(
    hass: HomeAssistant,
    plex_client: str,
    zone_data: Mapping[str, Any] | None = None,
    target_key: int | None = None,
    api: Any = None,
    *,
    exclude_machine_ids: set[str] | None = None,
    exclude_player_titles: set[str] | None = None,
) -> str:
    """Resolve target Plex client machineIdentifier (HA zone registries FIRST, then filtered sessions)."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    client_ids = [plex_client] if plex_client else []
    if zone_data:
        for cand in get_plex_client_candidates(hass, zone_data):
            if cand not in client_ids:
                client_ids.append(cand)

    reg_mids: list[str] = []
    for cid in client_ids:
        ent = ent_reg.async_get(cid)
        uid = str(getattr(ent, "unique_id", "") or "").strip() if ent else ""
        if uid:
            part = uid.split(":")[-1].strip()
            if part and part.lower() != "plex.tv-clients" and part not in reg_mids:
                reg_mids.append(part)
        dev_id = getattr(ent, "device_id", None) if ent else None
        if dev_id:
            dev = dev_reg.async_get(str(dev_id))
            if dev:
                for dom, ident in getattr(dev, "identifiers", ()):
                    s_id = str(ident).strip()
                    if (
                        dom == "plex"
                        and s_id
                        and s_id.lower() != "plex.tv-clients"
                        and s_id not in reg_mids
                    ):
                        reg_mids.append(s_id)

    raw_dev = str((zone_data or {}).get(CONF_PLEX_DEVICE_ID) or "").strip()
    if raw_dev:
        dev = dev_reg.async_get(raw_dev)
        if dev:
            for dom, ident in getattr(dev, "identifiers", ()):
                s_id = str(ident).strip()
                if (
                    dom == "plex"
                    and s_id
                    and s_id.lower() != "plex.tv-clients"
                    and s_id not in reg_mids
                ):
                    reg_mids.append(s_id)

    get_sessions = getattr(api, "async_get_active_sessions", None)
    sessions: list[PlexActiveSession] = []
    if callable(get_sessions):
        try:
            sessions = list(await get_sessions() or ())
        except Exception:  # noqa: BLE001
            sessions = []

    if reg_mids:
        reg_low = {m.lower(): m for m in reg_mids}
        for sess in sessions:
            s_mid = str(sess.player_machine_id or "").strip()
            if s_mid and s_mid.lower() in reg_low:
                if target_key is None or sess.rating_key == target_key:
                    return s_mid
        return reg_mids[0]

    filtered = [
        sess
        for sess in sessions
        if sess.player_machine_id
        and _session_matches_target_zone(
            None,
            sess,
            exclude_machine_ids=exclude_machine_ids,
            exclude_player_titles=exclude_player_titles,
        )
    ]
    if target_key is not None:
        for sess in filtered:
            if sess.rating_key == target_key:
                return str(sess.player_machine_id).strip()
    if len(filtered) == 1:
        return str(filtered[0].player_machine_id).strip()
    return ""


async def _async_ensure_resume_seek(
    hass: HomeAssistant,
    plex_client: str,
    target_offset_sec: int,
    *,
    play_call_task: asyncio.Task[Any] | None = None,
    zone_data: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    media_type: str = "video",
    api: Any = None,
) -> None:
    """Closed-loop verified integer seek strictly AFTER play_media proxy finishes."""
    if not plex_client or target_offset_sec < 15:
        return

    current_task = asyncio.current_task()
    try:
        # CRITICAL: Never send seekTo while a 30s playMedia proxy HTTP request is still open,
        # otherwise PMS Companion proxy drops seekTo or crashes (verified on LG webOS & Tizen).
        if play_call_task is not None and not play_call_task.done():
            proxy_done = asyncio.Event()

            def _on_proxy_done(t: asyncio.Task[Any]) -> None:
                if not t.cancelled():
                    t.exception()
                proxy_done.set()

            play_call_task.add_done_callback(_on_proxy_done)
            if not play_call_task.done():
                await proxy_done.wait()

        if zone_data:
            plex_client = resolve_plex_client(hass, zone_data) or plex_client
        watch_clients = (
            get_plex_client_candidates(hass, zone_data)
            if zone_data
            else ([plex_client] if plex_client else [])
        ) or ([plex_client] if plex_client else [])

        profile = str((zone_data or {}).get(CONF_POWER_PROFILE) or "")
        zone_name = str((zone_data or {}).get(CONF_ZONE_NAME) or "")
        raw_key = (payload or {}).get("plex_key")
        target_key = raw_key if isinstance(raw_key, int) else None
        exclude_mids = {
            str(m).strip().lower()
            for m in ((payload or {}).get("_exclude_machine_ids") or ())
            if str(m).strip()
        } or None
        exclude_titles = {
            normalize_title(str(t))
            for t in ((payload or {}).get("_exclude_player_titles") or ())
            if normalize_title(str(t))
        } or None
        tol = min(25.0, max(8.0, target_offset_sec * 0.4))

        playing_event = asyncio.Event()

        def _is_playing_target(state_obj: Any) -> bool:
            return (
                state_obj is not None
                and state_obj.state == "playing"
                and _state_matches_target_key(state_obj, target_key)
            )

        if any(_is_playing_target(hass.states.get(cid)) for cid in watch_clients) or (
            await _async_has_matching_pms_session(
                api,
                target_key,
                require_playing=True,
                hass=hass,
                plex_client=plex_client,
                zone_data=zone_data,
                exclude_machine_ids=exclude_mids,
                exclude_player_titles=exclude_titles,
            )
        ):
            playing_event.set()
        else:

            @callback
            def _on_playing_state(event: Event) -> None:
                if _is_playing_target(event.data.get("new_state")):
                    playing_event.set()

            unsub_play = async_track_state_change_event(
                hass, watch_clients, _on_playing_state
            )
            try:
                spinup_deadline = time.monotonic() + RESUME_SPINUP_TIMEOUT_SEC
                while not playing_event.is_set():
                    rem = spinup_deadline - time.monotonic()
                    if rem <= 0:
                        break
                    if await _async_wait_event_or_delay(
                        playing_event, min(1.5, rem)
                    ):
                        break
                    if await _async_has_matching_pms_session(
                        api,
                        target_key,
                        require_playing=True,
                        hass=hass,
                        plex_client=plex_client,
                        zone_data=zone_data,
                        exclude_machine_ids=exclude_mids,
                        exclude_player_titles=exclude_titles,
                    ):
                        playing_event.set()
                        break
            finally:
                unsub_play()

        if zone_data:
            plex_client = resolve_plex_client(hass, zone_data) or plex_client
        st0 = hass.states.get(plex_client)
        if (st0 is None or st0.state not in ACTIVE_PLAYBACK_STATES) and not (
            await _async_has_matching_pms_session(
                api,
                target_key,
                hass=hass,
                plex_client=plex_client,
                zone_data=zone_data,
                exclude_machine_ids=exclude_mids,
                exclude_player_titles=exclude_titles,
            )
        ):
            _LOGGER.debug(
                "SmartPlex resume seek aborted: '%s' is not active for key=%s",
                plex_client,
                target_key,
            )
            return

        # Fast exit ONLY if THIS target player already opened the stream at target_offset_sec!
        already_pos = await _async_read_confirmed_position_sec(
            hass,
            plex_client,
            target_key,
            target_offset_sec,
            tol,
            api=api,
            zone_data=zone_data,
            exclude_machine_ids=exclude_mids,
            exclude_player_titles=exclude_titles,
        )
        if already_pos is not None and already_pos >= (target_offset_sec - tol):
            _LOGGER.debug(
                "SmartPlex resume seek skipped: '%s' already at %.1fs (target=%ss)",
                plex_client,
                already_pos,
                target_offset_sec,
            )
            _record_confirmed_playback_position(
                hass, target_key, int(already_pos), zone_name
            )
            return

        init_pos = (
            st0.attributes.get("media_position")
            if st0 is not None and _state_matches_target_key(st0, target_key)
            else None
        )
        if profile == PROFILE_SAMSUNG_TIZEN:
            settle_sec = SAMSUNG_RESUME_INITIAL_SETTLE_SEC
        elif profile == PROFILE_LG_WEBOS:
            settle_sec = LG_WEBOS_RESUME_INITIAL_SETTLE_SEC
        elif not isinstance(init_pos, (int, float)) or float(init_pos) < 2.0:
            settle_sec = 1.2
        else:
            settle_sec = 0.8
        await asyncio.sleep(settle_sec)

        seek_api_fn = getattr(api, "async_seek_client_player", None)
        for attempt in range(RESUME_SEEK_MAX_ATTEMPTS):
            if zone_data:
                plex_client = resolve_plex_client(hass, zone_data) or plex_client
            st_cur = hass.states.get(plex_client)
            if (
                st_cur is not None
                and st_cur.state in OFFLINE_CLIENT_STATES
                and not (
                    await _async_has_matching_pms_session(
                        api,
                        target_key,
                        hass=hass,
                        plex_client=plex_client,
                        zone_data=zone_data,
                        exclude_machine_ids=exclude_mids,
                        exclude_player_titles=exclude_titles,
                    )
                )
            ):
                return

            pos = await _async_read_confirmed_position_sec(
                hass,
                plex_client,
                target_key,
                target_offset_sec,
                tol,
                api=api,
                zone_data=zone_data,
                exclude_machine_ids=exclude_mids,
                exclude_player_titles=exclude_titles,
            )
            if pos is not None and pos >= (target_offset_sec - tol):
                _record_confirmed_playback_position(
                    hass, target_key, int(pos), zone_name
                )
                return

            _LOGGER.info(
                "SmartPlex safe post-proxy resume seek (%s/%s) on '%s' -> %ss (current_pos=%s)",
                attempt + 1,
                RESUME_SEEK_MAX_ATTEMPTS,
                plex_client,
                target_offset_sec,
                pos,
            )
            pos_event = asyncio.Event()

            @callback
            def _on_pos_update(event: Event) -> None:
                ns = event.data.get("new_state")
                if ns is None or not _state_matches_target_key(ns, target_key):
                    return
                new_p = ns.attributes.get("media_position")
                if isinstance(new_p, (int, float)) and float(new_p) >= (
                    target_offset_sec - tol
                ):
                    pos_event.set()

            unsub_pos = async_track_state_change_event(
                hass, watch_clients, _on_pos_update
            )
            try:
                # Never fire direct seekTo and HA media_seek simultaneously in the same millisecond;
                # run clean integer seekTo first, then separate HA media_seek fallback on retries if needed.
                direct_sent = False
                if callable(seek_api_fn) and attempt < 2:
                    machine_id = await _async_resolve_client_machine_id(
                        hass,
                        plex_client,
                        zone_data=zone_data,
                        target_key=target_key,
                        api=api,
                        exclude_machine_ids=exclude_mids,
                        exclude_player_titles=exclude_titles,
                    )
                    if machine_id:
                        direct_sent = bool(
                            await seek_api_fn(
                                machine_id,
                                int(target_offset_sec) * 1000,
                                media_type,
                            )
                        )

                if not direct_sent:
                    seek_call = asyncio.create_task(
                        hass.services.async_call(
                            "media_player",
                            "media_seek",
                            {
                                "entity_id": plex_client,
                                "seek_position": int(target_offset_sec),
                            },
                            blocking=True,
                        )
                    )
                    seek_call.add_done_callback(
                        lambda t: t.exception() if not t.cancelled() else None
                    )
                    pos_wait = asyncio.create_task(pos_event.wait())
                    try:
                        done_seek, pending_seek = await asyncio.wait(
                            {seek_call, pos_wait},
                            timeout=RESUME_SEEK_CALL_TIMEOUT_SEC,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for p_sk in pending_seek:
                            p_sk.cancel()
                        if seek_call in done_seek and seek_call.exception() is not None:
                            err_txt = redact_sensitive_text(seek_call.exception())
                            if err_txt:
                                _LOGGER.debug("Resume media_seek warning: %s", err_txt)
                    finally:
                        if not pos_wait.done():
                            pos_wait.cancel()
                        if not seek_call.done():
                            seek_call.cancel()
                        await asyncio.gather(seek_call, pos_wait, return_exceptions=True)

                if pos_event.is_set():
                    _record_confirmed_playback_position(
                        hass, target_key, int(target_offset_sec), zone_name
                    )
                    return

                pos_after = await _async_read_confirmed_position_sec(
                    hass,
                    plex_client,
                    target_key,
                    target_offset_sec,
                    tol,
                    api=api,
                    zone_data=zone_data,
                    exclude_machine_ids=exclude_mids,
                    exclude_player_titles=exclude_titles,
                )
                if pos_after is not None and pos_after >= (target_offset_sec - tol):
                    _record_confirmed_playback_position(
                        hass, target_key, int(pos_after), zone_name
                    )
                    return

                if await _async_wait_event_or_delay(
                    pos_event, RESUME_SEEK_VERIFY_WAIT_SEC
                ):
                    _record_confirmed_playback_position(
                        hass, target_key, int(target_offset_sec), zone_name
                    )
                    return
                pos_final = await _async_read_confirmed_position_sec(
                    hass,
                    plex_client,
                    target_key,
                    target_offset_sec,
                    tol,
                    api=api,
                    zone_data=zone_data,
                    exclude_machine_ids=exclude_mids,
                    exclude_player_titles=exclude_titles,
                )
                if pos_final is not None and pos_final >= (target_offset_sec - tol):
                    _record_confirmed_playback_position(
                        hass, target_key, int(pos_final), zone_name
                    )
                    return
            finally:
                unsub_pos()

        # Preserve saved watch progress on PMS if player could not seek
        set_prog_fn = getattr(api, "async_set_playback_progress", None)
        if target_key is not None and callable(set_prog_fn):
            await set_prog_fn(target_key, int(target_offset_sec) * 1000)
    finally:
        seek_map = _get_seek_tasks_map(hass)
        for k_cid, reg_task in list(seek_map.items()):
            if reg_task is current_task:
                seek_map.pop(k_cid, None)


def _schedule_resume_seek(
    hass: HomeAssistant,
    plex_client: str,
    payload: Mapping[str, Any],
    *,
    play_call_task: asyncio.Task[Any] | None = None,
    zone_data: Mapping[str, Any] | None = None,
    media_type: str = "video",
    api: Any = None,
) -> None:
    target_offset = int(payload.get("offset") or 0)
    if not plex_client or target_offset < 15:
        return
    _cancel_pending_seek_task(hass, plex_client)
    create_fn = getattr(hass, "async_create_task", None) or asyncio.create_task
    task = create_fn(
        _async_ensure_resume_seek(
            hass,
            plex_client,
            target_offset,
            play_call_task=play_call_task,
            zone_data=zone_data,
            payload=payload,
            media_type=media_type,
            api=api,
        )
    )
    if isinstance(task, asyncio.Task):
        _get_seek_tasks_map(hass)[plex_client] = task


async def _async_wait_client_online(
    hass: HomeAssistant,
    plex_client: str,
    scan_button: str | None,
    timeout_sec: float,
    *,
    zone_data: Mapping[str, Any] | None = None,
) -> bool:
    """Press scan_clients and wait reactively for plex_client (or _2/_3 sibling) to leave unavailable/off."""
    watch_ids = (
        get_plex_client_candidates(hass, zone_data)
        if zone_data
        else ([plex_client] if plex_client else [])
    ) or ([plex_client] if plex_client else [])
    if any(is_plex_client_ready(hass, cid) for cid in watch_ids):
        return True

    ready_event = asyncio.Event()

    @callback
    def _on_ready(event: Event) -> None:
        ns = event.data.get("new_state")
        if ns is not None and ns.state not in OFFLINE_CLIENT_STATES:
            ready_event.set()

    unsub = async_track_state_change_event(hass, watch_ids, _on_ready)
    try:
        await _press_scan_button(hass, scan_button)
        if any(is_plex_client_ready(hass, cid) for cid in watch_ids):
            return True
        if timeout_sec > 0:
            try:
                await asyncio.wait_for(ready_event.wait(), timeout=timeout_sec)
            except TimeoutError:
                pass
        return any(is_plex_client_ready(hass, cid) for cid in watch_ids)
    finally:
        unsub()


async def _async_has_matching_pms_session(
    api: Any,
    plex_key: int | None,
    *,
    require_playing: bool = False,
    hass: HomeAssistant | None = None,
    plex_client: str | None = None,
    zone_data: Mapping[str, Any] | None = None,
    exclude_machine_ids: set[str] | None = None,
    exclude_player_titles: set[str] | None = None,
) -> bool:
    """Check PMS /status/sessions to confirm if target item already started playing on THIS TV."""
    get_sessions = getattr(api, "async_get_active_sessions", None)
    if not callable(get_sessions):
        return False
    try:
        sessions = await get_sessions()
    except Exception:  # noqa: BLE001
        return False
    for sess in sessions or ():
        if require_playing:
            if sess.player_state != "playing":
                continue
        elif sess.player_state not in ACTIVE_PLAYBACK_STATES:
            continue
        if isinstance(plex_key, int) and sess.rating_key != plex_key:
            continue
        if not _session_matches_target_zone(
            hass,
            sess,
            plex_client,
            zone_data,
            exclude_machine_ids=exclude_machine_ids,
            exclude_player_titles=exclude_player_titles,
        ):
            continue
        if isinstance(plex_key, int):
            return True
    return False


async def _async_confirm_post_timeout_playback(
    hass: HomeAssistant,
    plex_client: str,
    payload: Mapping[str, Any],
    *,
    api: Any = None,
    zone_data: Mapping[str, Any] | None = None,
    grace_sec: float = PROXY_READ_TIMEOUT_GRACE_SEC,
) -> bool:
    """Confirm whether Samsung Tizen / LG webOS actually started playback after 30s proxy ReadTimeout."""
    if not plex_client:
        return False
    st_now = hass.states.get(plex_client)
    if st_now is not None and st_now.state in ACTIVE_PLAYBACK_STATES:
        return True

    plex_key = payload.get("plex_key") if isinstance(payload.get("plex_key"), int) else None
    exclude_mids = {
        str(m).strip().lower()
        for m in (payload.get("_exclude_machine_ids") or ())
        if str(m).strip()
    } or None
    exclude_titles = {
        normalize_title(str(t))
        for t in (payload.get("_exclude_player_titles") or ())
        if normalize_title(str(t))
    } or None
    if await _async_has_matching_pms_session(
        api,
        plex_key,
        hass=hass,
        plex_client=plex_client,
        zone_data=zone_data,
        exclude_machine_ids=exclude_mids,
        exclude_player_titles=exclude_titles,
    ):
        return True
    if grace_sec <= 0:
        return False

    play_event = asyncio.Event()

    @callback
    def _on_active(event: Event) -> None:
        ns = event.data.get("new_state")
        if ns is not None and ns.state in ACTIVE_PLAYBACK_STATES:
            play_event.set()

    unsub = async_track_state_change_event(hass, [plex_client], _on_active)
    try:
        try:
            await asyncio.wait_for(play_event.wait(), timeout=grace_sec)
            return True
        except TimeoutError:
            pass
    finally:
        unsub()

    st_end = hass.states.get(plex_client)
    if st_end is not None and st_end.state in ACTIVE_PLAYBACK_STATES:
        return True
    return await _async_has_matching_pms_session(
        api,
        plex_key,
        hass=hass,
        plex_client=plex_client,
        zone_data=zone_data,
        exclude_machine_ids=exclude_mids,
        exclude_player_titles=exclude_titles,
    )


async def async_play_on_zone_with_retry(
    hass: HomeAssistant,
    entry_data: Mapping[str, Any],
    zone_data: Mapping[str, Any],
    payload: dict[str, Any],
    media_content_type: str,
    *,
    api: Any = None,
) -> tuple[bool, int, str]:
    """Send media_player.play_media using native plex:// URI (or Apple TV deep link) with retries.

    Returns (success, retries_used, last_error).
    """
    plex_client = resolve_plex_client(hass, zone_data)
    plex_device_id = str(zone_data.get(CONF_PLEX_DEVICE_ID) or "").strip()
    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    scan_button = entry_data.get(CONF_SCAN_BUTTON)

    # Apple TV (Plex 2026.20+): port 32500 is closed even during playback -> ALWAYS use tvOS Deep Link!
    if profile == PROFILE_APPLE_TV and isinstance(payload.get("plex_key"), int):
        _, hw_entity, _ = resolve_zone_hardware(hass, zone_data)
        if hw_entity and entry_data.get(CONF_SERVER_ID):
            return await async_play_apple_tv_deeplink(
                hass, entry_data, zone_data, payload, media_content_type, api=api
            )

    if plex_client:
        ent = er.async_get(hass).async_get(plex_client)
        if ent is not None and getattr(ent, "platform", "plex") != "plex":
            raise HomeAssistantError(
                f"Configured plex_client '{plex_client}' belongs to integration "
                f"'{ent.platform}', not 'plex'. Reconfigure the zone and select a "
                "media_player.plex_* client entity."
            )

    max_retries = int(_zone_timing(zone_data, CONF_PLAY_RETRY_COUNT))
    raw_delay = _zone_timing(zone_data, CONF_PLAY_RETRY_DELAY)
    retry_delay = raw_delay if 0.0 < raw_delay < 1.0 else max(1.0, raw_delay)
    if profile == PROFILE_SAMSUNG_TIZEN and max_retries > 0:
        max_retries = max(max_retries, SAMSUNG_MIN_RETRY_COUNT)
        if retry_delay >= 1.0:
            retry_delay = max(retry_delay, SAMSUNG_MIN_RETRY_DELAY_SEC)
    elif profile == PROFILE_LG_WEBOS and max_retries > 0:
        max_retries = max(max_retries, LG_MIN_RETRY_COUNT)
        if retry_delay >= 1.0:
            retry_delay = max(retry_delay, LG_MIN_RETRY_DELAY_SEC)

    content_id, norm_type = build_plex_media_args(
        entry_data, payload, media_content_type, power_profile=profile
    )
    service_data: dict[str, Any] = {
        "media_content_id": content_id,
        "media_content_type": norm_type,
    }
    if plex_client:
        service_data["entity_id"] = plex_client
    elif plex_device_id:
        service_data["device_id"] = plex_device_id

    retries_used = 0
    last_error = ""
    call_succeeded = False
    relaunched_app = False
    for cand_cid in get_plex_client_candidates(hass, zone_data) or ([plex_client] if plex_client else []):
        _cancel_pending_seek_task(hass, cand_cid)

    for attempt in range(max_retries + 1):
        resolved_now = resolve_plex_client(hass, zone_data)
        if resolved_now:
            plex_client = resolved_now
            service_data["entity_id"] = plex_client
            service_data.pop("device_id", None)

        if plex_client and not is_plex_client_ready(hass, plex_client):
            if not relaunched_app:
                relaunched_app = True
                await _async_launch_plex_app(hass, zone_data)
            wait_online = retry_delay if retry_delay < 1.0 else min(retry_delay, 5.0)
            online = await _async_wait_client_online(
                hass,
                plex_client,
                scan_button,
                wait_online,
                zone_data=zone_data,
            )
            resolved_after = resolve_plex_client(hass, zone_data)
            if resolved_after:
                plex_client = resolved_after
                service_data["entity_id"] = plex_client
                service_data.pop("device_id", None)
            if not online and not is_plex_client_ready(hass, plex_client):
                last_error = f"Plex client '{plex_client}' is offline/unavailable."
                if attempt < max_retries:
                    retries_used += 1
                    continue
                return False, retries_used, last_error

        started_event = asyncio.Event()
        unsub = None
        pre_state = hass.states.get(plex_client) if plex_client else None
        was_already_active = (
            pre_state is not None and pre_state.state in ACTIVE_PLAYBACK_STATES
        )
        if plex_client:

            @callback
            def _on_play_state(event: Event) -> None:
                new_state = event.data.get("new_state")
                if new_state is not None and new_state.state in ACTIVE_PLAYBACK_STATES:
                    started_event.set()

            unsub = async_track_state_change_event(hass, [plex_client], _on_play_state)

        try:
            call_task = asyncio.create_task(
                hass.services.async_call(
                    "media_player", "play_media", service_data, blocking=True
                )
            )
            if plex_client:
                target_rk = (
                    payload.get("plex_key")
                    if isinstance(payload.get("plex_key"), int)
                    else None
                )
                pms_poll_task: asyncio.Task[None] | None = None
                exclude_mids_play = {
                    str(m).strip().lower()
                    for m in (payload.get("_exclude_machine_ids") or ())
                    if str(m).strip()
                } or None
                exclude_titles_play = {
                    normalize_title(str(t))
                    for t in (payload.get("_exclude_player_titles") or ())
                    if normalize_title(str(t))
                } or None
                if api is not None and target_rk is not None:

                    async def _poll_pms_while_calling() -> None:
                        await asyncio.sleep(2.0)
                        while not call_task.done() and not started_event.is_set():
                            if await _async_has_matching_pms_session(
                                api,
                                target_rk,
                                hass=hass,
                                plex_client=plex_client,
                                zone_data=zone_data,
                                exclude_machine_ids=exclude_mids_play,
                                exclude_player_titles=exclude_titles_play,
                            ):
                                started_event.set()
                                return
                            await asyncio.sleep(1.5)

                    pms_poll_task = asyncio.create_task(_poll_pms_while_calling())

                wait_play_task = asyncio.create_task(started_event.wait())
                race_timeout = 3.5 if was_already_active else None
                try:
                    done, _ = await asyncio.wait(
                        {call_task, wait_play_task},
                        timeout=race_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if pms_poll_task is not None and not pms_poll_task.done():
                        pms_poll_task.cancel()
                        await asyncio.gather(pms_poll_task, return_exceptions=True)

                if not call_task.done() and (
                    wait_play_task in done
                    or (
                        was_already_active
                        and (cur_mid := hass.states.get(plex_client)) is not None
                        and cur_mid.state in ACTIVE_PLAYBACK_STATES
                    )
                ):
                    # Client transitioned (or remained actively playing new item) over WebSocket!
                    # Do NOT wait 30s for LG/webOS plexapi proxy HTTP ReadTimeout.
                    if not wait_play_task.done():
                        wait_play_task.cancel()
                    call_task.add_done_callback(
                        lambda t: t.exception() if not t.cancelled() else None
                    )
                    _schedule_resume_seek(
                        hass,
                        plex_client,
                        payload,
                        play_call_task=call_task,
                        zone_data=zone_data,
                        media_type=norm_type,
                        api=api,
                    )
                    return True, retries_used, ""
                if not wait_play_task.done():
                    wait_play_task.cancel()
                    try:
                        await wait_play_task
                    except asyncio.CancelledError:
                        pass
                if not call_task.done():
                    await call_task
                else:
                    call_task.result()
            else:
                await call_task
            call_succeeded = True
            last_error = ""
        except Exception as err:  # noqa: BLE001
            # CRITICAL: On LG webOS & Samsung Tizen proxy clients, plexapi may raise Read timed out (30s)
            # AFTER or RIGHT AS playback starts! Confirm live state / PMS session before ever retrying!
            raw_err_lower = str(err).lower()
            is_proxy_timeout = (
                "read timed out" in raw_err_lower or isinstance(err, TimeoutError)
            )
            if plex_client:
                cur_after_err = hass.states.get(plex_client)
                if cur_after_err and cur_after_err.state in ACTIVE_PLAYBACK_STATES:
                    _LOGGER.debug(
                        "play_media returned error (%s) after '%s' already entered '%s'; treating as success",
                        redact_sensitive_text(err),
                        plex_client,
                        cur_after_err.state,
                    )
                    _schedule_resume_seek(
                        hass,
                        plex_client,
                        payload,
                        zone_data=zone_data,
                        media_type=norm_type,
                        api=api,
                    )
                    return True, retries_used, ""

                if is_proxy_timeout:
                    grace = 0.05 if retry_delay < 1.0 else PROXY_READ_TIMEOUT_GRACE_SEC
                    if await _async_confirm_post_timeout_playback(
                        hass,
                        plex_client,
                        payload,
                        api=api,
                        zone_data=zone_data,
                        grace_sec=grace,
                    ):
                        _LOGGER.info(
                            "SmartPlex confirmed active playback on '%s' after 30s proxy timeout; skipping retry",
                            plex_client,
                        )
                        _schedule_resume_seek(
                            hass,
                            plex_client,
                            payload,
                            zone_data=zone_data,
                            media_type=norm_type,
                            api=api,
                        )
                        return True, retries_used, ""

            last_error = redact_sensitive_text(err)
            _LOGGER.warning(
                "SmartPlex play_media attempt %s/%s failed (%s): %s",
                attempt + 1,
                max_retries + 1,
                plex_client or plex_device_id,
                last_error,
            )
            if attempt < max_retries:
                retries_used += 1
                if not relaunched_app and (
                    not is_plex_client_ready(hass, plex_client)
                    or any(
                        tok in raw_err_lower
                        for tok in (
                            "connection refused",
                            "newconnectionerror",
                            "failed to establish",
                            "(404)",
                            "not_found",
                        )
                    )
                ):
                    relaunched_app = True
                    await _async_launch_plex_app(hass, zone_data)
                await _press_scan_button(hass, scan_button)
                await asyncio.sleep(retry_delay)
                continue
            return False, retries_used, last_error
        finally:
            if unsub is not None:
                unsub()

        if not plex_client:
            return True, retries_used, ""

        cur_state = hass.states.get(plex_client)
        if cur_state and cur_state.state in ACTIVE_PLAYBACK_STATES:
            _schedule_resume_seek(
                hass,
                plex_client,
                payload,
                zone_data=zone_data,
                media_type=norm_type,
                api=api,
            )
            return True, retries_used, ""

        if attempt >= max_retries:
            break

        # Give NAS disks / heavy 4K Remux files time to spin up & open stream before retrying
        spinup_wait = (
            retry_delay
            if 0.0 < retry_delay < 1.0
            else max(retry_delay, POST_PLAY_SPINUP_WAIT_SEC)
        )
        confirm_event = asyncio.Event()
        target_rk_confirm = (
            payload.get("plex_key")
            if isinstance(payload.get("plex_key"), int)
            else None
        )

        @callback
        def _on_confirm_state(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is not None and new_state.state in ACTIVE_PLAYBACK_STATES:
                confirm_event.set()

        watch_confirm = get_plex_client_candidates(hass, zone_data) or [plex_client]
        unsub_confirm = async_track_state_change_event(
            hass, watch_confirm, _on_confirm_state
        )
        try:
            confirm_deadline = time.monotonic() + spinup_wait
            confirmed = False
            while time.monotonic() < confirm_deadline:
                rem_c = confirm_deadline - time.monotonic()
                step_c = min(2.0, rem_c) if spinup_wait >= 2.0 else rem_c
                if await _async_wait_event_or_delay(confirm_event, step_c):
                    confirmed = True
                    break
                if await _async_has_matching_pms_session(
                    api,
                    target_rk_confirm,
                    hass=hass,
                    plex_client=plex_client,
                    zone_data=zone_data,
                    exclude_machine_ids=(
                        {
                            str(m).strip().lower()
                            for m in (payload.get("_exclude_machine_ids") or ())
                            if str(m).strip()
                        }
                        or None
                    ),
                    exclude_player_titles=(
                        {
                            normalize_title(str(t))
                            for t in (payload.get("_exclude_player_titles") or ())
                            if normalize_title(str(t))
                        }
                        or None
                    ),
                ):
                    confirmed = True
                    break
            if confirmed:
                plex_client = resolve_plex_client(hass, zone_data) or plex_client
                _schedule_resume_seek(
                    hass,
                    plex_client,
                    payload,
                    zone_data=zone_data,
                    media_type=norm_type,
                    api=api,
                )
                return True, retries_used, ""

            retries_used += 1
            if not is_plex_client_ready(hass, plex_client):
                await _async_wait_client_online(
                    hass,
                    plex_client,
                    scan_button,
                    retry_delay if retry_delay < 1.0 else min(retry_delay, 3.0),
                    zone_data=zone_data,
                )
        finally:
            unsub_confirm()

    if plex_client and not is_plex_client_ready(hass, plex_client):
        return (
            False,
            retries_used,
            f"Plex client '{plex_client}' is offline/unavailable.",
        )
    return call_succeeded, retries_used, last_error


def _match_session_to_zone(
    hass: HomeAssistant,
    zones: list[ConfigSubentry],
    sessions: list[PlexActiveSession],
    exclude_subentry_id: str,
) -> tuple[ConfigSubentry | None, PlexActiveSession | None]:
    """Find an active/paused Plex session belonging to another configured zone."""
    if not sessions:
        return None, None

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    def _zone_machine_ids(sub: ConfigSubentry) -> set[str]:
        ids: set[str] = set()
        cand_ids = get_plex_client_candidates(hass, sub.data)
        raw_cfg = str(sub.data.get(CONF_PLEX_CLIENT) or "").strip()
        if raw_cfg and raw_cfg not in cand_ids:
            cand_ids.append(raw_cfg)

        dev_ids: set[str] = set()
        if sub.data.get(CONF_PLEX_DEVICE_ID):
            dev_ids.add(str(sub.data[CONF_PLEX_DEVICE_ID]))

        for cid in cand_ids:
            ent = ent_reg.async_get(cid)
            if ent is not None:
                uid = str(getattr(ent, "unique_id", "") or "").strip()
                if uid:
                    tail = uid.split(":")[-1].strip().lower()
                    if tail and tail != "plex.tv-clients":
                        ids.add(tail)
                if getattr(ent, "device_id", None):
                    dev_ids.add(str(ent.device_id))

        for did in dev_ids:
            dev = dev_reg.async_get(did)
            if dev:
                for _, ident in getattr(dev, "identifiers", ()):
                    low = str(ident).lower()
                    if low and low != "plex.tv-clients":
                        ids.add(low)
        return ids

    other_zones = [z for z in zones if z.subentry_id != exclude_subentry_id]
    excluded_mids: set[str] = set()
    if exclude_subentry_id:
        for z in zones:
            if z.subentry_id == exclude_subentry_id:
                excluded_mids.update(_zone_machine_ids(z))

    eligible_sessions = [
        s
        for s in sessions
        if not (
            excluded_mids
            and s.player_machine_id
            and any(s.player_machine_id.lower() in ident for ident in excluded_mids)
        )
    ]
    if not eligible_sessions:
        return None, None

    for sub in other_zones:
        m_ids = _zone_machine_ids(sub)
        for sess in eligible_sessions:
            if sess.player_machine_id and any(
                sess.player_machine_id.lower() in ident for ident in m_ids
            ):
                return sub, sess

    for sub in other_zones:
        plex_client = resolve_plex_client(hass, sub.data)
        st = hass.states.get(plex_client) if plex_client else None
        friendly = str(st.attributes.get("friendly_name") or "") if st else ""
        _, hw_ent, _ = resolve_zone_hardware(hass, sub.data)
        hw_st = hass.states.get(hw_ent) if hw_ent else None
        hw_friendly = str(hw_st.attributes.get("friendly_name") or "") if hw_st else ""
        haystack = normalize_title(f"{plex_client} {friendly} {hw_ent} {hw_friendly}")
        for sess in eligible_sessions:
            pt = normalize_title(sess.player_title)
            if pt and (pt in haystack or haystack in pt):
                return sub, sess

    for sub in other_zones:
        plex_client = resolve_plex_client(hass, sub.data)
        st = hass.states.get(plex_client) if plex_client else None
        if st and st.state in ACTIVE_PLAYBACK_STATES:
            return sub, eligible_sessions[0]

    awake_zones = []
    for sub in other_zones:
        _, hw_ent, _ = resolve_zone_hardware(hass, sub.data)
        hw_st = hass.states.get(hw_ent) if hw_ent else None
        if hw_st is not None and hw_st.state not in _HW_OFF_STATES:
            awake_zones.append(sub)
    if len(awake_zones) == 1:
        return awake_zones[0], eligible_sessions[0]

    return None, eligible_sessions[0]


def _matches_requested_media_type(item_type: str | None, requested: str | None) -> bool:
    """Return True if a session/On-Deck item matches an optional media_type filter."""
    if not requested:
        return True
    req = requested.strip().lower()
    it = (item_type or "").strip().lower()
    if req == "movie":
        return it == "movie"
    if req == "show":
        return it in ("show", "episode")
    if req == "music":
        return it in ("music", "track", "artist", "album")
    return True


def _collect_zone_exclude_markers(
    hass: HomeAssistant,
    zones_list: list[ConfigSubentry],
    session: PlexActiveSession | None = None,
) -> tuple[list[str], list[str]]:
    """Collect source TV machine_ids and player titles so target seek ignores stale source sessions."""
    mids: set[str] = set()
    titles: set[str] = set()
    if session is not None:
        if session.player_machine_id:
            mids.add(str(session.player_machine_id).strip().lower())
        if session.player_title:
            titles.add(str(session.player_title).strip())
    for z in zones_list:
        mids.update(extract_zone_machine_ids(hass, None, z.data))
        pc = resolve_plex_client(hass, z.data)
        st = hass.states.get(pc) if pc else None
        if st is not None:
            fn = str(st.attributes.get("friendly_name") or "").strip()
            if fn:
                titles.add(fn)
    return sorted(mids), sorted(titles)


def _build_handoff_payload(
    rating_key: int,
    media_type: str,
    offset_sec: int,
    *,
    exclude_mids: list[str] | None = None,
    exclude_titles: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Build canonical START/RESUME payload for movie, episode (continuous=1), or track."""
    m_low = str(media_type or "movie").strip().lower()
    if m_low in ("show", "episode"):
        payload = build_episode_template(
            rating_key,
            resume=offset_sec > 0,
            live_offset_sec=offset_sec,
            explicit_offset_sec=offset_sec if offset_sec > 0 else None,
            continuous=True,
        )
        ctype = "episode"
    elif m_low in ("music", "track"):
        payload = build_music_item_template(
            rating_key,
            item_kind="track",
            resume=offset_sec > 0,
            live_offset_sec=offset_sec,
            explicit_offset_sec=offset_sec if offset_sec > 0 else None,
        )
        ctype = "music"
    else:
        payload = build_movie_template(
            rating_key,
            resume=offset_sec > 0,
            live_offset_sec=offset_sec,
            explicit_offset_sec=offset_sec if offset_sec > 0 else None,
        )
        ctype = "movie"
    if exclude_mids:
        payload["_exclude_machine_ids"] = list(exclude_mids)
    if exclude_titles:
        payload["_exclude_player_titles"] = list(exclude_titles)
    return payload, ctype


async def async_handoff_or_resume(
    hass: HomeAssistant,
    entry: ConfigEntry,
    target_zone: ConfigSubentry,
    source_zone: ConfigSubentry | None = None,
    turn_off_source: bool | None = None,
    boot_task: asyncio.Task[tuple[str, float]] | None = None,
    media_type: str | None = None,
) -> LaunchResult:
    """Transfer active playback from another room (turning off old TV) or resume On Deck."""
    api = entry.runtime_data.api
    target_data = target_zone.data
    target_name = str(target_data.get(CONF_ZONE_NAME) or target_zone.title)
    rewind_sec = int(target_data.get(CONF_HANDOFF_REWIND, DEFAULT_HANDOFF_REWIND))

    sessions = await api.async_get_active_sessions()
    if media_type and sessions:
        sessions = [
            s for s in sessions if _matches_requested_media_type(s.media_type, media_type)
        ]

    # Fast-path unpause: if target zone ALREADY has a paused Plex player/session and no other
    # source_zone was requested, resume in-place in 50ms instead of restarting stream from 0:00!
    if source_zone is None or source_zone.subentry_id == target_zone.subentry_id:
        tgt_pc = resolve_plex_client(hass, target_data)
        tgt_st = hass.states.get(tgt_pc) if tgt_pc else None
        tgt_mids = extract_zone_machine_ids(hass, tgt_pc, target_data)
        tgt_paused_sess = next(
            (
                s
                for s in (sessions or ())
                if s.player_state == "paused"
                and (
                    (tgt_mids and s.player_machine_id and s.player_machine_id.lower() in tgt_mids)
                    or (tgt_st is not None and tgt_st.state == "paused")
                )
            ),
            None,
        )
        if (tgt_st is not None and tgt_st.state == "paused") or tgt_paused_sess is not None:
            if boot_task is not None and not boot_task.done():
                boot_task.cancel()
            return await async_control_zone_playback(
                hass, entry, target_zone, "resume_play"
            )

    own_boot = boot_task is None
    active_boot = boot_task or asyncio.create_task(
        async_ensure_zone_ready(hass, entry.data, target_data)
    )
    zones = get_zone_subentries(entry)
    matched_source: ConfigSubentry | None = source_zone
    matched_session: PlexActiveSession | None = None

    source_candidates: list[ConfigSubentry] = []
    if source_zone is not None:
        src_area = str(source_zone.data.get(CONF_AREA_ID) or "").strip()
        source_candidates = [source_zone] + [
            z
            for z in zones
            if z.subentry_id not in (source_zone.subentry_id, target_zone.subentry_id)
            and src_area
            and str(z.data.get(CONF_AREA_ID) or "").strip() == src_area
        ]
    else:
        source_candidates = [
            z for z in zones if z.subentry_id != target_zone.subentry_id
        ]

    if source_zone is not None and sessions:
        cand_src, matched_session = _match_session_to_zone(
            hass, source_candidates, sessions, exclude_subentry_id=target_zone.subentry_id
        )
        matched_source = cand_src or source_zone
    elif sessions:
        matched_source, matched_session = _match_session_to_zone(
            hass, zones, sessions, exclude_subentry_id=target_zone.subentry_id
        )

    saved_map = getattr(entry.runtime_data, "saved_resume_offsets", None)
    anchors_map = getattr(entry.runtime_data, "playback_anchors", None)

    if matched_session is not None:
        source_name = (
            str(matched_source.data.get(CONF_ZONE_NAME) or matched_source.title)
            if matched_source is not None
            else (matched_session.player_title or "Plex")
        )
        ha_src_pos, ha_src_playing = (
            _read_ha_client_live_position_sec(
                hass, matched_source.data, matched_session.rating_key
            )
            if matched_source is not None
            else (0, False)
        )
        sess_sec = max(0, matched_session.view_offset_ms // 1000)
        raw_sec = resolve_protected_offset_sec(
            saved_map,
            matched_session.rating_key,
            max(sess_sec, ha_src_pos),
            anchors=anchors_map,
            source_zone_name=source_name,
            source_was_playing=ha_src_playing or matched_session.player_state == "playing",
        )
        offset_sec = max(0, raw_sec - rewind_sec) if raw_sec > 0 else 0
        exclude_mids, exclude_titles = _collect_zone_exclude_markers(
            hass,
            [z for z in (matched_source, *source_candidates) if z is not None],
            matched_session,
        )
        should_turn_off = (
            turn_off_source
            if turn_off_source is not None
            else (
                bool(
                    matched_source.data.get(
                        CONF_TURN_OFF_ON_HANDOFF, DEFAULT_TURN_OFF_ON_HANDOFF
                    )
                )
                if matched_source is not None
                else False
            )
        )

        _LOGGER.info(
            "SmartPlex handoff: title='%s', source='%s' -> target='%s', raw=%ss, offset=%ss, key=%s, exclude_mids=%s",
            matched_session.display_title,
            source_name,
            target_name,
            raw_sec,
            offset_sec,
            matched_session.rating_key,
            len(exclude_mids),
        )

        shared_display = (
            _zones_share_physical_display(hass, matched_source.data, target_data)
            if matched_source is not None
            else False
        )
        power_off_task: asyncio.Task[None] | None = None
        if matched_source is not None and (should_turn_off or shared_display):
            if shared_display:
                # Stop old player BEFORE switching shared TV HDMI input, without turning TV off!
                await async_power_off_zone(
                    hass, matched_source.data, keep_shared_display=True
                )
            elif should_turn_off:
                power_off_task = asyncio.create_task(
                    async_power_off_zone(hass, matched_source.data)
                )
                power_off_task.add_done_callback(
                    lambda t: t.exception() if not t.cancelled() else None
                )

        start_phase, boot_sec = await active_boot

        payload, content_type = _build_handoff_payload(
            matched_session.rating_key,
            matched_session.media_type,
            offset_sec,
            exclude_mids=exclude_mids,
            exclude_titles=exclude_titles,
        )
        ok, retries, play_err = await async_play_on_zone_with_retry(
            hass, entry.data, target_data, payload, content_type, api=api
        )
        if power_off_task is not None and not power_off_task.done():
            await asyncio.wait({power_off_task}, timeout=3.0)

        is_ru = (getattr(hass.config, "language", None) or "en").lower().startswith("ru")
        off_note = (
            (f" ({source_name} выключен)" if is_ru else f" (turned off {source_name})")
            if (matched_source and should_turn_off and not shared_display)
            else ""
        )
        pos_str = f"{offset_sec // 60}:{offset_sec % 60:02d}"
        return LaunchResult(
            success=ok,
            zone_name=target_name,
            matched_title=matched_session.display_title,
            rating_key=matched_session.rating_key,
            media_type=matched_session.media_type,
            boot_seconds=boot_sec,
            retries_used=retries,
            handoff_from=source_name,
            start_phase=start_phase,
            message=(
                format_response(
                    hass,
                    entry.data,
                    "handoff_ok",
                    title=matched_session.display_title,
                    source=source_name,
                    zone=target_name,
                    position=pos_str,
                    off_note=off_note,
                )
                if ok
                else format_response(
                    hass,
                    entry.data,
                    "handoff_failed",
                    title=matched_session.display_title,
                    zone=target_name,
                    error=play_err,
                )
            ),
        )

    # Fallback when /status/sessions is empty (e.g. previous TV paused/stopped or session closed):
    # 1. Still power off the source zone TV if requested/awake!
    fallback_source: ConfigSubentry | None = None
    last_launch = getattr(entry.runtime_data, "last_launch", None)
    if source_zone is not None:
        fallback_source = source_zone
        for cand in source_candidates:
            cand_pc = resolve_plex_client(hass, cand.data)
            cand_pst = hass.states.get(cand_pc) if cand_pc else None
            _, cand_hw, _ = resolve_zone_hardware(hass, cand.data)
            cand_hw_st = hass.states.get(cand_hw) if cand_hw else None
            if (cand_pst and cand_pst.state in ACTIVE_PLAYBACK_STATES) or (
                cand_hw_st and cand_hw_st.state not in _HW_OFF_STATES
            ):
                fallback_source = cand
                break
    elif last_launch is not None and last_launch.zone_name != target_name:
        for cand in source_candidates:
            c_name = str(cand.data.get(CONF_ZONE_NAME) or cand.title)
            _, cand_hw, _ = resolve_zone_hardware(hass, cand.data)
            cand_hw_st = hass.states.get(cand_hw) if cand_hw else None
            cand_pc = resolve_plex_client(hass, cand.data)
            cand_pst = hass.states.get(cand_pc) if cand_pc else None
            if c_name == last_launch.zone_name and (
                (cand_hw_st and cand_hw_st.state not in _HW_OFF_STATES)
                or (cand_pst and cand_pst.state in ACTIVE_PLAYBACK_STATES)
            ):
                fallback_source = cand
                break

    should_off_fb = (
        turn_off_source
        if turn_off_source is not None
        else (
            bool(
                fallback_source.data.get(
                    CONF_TURN_OFF_ON_HANDOFF, DEFAULT_TURN_OFF_ON_HANDOFF
                )
            )
            if fallback_source is not None
            else False
        )
    )
    # Snapshot source HA state BEFORE launching power_off_task!
    fb_source_name = (
        str(fallback_source.data.get(CONF_ZONE_NAME) or fallback_source.title)
        if fallback_source is not None
        else None
    )
    fb_ha_pos, fb_ha_playing = (
        _read_ha_client_live_position_sec(
            hass,
            fallback_source.data,
            last_launch.rating_key if last_launch and isinstance(last_launch.rating_key, int) else None,
        )
        if fallback_source is not None
        else (0, False)
    )
    fb_exclude_mids, fb_exclude_titles = _collect_zone_exclude_markers(
        hass,
        [z for z in (fallback_source, *source_candidates) if z is not None],
    )

    fb_shared_display = (
        _zones_share_physical_display(hass, fallback_source.data, target_data)
        if fallback_source is not None
        else False
    )
    fb_power_off_task: asyncio.Task[None] | None = None
    if fallback_source is not None and (should_off_fb or fb_shared_display):
        if fb_shared_display:
            await async_power_off_zone(
                hass, fallback_source.data, keep_shared_display=True
            )
        elif should_off_fb:
            fb_power_off_task = asyncio.create_task(
                async_power_off_zone(hass, fallback_source.data)
            )
            fb_power_off_task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )

    on_deck = await api.async_get_continue_watching()
    if media_type and on_deck:
        typed_deck = [
            it for it in on_deck if _matches_requested_media_type(it.media_type, media_type)
        ]
        if typed_deck:
            on_deck = typed_deck

    # 2. Prefer recent last_launch from source room (protects against wiped onDeck when previous TV started at 0:00)
    if (
        last_launch is not None
        and last_launch.success
        and isinstance(last_launch.rating_key, int)
        and last_launch.zone_name != target_name
        and _matches_requested_media_type(last_launch.media_type, media_type)
    ):
        ll_rk = int(last_launch.rating_key)
        ll_live_sec = 0
        get_state_fn = getattr(api, "async_get_item_playback_state", None)
        if callable(get_state_fn):
            live_st = await get_state_fn(ll_rk)
            if live_st is not None:
                ll_live_sec = max(0, int(live_st[0]) // 1000)
        ll_sec = resolve_protected_offset_sec(
            saved_map,
            ll_rk,
            max(ll_live_sec, fb_ha_pos),
            anchors=anchors_map,
            source_zone_name=fb_source_name or last_launch.zone_name,
            source_was_playing=fb_ha_playing,
        )
        if source_zone is not None or ll_sec > 0 or not on_deck:
            ll_offset = max(0, ll_sec - rewind_sec) if ll_sec > 0 else 0
            _LOGGER.info(
                "SmartPlex handoff (fallback last_launch): title='%s', source='%s' -> target='%s', offset=%ss, key=%s",
                last_launch.matched_title,
                fb_source_name or last_launch.zone_name,
                target_name,
                ll_offset,
                ll_rk,
            )
            start_phase, boot_sec = await active_boot
            ll_payload, ll_ctype = _build_handoff_payload(
                ll_rk,
                last_launch.media_type,
                ll_offset,
                exclude_mids=fb_exclude_mids,
                exclude_titles=fb_exclude_titles,
            )
            ok, retries, play_err = await async_play_on_zone_with_retry(
                hass, entry.data, target_data, ll_payload, ll_ctype, api=api
            )
            if fb_power_off_task is not None and not fb_power_off_task.done():
                await asyncio.wait({fb_power_off_task}, timeout=3.0)
            ll_title = last_launch.matched_title or f"Plex #{ll_rk}"
            is_ru = (getattr(hass.config, "language", None) or "en").lower().startswith("ru")
            src_disp = fb_source_name or last_launch.zone_name
            off_note = (
                (f" ({src_disp} выключен)" if is_ru else f" (turned off {src_disp})")
                if (fallback_source and should_off_fb and not fb_shared_display)
                else ""
            )
            pos_str = f"{ll_offset // 60}:{ll_offset % 60:02d}"
            return LaunchResult(
                success=ok,
                zone_name=target_name,
                matched_title=ll_title,
                rating_key=ll_rk,
                media_type=last_launch.media_type,
                boot_seconds=boot_sec,
                retries_used=retries,
                handoff_from=src_disp,
                start_phase=start_phase,
                message=(
                    format_response(
                        hass,
                        entry.data,
                        "handoff_ok",
                        title=ll_title,
                        source=src_disp,
                        zone=target_name,
                        position=pos_str,
                        off_note=off_note,
                    )
                    if ok
                    else format_response(
                        hass,
                        entry.data,
                        "handoff_failed",
                        title=ll_title,
                        zone=target_name,
                        error=play_err,
                    )
                ),
            )
    if not on_deck:
        if own_boot and not active_boot.done():
            active_boot.cancel()
        if fb_power_off_task is not None and not fb_power_off_task.done():
            await asyncio.wait({fb_power_off_task}, timeout=3.0)
        return LaunchResult(
            success=False,
            zone_name=target_name,
            matched_title="",
            rating_key=None,
            media_type="movie",
            boot_seconds=0.0,
            retries_used=0,
            handoff_from=fb_source_name,
            start_phase="none",
            message=format_response(hass, entry.data, "nothing_to_resume"),
        )

    item = on_deck[0]
    deck_sec = resolve_protected_offset_sec(
        saved_map,
        item.rating_key,
        max(0, item.view_offset_ms // 1000),
        anchors=anchors_map,
    )
    offset_sec = max(0, deck_sec - rewind_sec) if deck_sec > 0 else 0
    _LOGGER.info(
        "SmartPlex handoff (On Deck): title='%s', target='%s', offset=%ss, key=%s",
        item.title,
        target_name,
        offset_sec,
        item.rating_key,
    )
    start_phase, boot_sec = await active_boot
    payload, content_type = _build_handoff_payload(
        item.rating_key,
        item.media_type,
        offset_sec,
        exclude_mids=fb_exclude_mids,
        exclude_titles=fb_exclude_titles,
    )
    ok, retries, play_err = await async_play_on_zone_with_retry(
        hass, entry.data, target_data, payload, content_type, api=api
    )
    if fb_power_off_task is not None and not fb_power_off_task.done():
        await asyncio.wait({fb_power_off_task}, timeout=3.0)
    display = (
        f"{item.grandparent_title} S{item.season_index:02d}E{item.episode_index:02d} — {item.title}"
        if item.media_type == "episode"
        and item.grandparent_title
        and item.season_index is not None
        and item.episode_index is not None
        else item.title
    )
    return LaunchResult(
        success=ok,
        zone_name=target_name,
        matched_title=display,
        rating_key=item.rating_key,
        media_type=item.media_type,
        boot_seconds=boot_sec,
        retries_used=retries,
        handoff_from=fb_source_name,
        start_phase=start_phase,
        message=(
            format_response(
                hass, entry.data, "resume_ok", title=display, zone=target_name
            )
            if ok
            else format_response(
                hass,
                entry.data,
                "resume_failed",
                title=display,
                zone=target_name,
                error=play_err,
            )
        ),
    )


async def async_resolve_active_playback_zone(
    entry: ConfigEntry,
    hass: HomeAssistant,
    zone_query: str | None = None,
    *,
    area_id: str | None = None,
) -> ConfigSubentry | None:
    """Resolve zone for transport/seek/unpause: explicit zone -> active Plex session -> area -> default."""
    if zone_query and zone_query.strip():
        return resolve_zone(entry, hass, zone_query, area_id=area_id)

    zones = get_zone_subentries(entry)
    if not zones:
        return None
    if len(zones) == 1:
        return zones[0]

    active_ha: list[ConfigSubentry] = []
    for sub in zones:
        pc = resolve_plex_client(hass, sub.data)
        st = hass.states.get(pc) if pc else None
        if st is not None and st.state in ACTIVE_PLAYBACK_STATES:
            active_ha.append(sub)
    if len(active_ha) == 1:
        return active_ha[0]
    if active_ha and area_id:
        for sub in active_ha:
            if sub.data.get(CONF_AREA_ID) == area_id:
                return sub

    runtime = getattr(entry, "runtime_data", None)
    api = getattr(runtime, "api", None) if runtime else None
    get_sessions = getattr(api, "async_get_active_sessions", None)
    if callable(get_sessions):
        try:
            sessions = await get_sessions()
        except Exception:  # noqa: BLE001
            sessions = []
        if sessions:
            matched_sub, _ = _match_session_to_zone(
                hass, zones, sessions, exclude_subentry_id=""
            )
            if matched_sub is not None:
                return matched_sub

    return resolve_zone(entry, hass, None, area_id=area_id)


def _format_mmss(total_sec: int) -> str:
    sec = max(0, int(total_sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _async_try_control_service(
    hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]
) -> bool:
    try:
        await hass.services.async_call(domain, service, data, blocking=True)
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "SmartPlex control %s.%s warning: %s",
            domain,
            service,
            redact_sensitive_text(err),
        )
        return False


async def _async_control_queue_action(
    hass: HomeAssistant,
    *,
    profile: str,
    hw_entity: str,
    plex_client: str,
    plex_accepts_ctrl: bool,
    machine_id: str,
    send_cmd_fn: Any,
    active_mtype: str,
    action: str,
    repeat: str | None,
    shuffle: bool | None,
    zone_name: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Handle next/previous track and repeat/shuffle parameters (never hitting TV KEY_CHUP)."""
    ok = False
    msg_kw: dict[str, Any] = {"zone": zone_name}
    if action in ("next", "previous"):
        ha_srv = "media_next_track" if action == "next" else "media_previous_track"
        pms_cmd = "skipNext" if action == "next" else "skipPrevious"
        if profile == PROFILE_APPLE_TV and hw_entity:
            ok = await _async_try_control_service(
                hass, "media_player", ha_srv, {"entity_id": hw_entity}
            )
        else:
            if plex_client and plex_accepts_ctrl:
                ok = await _async_try_control_service(
                    hass, "media_player", ha_srv, {"entity_id": plex_client}
                )
            if not ok and machine_id and callable(send_cmd_fn):
                ok = bool(await send_cmd_fn(machine_id, pms_cmd, media_type=active_mtype))
        return ok, ("track_next_ok" if action == "next" else "track_prev_ok"), msg_kw

    if action == "set_repeat":
        rep_mode = str(repeat or "all").strip().lower()
        if rep_mode not in ("off", "one", "all"):
            rep_mode = "all"
        plex_rep = {"off": "0", "one": "1", "all": "2"}[rep_mode]
        if profile == PROFILE_APPLE_TV and hw_entity:
            ok = await _async_try_control_service(
                hass, "media_player", "repeat_set", {"entity_id": hw_entity, "repeat": rep_mode}
            )
        else:
            if machine_id and callable(send_cmd_fn):
                ok = bool(
                    await send_cmd_fn(
                        machine_id, "setParameters", {"repeat": plex_rep}, media_type=active_mtype
                    )
                )
            if not ok and plex_client and plex_accepts_ctrl:
                ok = await _async_try_control_service(
                    hass, "media_player", "repeat_set", {"entity_id": plex_client, "repeat": rep_mode}
                )
        msg_kw["mode"] = rep_mode
        return ok, "repeat_ok", msg_kw

    shuf_on = True if shuffle is None else bool(shuffle)
    if profile == PROFILE_APPLE_TV and hw_entity:
        ok = await _async_try_control_service(
            hass, "media_player", "shuffle_set", {"entity_id": hw_entity, "shuffle": shuf_on}
        )
    else:
        if machine_id and callable(send_cmd_fn):
            ok = bool(
                await send_cmd_fn(
                    machine_id,
                    "setParameters",
                    {"shuffle": "1" if shuf_on else "0"},
                    media_type=active_mtype,
                )
            )
        if not ok and plex_client and plex_accepts_ctrl:
            ok = await _async_try_control_service(
                hass, "media_player", "shuffle_set", {"entity_id": plex_client, "shuffle": shuf_on}
            )
    msg_kw["mode"] = "on" if shuf_on else "off"
    return ok, "shuffle_ok", msg_kw


async def _async_control_transport_or_seek(
    hass: HomeAssistant,
    *,
    zone_data: Mapping[str, Any],
    zone_name: str,
    profile: str,
    hw_entity: str,
    plex_client: str,
    plex_accepts_ctrl: bool,
    plex_st: Any,
    machine_id: str,
    zone_sess: PlexActiveSession | None,
    active_rk: int | None,
    active_mtype: str,
    send_cmd_fn: Any,
    seek_fn: Any,
    action: str,
    offset_sec: int | None,
    delta_sec: int | None,
) -> tuple[bool, str, dict[str, Any]]:
    """Execute pause/resume/stop/seek_to/seek_relative/restart cleanly without double toggles."""
    msg_kw: dict[str, Any] = {"zone": zone_name}
    if action in ("pause", "resume_play", "play_pause", "stop"):
        ha_srv = {
            "pause": "media_pause",
            "resume_play": "media_play",
            "play_pause": "media_play_pause",
            "stop": "media_stop",
        }[action]
        pms_cmd = {
            "pause": "pause",
            "resume_play": "play",
            "play_pause": "pause" if (zone_sess and zone_sess.player_state == "playing") else "play",
            "stop": "stop",
        }[action]
        ok = False
        if profile == PROFILE_APPLE_TV and hw_entity:
            ok = await _async_try_control_service(hass, "media_player", ha_srv, {"entity_id": hw_entity})
        else:
            if plex_client and plex_accepts_ctrl:
                ok = await _async_try_control_service(hass, "media_player", ha_srv, {"entity_id": plex_client})
            if not ok and machine_id and callable(send_cmd_fn):
                ok = bool(await send_cmd_fn(machine_id, pms_cmd, media_type=active_mtype))
            if not ok and hw_entity:
                ok = await _async_try_control_service(hass, "media_player", ha_srv, {"entity_id": hw_entity})
        msg_key = {
            "pause": "paused_ok",
            "resume_play": "unpaused_ok",
            "play_pause": "paused_ok" if pms_cmd == "pause" else "unpaused_ok",
            "stop": "stopped_ok",
        }[action]
        return ok, msg_key, msg_kw

    if action == "restart":
        target_sec = 0
    elif action == "seek_to":
        target_sec = max(0, int(offset_sec or 0))
    else:
        ha_pos, _ = _read_ha_client_live_position_sec(hass, zone_data, active_rk)
        sess_pos = (zone_sess.view_offset_ms // 1000) if zone_sess is not None else 0
        cur_pos = max(ha_pos, sess_pos)
        if cur_pos == 0 and zone_sess is None and (plex_st is None or plex_st.state not in ACTIVE_PLAYBACK_STATES):
            return False, "no_active_playback", msg_kw
        target_sec = max(0, cur_pos + int(delta_sec or 0))

    ok = False
    if profile == PROFILE_APPLE_TV and hw_entity:
        ok = await _async_try_control_service(
            hass, "media_player", "media_seek", {"entity_id": hw_entity, "seek_position": int(target_sec)}
        )
    else:
        if machine_id and callable(seek_fn):
            ok = bool(await seek_fn(machine_id, int(target_sec) * 1000, active_mtype))
        if not ok and plex_client and plex_accepts_ctrl:
            ok = await _async_try_control_service(
                hass, "media_player", "media_seek", {"entity_id": plex_client, "seek_position": int(target_sec)}
            )
    if ok and active_rk is not None and target_sec >= RESUME_WIPE_GUARD_SEC:
        _record_confirmed_playback_position(hass, active_rk, target_sec, zone_name)
    msg_kw["position"] = _format_mmss(target_sec)
    return ok, ("restarted_ok" if action == "restart" else "seek_ok"), msg_kw


async def async_control_zone_playback(
    hass: HomeAssistant,
    entry: ConfigEntry,
    zone: ConfigSubentry,
    action: str,
    *,
    offset_sec: int | None = None,
    delta_sec: int | None = None,
    repeat: str | None = None,
    shuffle: bool | None = None,
) -> LaunchResult:
    """Hardware-aware transport, seek (±sec / timestamp / restart), repeat & shuffle control."""
    api = entry.runtime_data.api
    zone_data = zone.data
    zone_name = str(zone_data.get(CONF_ZONE_NAME) or zone.title)
    profile = str(zone_data.get(CONF_POWER_PROFILE) or PROFILE_STANDARD_MEDIA_PLAYER)
    plex_client = resolve_plex_client(hass, zone_data)
    _, hw_entity, _ = resolve_zone_hardware(hass, zone_data)

    for cid in get_plex_client_candidates(hass, zone_data) or ([plex_client] if plex_client else []):
        _cancel_pending_seek_task(hass, cid)

    sessions: list[PlexActiveSession] = []
    get_sessions = getattr(api, "async_get_active_sessions", None)
    if callable(get_sessions):
        try:
            sessions = list(await get_sessions() or ())
        except Exception:  # noqa: BLE001
            sessions = []

    zone_mids = extract_zone_machine_ids(hass, plex_client, zone_data)
    zone_sess: PlexActiveSession | None = None
    for sess in sessions:
        if zone_mids and sess.player_machine_id and sess.player_machine_id.lower() in zone_mids:
            zone_sess = sess
            break
    if zone_sess is None and len(sessions) == 1 and not zone_mids:
        zone_sess = sessions[0]

    active_rk = zone_sess.rating_key if zone_sess is not None else None
    active_mtype = zone_sess.media_type if zone_sess is not None else "video"
    active_title = zone_sess.display_title if zone_sess is not None else ""
    plex_st = hass.states.get(plex_client) if plex_client else None
    plex_accepts_ctrl = (
        plex_st is not None
        and plex_st.state not in OFFLINE_CLIENT_STATES
        and str(plex_st.attributes.get("player_source") or "").lower() != "session"
    )
    machine_id = await _async_resolve_client_machine_id(
        hass, plex_client, zone_data=zone_data, target_key=active_rk, api=api
    )
    send_cmd_fn = getattr(api, "async_send_client_playback_command", None)
    seek_fn = getattr(api, "async_seek_client_player", None)

    if action in ("next", "previous", "set_repeat", "set_shuffle"):
        ok, msg_key, msg_kw = await _async_control_queue_action(
            hass,
            profile=profile,
            hw_entity=hw_entity,
            plex_client=plex_client,
            plex_accepts_ctrl=plex_accepts_ctrl,
            machine_id=machine_id,
            send_cmd_fn=send_cmd_fn,
            active_mtype=active_mtype,
            action=action,
            repeat=repeat,
            shuffle=shuffle,
            zone_name=zone_name,
        )
    else:
        ok, msg_key, msg_kw = await _async_control_transport_or_seek(
            hass,
            zone_data=zone_data,
            zone_name=zone_name,
            profile=profile,
            hw_entity=hw_entity,
            plex_client=plex_client,
            plex_accepts_ctrl=plex_accepts_ctrl,
            plex_st=plex_st,
            machine_id=machine_id,
            zone_sess=zone_sess,
            active_rk=active_rk,
            active_mtype=active_mtype,
            send_cmd_fn=send_cmd_fn,
            seek_fn=seek_fn,
            action=action,
            offset_sec=offset_sec,
            delta_sec=delta_sec,
        )

    _LOGGER.info(
        "SmartPlex control: action=%s, zone='%s', ok=%s, client='%s'",
        action,
        zone_name,
        ok,
        plex_client or hw_entity,
    )
    if not ok and msg_key == "no_active_playback":
        final_msg = format_response(hass, entry.data, "no_active_playback", zone=zone_name)
    elif ok:
        final_msg = format_response(hass, entry.data, msg_key, **msg_kw)
    else:
        final_msg = format_response(
            hass, entry.data, "control_failed", zone=zone_name, error="Player unavailable"
        )
    return LaunchResult(
        success=ok,
        zone_name=zone_name,
        matched_title=active_title,
        rating_key=active_rk,
        media_type=active_mtype,
        boot_seconds=0.0,
        retries_used=0,
        handoff_from=None,
        start_phase="control",
        message=final_msg,
    )




