"""SmartPlex Home Assistant services and unified playback execution engine."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import random
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .ai_parser import async_parse_command_via_ai_task
from .const import (
    CONF_OPTIMISTIC_MODE,
    CONF_POWER_PROFILE,
    CONF_ZONE_NAME,
    CONTROL_ACTIONS,
    DEFAULT_OPTIMISTIC_MODE,
    DOMAIN,
    DUPLICATE_LAUNCH_COOLDOWN_SEC,
    PROFILE_APPLE_TV,
    REPEAT_MODES,
    RESUME_WIPE_GUARD_SEC,
    SERVICE_COMMAND,
    SERVICE_CONTROL,
    SERVICE_HANDOFF,
    SERVICE_PLAY,
    SERVICE_REFRESH_CACHE,
    SERVICE_SPIN_ROULETTE,
    SIGNAL_LAST_LAUNCH_UPDATED,
    START_PHASE_HOT,
)
from .hardware import (
    async_control_zone_playback,
    async_ensure_zone_ready,
    async_handoff_or_resume,
    async_play_on_zone_with_retry,
    async_resolve_active_playback_zone,
    detect_zone_start_phase,
    resolve_protected_offset_sec,
)
from .messages import format_response
from .models import LaunchResult, PlexSmartRuntimeData
from .payload_templates import (
    build_episode_template,
    build_movie_template,
    build_music_filter_template,
    build_music_item_template,
    build_playlist_template,
    build_show_template,
)
from .privacy import redact_sensitive_text
from .roulette import async_spin_roulette
from .search import (
    build_actor_groups,
    extract_playback_control_intent,
    extract_pure_handoff_intent,
    filter_items_by_people_local,
    find_best_media,
    find_best_playlist,
    normalize_title,
    pre_detect_media_type,
    pre_resolve_zone_from_text,
    resolve_genre_tag,
    resolve_zone,
    split_people_list,
    try_direct_local_resolve,
)

_LOGGER = logging.getLogger(__name__)


def record_last_launch(
    hass: HomeAssistant, entry: ConfigEntry, result: LaunchResult
) -> None:
    """Persist last launch telemetry and notify diagnostic sensor."""
    runtime: PlexSmartRuntimeData = entry.runtime_data
    runtime.last_launch = result
    async_dispatcher_send(hass, f"{SIGNAL_LAST_LAUNCH_UPDATED}_{entry.entry_id}")


def _get_entry(hass: HomeAssistant, entry_id: str | None = None) -> ConfigEntry:
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if getattr(e, "runtime_data", None) is not None
    ]
    if not entries:
        raise HomeAssistantError("SmartPlex integration is not loaded.")
    if entry_id:
        for e in entries:
            if e.entry_id == entry_id:
                return e
        raise HomeAssistantError(f"SmartPlex entry '{entry_id}' not found.")
    return entries[0]


async def _resolve_person_tag(
    runtime: PlexSmartRuntimeData,
    raw_name: str | None,
    orig_name: str | None = None,
) -> str | None:
    """Try server-side /hubs/search for bilingual actor/director names."""
    candidates = [c.strip() for c in (raw_name, orig_name) if c and str(c).strip()]
    if not candidates:
        return None
    for cand in candidates:
        hits = await runtime.api.async_hub_search(cand, limit=5)
        for hit in hits:
            if hit.get("hub_type") in ("actor", "director", "person") and hit.get("tag"):
                return str(hit["tag"])
    return candidates[0]


def _parse_candidate_titles(params: Mapping[str, Any]) -> list[str]:
    """Extract optional list of candidate real-world titles suggested by LLM."""
    raw = params.get("candidate_titles")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x or "").strip()]
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def _pick_from_filtered_pool(
    pool: list[Any],
    *,
    sort_order: str,
    shuffle: bool,
    boost_keys: set[int] | None = None,
) -> Any:
    """Choose best or random item from a matched pool."""
    if shuffle or sort_order == "random":
        return random.choice(pool)
    ordered = list(pool)
    if sort_order == "newest":
        ordered.sort(key=lambda it: (it.year or 0, it.rating), reverse=True)
    elif sort_order == "oldest":
        ordered.sort(key=lambda it: (it.year or 9999, -it.rating))
    elif sort_order == "top_rated":
        ordered.sort(key=lambda it: (it.rating, it.year or 0), reverse=True)
    else:
        boost = boost_keys or set()
        ordered.sort(
            key=lambda it: (
                1 if it.rating_key in boost else 0,
                it.rating,
                it.year or 0,
            ),
            reverse=True,
        )
    return ordered[0]


async def async_match_people_and_candidates(
    runtime: PlexSmartRuntimeData,
    items: list[Any],
    *,
    media_type: str,
    actor_groups: list[list[str]],
    director_variants: list[str] | None = None,
    candidate_titles: list[str] | None = None,
    library_filter: str | None = None,
) -> tuple[list[Any], set[int]]:
    """Find items matching ALL actors (strict AND) + director, with candidate_titles fallback."""
    lib_norm = normalize_title(library_filter) if library_filter else ""
    base_pool = [
        it
        for it in items
        if not lib_norm or normalize_title(it.library_title) == lib_norm
    ]
    if not base_pool:
        return [], set()

    candidate_matched: dict[int, Any] = {}
    for ct in candidate_titles or []:
        hit, _ = find_best_media(base_pool, [ct], threshold=0.78)
        if hit is not None:
            candidate_matched[hit.rating_key] = hit

    if not actor_groups and not director_variants:
        return list(candidate_matched.values()), set(candidate_matched.keys())

    local_hits = filter_items_by_people_local(
        base_pool, actor_groups, director_variants
    )
    merged: dict[int, Any] = {it.rating_key: it for it in local_hits}

    index = runtime.coordinator.data
    section_ids = [
        s.section_id
        for s in (index.sections if index else [])
        if s.lib_type == media_type
        and (not lib_norm or normalize_title(s.title) == lib_norm)
    ]
    if section_ids and (
        not merged or len(actor_groups) >= 2 or bool(director_variants)
    ):
        server_keys = await runtime.api.async_intersect_people(
            section_ids, actor_groups, director_variants
        )
        if server_keys:
            by_rk = {it.rating_key: it for it in base_pool}
            for rk in server_keys:
                if rk in by_rk:
                    merged[rk] = by_rk[rk]

    if not merged and candidate_matched:
        merged = dict(candidate_matched)

    return list(merged.values()), set(candidate_matched.keys())


async def _resolve_live_resume_offset_sec(
    runtime: PlexSmartRuntimeData,
    rating_key: int,
    cached_offset_ms: int = 0,
    explicit_offset_sec: int | None = None,
) -> int:
    """Fetch fresh viewOffset from Plex server and protect against 0:00..0:64 proxy overwrites."""
    saved_map = getattr(runtime, "saved_resume_offsets", None)
    anchors_map = getattr(runtime, "playback_anchors", None)
    cached_sec = max(0, int(cached_offset_ms) // 1000)
    if isinstance(saved_map, dict) and cached_sec >= RESUME_WIPE_GUARD_SEC:
        saved_map.setdefault(rating_key, cached_sec)

    if explicit_offset_sec is not None and explicit_offset_sec > 0:
        if isinstance(saved_map, dict) and explicit_offset_sec >= RESUME_WIPE_GUARD_SEC:
            saved_map[rating_key] = int(explicit_offset_sec)
        return int(explicit_offset_sec)

    live_sec: int | None = None
    get_state = getattr(runtime.api, "async_get_item_playback_state", None)
    if callable(get_state):
        live = await get_state(rating_key)
        if live is not None:
            live_sec = max(0, int(live[0]) // 1000)

    best_sec = live_sec if live_sec is not None else cached_sec
    return resolve_protected_offset_sec(
        saved_map, rating_key, best_sec, anchors=anchors_map
    )


async def async_resolve_similar_items(
    runtime: PlexSmartRuntimeData,
    items: list[Any],
    similar_to: str,
    *,
    library_filter: str | None = None,
) -> list[Any]:
    """Find items similar to a reference title via PMS /similar (with local genre/cast fallback)."""
    lib_norm = normalize_title(library_filter) if library_filter else ""
    pool = [
        it
        for it in items
        if not lib_norm or normalize_title(it.library_title) == lib_norm
    ]
    if not pool or not similar_to.strip():
        return []

    base_item, _ = find_best_media(pool, [similar_to], threshold=0.68)
    if base_item is None:
        return []

    by_rk = {it.rating_key: it for it in pool if it.rating_key != base_item.rating_key}
    get_sim = getattr(runtime.api, "async_get_similar_item_keys", None)
    if callable(get_sim):
        sim_keys = await get_sim(base_item.rating_key, count=20)
        server_matches = [by_rk[rk] for rk in sim_keys if rk in by_rk]
        if server_matches:
            return server_matches

    # Local similarity fallback by shared genres, directors, and cast
    base_genres = {normalize_title(g) for g in base_item.genres if g}
    base_dirs = {normalize_title(d) for d in base_item.directors if d}
    base_actors = {normalize_title(a) for a in base_item.actors if a}
    scored: list[tuple[float, Any]] = []
    for cand in by_rk.values():
        c_genres = {normalize_title(g) for g in cand.genres if g}
        c_dirs = {normalize_title(d) for d in cand.directors if d}
        c_actors = {normalize_title(a) for a in cand.actors if a}
        overlap = (
            len(base_genres & c_genres) * 2.0
            + len(base_dirs & c_dirs) * 3.0
            + len(base_actors & c_actors) * 1.5
        )
        if overlap > 0:
            scored.append((overlap + cand.rating * 0.1, cand))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [it for _, it in scored[:15]]


def _apply_sort_and_resume(
    payload: dict[str, Any],
    *,
    sort_order: str,
    resume: bool,
    shuffle: bool,
    offset_sec: int | None = None,
) -> None:
    if resume:
        payload["resume"] = 1
        if offset_sec is not None and offset_sec > 0:
            payload["offset"] = int(offset_sec)
    else:
        payload["resume"] = 0
        payload["offset"] = int(offset_sec) if offset_sec and offset_sec > 0 else 0

    if sort_order == "newest":
        payload["sort"] = "originallyAvailableAt:desc"
        payload["shuffle"] = 0
    elif sort_order == "oldest":
        payload["sort"] = "originallyAvailableAt:asc"
        payload["shuffle"] = 0
    elif sort_order == "top_rated":
        payload["sort"] = "audienceRating:desc"
        payload["shuffle"] = 0
    elif shuffle or sort_order == "random":
        payload["shuffle"] = 1


async def _build_show_payload(
    runtime: PlexSmartRuntimeData,
    params: Mapping[str, Any],
    payload: dict[str, Any],
    title_queries: list[str],
    year: int | None,
    library_filter: str | None,
    genre: str | None,
    resume: bool,
    secondary_queries: list[str] | None = None,
) -> tuple[dict[str, Any], str, int | None, list[str] | None]:
    """Resolve show/episode payload. Returns (payload, matched_title, key, not_found_suggestions)."""
    index = runtime.coordinator.data
    shows = index.items_by_type.get("show", []) if index else []
    season = int(params["season"]) if params.get("season") is not None else None
    episode = int(params["episode"]) if params.get("episode") is not None else None
    actor_groups = build_actor_groups(params)
    director_variants = split_people_list(params.get("director")) + split_people_list(
        params.get("director_original")
    )
    candidate_titles = _parse_candidate_titles(params)

    show_item = None
    not_found_sugg: list[str] | None = None
    similar_to = str(params.get("similar_to") or "").strip()

    if similar_to and shows:
        sim_shows = await async_resolve_similar_items(
            runtime, shows, similar_to, library_filter=library_filter
        )
        if sim_shows:
            unwatched_sim = [s for s in sim_shows if s.view_count == 0]
            show_item = (unwatched_sim or sim_shows)[0]
        else:
            return payload, "", None, []

    if show_item is None and (title_queries or secondary_queries):
        show_item, sugg = find_best_media(
            shows,
            title_queries,
            year=year,
            library_filter=library_filter,
            secondary_queries=secondary_queries,
        )
        if show_item is None:
            not_found_sugg = [s.title for s in sugg]

    if show_item is None and (actor_groups or director_variants or candidate_titles) and shows:
        matched_shows, boost_keys = await async_match_people_and_candidates(
            runtime,
            shows,
            media_type="show",
            actor_groups=actor_groups,
            director_variants=director_variants or None,
            candidate_titles=candidate_titles,
            library_filter=library_filter,
        )
        if genre:
            g_norm = normalize_title(genre)
            matched_shows = [
                it
                for it in matched_shows
                if any(g_norm in normalize_title(g) for g in it.genres)
            ]
        if year:
            matched_shows = [it for it in matched_shows if it.year == year]
        if matched_shows:
            show_item = _pick_from_filtered_pool(
                matched_shows,
                sort_order=str(params.get("sort_order") or "default").lower(),
                shuffle=bool(params.get("shuffle")),
                boost_keys=boost_keys,
            )
            not_found_sugg = None
        else:
            return payload, "", None, []

    if not_found_sugg is not None:
        return payload, "", None, not_found_sugg

    if show_item is not None:
        shuffle = bool(params.get("shuffle"))
        if shuffle and season is None and episode is None:
            return (
                build_show_template(show_item.rating_key, resume=False, shuffle=True),
                show_item.title,
                show_item.rating_key,
                None,
            )

        ep_resolved = await runtime.api.async_resolve_episode(
            show_item.rating_key,
            season=season,
            episode=episode,
            resume_unwatched=resume,
            **({"shuffle": True} if shuffle else {}),
        )
        if ep_resolved is not None:
            ep_rk, ep_label, ep_off = ep_resolved
            explicit_off = params.get("offset_sec")
            ep_payload = build_episode_template(
                ep_rk,
                resume=resume,
                live_offset_sec=ep_off,
                explicit_offset_sec=explicit_off,
                continuous=True,
            )
            return ep_payload, ep_label, ep_rk, None

        if season is None and episode is None:
            return (
                build_show_template(
                    show_item.rating_key, resume=resume, shuffle=shuffle
                ),
                show_item.title,
                show_item.rating_key,
                None,
            )

        payload["library_name"] = show_item.library_title
        payload["show.title"] = show_item.title
        payload["continuous"] = 1
        if season is not None:
            payload["season.index"] = season
        if episode is not None:
            payload["episode.index"] = episode
        return payload, show_item.title, show_item.rating_key, None

    show_libs = [
        s.title for s in (index.sections if index else []) if s.lib_type == "show"
    ]
    if library_filter or show_libs:
        payload["library_name"] = library_filter or show_libs[0]
    if genre:
        payload["genre"] = genre
    if year:
        payload["year"] = year
    return payload, genre or "TV Shows", None, None


async def _build_music_payload(
    runtime: PlexSmartRuntimeData,
    params: Mapping[str, Any],
    payload: dict[str, Any],
    year: int | None,
    library_filter: str | None,
    genre: str | None,
    resume: bool = False,
) -> tuple[dict[str, Any], str, int | None]:
    """Resolve music artist / album / track / genre / decade payload via canonical templates."""
    index = runtime.coordinator.data
    artists = index.items_by_type.get("music", []) if index else []
    raw_artist = str(params.get("artist") or "").strip() or None
    raw_album = str(params.get("album") or "").strip() or None
    raw_track = str(params.get("track") or "").strip() or None
    raw_title = str(params.get("title") or "").strip() or None
    mood = str(params.get("mood") or "").strip() or None
    decade = int(params["decade"]) if params.get("decade") else None
    shuffle = bool(params.get("shuffle"))
    explicit_off = params.get("offset_sec")

    matched_artist = None
    artist_candidates = [v for v in (raw_artist, raw_title) if v]
    if artist_candidates and artists:
        matched_artist, _ = find_best_media(
            artists, artist_candidates, library_filter=library_filter, threshold=0.72
        )

    # If 'title' was passed without 'track' and did NOT match an artist (or artist was also passed),
    # treat 'title' as a song/track title!
    if not raw_track and raw_title and (raw_artist or matched_artist is None):
        raw_track = raw_title

    resolve_music = getattr(runtime.api, "async_resolve_music_target", None)
    if callable(resolve_music) and (raw_track or raw_album):
        resolved = await resolve_music(
            artist=matched_artist.title if matched_artist else raw_artist,
            album=raw_album,
            track=raw_track,
            resume=resume,
        )
        if resolved is not None:
            m_rk, m_kind, m_label, m_off = resolved
            return (
                build_music_item_template(
                    m_rk,
                    item_kind=m_kind,
                    resume=resume,
                    live_offset_sec=m_off,
                    explicit_offset_sec=explicit_off,
                    shuffle=shuffle,
                ),
                m_label,
                m_rk,
            )

    has_sub_filters = any((raw_album, raw_track, year, decade, genre, mood))
    if matched_artist and not has_sub_filters:
        # Default artist playback shuffles popular/all tracks unless user explicitly disabled it
        artist_shuffle = shuffle or ("shuffle" not in params)
        return (
            build_music_item_template(
                matched_artist.rating_key,
                item_kind="artist",
                resume=False,
                shuffle=artist_shuffle,
            ),
            matched_artist.title,
            matched_artist.rating_key,
        )

    music_libs = [
        s.title for s in (index.sections if index else []) if s.lib_type == "music"
    ]
    lib_name = (
        matched_artist.library_title
        if matched_artist
        else (library_filter or (music_libs[0] if music_libs else "Music"))
    )
    eff_artist = matched_artist.title if matched_artist else raw_artist
    filter_payload = build_music_filter_template(
        lib_name,
        artist=eff_artist,
        album=raw_album,
        track=raw_track,
        genre=genre,
        mood=mood,
        year=year,
        decade=decade,
        shuffle=shuffle or bool(genre or mood or decade or year),
    )
    label = str(
        raw_track or raw_album or eff_artist or genre or mood or decade or "Music"
    )
    return (
        filter_payload,
        label,
        matched_artist.rating_key if matched_artist else None,
    )


async def _build_movie_payload(
    runtime: PlexSmartRuntimeData,
    params: Mapping[str, Any],
    payload: dict[str, Any],
    title_queries: list[str],
    year: int | None,
    library_filter: str | None,
    genre: str | None,
    resume: bool,
    secondary_queries: list[str] | None = None,
) -> tuple[dict[str, Any], str, int | None, list[str] | None]:
    """Resolve movie payload (direct match or filtered queue)."""
    index = runtime.coordinator.data
    movies = index.items_by_type.get("movie", []) if index else []
    actor_groups = build_actor_groups(params)
    director_variants = split_people_list(params.get("director")) + split_people_list(
        params.get("director_original")
    )
    candidate_titles = _parse_candidate_titles(params)
    similar_to = str(params.get("similar_to") or "").strip()
    explicit_off = params.get("offset_sec")

    has_filters = (
        bool(actor_groups or director_variants or candidate_titles or similar_to)
        or any(
            params.get(k)
            for k in (
                "genre",
                "studio",
                "collection",
                "country",
                "decade",
                "content_rating",
                "unwatched",
            )
        )
    )

    if similar_to and movies:
        sim_movies = await async_resolve_similar_items(
            runtime, movies, similar_to, library_filter=library_filter
        )
        if genre:
            g_norm = normalize_title(genre)
            sim_movies = [
                it
                for it in sim_movies
                if any(g_norm in normalize_title(g) for g in it.genres)
            ]
        if params.get("unwatched", True):
            unwatched_sim = [it for it in sim_movies if it.view_count == 0]
            if unwatched_sim:
                sim_movies = unwatched_sim
        if not sim_movies:
            return payload, "", None, []
        chosen = _pick_from_filtered_pool(
            sim_movies,
            sort_order=str(params.get("sort_order") or "default").lower(),
            shuffle=bool(params.get("shuffle")),
        )
        live_off = (
            await _resolve_live_resume_offset_sec(
                runtime, chosen.rating_key, chosen.view_offset_ms, explicit_off
            )
            if resume
            else 0
        )
        title_str = (
            f"{chosen.title} ({chosen.year})" if chosen.year else chosen.title
        )
        return (
            build_movie_template(
                chosen.rating_key,
                resume=resume,
                live_offset_sec=live_off,
                explicit_offset_sec=explicit_off,
            ),
            title_str,
            chosen.rating_key,
            None,
        )

    if (title_queries or secondary_queries) and not has_filters:
        movie_item, sugg = find_best_media(
            movies,
            title_queries,
            year=year,
            library_filter=library_filter,
            secondary_queries=secondary_queries,
        )
        if movie_item is None:
            # Fast live server fallback in case movie was recently added before cache refresh
            for q in title_queries[:2]:
                hub_hits = await runtime.api.async_hub_search(q, limit=5)
                for hit in hub_hits:
                    if hit.get("type") == "movie" and hit.get("rating_key"):
                        rk = int(hit["rating_key"])
                        live_off = (
                            await _resolve_live_resume_offset_sec(
                                runtime, rk, 0, explicit_off
                            )
                            if resume
                            else 0
                        )
                        title_str = (
                            f"{hit['title']} ({hit['year']})"
                            if hit.get("year")
                            else str(hit.get("title") or q)
                        )
                        return (
                            build_movie_template(
                                rk,
                                resume=resume,
                                live_offset_sec=live_off,
                                explicit_offset_sec=explicit_off,
                            ),
                            title_str,
                            rk,
                            None,
                        )
            return (
                payload,
                "",
                None,
                [f"{s.title} ({s.year})" if s.year else s.title for s in sugg],
            )

        live_off = (
            await _resolve_live_resume_offset_sec(
                runtime,
                movie_item.rating_key,
                movie_item.view_offset_ms,
                explicit_off,
            )
            if resume
            else 0
        )
        direct_payload = build_movie_template(
            movie_item.rating_key,
            resume=resume,
            live_offset_sec=live_off,
            explicit_offset_sec=explicit_off,
        )
        title_str = (
            f"{movie_item.title} ({movie_item.year})"
            if movie_item.year
            else movie_item.title
        )
        return direct_payload, title_str, movie_item.rating_key, None

    if (actor_groups or director_variants or candidate_titles) and movies:
        matched_movies, boost_keys = await async_match_people_and_candidates(
            runtime,
            movies,
            media_type="movie",
            actor_groups=actor_groups,
            director_variants=director_variants or None,
            candidate_titles=candidate_titles,
            library_filter=library_filter,
        )
        if title_queries:
            t_hit, _ = find_best_media(
                matched_movies,
                title_queries,
                year=year,
                secondary_queries=secondary_queries,
            )
            matched_movies = [t_hit] if t_hit is not None else []
        if genre:
            g_norm = normalize_title(genre)
            matched_movies = [
                it
                for it in matched_movies
                if any(g_norm in normalize_title(g) for g in it.genres)
            ]
        if year:
            matched_movies = [it for it in matched_movies if it.year == year]
        if params.get("decade"):
            dec = int(params["decade"])
            matched_movies = [
                it for it in matched_movies if it.year and (it.year // 10) * 10 == dec
            ]
        if params.get("unwatched"):
            matched_movies = [it for it in matched_movies if it.view_count == 0]

        if not matched_movies:
            return payload, "", None, []

        chosen = _pick_from_filtered_pool(
            matched_movies,
            sort_order=str(params.get("sort_order") or "default").lower(),
            shuffle=bool(params.get("shuffle")),
            boost_keys=boost_keys,
        )
        live_off = (
            await _resolve_live_resume_offset_sec(
                runtime, chosen.rating_key, chosen.view_offset_ms, explicit_off
            )
            if resume
            else 0
        )
        direct_payload = build_movie_template(
            chosen.rating_key,
            resume=resume,
            live_offset_sec=live_off,
            explicit_offset_sec=explicit_off,
        )
        title_str = (
            f"{chosen.title} ({chosen.year})" if chosen.year else chosen.title
        )
        return direct_payload, title_str, chosen.rating_key, None

    movie_libs = [
        s.title for s in (index.sections if index else []) if s.lib_type == "movie"
    ]
    if library_filter or movie_libs:
        payload["library_name"] = library_filter or movie_libs[0]
    if title_queries:
        payload["title"] = title_queries[0]

    actor_tag = await _resolve_person_tag(
        runtime, params.get("actor"), params.get("actor_original")
    )
    if actor_tag:
        payload["actor"] = actor_tag
    if params.get("director"):
        dir_tag = await _resolve_person_tag(runtime, params.get("director"))
        if dir_tag:
            payload["director"] = dir_tag
    if genre:
        payload["genre"] = genre
    if year:
        payload["year"] = year
    if params.get("decade"):
        payload["decade"] = int(params["decade"])
    if params.get("studio"):
        payload["studio"] = str(params["studio"]).strip()
    if params.get("collection"):
        payload["collection"] = str(params["collection"]).strip()
    if params.get("country"):
        payload["country"] = str(params["country"]).strip()
    if params.get("content_rating"):
        payload["contentRating"] = str(params["content_rating"]).strip()
    if params.get("unwatched"):
        payload["unwatched"] = 1

    label = (
        ", ".join(
            str(p)
            for p in (
                title_queries[0] if title_queries else None,
                genre,
                actor_tag,
                year,
            )
            if p
        )
        or "Movies"
    )
    return payload, label, None, None


def _genres_for_type(index: Any, m_type: str) -> list[str]:
    """Return genres strictly from sections matching media_type (fallback to all_genres)."""
    if not index:
        return []
    if m_type in ("movie", "show", "music") and index.sections:
        scoped: list[str] = []
        seen: set[str] = set()
        for sec in index.sections:
            if sec.lib_type == m_type:
                for g in sec.genres:
                    if g not in seen:
                        seen.add(g)
                        scoped.append(g)
        if scoped:
            return scoped
    return list(index.all_genres)


async def _cancel_task_safely(task: asyncio.Task[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _cancel_zone_active_task(
    runtime: Any, zone_subentry_id: str | None
) -> None:
    """Cancel any superseded background launch task still running on the same zone."""
    if not zone_subentry_id:
        return
    active_tasks = getattr(runtime, "active_zone_tasks", None)
    if not isinstance(active_tasks, dict):
        return
    prev_task = active_tasks.pop(zone_subentry_id, None)
    if prev_task is not None and not prev_task.done():
        await _cancel_task_safely(prev_task)


async def _dispatch_playback(
    hass: HomeAssistant,
    entry: ConfigEntry,
    zone: Any,
    active_boot: asyncio.Task[tuple[str, float]],
    payload: dict[str, Any],
    content_type: str,
    *,
    matched_title: str,
    matched_key: int | None,
    media_type: str,
    initial_phase: str,
) -> LaunchResult:
    """Finish playback either synchronously (hot / optimistic=False) or optimistically in background."""
    zone_name = str(zone.data.get(CONF_ZONE_NAME) or zone.title)
    optimistic = bool(zone.data.get(CONF_OPTIMISTIC_MODE, DEFAULT_OPTIMISTIC_MODE))
    runtime = getattr(entry, "runtime_data", None)
    active_tasks = getattr(runtime, "active_zone_tasks", None) if runtime else None
    if (
        runtime is not None
        and isinstance(matched_key, int)
        and not payload.get("resume")
        and int(payload.get("offset") or 0) == 0
    ):
        saved_offsets = getattr(runtime, "saved_resume_offsets", None)
        if isinstance(saved_offsets, dict):
            saved_offsets.pop(matched_key, None)
        anchors = getattr(runtime, "playback_anchors", None)
        if isinstance(anchors, dict):
            anchors.pop(matched_key, None)

    if initial_phase != START_PHASE_HOT and optimistic:

        async def _finish_in_background() -> None:
            current = asyncio.current_task()
            try:
                start_phase, boot_sec = await active_boot
                ok, retries, play_err = await async_play_on_zone_with_retry(
                    hass,
                    entry.data,
                    zone.data,
                    payload,
                    content_type,
                    api=entry.runtime_data.api,
                    start_phase=start_phase,
                )
                final_res = LaunchResult(
                    success=ok,
                    zone_name=zone_name,
                    matched_title=matched_title,
                    rating_key=matched_key,
                    media_type=media_type,
                    boot_seconds=boot_sec,
                    retries_used=retries,
                    handoff_from=None,
                    start_phase=start_phase,
                    message=(
                        format_response(
                            hass,
                            entry.data,
                            "playing",
                            title=matched_title,
                            zone=zone_name,
                        )
                        if ok
                        else format_response(
                            hass,
                            entry.data,
                            "failed_play",
                            title=matched_title,
                            zone=zone_name,
                            error=play_err,
                        )
                    ),
                )
                record_last_launch(hass, entry, final_res)
            except asyncio.CancelledError:
                await _cancel_task_safely(active_boot)
                raise
            except Exception as err:  # noqa: BLE001
                safe_err = redact_sensitive_text(err)
                _LOGGER.warning("SmartPlex background playback failed: %s", safe_err)
                record_last_launch(
                    hass,
                    entry,
                    LaunchResult(
                        success=False,
                        zone_name=zone_name,
                        matched_title=matched_title,
                        rating_key=matched_key,
                        media_type=media_type,
                        boot_seconds=0.0,
                        retries_used=0,
                        handoff_from=None,
                        start_phase=initial_phase,
                        message=format_response(
                            hass,
                            entry.data,
                            "failed_play",
                            title=matched_title,
                            zone=zone_name,
                            error=safe_err,
                        ),
                    ),
                )
            finally:
                if (
                    isinstance(active_tasks, dict)
                    and active_tasks.get(zone.subentry_id) is current
                ):
                    active_tasks.pop(zone.subentry_id, None)

        bg_creator = getattr(entry, "async_create_background_task", None)
        bg_task: Any
        if callable(bg_creator):
            bg_task = bg_creator(
                hass,
                _finish_in_background(),
                f"{DOMAIN}_play_{zone.subentry_id}",
            )
        else:
            bg_task = asyncio.create_task(_finish_in_background())
        if isinstance(active_tasks, dict) and isinstance(bg_task, asyncio.Task):
            active_tasks[zone.subentry_id] = bg_task

        optimistic_res = LaunchResult(
            success=True,
            zone_name=zone_name,
            matched_title=matched_title,
            rating_key=matched_key,
            media_type=media_type,
            boot_seconds=0.0,
            retries_used=0,
            handoff_from=None,
            start_phase=f"{initial_phase}_optimistic",
            message=format_response(
                hass,
                entry.data,
                "starting_optimistic",
                title=matched_title,
                zone=zone_name,
            ),
        )
        record_last_launch(hass, entry, optimistic_res)
        return optimistic_res

    start_phase, boot_sec = await active_boot
    ok, retries, play_err = await async_play_on_zone_with_retry(
        hass,
        entry.data,
        zone.data,
        payload,
        content_type,
        api=entry.runtime_data.api,
        start_phase=start_phase,
    )
    res = LaunchResult(
        success=ok,
        zone_name=zone_name,
        matched_title=matched_title,
        rating_key=matched_key,
        media_type=media_type,
        boot_seconds=boot_sec,
        retries_used=retries,
        handoff_from=None,
        start_phase=start_phase,
        message=(
            format_response(
                hass, entry.data, "playing", title=matched_title, zone=zone_name
            )
            if ok
            else format_response(
                hass,
                entry.data,
                "failed_play",
                title=matched_title,
                zone=zone_name,
                error=play_err,
            )
        ),
    )
    record_last_launch(hass, entry, res)
    return res


def _build_launch_signature(params: Mapping[str, Any], m_type: str) -> str:
    """Build stable signature for anti-loop duplicate launch detection."""
    rk = params.get("rating_key")
    resume_flag = int(
        bool(params.get("resume"))
        or params.get("resume_mode") == "resume"
        or params.get("playback_mode") == "resume"
    )
    actors_sig = ",".join(
        normalize_title(a)
        for grp in build_actor_groups(params)
        for a in grp[:1]
    )
    title = normalize_title(
        str(
            params.get("show_name")
            or params.get("title")
            or params.get("track")
            or params.get("album")
            or params.get("similar_to")
            or actors_sig
            or params.get("director")
            or params.get("genre")
            or ""
        )
    )
    season = params.get("season")
    episode = params.get("episode")
    offset = params.get("offset_sec") or 0
    return f"{m_type}:{rk or title}:{season}:{episode}:r={resume_flag}:o={offset}"


async def async_execute_play_request(
    hass: HomeAssistant,
    entry: ConfigEntry,
    params: Mapping[str, Any],
    *,
    area_id: str | None = None,
    boot_task: asyncio.Task[tuple[str, float]] | None = None,
) -> LaunchResult:
    """Resolve media in target library, boot TV in parallel, and play with native plex:// URI."""
    runtime: PlexSmartRuntimeData = entry.runtime_data
    index = runtime.coordinator.data
    zone = resolve_zone(entry, hass, params.get("zone"), area_id=area_id)
    if zone is None:
        await _cancel_task_safely(boot_task)
        raise HomeAssistantError(
            "No media zones configured. Add at least one zone in SmartPlex settings."
        )

    zone_name = str(zone.data.get(CONF_ZONE_NAME) or zone.title)
    m_type = str(params.get("media_type") or params.get("type") or "movie").lower()

    # Anti-loop cooldown guard: if a weak LLM calls plex_smart_play again for the same zone
    # and same media within 15 seconds, return the previous result without restarting the TV!
    launch_sig = _build_launch_signature(params, m_type)
    recent_map = getattr(runtime, "recent_zone_launches", None)
    if isinstance(recent_map, dict):
        prev = recent_map.get(zone.subentry_id)
        if prev is not None:
            prev_ts, prev_sig, prev_res = prev
            if (
                prev_sig == launch_sig
                and (time.monotonic() - prev_ts) < DUPLICATE_LAUNCH_COOLDOWN_SEC
            ):
                await _cancel_task_safely(boot_task)
                _LOGGER.debug(
                    "Suppressed duplicate launch on zone '%s' within cooldown window",
                    zone_name,
                )
                return prev_res

    # Cancel any previous in-flight background boot/retry task on this zone so old retries
    # never collide with the newly requested title!
    await _cancel_zone_active_task(runtime, zone.subentry_id)

    resume = (
        bool(params.get("resume"))
        or params.get("resume_mode") == "resume"
        or params.get("playback_mode") == "resume"
    )
    shuffle = bool(params.get("shuffle"))
    sort_order = str(params.get("sort_order") or "default").lower()
    offset_sec = params.get("offset_sec")
    rating_key = params.get("rating_key")

    initial_phase = detect_zone_start_phase(hass, zone.data)
    own_boot = boot_task is None
    active_boot = boot_task or asyncio.create_task(
        async_ensure_zone_ready(hass, entry.data, zone.data)
    )

    if rating_key:
        rk = int(rating_key)
        cached = index.items_by_key.get(rk) if index else None
        matched_title = cached.title if cached else f"Plex #{rk}"
        content_type = (
            "episode"
            if (cached and cached.media_type == "episode") or m_type == "show"
            else ("music" if m_type == "music" else "movie")
        )
        live_off = (
            await _resolve_live_resume_offset_sec(
                runtime,
                rk,
                cached.view_offset_ms if cached else 0,
                offset_sec,
            )
            if resume
            else 0
        )
        if content_type == "episode":
            payload = build_episode_template(
                rk,
                resume=resume,
                live_offset_sec=live_off,
                explicit_offset_sec=offset_sec,
            )
        elif content_type == "music":
            music_kind = (
                "track"
                if (
                    (cached and cached.media_type == "track")
                    or resume
                    or (offset_sec is not None and int(offset_sec) > 0)
                )
                else "artist"
            )
            payload = build_music_item_template(
                rk,
                item_kind=music_kind,
                resume=resume,
                live_offset_sec=live_off,
                explicit_offset_sec=offset_sec,
                shuffle=shuffle,
            )
        else:
            payload = build_movie_template(
                rk,
                resume=resume,
                live_offset_sec=live_off,
                explicit_offset_sec=offset_sec,
            )
        res = await _dispatch_playback(
            hass,
            entry,
            zone,
            active_boot,
            payload,
            content_type,
            matched_title=matched_title,
            matched_key=rk,
            media_type=m_type,
            initial_phase=initial_phase,
        )
        if isinstance(recent_map, dict) and res.success:
            recent_map[zone.subentry_id] = (time.monotonic(), launch_sig, res)
        return res

    title_queries = [
        str(v).strip()
        for v in (params.get("show_name"), params.get("title"))
        if v and str(v).strip()
    ]
    secondary_queries = [
        str(params["original_title"]).strip()
    ] if params.get("original_title") and str(params["original_title"]).strip() else []
    year = int(params["year"]) if params.get("year") else None
    library_filter = str(params.get("library") or "").strip() or None
    genre = resolve_genre_tag(params.get("genre"), _genres_for_type(index, m_type))

    payload = {"allow_multiple": 1}
    _apply_sort_and_resume(
        payload,
        sort_order=sort_order,
        resume=resume,
        shuffle=shuffle,
        offset_sec=offset_sec,
    )

    try:
        not_found_sugg: list[str] | None = None
        if m_type == "playlist":
            content_type = "playlist"
            pl, sugg = find_best_playlist(
                index.playlists if index else [], title_queries + secondary_queries
            )
            if pl is None and (title_queries or secondary_queries):
                not_found_sugg = [s.title for s in sugg]
                matched_title, matched_key = "", None
            else:
                pl_title = pl.title if pl else (title_queries[0] if title_queries else "")
                payload = build_playlist_template(pl_title, shuffle=shuffle)
                matched_title, matched_key = pl_title, (pl.rating_key if pl else None)
        elif m_type == "show":
            content_type = "episode"
            (
                payload,
                matched_title,
                matched_key,
                not_found_sugg,
            ) = await _build_show_payload(
                runtime,
                params,
                payload,
                title_queries,
                year,
                library_filter,
                genre,
                resume,
                secondary_queries=secondary_queries,
            )
        elif m_type == "music":
            content_type = "music"
            payload, matched_title, matched_key = await _build_music_payload(
                runtime, params, payload, year, library_filter, genre, resume=resume
            )
        else:
            content_type = "movie"
            (
                payload,
                matched_title,
                matched_key,
                not_found_sugg,
            ) = await _build_movie_payload(
                runtime,
                params,
                payload,
                title_queries,
                year,
                library_filter,
                genre,
                resume,
                secondary_queries=secondary_queries,
            )

        if not_found_sugg is not None:
            if own_boot:
                await _cancel_task_safely(active_boot)
            actor_label = ", ".join(
                grp[0] for grp in build_actor_groups(params) if grp
            )
            q_label = (
                title_queries[0]
                if title_queries
                else (actor_label or str(params.get("director") or m_type))
            )
            res = LaunchResult(
                success=False,
                zone_name=zone_name,
                matched_title="",
                rating_key=None,
                media_type=m_type,
                boot_seconds=0.0,
                retries_used=0,
                handoff_from=None,
                message=format_response(
                    hass, entry.data, "not_found", query=q_label
                ),
                suggestions=not_found_sugg,
            )
            record_last_launch(hass, entry, res)
            return res

        if (
            zone.data.get(CONF_POWER_PROFILE) == PROFILE_APPLE_TV
            and "plex_key" not in payload
            and index is not None
            and m_type in ("movie", "show")
        ):
            pool = list(index.items_by_type.get(m_type, []))
            if library_filter:
                lf_norm = normalize_title(library_filter)
                pool = [
                    it for it in pool if normalize_title(it.library_title) == lf_norm
                ]
            if genre:
                g_norm = normalize_title(genre)
                pool = [
                    it
                    for it in pool
                    if any(g_norm in normalize_title(g) for g in it.genres)
                ]
            if year:
                pool = [it for it in pool if it.year == year]
            if params.get("unwatched"):
                pool = [it for it in pool if it.view_count == 0]
            if pool:
                if sort_order == "newest":
                    pool.sort(key=lambda it: (it.year or 0, it.rating), reverse=True)
                    chosen = pool[0]
                elif sort_order == "oldest":
                    pool.sort(key=lambda it: (it.year or 9999, -it.rating))
                    chosen = pool[0]
                elif sort_order == "top_rated":
                    pool.sort(key=lambda it: (it.rating, it.year or 0), reverse=True)
                    chosen = pool[0]
                else:
                    chosen = random.choice(pool[:20] if not shuffle else pool)

                payload = {
                    "plex_key": chosen.rating_key,
                    "resume": 1 if resume else 0,
                    "offset": 0,
                    "_is_show_root": m_type == "show",
                }
                matched_title = (
                    f"{chosen.title} ({chosen.year})" if chosen.year else chosen.title
                )
                matched_key = chosen.rating_key

        _LOGGER.info(
            "SmartPlex launch: title='%s', zone='%s', phase=%s, mode=%s, offset=%ss, key=%s",
            matched_title,
            zone_name,
            initial_phase,
            "RESUME" if payload.get("resume") else "START",
            int(payload.get("offset") or 0),
            matched_key,
        )
        res = await _dispatch_playback(
            hass,
            entry,
            zone,
            active_boot,
            payload,
            content_type,
            matched_title=matched_title,
            matched_key=matched_key,
            media_type=m_type,
            initial_phase=initial_phase,
        )
        if isinstance(recent_map, dict) and res.success:
            recent_map[zone.subentry_id] = (time.monotonic(), launch_sig, res)
        return res
    except Exception:
        if own_boot:
            await _cancel_task_safely(active_boot)
        raise


