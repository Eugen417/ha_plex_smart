"""Multilingual AI command parser backed by Home Assistant ai_task.generate_data."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import CONF_AI_ENTITY, CONF_ALIASES, CONF_IS_DEFAULT, CONF_ZONE_NAME
from .models import PlexSmartRuntimeData
from .privacy import redact_sensitive_text
from .search import get_zone_subentries, parse_aliases, pre_detect_media_type

_LOGGER = logging.getLogger(__name__)

_QUERY_SCHEMAS: dict[str | None, str] = {
    "music": (
        '{"artist": null, "album": null, "track": null, "title": null, '
        '"genre": null, "mood": null, "year": null, "decade": null}'
    ),
    "show": (
        '{"show_name": null, "original_title": null, "season": null, '
        '"episode": null, "similar_to": null, "actors": [], "actors_original": [], '
        '"candidate_titles": [], "genre": null, "year": null, "library": null}'
    ),
    "movie": (
        '{"title": null, "original_title": null, "similar_to": null, "actor": null, '
        '"actors": [], "actors_original": [], "candidate_titles": [], '
        '"director": null, "genre": null, "year": null, "decade": null, '
        '"library": null, "unwatched": false}'
    ),
    "playlist": '{"title": null}',
    None: (
        '{"title": null, "original_title": null, "show_name": null, '
        '"artist": null, "album": null, "track": null, "similar_to": null, '
        '"season": null, "episode": null, '
        '"actor": null, "actors": [], "actors_original": [], '
        '"candidate_titles": [], "director": null, '
        '"genre": null, "year": null, "decade": null, "studio": null, '
        '"collection": null, "country": null, "content_rating": null, '
        '"mood": null, "library": null, "unwatched": false}'
    ),
}


def build_ai_parser_prompt(
    hass: HomeAssistant,
    entry: ConfigEntry,
    command_text: str,
    detected_type: str | None = None,
) -> str:
    """Build compact, language-aware instructions scoped to the detected media type."""
    runtime: PlexSmartRuntimeData = entry.runtime_data
    index = runtime.coordinator.data
    user_lang = hass.config.language or "en"
    current_year = dt_util.now().year
    scoped_type = detected_type or pre_detect_media_type(command_text)

    zones_desc: list[str] = []
    for sub in get_zone_subentries(entry):
        z_name = str(sub.data.get(CONF_ZONE_NAME) or sub.title)
        aliases = parse_aliases(sub.data.get(CONF_ALIASES))
        alias_str = f" ({', '.join(aliases)})" if aliases else ""
        def_tag = " [DEFAULT]" if sub.data.get(CONF_IS_DEFAULT) else ""
        zones_desc.append(f"- '{z_name}'{alias_str}{def_tag}")
    zones_block = "\n".join(zones_desc) if zones_desc else "- (default)"

    sections = index.sections if index else []
    if scoped_type in ("movie", "show", "music"):
        scoped_sections = [s for s in sections if s.lib_type == scoped_type]
        if not scoped_sections:
            scoped_sections = sections
    else:
        scoped_sections = sections

    libraries_block = (
        ", ".join(f"{s.title} [{s.lib_type}]" for s in scoped_sections)
        if scoped_sections
        else "Movies, TV Shows, Music"
    )

    genres_line = ""
    if scoped_type != "playlist":
        if scoped_type in ("movie", "show", "music") and scoped_sections:
            seen_g: set[str] = set()
            scoped_genres: list[str] = []
            for sec in scoped_sections:
                for g in sec.genres:
                    if g not in seen_g:
                        seen_g.add(g)
                        scoped_genres.append(g)
            genres_list = scoped_genres[:35]
        else:
            genres_list = index.all_genres[:45] if index and index.all_genres else []
        if genres_list:
            genres_line = f"SERVER GENRES (prefer exact match): {', '.join(genres_list)}\n"

    type_line = (
        f"LOCKED MEDIA TYPE: '{scoped_type}' (set control.type='{scoped_type}').\n"
        if scoped_type
        else "MEDIA TYPES (control.type): 'movie', 'show', 'music', 'playlist'.\n"
    )
    query_schema = _QUERY_SCHEMAS.get(scoped_type, _QUERY_SCHEMAS[None])

    return (
        "You are SmartPlex, a multilingual Plex command parser. Return ONLY valid JSON.\n"
        f"Year: {current_year}. Language: '{user_lang}'.\n"
        "Strip quality tags (4K, UHD, HDR, IMAX, remux). "
        "Keep the exact movie/show title from the user command in 'title'. "
        "NEVER guess a different movie from the same franchise in 'original_title'; "
        "leave 'original_title' null unless you are 100% certain it is the exact same movie. "
        "Normalize genres/names to nominative singular.\n"
        f"ZONES:\n{zones_block}\n"
        f"LIBRARIES: {libraries_block}\n"
        f"{genres_line}"
        f"{type_line}"
        "INTENTS (control.intent): 'play' (ANY request naming a specific movie/show/music title, including 'продолжи фильм X' -> intent='play', resume_mode='resume') "
        "or 'handoff' (ONLY when NO specific movie/show title is named, e.g. 'перенеси/перенос фильма из зала в спальню' -> intent='handoff', source_zone='Зал', zone='Спальня', or 'продолжи просмотр').\n"
        "ACTORS & SIMILAR: For 1+ actors together put nominative names in query.actors, English names in query.actors_original, and up to 5 real collaboration titles in query.candidate_titles. For 'похожее на X' put X in query.similar_to. For songs put song name in query.track.\n"
        "SORT (control.sort_order): default|newest|oldest|top_rated|random. "
        "resume_mode: start|resume.\n"
        "JSON:\n"
        "{\n"
        '  "control": {"intent": "play|handoff", "zone": null, "source_zone": null, '
        f'"type": "{scoped_type or "movie|show|music|playlist"}", '
        '"resume_mode": "start|resume", "sort_order": "default", "shuffle": false, '
        '"turn_off_previous_tv": true},\n'
        f'  "query": {query_schema}\n'
        "}\n"
        f"USER COMMAND: {command_text}"
    )


def parse_ai_json_response(raw_data: Any) -> dict[str, Any]:
    """Extract JSON dict from ai_task.generate_data response payload."""
    if isinstance(raw_data, dict):
        return raw_data
    text = str(raw_data or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("AI response is not a JSON object")
    return parsed


async def async_parse_command_via_ai_task(
    hass: HomeAssistant,
    entry: ConfigEntry,
    command_text: str,
    ai_entity_override: str | None = None,
    detected_type: str | None = None,
) -> dict[str, Any]:
    """Call Home Assistant ai_task.generate_data and return parsed command dict."""
    ai_entity = (ai_entity_override or entry.data.get(CONF_AI_ENTITY) or "").strip()
    if not ai_entity:
        raise HomeAssistantError(
            "No AI Task entity configured for SmartPlex. "
            "Set an ai_task.* entity in SmartPlex configuration or pass ai_entity in the service call."
        )

    scoped_type = detected_type or pre_detect_media_type(command_text)
    prompt = build_ai_parser_prompt(
        hass, entry, command_text, detected_type=scoped_type
    )
    try:
        response = await hass.services.async_call(
            "ai_task",
            "generate_data",
            {
                "entity_id": ai_entity,
                "task_name": "SmartPlex",
                "instructions": prompt,
            },
            blocking=True,
            return_response=True,
        )
    except Exception as err:
        raise HomeAssistantError(
            f"AI Task call failed ({ai_entity}): {redact_sensitive_text(err)}"
        ) from err

    raw_data = response.get("data") if isinstance(response, dict) else response
    try:
        parsed = parse_ai_json_response(raw_data)
    except (ValueError, json.JSONDecodeError) as err:
        _LOGGER.error("Failed to parse SmartPlex AI JSON: %s", redact_sensitive_text(err))
        raise HomeAssistantError(f"Invalid JSON from AI Task: {err}") from err

    if scoped_type and isinstance(parsed.get("control"), dict):
        parsed["control"].setdefault("type", scoped_type)
        if not parsed["control"].get("type"):
            parsed["control"]["type"] = scoped_type
    return parsed

