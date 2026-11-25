"""Data models for SmartPlex."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import PlexApiClient
    from .coordinator import SmartPlexCoordinator
    from .roulette import RouletteHistoryStore


@dataclass(slots=True)
class PlexLibrarySection:
    """Represents a Plex library section."""

    section_id: str
    title: str
    lib_type: str  # "movie", "show", "music"
    genres: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PlexMediaItem:
    """Represents a cached Plex media item (movie, show, artist, or episode)."""

    rating_key: int
    section_id: str
    library_title: str
    media_type: str  # "movie", "show", "music", "episode"
    title: str
    title_norm: str
    original_title: str
    orig_norm: str
    year: int | None = None
    rating: float = 0.0
    duration_ms: int = 0
    view_count: int = 0
    view_offset_ms: int = 0
    last_viewed_at: int = 0
    genres: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()
    directors: tuple[str, ...] = ()
    summary: str = ""
    thumb: str = ""
    parent_title: str = ""
    grandparent_title: str = ""
    season_index: int | None = None
    episode_index: int | None = None


@dataclass(slots=True)
class PlexPlaylistItem:
    """Represents a cached Plex playlist."""

    rating_key: int
    title: str
    title_norm: str
    playlist_type: str  # "video", "audio"
    leaf_count: int = 0


@dataclass(slots=True)
class PlexActiveSession:
    """Represents an active playback session from /status/sessions."""

    session_key: str
    rating_key: int
    media_type: str
    title: str
    display_title: str
    view_offset_ms: int
    player_machine_id: str
    player_title: str
    player_state: str
    library_title: str = ""


@dataclass(slots=True)
class PlexIndexData:
    """Aggregated index fetched by SmartPlexCoordinator."""

    sections: list[PlexLibrarySection] = field(default_factory=list)
    items_by_type: dict[str, list[PlexMediaItem]] = field(
        default_factory=lambda: {"movie": [], "show": [], "music": []}
    )
    items_by_key: dict[int, PlexMediaItem] = field(default_factory=dict)
    playlists: list[PlexPlaylistItem] = field(default_factory=list)
    all_genres: list[str] = field(default_factory=list)
    active_sessions: list[PlexActiveSession] = field(default_factory=list)


@dataclass(slots=True)
class LaunchResult:
    """Outcome of a playback or handoff execution."""

    success: bool
    zone_name: str
    matched_title: str
    rating_key: int | None
    media_type: str
    boot_seconds: float
    retries_used: int
    handoff_from: str | None
    message: str
    suggestions: list[str] = field(default_factory=list)
    start_phase: str = "hot"


@dataclass
class PlexSmartRuntimeData:
    """Runtime objects stored on ConfigEntry.runtime_data."""

    coordinator: SmartPlexCoordinator
    api: PlexApiClient
    roulette_store: RouletteHistoryStore
    last_launch: LaunchResult | None = None
    zone_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    recent_zone_launches: dict[str, tuple[float, str, LaunchResult]] = field(
        default_factory=dict
    )
    active_zone_tasks: dict[str, asyncio.Task[object]] = field(default_factory=dict)
    saved_resume_offsets: dict[int, int] = field(default_factory=dict)
    playback_anchors: dict[int, tuple[float, int, str]] = field(default_factory=dict)
