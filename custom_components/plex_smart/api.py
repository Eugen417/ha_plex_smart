"""Async Plex Media Server HTTP API client (JSON with XML fallback)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any
from urllib.parse import quote
import xml.etree.ElementTree as ET

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import PLEX_CLIENT_IDENTIFIER, PLEX_PRODUCT_NAME, SEEK_PROXY_TIMEOUT_SEC
from .models import (
    PlexActiveSession,
    PlexIndexData,
    PlexLibrarySection,
    PlexMediaItem,
    PlexPlaylistItem,
)
from .privacy import redact_sensitive_text
from .search import normalize_title, person_matches

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=20)
_POSTER_TIMEOUT = aiohttp.ClientTimeout(total=15)
_SEEK_TIMEOUT = aiohttp.ClientTimeout(total=SEEK_PROXY_TIMEOUT_SEC)


class PlexApiError(Exception):
    """Base error for Plex API communication."""


class PlexAuthError(PlexApiError):
    """Raised when Plex returns HTTP 401 Unauthorized."""


class PlexConnectionError(PlexApiError):
    """Raised on network/timeout/HTTP 5xx errors."""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _safe_opt_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def _xml_element_to_dict(elem: ET.Element) -> dict[str, Any]:
    """Convert a Plex XML metadata element into a JSON-compatible dict."""
    node: dict[str, Any] = dict(elem.attrib)
    genres = [
        g.attrib.get("tag", "")
        for g in elem.findall("./Genre")
        if g.attrib.get("tag")
    ]
    if genres:
        node["Genre"] = [{"tag": g} for g in genres]
    roles = [
        r.attrib.get("tag", "")
        for r in elem.findall("./Role")
        if r.attrib.get("tag")
    ]
    if roles:
        node["Role"] = [{"tag": r} for r in roles]
    directors = [
        d.attrib.get("tag", "")
        for d in elem.findall("./Director")
        if d.attrib.get("tag")
    ]
    if directors:
        node["Director"] = [{"tag": d} for d in directors]
    player = elem.find("./Player")
    if player is not None:
        node["Player"] = dict(player.attrib)
    session = elem.find("./Session")
    if session is not None:
        node["Session"] = dict(session.attrib)
    return node


def _parse_xml_container(xml_text: str) -> dict[str, Any]:
    """Parse Plex XML response into a MediaContainer dict."""
    root = ET.fromstring(xml_text)
    container: dict[str, Any] = dict(root.attrib)
    dirs: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    hubs: list[dict[str, Any]] = []

    for child in root:
        if child.tag == "Directory":
            item = _xml_element_to_dict(child)
            dirs.append(item)
            meta.append(item)
        elif child.tag in ("Video", "Track", "Playlist"):
            meta.append(_xml_element_to_dict(child))
        elif child.tag == "Hub":
            hub_dict = dict(child.attrib)
            hub_dict["Metadata"] = [_xml_element_to_dict(c) for c in child]
            hubs.append(hub_dict)

    if dirs:
        container["Directory"] = dirs
    if meta:
        container["Metadata"] = meta
    if hubs:
        container["Hub"] = hubs
    return container


class PlexApiClient:
    """Async client for communicating with a Plex Media Server."""

    def __init__(
        self,
        hass: HomeAssistant,
        base_url: str,
        token: str,
        verify_ssl: bool = False,
    ) -> None:
        """Initialize Plex API client."""
        self._hass = hass
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._verify_ssl = verify_ssl
        self._command_id: int = 0

    @property
    def base_url(self) -> str:
        """Return normalized server base URL."""
        return self._base_url

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Token": self._token,
            "X-Plex-Product": PLEX_PRODUCT_NAME,
            "X-Plex-Client-Identifier": PLEX_CLIENT_IDENTIFIER,
        }

    def _session(self) -> aiohttp.ClientSession:
        return async_get_clientsession(self._hass, verify_ssl=self._verify_ssl)

    async def _async_get_container(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch a Plex endpoint and return its MediaContainer dict."""
        url = f"{self._base_url}{path}"
        session = self._session()
        try:
            async with session.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=_DEFAULT_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    raise PlexAuthError("Invalid or expired Plex token (HTTP 401)")
                if resp.status >= 400:
                    raise PlexConnectionError(
                        f"Plex server returned HTTP {resp.status} for {path}"
                    )
                ctype = (resp.headers.get("Content-Type") or "").lower()
                text = await resp.text()
        except (PlexAuthError, PlexConnectionError):
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PlexConnectionError(
                f"Failed to connect to Plex at {path}: {redact_sensitive_text(err)}"
            ) from err

        if "json" in ctype or text.lstrip().startswith("{"):
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    return payload.get("MediaContainer", payload)
            except ValueError:
                pass

        try:
            return _parse_xml_container(text)
        except ET.ParseError as err:
            raise PlexApiError(
                f"Failed to parse Plex response for {path}: {err}"
            ) from err

    async def async_validate_connection(self) -> tuple[str, str]:
        """Validate server URL and token; return (machine_identifier, friendly_name)."""
        container = await self._async_get_container("/")
        machine_id = str(container.get("machineIdentifier") or "").strip()
        friendly_name = str(
            container.get("friendlyName") or "Plex Media Server"
        ).strip()
        if not machine_id:
            raise PlexApiError("Plex server did not return a machineIdentifier")
        return machine_id, friendly_name

    def _parse_media_item(
        self,
        raw: dict[str, Any],
        section_id: str,
        library_title: str,
        media_type: str,
    ) -> PlexMediaItem | None:
        rating_key = _safe_int(raw.get("ratingKey"))
        title = str(raw.get("title") or "").strip()
        if not rating_key or not title:
            return None

        orig = str(raw.get("originalTitle") or "").strip()
        raw_genres = raw.get("Genre") or []
        genres: list[str] = []
        if isinstance(raw_genres, list):
            for g in raw_genres:
                tag = g.get("tag") if isinstance(g, dict) else str(g)
                if tag:
                    genres.append(str(tag).strip())

        raw_roles = raw.get("Role") or []
        actors: list[str] = []
        if isinstance(raw_roles, list):
            for r in raw_roles:
                tag = r.get("tag") if isinstance(r, dict) else str(r)
                if tag:
                    actors.append(str(tag).strip())

        raw_directors = raw.get("Director") or []
        directors: list[str] = []
        if isinstance(raw_directors, list):
            for d in raw_directors:
                tag = d.get("tag") if isinstance(d, dict) else str(d)
                if tag:
                    directors.append(str(tag).strip())

        rating = _safe_float(raw.get("audienceRating")) or _safe_float(
            raw.get("rating")
        )

        return PlexMediaItem(
            rating_key=rating_key,
            section_id=section_id,
            library_title=library_title,
            media_type=media_type,
            title=title,
            title_norm=normalize_title(title),
            original_title=orig,
            orig_norm=normalize_title(orig),
            year=_safe_opt_int(raw.get("year")),
            rating=round(rating, 1),
            duration_ms=_safe_int(raw.get("duration")),
            view_count=_safe_int(raw.get("viewCount")),
            view_offset_ms=_safe_int(raw.get("viewOffset")),
            last_viewed_at=_safe_int(raw.get("lastViewedAt")),
            genres=tuple(genres),
            actors=tuple(actors),
            directors=tuple(directors),
            summary=str(raw.get("summary") or "").strip(),
            thumb=str(raw.get("thumb") or "").strip(),
            parent_title=str(raw.get("parentTitle") or "").strip(),
            grandparent_title=str(raw.get("grandparentTitle") or "").strip(),
            season_index=_safe_opt_int(raw.get("parentIndex")),
            episode_index=_safe_opt_int(raw.get("index")),
        )

    async def async_fetch_full_index(self) -> PlexIndexData:
        """Fetch all movie/show/music libraries, genres, items, and playlists."""
        index = PlexIndexData()
        sections_container = await self._async_get_container("/library/sections")
        directories = sections_container.get("Directory") or []

        all_genres_set: set[str] = set()

        for sec in directories:
            raw_type = str(sec.get("type") or "").lower()
            if raw_type == "artist":
                lib_type = "music"
            elif raw_type in ("movie", "show"):
                lib_type = raw_type
            else:
                continue

            sec_id = str(sec.get("key") or "").strip()
            sec_title = str(sec.get("title") or "").strip()
            if not sec_id or not sec_title:
                continue

            sec_genres: list[str] = []
            try:
                genre_container = await self._async_get_container(
                    f"/library/sections/{sec_id}/genre"
                )
                for g_dir in genre_container.get("Directory") or []:
                    g_title = str(g_dir.get("title") or "").strip()
                    if g_title:
                        sec_genres.append(g_title)
                        all_genres_set.add(g_title)
            except PlexApiError as err:
                _LOGGER.debug(
                    "Skipping genre fetch for section %s: %s",
                    sec_id,
                    redact_sensitive_text(err),
                )

            index.sections.append(
                PlexLibrarySection(
                    section_id=sec_id,
                    title=sec_title,
                    lib_type=lib_type,
                    genres=sec_genres,
                )
            )

            try:
                items_container = await self._async_get_container(
                    f"/library/sections/{sec_id}/all",
                    params={"excludeElements": "Media,Part,Country,Writer,Producer"},
                )
            except PlexApiError as err:
                _LOGGER.warning(
                    "Failed to fetch section %s (%s): %s",
                    sec_id,
                    sec_title,
                    redact_sensitive_text(err),
                )
                continue

            raw_items = (
                items_container.get("Metadata")
                or items_container.get("Directory")
                or []
            )
            for raw_item in raw_items:
                parsed = self._parse_media_item(raw_item, sec_id, sec_title, lib_type)
                if parsed is None:
                    continue
                index.items_by_type[lib_type].append(parsed)
                index.items_by_key[parsed.rating_key] = parsed
                for g in parsed.genres:
                    all_genres_set.add(g)

        try:
            pl_container = await self._async_get_container("/playlists")
            for raw_pl in pl_container.get("Metadata") or []:
                rk = _safe_int(raw_pl.get("ratingKey"))
                title = str(raw_pl.get("title") or "").strip()
                if not rk or not title:
                    continue
                index.playlists.append(
                    PlexPlaylistItem(
                        rating_key=rk,
                        title=title,
                        title_norm=normalize_title(title),
                        playlist_type=str(raw_pl.get("playlistType") or "video"),
                        leaf_count=_safe_int(raw_pl.get("leafCount")),
                    )
                )
        except PlexApiError as err:
            _LOGGER.debug("Failed to fetch playlists: %s", redact_sensitive_text(err))

        index.all_genres = sorted(all_genres_set)
        return index

    async def async_get_active_sessions(self) -> list[PlexActiveSession]:
        """Return currently active/paused Plex playback sessions."""
        container = await self._async_get_container("/status/sessions")
        sessions: list[PlexActiveSession] = []
        for raw in container.get("Metadata") or []:
            rk = _safe_int(raw.get("ratingKey"))
            if not rk:
                continue
            player = raw.get("Player") or {}
            session_info = raw.get("Session") or {}
            m_type = str(raw.get("type") or "movie").lower()
            title = str(raw.get("title") or "").strip()
            grandparent = str(raw.get("grandparentTitle") or "").strip()
            season_idx = _safe_opt_int(raw.get("parentIndex"))
            ep_idx = _safe_opt_int(raw.get("index"))

            if m_type == "episode" and grandparent:
                ep_code = (
                    f" S{season_idx:02d}E{ep_idx:02d}"
                    if season_idx is not None and ep_idx is not None
                    else ""
                )
                display_title = (
                    f"{grandparent}{ep_code} — {title}"
                    if title
                    else f"{grandparent}{ep_code}"
                )
            else:
                display_title = title

            sessions.append(
                PlexActiveSession(
                    session_key=str(
                        raw.get("sessionKey") or session_info.get("id") or rk
                    ),
                    rating_key=rk,
                    media_type=m_type,
                    title=title,
                    display_title=display_title,
                    view_offset_ms=_safe_int(raw.get("viewOffset")),
                    player_machine_id=str(
                        player.get("machineIdentifier") or ""
                    ).strip(),
                    player_title=str(player.get("title") or "").strip(),
                    player_state=str(player.get("state") or "playing").lower(),
                    library_title=str(raw.get("librarySectionTitle") or "").strip(),
                )
            )
        return sessions

    async def async_get_continue_watching(self) -> list[PlexMediaItem]:
        """Fetch Continue Watching / On Deck items from the Plex server."""
        raw_items: list[dict[str, Any]] = []
        try:
            container = await self._async_get_container("/hubs/continueWatching")
            raw_items = container.get("Metadata") or []
            if not raw_items:
                for hub in container.get("Hub") or []:
                    raw_items.extend(hub.get("Metadata") or [])
        except PlexApiError:
            raw_items = []

        if not raw_items:
            try:
                container = await self._async_get_container("/library/onDeck")
                raw_items = container.get("Metadata") or []
            except PlexApiError:
                return []

        results: list[PlexMediaItem] = []
        for raw in raw_items:
            m_type = str(raw.get("type") or "movie").lower()
            sec_id = str(raw.get("librarySectionID") or "")
            lib_title = str(raw.get("librarySectionTitle") or "")
            parsed = self._parse_media_item(raw, sec_id, lib_title, m_type)
            if parsed is not None:
                results.append(parsed)
        return results

    async def async_resolve_episode(
        self,
        show_rating_key: int,
        season: int | None = None,
        episode: int | None = None,
        resume_unwatched: bool = False,
        shuffle: bool = False,
    ) -> tuple[int, str, int] | None:
        """Resolve exact episode (rating_key, display_title, offset_sec) for Start vs Resume."""
        container = await self._async_get_container(
            f"/library/metadata/{show_rating_key}/allLeaves",
            params={"excludeElements": "Media,Part"},
        )
        leaves = container.get("Metadata") or []
        if not leaves:
            return None

        def _format_ep(
            raw_ep: dict[str, Any], *, keep_offset: bool
        ) -> tuple[int, str, int]:
            rk = _safe_int(raw_ep.get("ratingKey"))
            show_name = str(raw_ep.get("grandparentTitle") or "").strip()
            ep_title = str(raw_ep.get("title") or "").strip()
            s_idx = _safe_opt_int(raw_ep.get("parentIndex"))
            e_idx = _safe_opt_int(raw_ep.get("index"))
            code = (
                f"S{s_idx:02d}E{e_idx:02d}"
                if s_idx is not None and e_idx is not None
                else ""
            )
            label = " ".join(
                part
                for part in (show_name, code, f"— {ep_title}" if ep_title else "")
                if part
            )
            offset_sec = (
                (_safe_int(raw_ep.get("viewOffset")) // 1000) if keep_offset else 0
            )
            return rk, label or ep_title, offset_sec

        # 1. Explicit episode (with or without season)
        if episode is not None:
            for raw_ep in leaves:
                s_idx = _safe_opt_int(raw_ep.get("parentIndex"))
                e_idx = _safe_opt_int(raw_ep.get("index"))
                if season is not None and s_idx != season:
                    continue
                if e_idx != episode:
                    continue
                return _format_ep(raw_ep, keep_offset=resume_unwatched)
            return None

        # 2. Specific season without episode number
        if season is not None:
            season_leaves = [
                ep for ep in leaves if _safe_opt_int(ep.get("parentIndex")) == season
            ]
            if not season_leaves:
                return None
            if shuffle:
                return _format_ep(random.choice(season_leaves), keep_offset=False)
            if resume_unwatched:
                in_prog = [
                    ep for ep in season_leaves if _safe_int(ep.get("viewOffset")) > 0
                ]
                if in_prog:
                    in_prog.sort(
                        key=lambda ep: _safe_int(ep.get("lastViewedAt")), reverse=True
                    )
                    return _format_ep(in_prog[0], keep_offset=True)
                for raw_ep in season_leaves:
                    if _safe_int(raw_ep.get("viewCount")) == 0:
                        return _format_ep(raw_ep, keep_offset=True)
            return _format_ep(season_leaves[0], keep_offset=False)

        # 3. Whole show: prefer regular seasons (>=1) over Season 00 Specials
        regular_leaves = [
            ep for ep in leaves if (_safe_opt_int(ep.get("parentIndex")) or 0) >= 1
        ] or leaves

        if resume_unwatched:
            by_rk = {_safe_int(ep.get("ratingKey")): ep for ep in leaves}
            try:
                cw_items = await self.async_get_continue_watching()
                for cw in cw_items:
                    if cw.rating_key in by_rk:
                        return _format_ep(by_rk[cw.rating_key], keep_offset=True)
            except PlexApiError:
                pass

            in_progress = [
                ep for ep in regular_leaves if _safe_int(ep.get("viewOffset")) > 0
            ]
            if in_progress:
                in_progress.sort(
                    key=lambda ep: _safe_int(ep.get("lastViewedAt")), reverse=True
                )
                return _format_ep(in_progress[0], keep_offset=True)
            for raw_ep in regular_leaves:
                if _safe_int(raw_ep.get("viewCount")) == 0:
                    return _format_ep(raw_ep, keep_offset=True)

        return _format_ep(regular_leaves[0], keep_offset=False)

    async def async_hub_search(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Perform fast server-side /hubs/search for actors, directors, tracks, albums."""
        if not query.strip():
            return []
        try:
            container = await self._async_get_container(
                "/hubs/search",
                params={"query": query.strip(), "limit": str(limit)},
            )
        except PlexApiError as err:
            _LOGGER.debug(
                "Hub search failed for '%s': %s", query, redact_sensitive_text(err)
            )
            return []

        results: list[dict[str, Any]] = []
        for hub in container.get("Hub") or []:
            hub_type = str(hub.get("type") or "").lower()
            for meta in hub.get("Metadata") or []:
                results.append(
                    {
                        "hub_type": hub_type,
                        "type": str(meta.get("type") or hub_type).lower(),
                        "rating_key": _safe_opt_int(meta.get("ratingKey")),
                        "tag": str(meta.get("tag") or meta.get("title") or "").strip(),
                        "title": str(
                            meta.get("title") or meta.get("tag") or ""
                        ).strip(),
                        "year": _safe_opt_int(meta.get("year")),
                        "library_title": str(
                            meta.get("librarySectionTitle") or ""
                        ).strip(),
                        "grandparent_title": str(
                            meta.get("grandparentTitle") or ""
                        ).strip(),
                        "parent_title": str(meta.get("parentTitle") or "").strip(),
                    }
                )
        return results

    async def async_fetch_poster_bytes(
        self,
        thumb_path: str,
        width: int = 480,
        height: int = 720,
    ) -> bytes | None:
        """Download transcoded poster JPEG bytes locally without exposing the Plex token."""
        if not thumb_path:
            return None
        encoded_thumb = quote(thumb_path, safe="")
        url = (
            f"{self._base_url}/photo/:/transcode"
            f"?width={width}&height={height}&minSize=1&upscale=1&url={encoded_thumb}"
        )
        session = self._session()
        try:
            async with session.get(
                url,
                headers={"X-Plex-Token": self._token},
                timeout=_POSTER_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                return data if data else None
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Poster fetch failed for %s: %s", thumb_path, redact_sensitive_text(err))
            return None

    async def async_get_item_playback_state(
        self, rating_key: int
    ) -> tuple[int, int] | None:
        """Fetch live (view_offset_ms, duration_ms) directly from /library/metadata/{id}."""
        try:
            container = await self._async_get_container(
                f"/library/metadata/{int(rating_key)}",
                params={"excludeElements": "Media,Part"},
            )
        except PlexApiError:
            return None
        items = container.get("Metadata") or []
        if not items:
            return None
        raw = items[0]
        return _safe_int(raw.get("viewOffset")), _safe_int(raw.get("duration"))

    async def async_set_playback_progress(
        self, rating_key: int, offset_ms: int, duration_ms: int = 0
    ) -> bool:
        """Sync saved viewOffset on Plex server via GET /:/unscrobble|/:/scrobble or /:/timeline."""
        session = self._session()
        clean_offset = max(0, int(offset_ms))
        headers = self._headers()
        ok = False

        if clean_offset == 0:
            # In Plex Media Server, /:/progress?time=0 is ignored and PUT /:/unscrobble fails.
            # Official plexapi resets viewOffset=0 via HTTP GET /:/scrobble (keeps viewCount>0)
            # or HTTP GET /:/unscrobble (when viewCount==0).
            view_count = 0
            try:
                meta = await self._async_get_container(
                    f"/library/metadata/{int(rating_key)}",
                    params={"excludeElements": "Media,Part"},
                )
                items = meta.get("Metadata") or []
                if items:
                    view_count = _safe_int(items[0].get("viewCount"))
            except PlexApiError:
                view_count = 0

            reset_endpoint = "/:/scrobble" if view_count > 0 else "/:/unscrobble"
            try:
                async with session.get(
                    f"{self._base_url}{reset_endpoint}",
                    headers=headers,
                    params={
                        "key": str(rating_key),
                        "identifier": "com.plexapp.plugins.library",
                    },
                    timeout=_DEFAULT_TIMEOUT,
                ) as resp:
                    return resp.status < 400
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug(
                    "Failed to reset Plex progress via %s: %s",
                    reset_endpoint,
                    redact_sensitive_text(err),
                )
                return False

        try:
            est_duration = max(int(duration_ms), clean_offset + 60000)
            async with session.post(
                f"{self._base_url}/:/timeline",
                headers=headers,
                params={
                    "ratingKey": str(rating_key),
                    "key": f"/library/metadata/{int(rating_key)}",
                    "state": "paused",
                    "time": str(clean_offset),
                    "duration": str(est_duration),
                },
                timeout=_DEFAULT_TIMEOUT,
            ) as resp:
                ok = resp.status < 400
        except (aiohttp.ClientError, TimeoutError):
            ok = False

        try:
            async with session.get(
                f"{self._base_url}/:/progress",
                headers=headers,
                params={
                    "key": str(rating_key),
                    "identifier": "com.plexapp.plugins.library",
                    "time": str(clean_offset),
                    "state": "stopped",
                },
                timeout=_DEFAULT_TIMEOUT,
            ) as resp:
                return ok or resp.status < 400
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug(
                "Failed to update Plex progress: %s", redact_sensitive_text(err)
            )
            return ok

    async def async_send_client_playback_command(
        self,
        client_machine_id: str,
        command: str,
        params: dict[str, str] | None = None,
        media_type: str = "video",
    ) -> bool:
        """Send a PMS Companion /player/playback/<command> request with integer commandID."""
        machine_id = str(client_machine_id or "").strip()
        cmd = str(command or "").strip().lstrip("/")
        if not machine_id or not cmd:
            return False

        self._command_id += 1
        mtype = (
            "music"
            if str(media_type or "").strip().lower() in ("music", "track", "audio")
            else "video"
        )
        headers = {
            **self._headers(),
            "X-Plex-Target-Client-Identifier": machine_id,
            "X-Plex-Device-Name": PLEX_PRODUCT_NAME,
        }
        req_params: dict[str, str] = {
            **(params or {}),
            "type": mtype,
            "commandID": str(self._command_id),
        }
        session = self._session()
        try:
            async with session.get(
                f"{self._base_url}/player/playback/{cmd}",
                headers=headers,
                params=req_params,
                timeout=_SEEK_TIMEOUT,
            ) as resp:
                return resp.status < 400
        except TimeoutError:
            # PMS Companion forwards commands over WebSocket in <50ms while HTTP proxy response may hang.
            return True
        except aiohttp.ClientError as err:
            _LOGGER.debug(
                "Direct Plex %s warning: %s", cmd, redact_sensitive_text(err)
            )
            return False

    async def async_seek_client_player(
        self,
        client_machine_id: str,
        offset_ms: int,
        media_type: str = "video",
    ) -> bool:
        """Send strict integer /player/playback/seekTo via PMS Companion proxy.

        Avoids HA Core PlexMediaPlayer.media_seek's float `position * 1000` ('1801000.0')
        serialization bug on Samsung Tizen / LG webOS and works even when HA's entity
        currently has player_source='session'. Call strictly AFTER playMedia proxy finishes.
        """
        machine_id = str(client_machine_id or "").strip()
        if not machine_id or int(offset_ms) < 0:
            return False
        clean_offset_ms = max(0, int(offset_ms))
        return await self.async_send_client_playback_command(
            machine_id,
            "seekTo",
            {"offset": str(clean_offset_ms)},
            media_type=media_type,
        )

    async def async_find_person_item_keys(
        self,
        section_ids: list[str],
        person_variants: list[str],
        role_type: str = "actor",
    ) -> set[int]:
        """Return all item ratingKeys in given sections matching a person (full cast in Plex DB)."""
        variants = [str(v).strip() for v in person_variants if v and str(v).strip()]
        if not section_ids or not variants:
            return set()

        found_keys: set[int] = set()
        endpoint_role = "director" if role_type == "director" else "actor"

        for sec_id in section_ids:
            try:
                dir_container = await self._async_get_container(
                    f"/library/sections/{sec_id}/{endpoint_role}"
                )
            except PlexApiError as err:
                _LOGGER.debug(
                    "Person directory lookup failed on section %s (%s): %s",
                    sec_id,
                    endpoint_role,
                    redact_sensitive_text(err),
                )
                continue

            matched_tag_ids: list[str] = []
            for entry in dir_container.get("Directory") or []:
                tag_id = str(entry.get("key") or "").strip()
                tag_title = str(entry.get("title") or "").strip()
                if not tag_id or not tag_title or not tag_id.isdigit():
                    continue
                if person_matches(variants, (tag_title,)):
                    matched_tag_ids.append(tag_id)

            for tag_id in matched_tag_ids[:4]:
                try:
                    items_container = await self._async_get_container(
                        f"/library/sections/{sec_id}/all",
                        params={endpoint_role: tag_id},
                    )
                except PlexApiError:
                    continue
                raw_items = (
                    items_container.get("Metadata")
                    or items_container.get("Directory")
                    or []
                )
                for raw in raw_items:
                    rk = _safe_opt_int(raw.get("ratingKey"))
                    if rk:
                        found_keys.add(rk)

        return found_keys

    async def async_intersect_people(
        self,
        section_ids: list[str],
        actor_groups: list[list[str]],
        director_variants: list[str] | None = None,
    ) -> set[int]:
        """Find strict AND intersection of ratingKeys across multiple actors and/or director."""
        tasks = [
            self.async_find_person_item_keys(section_ids, grp, "actor")
            for grp in actor_groups
            if grp
        ]
        if director_variants:
            tasks.append(
                self.async_find_person_item_keys(
                    section_ids, director_variants, "director"
                )
            )
        if not tasks:
            return set()
        sets = await asyncio.gather(*tasks)
        return set.intersection(*sets) if sets else set()

    async def async_get_similar_item_keys(
        self, rating_key: int, count: int = 15
    ) -> list[int]:
        """Fetch similar item ratingKeys via official PMS /library/metadata/{id}/similar."""
        try:
            container = await self._async_get_container(
                f"/library/metadata/{int(rating_key)}/similar",
                params={"count": str(count), "excludeElements": "Media,Part"},
            )
        except PlexApiError:
            return []
        out: list[int] = []
        for raw in container.get("Metadata") or []:
            rk = _safe_opt_int(raw.get("ratingKey"))
            if rk and rk != rating_key and rk not in out:
                out.append(rk)
        return out

    async def async_resolve_music_target(
        self,
        *,
        artist: str | None = None,
        album: str | None = None,
        track: str | None = None,
        resume: bool = False,
    ) -> tuple[int, str, str, int] | None:
        """Resolve (rating_key, item_kind, label, offset_sec) for track or album."""
        artist_norm = normalize_title(artist)
        album_norm = normalize_title(album)

        if track:
            hits = await self.async_hub_search(track, limit=10)
            for hit in hits:
                if hit.get("type") != "track" or not hit.get("rating_key"):
                    continue
                gp_norm = normalize_title(hit.get("grandparent_title"))
                p_norm = normalize_title(hit.get("parent_title"))
                if (
                    artist_norm
                    and gp_norm
                    and artist_norm not in gp_norm
                    and gp_norm not in artist_norm
                ):
                    continue
                if (
                    album_norm
                    and p_norm
                    and album_norm not in p_norm
                    and p_norm not in album_norm
                ):
                    continue
                rk = int(hit["rating_key"])
                off_sec = 0
                if resume:
                    state = await self.async_get_item_playback_state(rk)
                    if state:
                        off_sec = state[0] // 1000
                label = (
                    f"{hit['grandparent_title']} — {hit['title']}"
                    if hit.get("grandparent_title")
                    else str(hit.get("title") or track)
                )
                return rk, "track", label, off_sec

        if album:
            hits = await self.async_hub_search(album, limit=10)
            for hit in hits:
                if hit.get("type") != "album" or not hit.get("rating_key"):
                    continue
                p_norm = normalize_title(hit.get("parent_title"))
                if (
                    artist_norm
                    and p_norm
                    and artist_norm not in p_norm
                    and p_norm not in artist_norm
                ):
                    continue
                album_rk = int(hit["rating_key"])
                label = (
                    f"{hit['parent_title']} — {hit['title']}"
                    if hit.get("parent_title")
                    else str(hit.get("title") or album)
                )
                if resume:
                    try:
                        ch_cont = await self._async_get_container(
                            f"/library/metadata/{album_rk}/children",
                            params={"excludeElements": "Media,Part"},
                        )
                        tracks = ch_cont.get("Metadata") or []
                        in_prog = [
                            t for t in tracks if _safe_int(t.get("viewOffset")) > 0
                        ]
                        if in_prog:
                            t0 = in_prog[0]
                            t_rk = _safe_int(t0.get("ratingKey"))
                            t_off = _safe_int(t0.get("viewOffset")) // 1000
                            if t_rk:
                                return (
                                    t_rk,
                                    "track",
                                    f"{label} — {t0.get('title', '')}".strip(" —"),
                                    t_off,
                                )
                        for t in tracks:
                            if _safe_int(t.get("viewCount")) == 0 and _safe_int(
                                t.get("ratingKey")
                            ):
                                return (
                                    _safe_int(t["ratingKey"]),
                                    "track",
                                    f"{label} — {t.get('title', '')}".strip(" —"),
                                    0,
                                )
                    except PlexApiError:
                        pass
                return album_rk, "album", label, 0

        return None




