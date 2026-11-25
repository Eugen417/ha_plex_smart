"""Native Home Assistant LLM API (Assist tools) for SmartPlex."""

from __future__ import annotations

import logging
import re

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.util.json import JsonObjectType


def _wrap_tool_result(data: JsonObjectType, error: bool = False) -> Any:
    """Return llm.ToolResult on HA 2026.11+, falling back to raw dict on older HA."""
    tool_result_cls = getattr(llm, "ToolResult", None)
    if tool_result_cls is not None:
        return tool_result_cls(data=data, error=error)
    return data


def _make_annotations(
    *,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> Any:
    """Build native HA 2026.11+ llm.ToolAnnotations when available."""
    ann_cls = getattr(llm, "ToolAnnotations", None)
    if ann_cls is not None:
        return ann_cls(
            read_only=read_only,
            destructive=destructive,
            idempotent=idempotent,
            open_world=open_world,
        )
    return None

from .const import (
    CONF_ALIASES,
    CONF_IS_DEFAULT,
    CONF_USER_TITLE,
    CONF_ZONE_NAME,
    CONTROL_ACTIONS,
    DOMAIN,
    LLM_API_ID,
    LLM_API_NAME,
    REPEAT_MODES,
)
from .hardware import (
    async_control_zone_playback,
    async_handoff_or_resume,
    async_power_off_zone,
    async_resolve_active_playback_zone,
)
from .models import PlexSmartRuntimeData
from .payload_templates import LLM_MEDIA_TEMPLATES_GUIDE
from .search import (
    extract_playback_control_intent,
    extract_pure_handoff_intent,
    extract_source_zone_from_transfer_text,
    get_zone_subentries,
    normalize_title,
    parse_aliases,
    resolve_zone,
)

_LOGGER = logging.getLogger(__name__)


def _get_primary_entry(hass: HomeAssistant) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "runtime_data", None) is not None:
            return entry
    return None


def _area_id_from_context(
    hass: HomeAssistant, llm_context: llm.LLMContext
) -> str | None:
    device_id = getattr(llm_context, "device_id", None)
    if not device_id:
        return None
    dev = dr.async_get(hass).async_get(device_id)
    return dev.area_id if dev else None


_CHATLOG_START_RE = re.compile(
    r"\b(с\s+начала|сначала|заново|с\s+первой\s+минут\w*|from\s+the\s+beginning|from\s+start|start\s+over)\b",
    re.IGNORECASE | re.UNICODE,
)
_CHATLOG_RESUME_RE = re.compile(
    r"\b(продолж\w*|досмотр\w*|возобнов\w*|с\s+места\s+останов\w*|с\s+того\s+места|дальше\s+смотр\w*|resume|continue\b|keep\s+watching)",
    re.IGNORECASE | re.UNICODE,
)


_GENERIC_MEDIA_TITLES = frozenset(
    {
        "фильм", "фильма", "фильмы", "кино", "сериал", "сериала", "сериалы",
        "серия", "серию", "эпизод", "мультфильм", "мультик", "видео", "просмотр",
        "музыка", "музыку", "movie", "movies", "film", "show", "series", "video",
        "playback", "music",
    }
)


def _get_last_chat_log_utterance() -> str | None:
    """Return the latest raw user message from HA's active conversation ChatLog."""
    try:
        from homeassistant.components.conversation.chat_log import current_chat_log
    except ImportError:
        return None

    chat_log = current_chat_log.get()
    if chat_log is None:
        return None

    for msg in reversed(getattr(chat_log, "content", None) or []):
        if getattr(msg, "role", None) == "user":
            raw_text = str(getattr(msg, "content", "") or "").strip()
            return raw_text or None
    return None


def _detect_resume_override_from_chat_log() -> bool | None:
    """Inspect active HA ChatLog user utterance to guarantee RESUME vs START regardless of LLM."""
    raw_text = _get_last_chat_log_utterance()
    if not raw_text:
        return None
    norm = raw_text.lower().replace("ё", "е")
    if _CHATLOG_START_RE.search(norm):
        return False
    if _CHATLOG_RESUME_RE.search(norm):
        return True
    return None


