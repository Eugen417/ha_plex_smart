"""Canonical Home Assistant Plex payload templates for PLAY (start) vs RESUME across all media types.

Aligned with Home Assistant Core `homeassistant/components/plex/{services,media_search,models}.py`
and Plex Media Server OpenAPI v1.2.3.
"""

from __future__ import annotations

from typing import Any

LLM_MEDIA_TEMPLATES_GUIDE = (
    "MEDIA PLAY vs RESUME PARAMETER TEMPLATES:\n"
    "- MOVIE Start ('включи фильм X'): media_type='movie', title='X', playback_mode='start', resume=false.\n"
    "- MOVIE Resume ('продолжи/досмотри фильм X'): ALWAYS call plex_smart_play with "
    "media_type='movie', title='X', playback_mode='resume', resume=true "
    "(NEVER call plex_smart_transfer_or_resume when a title is named!).\n"
    "- TV SHOW Start from S01E01 ('включи сериал X'): media_type='show', title='X', playback_mode='start', resume=false.\n"
    "- TV SHOW Resume On-Deck ('продолжи сериал X'): media_type='show', title='X', playback_mode='resume', resume=true.\n"
    "- TV SEASON ('включи/продолжи 2 сезон X'): media_type='show', title='X', season=2, resume=false|true.\n"
    "- TV EPISODE ('включи/продолжи X сезон 2 серия 5'): media_type='show', title='X', season=2, episode=5, resume=false|true.\n"
    "- MUSIC Artist ('включи группу/исполнителя X'): media_type='music', artist='X', shuffle=true.\n"
    "- MUSIC Album / Audiobook ('включи/продолжи альбом X [исполнителя Y]'): media_type='music', album='X', artist='Y', resume=false|true.\n"
    "- MUSIC Song / Track ('включи песню/трек X [исполнителя Y]'): media_type='music', track='X', artist='Y', resume=false|true.\n"
    "- MUSIC Genre / Mood / Decade ('включи рок 90-х'): media_type='music', genre='Rock', decade=1990, shuffle=true.\n"
    "- PLAYLIST ('включи плейлист X'): media_type='playlist', title='X', shuffle=false|true.\n"
    "- SIMILAR ('включи что-нибудь похожее на X'): media_type='movie'|'show', similar_to='X'.\n"
    "- HANDOFF / CONTINUE WITHOUT TITLE ('продолжи просмотр в спальне', 'перенеси кино в зал'): "
    "ONLY when NO movie/show title is mentioned, call plex_smart_transfer_or_resume."
)


def build_movie_template(
    rating_key: int,
    *,
    resume: bool = False,
    live_offset_sec: int = 0,
    explicit_offset_sec: int | None = None,
) -> dict[str, Any]:
    """Build canonical movie payload for Start (0:00) vs Resume (saved viewOffset)."""
    if explicit_offset_sec is not None and explicit_offset_sec > 0:
        return {
            "plex_key": int(rating_key),
            "resume": 1 if resume else 0,
            "offset": int(explicit_offset_sec),
        }
    if resume:
        payload: dict[str, Any] = {"plex_key": int(rating_key), "resume": 1}
        if live_offset_sec > 0:
            payload["offset"] = int(live_offset_sec)
        return payload
    return {"plex_key": int(rating_key), "resume": 0, "offset": 0}


def build_episode_template(
    episode_rating_key: int,
    *,
    resume: bool = False,
    live_offset_sec: int = 0,
    explicit_offset_sec: int | None = None,
    continuous: bool = True,
) -> dict[str, Any]:
    """Build canonical TV episode payload with continuous=1 PlayQueue auto-advance."""
    payload: dict[str, Any] = {"plex_key": int(episode_rating_key)}
    if continuous:
        payload["continuous"] = 1
    if explicit_offset_sec is not None and explicit_offset_sec > 0:
        payload["resume"] = 1 if resume else 0
        payload["offset"] = int(explicit_offset_sec)
        return payload
    if resume:
        payload["resume"] = 1
        if live_offset_sec > 0:
            payload["offset"] = int(live_offset_sec)
        return payload
    payload["resume"] = 0
    payload["offset"] = 0
    return payload


def build_show_template(
    show_rating_key: int,
    *,
    resume: bool = False,
    shuffle: bool = False,
) -> dict[str, Any]:
    """Build canonical show-level fallback/shuffle payload (uses Plex onDeck when resume=True)."""
    payload: dict[str, Any] = {
        "plex_key": int(show_rating_key),
        "continuous": 1,
        "_is_show_root": True,
    }
    if shuffle:
        payload["shuffle"] = 1
        payload["resume"] = 0
    else:
        payload["resume"] = 1 if resume else 0
    return payload


def build_music_item_template(
    rating_key: int,
    *,
    item_kind: str,
    resume: bool = False,
    live_offset_sec: int = 0,
    explicit_offset_sec: int | None = None,
    shuffle: bool = False,
) -> dict[str, Any]:
    """Build canonical payload for a resolved artist, album, or single track/audiobook chapter."""
    payload: dict[str, Any] = {
        "plex_key": int(rating_key),
        "continuous": 1,
    }
    if item_kind == "track":
        if explicit_offset_sec is not None and explicit_offset_sec > 0:
            payload["resume"] = 1 if resume else 0
            payload["offset"] = int(explicit_offset_sec)
        elif resume:
            payload["resume"] = 1
            if live_offset_sec > 0:
                payload["offset"] = int(live_offset_sec)
        else:
            payload["resume"] = 0
            payload["offset"] = 0
        return payload

    payload["shuffle"] = 1 if shuffle else 0
    payload["resume"] = 0
    return payload


def build_music_filter_template(
    library_name: str,
    *,
    artist: str | None = None,
    album: str | None = None,
    track: str | None = None,
    genre: str | None = None,
    mood: str | None = None,
    year: int | None = None,
    decade: int | None = None,
    shuffle: bool = True,
) -> dict[str, Any]:
    """Build HA Plex music search payload using valid plexapi scoped attributes."""
    payload: dict[str, Any] = {
        "library_name": library_name,
        "allow_multiple": 1,
        "continuous": 1,
    }
    if track:
        payload["libtype"] = "track"
        payload["track.title"] = track
        if artist:
            payload["artist.title"] = artist
        if album:
            payload["album.title"] = album
        payload["shuffle"] = 1 if shuffle else 0
    elif album:
        payload["libtype"] = "album"
        payload["album.title"] = album
        if artist:
            payload["artist.title"] = artist
        payload["shuffle"] = 1 if shuffle else 0
    elif genre or mood or year or decade:
        payload["libtype"] = "track"
        if artist:
            payload["artist.title"] = artist
        payload["shuffle"] = 1 if shuffle else 0
    else:
        payload["libtype"] = "artist"
        if artist:
            payload["artist.title"] = artist
        payload["shuffle"] = 1 if shuffle else 0

    if genre:
        payload["genre"] = genre
    if mood:
        payload["mood"] = mood
    if year:
        payload["album.year"] = int(year)
    if decade:
        payload["album.decade"] = int(decade)

    return payload


def build_playlist_template(
    playlist_name: str,
    *,
    shuffle: bool = False,
) -> dict[str, Any]:
    """Build canonical playlist payload (preserves playlist order unless shuffle=True)."""
    return {
        "playlist_name": playlist_name,
        "shuffle": 1 if shuffle else 0,
    }