def _launch_result_to_dict(res: LaunchResult) -> dict[str, Any]:
    return {
        "success": res.success,
        "start_phase": res.start_phase,
        "zone": res.zone_name,
        "matched_title": res.matched_title,
        "rating_key": res.rating_key,
        "media_type": res.media_type,
        "boot_seconds": res.boot_seconds,
        "retries_used": res.retries_used,
        "handoff_from": res.handoff_from,
        "message": res.message,
        "suggestions": res.suggestions,
    }


COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required("command_text"): cv.string,
        vol.Optional("zone"): cv.string,
        vol.Optional("ai_entity"): cv.entity_id,
        vol.Optional("entry_id"): cv.string,
    }
)

PLAY_SCHEMA = vol.Schema(
    {
        vol.Optional("zone"): cv.string,
        vol.Optional("rating_key"): vol.Coerce(int),
        vol.Optional("media_type", default="movie"): vol.In(
            ["movie", "show", "music", "playlist"]
        ),
        vol.Optional("title"): cv.string,
        vol.Optional("original_title"): cv.string,
        vol.Optional("artist"): cv.string,
        vol.Optional("album"): cv.string,
        vol.Optional("track"): cv.string,
        vol.Optional("similar_to"): cv.string,
        vol.Optional("season"): vol.Coerce(int),
        vol.Optional("episode"): vol.Coerce(int),
        vol.Optional("actor"): cv.string,
        vol.Optional("actors"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("actors_original"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("candidate_titles"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("director"): cv.string,
        vol.Optional("genre"): cv.string,
        vol.Optional("year"): vol.Coerce(int),
        vol.Optional("library"): cv.string,
        vol.Optional("sort_order", default="default"): vol.In(
            ["default", "newest", "oldest", "top_rated", "random"]
        ),
        vol.Optional("resume", default=False): cv.boolean,
        vol.Optional("shuffle", default=False): cv.boolean,
        vol.Optional("offset_sec"): vol.Coerce(int),
        vol.Optional("entry_id"): cv.string,
    }
)

HANDOFF_SCHEMA = vol.Schema(
    {
        vol.Required("target_zone"): cv.string,
        vol.Optional("source_zone"): cv.string,
        vol.Optional("turn_off_source"): cv.boolean,
        vol.Optional("media_type"): vol.In(["movie", "show", "music"]),
        vol.Optional("entry_id"): cv.string,
    }
)

CONTROL_SCHEMA = vol.Schema(
    {
        vol.Required("action"): vol.In(list(CONTROL_ACTIONS)),
        vol.Optional("zone"): cv.string,
        vol.Optional("offset_sec"): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
        vol.Optional("delta_sec"): vol.All(vol.Coerce(int), vol.Range(min=-3600, max=3600)),
        vol.Optional("repeat"): vol.In(list(REPEAT_MODES)),
        vol.Optional("shuffle"): cv.boolean,
        vol.Optional("entry_id"): cv.string,
    }
)

ROULETTE_SCHEMA = vol.Schema(
    {
        vol.Optional("preset"): cv.string,
        vol.Optional("zone"): cv.string,
        vol.Optional("libraries"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("watch_filter"): vol.In(
            ["unwatched", "in_progress", "watched", "any"]
        ),
        vol.Optional("include_genres"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("exclude_genres"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("min_rating"): vol.Coerce(float),
        vol.Optional("year_from"): vol.Coerce(int),
        vol.Optional("max_duration_min"): vol.Coerce(int),
        vol.Optional("pick_count"): vol.All(vol.Coerce(int), vol.Range(min=1, max=6)),
        vol.Optional("collage_layout"): vol.In(["auto", "grid_2x2", "row", "single"]),
        vol.Optional("anti_repeat_days"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=365)
        ),
        vol.Optional("entry_id"): cv.string,
    }
)

REFRESH_SCHEMA = vol.Schema({vol.Optional("entry_id"): cv.string})


async def async_register_services(hass: HomeAssistant) -> None:
    """Register SmartPlex services once."""
    if hass.services.has_service(DOMAIN, SERVICE_COMMAND):
        return

    async def _handle_command(call: ServiceCall) -> dict[str, Any]:
        entry = _get_entry(hass, call.data.get("entry_id"))
        command_text = call.data["command_text"]
        explicit_zone = call.data.get("zone")

        # 0. Deterministic <1 ms check for transport/seek/repeat/shuffle commands
        ctrl_intent = extract_playback_control_intent(entry, hass, command_text)
        if ctrl_intent is not None:
            c_zone = await async_resolve_active_playback_zone(
                entry, hass, explicit_zone or ctrl_intent.get("zone")
            )
            if c_zone is None:
                raise HomeAssistantError("No media zones configured in SmartPlex.")
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
            return _launch_result_to_dict(c_res)

        # 1. Deterministic <1 ms check for pure room-to-room transfer ("перенос фильма из зала в спальню")
        pure_handoff = extract_pure_handoff_intent(entry, hass, command_text)
        if pure_handoff is not None:
            raw_tgt_name = explicit_zone or pure_handoff.get("target_zone")
            if raw_tgt_name or pure_handoff.get("is_transfer"):
                h_target = resolve_zone(entry, hass, raw_tgt_name)
            else:
                h_target = await async_resolve_active_playback_zone(entry, hass, None)
            if h_target is None:
                raise HomeAssistantError("No media zones configured in SmartPlex.")
            await _cancel_zone_active_task(entry.runtime_data, h_target.subentry_id)
            h_source = (
                resolve_zone(entry, hass, pure_handoff.get("source_zone"))
                if pure_handoff.get("source_zone")
                else None
            )
            h_res = await async_handoff_or_resume(
                hass,
                entry,
                target_zone=h_target,
                source_zone=h_source,
                turn_off_source=True if pure_handoff.get("is_transfer") else None,
                media_type=pure_handoff.get("media_type"),
            )
            record_last_launch(hass, entry, h_res)
            return _launch_result_to_dict(h_res)

        # 2. Pre-detect target zone & media type locally (<1 ms) and start TV boot BEFORE AI!
        detected_type = pre_detect_media_type(command_text)
        pre_zone = pre_resolve_zone_from_text(
            entry, hass, command_text, explicit_zone=explicit_zone
        )
        pre_boot_task: asyncio.Task[tuple[str, float]] | None = (
            asyncio.create_task(async_ensure_zone_ready(hass, entry.data, pre_zone.data))
            if pre_zone is not None
            else None
        )

        try:
            # 2. Fast local resolution (<1 ms, 0 LLM tokens) for direct title/episode commands!
            direct_params = try_direct_local_resolve(
                entry, hass, command_text, explicit_zone=explicit_zone
            )
            if direct_params is not None:
                res = await async_execute_play_request(
                    hass, entry, direct_params, boot_task=pre_boot_task
                )
                if not res.success:
                    await _cancel_task_safely(pre_boot_task)
                return _launch_result_to_dict(res)

            parsed = await async_parse_command_via_ai_task(
                hass,
                entry,
                command_text,
                ai_entity_override=call.data.get("ai_entity"),
                detected_type=detected_type,
            )
            control = parsed.get("control") or {}
            query = parsed.get("query") or {}

            target_zone_name = (
                explicit_zone or control.get("zone") or control.get("room")
            )
            resolved_target = resolve_zone(entry, hass, target_zone_name)

            # Reuse pre-boot task if it matches the final target zone; otherwise cancel it
            shared_boot: asyncio.Task[tuple[str, float]] | None = None
            if pre_boot_task is not None:
                if (
                    pre_zone is not None
                    and resolved_target is not None
                    and pre_zone.subentry_id == resolved_target.subentry_id
                ):
                    shared_boot = pre_boot_task
                else:
                    await _cancel_task_safely(pre_boot_task)

            if str(control.get("intent") or "play").lower() == "handoff":
                if resolved_target is None:
                    await _cancel_task_safely(shared_boot)
                    raise HomeAssistantError("No media zones configured in SmartPlex.")
                await _cancel_zone_active_task(
                    entry.runtime_data, resolved_target.subentry_id
                )
                source = (
                    resolve_zone(entry, hass, control.get("source_zone"))
                    if control.get("source_zone")
                    else None
                )
                res = await async_handoff_or_resume(
                    hass,
                    entry,
                    target_zone=resolved_target,
                    source_zone=source,
                    turn_off_source=control.get("turn_off_previous_tv"),
                    media_type=detected_type or control.get("type"),
                    boot_task=shared_boot,
                )
                record_last_launch(hass, entry, res)
                return _launch_result_to_dict(res)

            final_type = detected_type or control.get("type", "movie")
            merged: dict[str, Any] = {
                **query,
                "zone": target_zone_name,
                "media_type": final_type,
                "resume_mode": control.get("resume_mode", "start"),
                "sort_order": control.get("sort_order", "default"),
                "shuffle": bool(control.get("shuffle", False)),
            }
            res = await async_execute_play_request(
                hass, entry, merged, boot_task=shared_boot
            )
            if not res.success:
                await _cancel_task_safely(shared_boot)
            return _launch_result_to_dict(res)
        except Exception:
            await _cancel_task_safely(pre_boot_task)
            raise

    async def _handle_play(call: ServiceCall) -> dict[str, Any]:
        entry = _get_entry(hass, call.data.get("entry_id"))
        res = await async_execute_play_request(hass, entry, call.data)
        return _launch_result_to_dict(res)

    async def _handle_handoff(call: ServiceCall) -> dict[str, Any]:
        entry = _get_entry(hass, call.data.get("entry_id"))
        target = resolve_zone(entry, hass, call.data.get("target_zone"))
        if target is None:
            raise HomeAssistantError("No media zones configured in SmartPlex.")
        await _cancel_zone_active_task(entry.runtime_data, target.subentry_id)
        source = (
            resolve_zone(entry, hass, call.data.get("source_zone"))
            if call.data.get("source_zone")
            else None
        )
        res = await async_handoff_or_resume(
            hass,
            entry,
            target_zone=target,
            source_zone=source,
            turn_off_source=call.data.get("turn_off_source"),
            media_type=call.data.get("media_type"),
        )
        record_last_launch(hass, entry, res)
        return _launch_result_to_dict(res)

    async def _handle_control(call: ServiceCall) -> dict[str, Any]:
        entry = _get_entry(hass, call.data.get("entry_id"))
        c_zone = await async_resolve_active_playback_zone(
            entry, hass, call.data.get("zone")
        )
        if c_zone is None:
            raise HomeAssistantError("No media zones configured in SmartPlex.")
        res = await async_control_zone_playback(
            hass,
            entry,
            c_zone,
            call.data["action"],
            offset_sec=call.data.get("offset_sec"),
            delta_sec=call.data.get("delta_sec"),
            repeat=call.data.get("repeat"),
            shuffle=call.data.get("shuffle"),
        )
        return _launch_result_to_dict(res)

    async def _handle_spin_roulette(call: ServiceCall) -> dict[str, Any]:
        entry = _get_entry(hass, call.data.get("entry_id"))
        return await async_spin_roulette(hass, entry, call.data)

    async def _handle_refresh_cache(call: ServiceCall) -> None:
        entry = _get_entry(hass, call.data.get("entry_id"))
        await entry.runtime_data.coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_COMMAND,
        _handle_command,
        schema=COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY,
        _handle_play,
        schema=PLAY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_HANDOFF,
        _handle_handoff,
        schema=HANDOFF_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL,
        _handle_control,
        schema=CONTROL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SPIN_ROULETTE,
        _handle_spin_roulette,
        schema=ROULETTE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_CACHE,
        _handle_refresh_cache,
        schema=REFRESH_SCHEMA,
    )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove SmartPlex services when the last config entry is unloaded."""
    for srv in (
        SERVICE_COMMAND,
        SERVICE_PLAY,
        SERVICE_HANDOFF,
        SERVICE_CONTROL,
        SERVICE_SPIN_ROULETTE,
        SERVICE_REFRESH_CACHE,
    ):
        if hass.services.has_service(DOMAIN, srv):
            hass.services.async_remove(DOMAIN, srv)