class SmartPlexPlayTool(llm.Tool):
    """Tool to search and play movies, TV shows, music, or playlists on a Plex zone."""

    name = "plex_smart_play"
    integration = DOMAIN
    annotations = _make_annotations(read_only=False, destructive=False, open_world=False)
    description = (
        "Play OR resume a named movie, TV show/season/episode, music (artist/album/track/genre), or playlist on Plex. "
        "CRITICAL: Call this tool ONLY ONCE per user request and NEVER retry it. "
        "When the user says 'продолжи' / 'досмотри' / 'resume' / 'continue' WITH a specific movie/show/album title, "
        "ALWAYS call THIS tool with resume=true (NEVER call plex_smart_transfer_or_resume when a title is named!). "
        "For TV shows set media_type='show' and pass 'title', plus optional 'season' and 'episode'. "
        "For music songs pass 'track' (and optional 'artist'/'album'). "
        "For 2+ actors TOGETHER pass 'actors', 'actors_original', and 'candidate_titles'. "
        "For 'something like X' pass 'similar_to'. Leave 'zone' empty to use the room's default player."
    )
    parameters = vol.Schema(
        {
            vol.Optional("media_type", default="movie"): vol.In(
                ["movie", "show", "music", "playlist"]
            ),
            vol.Optional("title"): str,
            vol.Optional("original_title"): str,
            vol.Optional("artist"): str,
            vol.Optional("album"): str,
            vol.Optional("track"): str,
            vol.Optional("similar_to"): str,
            vol.Optional("season"): vol.Coerce(int),
            vol.Optional("episode"): vol.Coerce(int),
            vol.Optional("actor"): str,
            vol.Optional("actor_original"): str,
            vol.Optional("actors"): [str],
            vol.Optional("actors_original"): [str],
            vol.Optional("candidate_titles"): [str],
            vol.Optional("director"): str,
            vol.Optional("genre"): str,
            vol.Optional("year"): vol.Coerce(int),
            vol.Optional("decade"): vol.Coerce(int),
            vol.Optional("sort_order", default="default"): vol.In(
                ["default", "newest", "oldest", "top_rated", "random"]
            ),
            vol.Optional("playback_mode", default="start"): vol.In(
                ["start", "resume"]
            ),
            vol.Optional("resume", default=False): bool,
            vol.Optional("shuffle", default=False): bool,
            vol.Optional("offset_sec"): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
            vol.Optional("zone"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> Any:
        from .services import (
            _cancel_zone_active_task,
            async_execute_play_request,
            record_last_launch,
        )

        entry = _get_primary_entry(hass)
        if entry is None:
            return _wrap_tool_result(
                {"success": False, "error": "SmartPlex is not configured."}, error=True
            )

        args = dict(tool_input.tool_args)
        area_id = _area_id_from_context(hass, llm_context)
        raw_utterance = _get_last_chat_log_utterance()

        # Guard 0: if user actually asked for transport/seek/repeat/shuffle control, route to control!
        ctrl_intent = extract_playback_control_intent(entry, hass, raw_utterance)
        if ctrl_intent is not None:
            c_zone = await async_resolve_active_playback_zone(
                entry,
                hass,
                ctrl_intent.get("zone") or args.get("zone"),
                area_id=area_id,
            )
            if c_zone is None:
                return _wrap_tool_result(
                    {"success": False, "error": "No media zones configured in SmartPlex."},
                    error=True,
                )
            c_res = await async_control_zone_playback(
                hass,
                entry,
                c_zone,
                ctrl_intent["action"],
                offset_sec=ctrl_intent.get("offset_sec"),
                delta_sec=ctrl_intent.get("delta_sec"),
                repeat=ctrl_intent.get("repeat"),
                shuffle=ctrl_intent.get("shuffle"),
            )
            return _wrap_tool_result(
                {
                    "action_complete": True,
                    "success": c_res.success,
                    "zone": c_res.zone_name,
                    "message": c_res.message,
                    "instruction": "Done. Confirm briefly without calling more tools.",
                },
                error=not c_res.success,
            )

        # Guard against LLM glitch: if the user actually asked for a pure room-to-room transfer
        # ("перенеси/перенос фильма из зала в спальню") with NO movie title, route to Handoff!
        pure_handoff = extract_pure_handoff_intent(entry, hass, raw_utterance)
        if pure_handoff is not None:
            raw_tgt = pure_handoff.get("target_zone") or args.get("zone")
            if raw_tgt or pure_handoff.get("is_transfer"):
                target = resolve_zone(entry, hass, raw_tgt, area_id=area_id)
            else:
                target = await async_resolve_active_playback_zone(
                    entry, hass, None, area_id=area_id
                )
            if target is None:
                return _wrap_tool_result(
                    {"success": False, "error": "No media zones configured in SmartPlex."},
                    error=True,
                )
            await _cancel_zone_active_task(entry.runtime_data, target.subentry_id)
            source = (
                resolve_zone(entry, hass, pure_handoff.get("source_zone"))
                if pure_handoff.get("source_zone")
                else None
            )
            h_res = await async_handoff_or_resume(
                hass,
                entry,
                target_zone=target,
                source_zone=source,
                turn_off_source=True if pure_handoff.get("is_transfer") else None,
                media_type=pure_handoff.get("media_type") or args.get("media_type"),
            )
            record_last_launch(hass, entry, h_res)
            return _wrap_tool_result(
                {
                    "action_complete": True,
                    "success": h_res.success,
                    "zone": h_res.zone_name,
                    "handoff_from": h_res.handoff_from,
                    "matched_title": h_res.matched_title,
                    "rating_key": h_res.rating_key,
                    "boot_seconds": h_res.boot_seconds,
                    "message": h_res.message,
                    "instruction": (
                        "Done. Do NOT call any more tools; confirm handoff briefly."
                    ),
                },
                error=not h_res.success,
            )

        chat_resume = _detect_resume_override_from_chat_log()
        if chat_resume is not None:
            args["resume"] = chat_resume
            args["playback_mode"] = "resume" if chat_resume else "start"

        transfer_src_name = extract_source_zone_from_transfer_text(
            entry, hass, raw_utterance
        )
        if transfer_src_name:
            # Transfer from another room: route via async_handoff_or_resume so live offset is
            # captured BEFORE powering off the source TV (never kill source TV before reading session!).
            src_sub = resolve_zone(entry, hass, transfer_src_name)
            tgt_sub = resolve_zone(entry, hass, args.get("zone"), area_id=area_id)
            if (
                src_sub is not None
                and tgt_sub is not None
                and src_sub.subentry_id != tgt_sub.subentry_id
            ):
                await _cancel_zone_active_task(entry.runtime_data, tgt_sub.subentry_id)
                h_res = await async_handoff_or_resume(
                    hass,
                    entry,
                    target_zone=tgt_sub,
                    source_zone=src_sub,
                    turn_off_source=True,
                    media_type=args.get("media_type"),
                )
                record_last_launch(hass, entry, h_res)
                return _wrap_tool_result(
                    {
                        "action_complete": True,
                        "success": h_res.success,
                        "zone": h_res.zone_name,
                        "handoff_from": h_res.handoff_from,
                        "matched_title": h_res.matched_title,
                        "rating_key": h_res.rating_key,
                        "boot_seconds": h_res.boot_seconds,
                        "message": h_res.message,
                        "instruction": "Done. Do NOT call any more tools; confirm handoff briefly.",
                    },
                    error=not h_res.success,
                )

        result = await async_execute_play_request(
            hass, entry, args, area_id=area_id
        )
        return _wrap_tool_result(
            {
                "action_complete": True,
                "success": result.success,
                "zone": result.zone_name,
                "matched_title": result.matched_title,
                "rating_key": result.rating_key,
                "boot_seconds": result.boot_seconds,
                "message": result.message,
                "suggestions": result.suggestions,
                "instruction": (
                    "Done. Do NOT call any more tools; confirm playback briefly without repeating sensitive/provocative title words."
                ),
            },
            error=False,
        )


class SmartPlexHandoffTool(llm.Tool):
    """Tool to move current playback to another TV/room or resume Continue Watching."""

    name = "plex_smart_transfer_or_resume"
    integration = DOMAIN
    annotations = _make_annotations(read_only=False, destructive=False, open_world=False)
    description = (
        "ONLY for transferring active playback between rooms OR continuing the generic last item "
        "when NO movie/show title is mentioned (e.g. 'перенеси кино в спальню', 'продолжи просмотр'). "
        "NEVER use this tool if the user names a specific movie or show — use plex_smart_play(resume=true) instead!"
    )
    parameters = vol.Schema(
        {
            vol.Optional("target_zone"): str,
            vol.Optional("source_zone"): str,
            vol.Optional("turn_off_previous_tv", default=True): bool,
            vol.Optional("media_type"): vol.In(["movie", "show", "music"]),
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> Any:
        from .services import (
            _cancel_zone_active_task,
            async_execute_play_request,
            record_last_launch,
        )

        entry = _get_primary_entry(hass)
        if entry is None:
            return _wrap_tool_result(
                {"success": False, "error": "SmartPlex is not configured."}, error=True
            )

        args = dict(tool_input.tool_args)
        area_id = _area_id_from_context(hass, llm_context)
        raw_utterance = _get_last_chat_log_utterance()
        pure_handoff = extract_pure_handoff_intent(entry, hass, raw_utterance)

        # Safety net if an LLM still injected an extra 'title' key despite schema:
        # ignore generic media words ("фильм", "кино") and ChatLog pure-handoff commands!
        named_title = str(args.get("title") or "").strip()
        if normalize_title(named_title) in _GENERIC_MEDIA_TITLES or pure_handoff is not None:
            named_title = ""

        eff_target_name = args.get("target_zone") or (
            pure_handoff.get("target_zone") if pure_handoff else None
        )
        eff_source_name = args.get("source_zone") or (
            pure_handoff.get("source_zone")
            if pure_handoff
            else extract_source_zone_from_transfer_text(entry, hass, raw_utterance)
        )
        eff_media_type = args.get("media_type") or (
            pure_handoff.get("media_type") if pure_handoff else None
        )

        if named_title:
            if eff_source_name and args.get("turn_off_previous_tv", True):
                src_sub = resolve_zone(entry, hass, eff_source_name)
                tgt_sub = resolve_zone(entry, hass, eff_target_name, area_id=area_id)
                if src_sub is not None and (
                    tgt_sub is None or src_sub.subentry_id != tgt_sub.subentry_id
                ):
                    await async_power_off_zone(hass, src_sub.data)
            redirected = await async_execute_play_request(
                hass,
                entry,
                {
                    "title": named_title,
                    "media_type": eff_media_type or "movie",
                    "zone": eff_target_name,
                    "resume": True,
                },
                area_id=area_id,
            )
            return _wrap_tool_result(
                {
                    "action_complete": True,
                    "success": redirected.success,
                    "zone": redirected.zone_name,
                    "matched_title": redirected.matched_title,
                    "rating_key": redirected.rating_key,
                    "boot_seconds": redirected.boot_seconds,
                    "message": redirected.message,
                },
                error=False,
            )

        if eff_target_name or (pure_handoff and pure_handoff.get("is_transfer")):
            target = resolve_zone(entry, hass, eff_target_name, area_id=area_id)
        else:
            target = await async_resolve_active_playback_zone(
                entry, hass, None, area_id=area_id
            )
        if target is None:
            return _wrap_tool_result(
                {"success": False, "error": "No media zones configured in SmartPlex."},
                error=True,
            )
        await _cancel_zone_active_task(entry.runtime_data, target.subentry_id)

        source = (
            resolve_zone(entry, hass, eff_source_name)
            if eff_source_name
            else None
        )
        result = await async_handoff_or_resume(
            hass,
            entry,
            target_zone=target,
            source_zone=source,
            turn_off_source=args.get("turn_off_previous_tv"),
            media_type=eff_media_type,
        )
        record_last_launch(hass, entry, result)
        return _wrap_tool_result(
            {
                "success": result.success,
                "zone": result.zone_name,
                "handoff_from": result.handoff_from,
                "matched_title": result.matched_title,
                "boot_seconds": result.boot_seconds,
                "message": result.message,
            },
            error=not result.success,
        )


class SmartPlexControlTool(llm.Tool):
    """Tool for pause, unpause, stop, seek (±sec / timestamp / restart), next/prev, repeat, shuffle."""

    name = "plex_smart_control"
    integration = DOMAIN
    annotations = _make_annotations(read_only=False, destructive=False, open_world=False)
    description = (
        "Control active Plex playback: pause ('поставь на паузу'), unpause ('сними с паузы' / 'продолжи' when paused), "
        "stop, restart from 0:00 ('в начало' / 'с начала'), seek forward/backward by seconds ('перемотай вперёд/назад на 10 секунд' -> "
        "action='seek_relative', delta_sec=10 or -10), seek to exact timestamp ('перемотай на 30 минуту' -> action='seek_to', offset_sec=1800), "
        "next/previous track/episode, repeat ('off'/'one'/'all'), or shuffle (true/false). Leave 'zone' empty to control the currently active TV."
    )
    parameters = vol.Schema(
        {
            vol.Required("action"): vol.In(list(CONTROL_ACTIONS)),
            vol.Optional("zone"): str,
            vol.Optional("offset_sec"): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
            vol.Optional("delta_sec"): vol.All(vol.Coerce(int), vol.Range(min=-3600, max=3600)),
            vol.Optional("repeat"): vol.In(list(REPEAT_MODES)),
            vol.Optional("shuffle"): bool,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> Any:
        entry = _get_primary_entry(hass)
        if entry is None:
            return _wrap_tool_result(
                {"success": False, "error": "SmartPlex is not configured."}, error=True
            )
        args = dict(tool_input.tool_args)
        area_id = _area_id_from_context(hass, llm_context)
        zone = await async_resolve_active_playback_zone(
            entry, hass, args.get("zone"), area_id=area_id
        )
        if zone is None:
            return _wrap_tool_result(
                {"success": False, "error": "No media zones configured in SmartPlex."},
                error=True,
            )
        res = await async_control_zone_playback(
            hass,
            entry,
            zone,
            args["action"],
            offset_sec=args.get("offset_sec"),
            delta_sec=args.get("delta_sec"),
            repeat=args.get("repeat"),
            shuffle=args.get("shuffle"),
        )
        return _wrap_tool_result(
            {
                "action_complete": True,
                "success": res.success,
                "zone": res.zone_name,
                "message": res.message,
                "instruction": "Done. Confirm briefly without calling more tools.",
            },
            error=not res.success,
        )


class SmartPlexSearchTool(llm.Tool):
    """Read-only search in the cached Plex library without waking any TV."""

    name = "plex_smart_search"
    integration = DOMAIN
    annotations = _make_annotations(
        read_only=True, destructive=False, idempotent=True, open_world=False
    )
    description = (
        "Search the user's Plex library (movies, shows, music, or Continue Watching) "
        "without turning on any TV. Supports multi-actor intersection via 'actors' / 'actors_original' "
        "and plot/collaboration lookup via 'candidate_titles'."
    )
    parameters = vol.Schema(
        {
            vol.Optional("query"): str,
            vol.Optional("media_type", default="movie"): vol.In(
                ["movie", "show", "music", "continue_watching"]
            ),
            vol.Optional("actors"): [str],
            vol.Optional("actors_original"): [str],
            vol.Optional("director"): str,
            vol.Optional("candidate_titles"): [str],
            vol.Optional("similar_to"): str,
            vol.Optional("genre"): str,
            vol.Optional("year"): vol.Coerce(int),
            vol.Optional("unwatched_only", default=False): bool,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> Any:
        from .search import (
            build_actor_groups,
            find_best_media,
            normalize_title,
            split_people_list,
        )
        from .services import (
            _parse_candidate_titles,
            async_match_people_and_candidates,
            async_resolve_similar_items,
        )

        entry = _get_primary_entry(hass)
        if entry is None:
            return _wrap_tool_result(
                {"results": [], "error": "SmartPlex is not configured."}, error=True
            )

        runtime: PlexSmartRuntimeData = entry.runtime_data
        args = dict(tool_input.tool_args)
        m_type = str(args.get("media_type") or "movie")

        if m_type == "continue_watching":
            items = await runtime.api.async_get_continue_watching()
            return _wrap_tool_result(
                {
                    "results": [
                        {
                            "title": it.title,
                            "show": it.grandparent_title,
                            "season": it.season_index,
                            "episode": it.episode_index,
                            "year": it.year,
                            "rating": it.rating,
                        }
                        for it in items[:8]
                    ]
                }
            )

        index = runtime.coordinator.data
        pool = list(index.items_by_type.get(m_type, [])) if index else []
        actor_groups = build_actor_groups(args)
        director_variants = split_people_list(args.get("director"))
        candidate_titles = _parse_candidate_titles(args)
        similar_to = str(args.get("similar_to") or "").strip()

        if similar_to and m_type in ("movie", "show"):
            pool = await async_resolve_similar_items(runtime, pool, similar_to)
            boost_keys = set()
        elif (actor_groups or director_variants or candidate_titles) and m_type in (
            "movie",
            "show",
        ):
            pool, boost_keys = await async_match_people_and_candidates(
                runtime,
                pool,
                media_type=m_type,
                actor_groups=actor_groups,
                director_variants=director_variants or None,
                candidate_titles=candidate_titles,
            )
        else:
            boost_keys = set()

        if args.get("unwatched_only"):
            pool = [it for it in pool if it.view_count == 0]
        if args.get("year"):
            y = int(args["year"])
            pool = [it for it in pool if it.year == y]
        if args.get("genre"):
            g_norm = normalize_title(args["genre"])
            pool = [
                it
                for it in pool
                if any(g_norm in normalize_title(g) for g in it.genres)
            ]

        query = str(args.get("query") or "").strip()
        if query:
            best, sugg = find_best_media(pool, [query], year=args.get("year"))
            matched = ([best] if best else []) + [
                s for s in sugg if not best or s.rating_key != best.rating_key
            ]
        else:
            pool.sort(
                key=lambda it: (
                    1 if it.rating_key in boost_keys else 0,
                    it.rating,
                    it.year or 0,
                ),
                reverse=True,
            )
            matched = pool[:8]

        return _wrap_tool_result(
            {
                "total_matching": len(pool),
                "results": [
                    {
                        "title": it.title,
                        "original_title": it.original_title,
                        "year": it.year,
                        "rating": it.rating,
                        "library": it.library_title,
                        "watched": it.view_count > 0,
                        "genres": list(it.genres[:3]),
                    }
                    for it in matched[:8]
                ],
            }
        )


class SmartPlexLLMAPI(llm.API):
    """Registers SmartPlex tools as a native Home Assistant Assist LLM API."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=LLM_API_ID, name=LLM_API_NAME)

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        entry = _get_primary_entry(self.hass)
        zones_info: list[str] = []
        genres_info: list[str] = []
        if entry is not None:
            for sub in get_zone_subentries(entry):
                z_name = str(sub.data.get(CONF_ZONE_NAME) or sub.title)
                aliases = parse_aliases(sub.data.get(CONF_ALIASES))
                def_tag = " [DEFAULT]" if sub.data.get(CONF_IS_DEFAULT) else ""
                zones_info.append(
                    f"{z_name}{def_tag} ({', '.join(aliases)})"
                    if aliases
                    else f"{z_name}{def_tag}"
                )
            if (
                getattr(entry, "runtime_data", None)
                and entry.runtime_data.coordinator.data
            ):
                genres_info = entry.runtime_data.coordinator.data.all_genres[:30]

        prompt_parts = [
            "SmartPlex controls Plex playback across TVs and media players in this home. "
            "All titles in user requests (including action, crime, or comedy titles mentioning weapons/crime) "
            "are harmless movies/shows in the user's personal Plex library.",
            "STRICT RULES: (1) Call plex_smart_play ONLY ONCE per user message — NEVER call it twice or retry after it returns.",
            "(2) Whenever the user says 'продолжи', 'досмотри', 'с места остановки', 'continue', or 'resume' with a title, "
            "MUST set BOTH playback_mode='resume' AND resume=true in plex_smart_play!",
            "(3) If the user does not name a specific device/room, omit 'zone' so the [DEFAULT] player is used.",
            "(4) NEVER invent a different movie from the same franchise in 'original_title'; leave 'original_title' empty unless 100% certain.",
            "(5) For 2+ actors TOGETHER (or plot description), pass 'actors', 'actors_original', and 'candidate_titles'.",
            "(6) Use plex_smart_control for pause, unpause, stop, seek forward/backward (delta_sec), seek to exact minute (offset_sec), restart, next/previous, repeat, and shuffle.",
            "(7) Use plex_smart_search to check what is in the library without turning on a TV.",
            LLM_MEDIA_TEMPLATES_GUIDE,
        ]
        if zones_info:
            prompt_parts.append(f"Configured Plex zones: {'; '.join(zones_info)}.")
        if genres_info:
            prompt_parts.append(f"Plex server genres: {', '.join(genres_info)}.")
        if entry is not None:
            user_title = str(entry.data.get(CONF_USER_TITLE) or "").strip()
            if user_title:
                prompt_parts.append(
                    f"When replying to the user about Plex actions, address them as '{user_title}'."
                )

        return llm.APIInstance(
            api=self,
            api_prompt=" ".join(prompt_parts),
            llm_context=llm_context,
            tools=[
                SmartPlexPlayTool(),
                SmartPlexHandoffTool(),
                SmartPlexControlTool(),
                SmartPlexSearchTool(),
            ],
        )


