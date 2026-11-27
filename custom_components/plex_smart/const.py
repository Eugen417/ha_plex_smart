"""Constants for the SmartPlex integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "plex_smart"
PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

LLM_API_ID = "plex_smart"
LLM_API_NAME = "SmartPlex"

PLEX_URI_SCHEME = "plex://"
PLEX_CLIENT_IDENTIFIER = "homeassistant-smartplex"
PLEX_PRODUCT_NAME = "SmartPlex"
SAMSUNG_TIZEN_PLEX_APP_ID = "3201512006963"
SAMSUNG_MIN_RETRY_COUNT = 6
SAMSUNG_MIN_RETRY_DELAY_SEC = 5.0
LG_MIN_RETRY_COUNT = 4
LG_MIN_RETRY_DELAY_SEC = 3.0
LG_COLD_MIN_BOOT_WAIT_SEC = 15.0
ATV_COLD_MIN_CARD_DELAY_SEC = 4.0
ATV_WARM_MIN_CARD_DELAY_SEC = 2.2
PROXY_READ_TIMEOUT_GRACE_SEC = 6.0
POST_PLAY_SPINUP_WAIT_SEC = 18.0
RESUME_SPINUP_TIMEOUT_SEC = 45.0
RESUME_WIPE_GUARD_SEC = 65
RESUME_SEEK_MAX_ATTEMPTS = 5
RESUME_SEEK_CALL_TIMEOUT_SEC = 3.5
RESUME_SEEK_VERIFY_WAIT_SEC = 3.0
SEEK_PROXY_TIMEOUT_SEC = 5.0
SAMSUNG_RESUME_INITIAL_SETTLE_SEC = 2.5
LG_WEBOS_RESUME_INITIAL_SETTLE_SEC = 2.5
MEDIA_STOP_TIMEOUT_SEC = 2.5

# Server ConfigEntry keys
CONF_URL = "url"
CONF_TOKEN = "token"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SERVER_ID = "server_id"
CONF_SERVER_NAME = "server_name"
CONF_SCAN_BUTTON = "scan_button"
CONF_AI_ENTITY = "ai_entity"
CONF_CACHE_INTERVAL = "cache_interval"
CONF_USER_TITLE = "user_title"

DEFAULT_VERIFY_SSL = False
DEFAULT_CACHE_INTERVAL = 60  # minutes
DEFAULT_USER_TITLE = ""

# ConfigSubentry types
SUBENTRY_TYPE_ZONE = "zone"
SUBENTRY_TYPE_ROULETTE = "roulette"

# Zone subentry keys
CONF_ZONE_NAME = "zone_name"
CONF_AREA_ID = "area_id"
CONF_ALIASES = "aliases"
CONF_PLEX_CLIENT = "plex_client"
CONF_PLEX_DEVICE_ID = "plex_device_id"
CONF_POWER_PROFILE = "power_profile"
CONF_HARDWARE_ENTITY = "hardware_entity"
CONF_HARDWARE_DEVICE_ID = "hardware_device_id"
CONF_REMOTE_ENTITY = "remote_entity"
CONF_DISPLAY_DEVICE_ID = "display_device_id"
CONF_DISPLAY_ENTITY = "display_entity"
CONF_DISPLAY_SOURCE = "display_source"
CONF_APP_SOURCE = "app_source"
CONF_TURN_OFF_ON_HANDOFF = "turn_off_on_handoff"
CONF_ATV_AUTO_SELECT = "atv_auto_select"
CONF_IS_DEFAULT = "is_default"

CONF_COLD_BOOT_TIMEOUT = "cold_boot_timeout"
CONF_COLD_OS_DELAY = "cold_os_delay"
CONF_COLD_APP_DELAY = "cold_app_delay"
CONF_WARM_APP_DELAY = "warm_app_delay"
CONF_HOT_DELAY = "hot_delay"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_SCAN_TIMEOUT = "scan_timeout"
CONF_PLAY_RETRY_COUNT = "play_retry_count"
CONF_PLAY_RETRY_DELAY = "play_retry_delay"
CONF_HANDOFF_REWIND = "handoff_rewind_sec"
CONF_OPTIMISTIC_MODE = "optimistic_mode"

CONF_CUSTOM_BOOT_ACTIONS = "custom_boot_actions"
CONF_CUSTOM_LAUNCH_ACTIONS = "custom_launch_actions"
CONF_CUSTOM_OFF_ACTIONS = "custom_off_actions"

DEFAULT_APP_SOURCE = "Plex"
DEFAULT_TURN_OFF_ON_HANDOFF = True
DEFAULT_HANDOFF_REWIND = 5
DEFAULT_OPTIMISTIC_MODE = True
DEFAULT_ATV_AUTO_SELECT = True
DEFAULT_IS_DEFAULT = False
DUPLICATE_LAUNCH_COOLDOWN_SEC = 15.0

# Startup phases
START_PHASE_COLD = "cold"
START_PHASE_WARM = "warm"
START_PHASE_HOT = "hot"

# Power profiles
PROFILE_APPLE_TV = "apple_tv"
PROFILE_LG_WEBOS = "lg_webos"
PROFILE_SAMSUNG_TIZEN = "samsung_tizen"
PROFILE_ANDROID_TV = "android_tv"
PROFILE_STANDARD_MEDIA_PLAYER = "standard_media_player"
PROFILE_CUSTOM_ACTIONS = "custom_actions"

POWER_PROFILES: list[str] = [
    PROFILE_APPLE_TV,
    PROFILE_LG_WEBOS,
    PROFILE_SAMSUNG_TIZEN,
    PROFILE_ANDROID_TV,
    PROFILE_STANDARD_MEDIA_PLAYER,
    PROFILE_CUSTOM_ACTIONS,
]

PROFILE_TIMING_DEFAULTS: dict[str, dict[str, int]] = {
    PROFILE_APPLE_TV: {
        CONF_COLD_BOOT_TIMEOUT: 4,
        CONF_COLD_OS_DELAY: 1,
        CONF_COLD_APP_DELAY: 4,
        CONF_WARM_APP_DELAY: 2,
        CONF_HOT_DELAY: 0,
        CONF_SCAN_INTERVAL: 2,
        CONF_SCAN_TIMEOUT: 24,
        CONF_PLAY_RETRY_COUNT: 3,
        CONF_PLAY_RETRY_DELAY: 2,
        CONF_HANDOFF_REWIND: DEFAULT_HANDOFF_REWIND,
    },
    PROFILE_LG_WEBOS: {
        CONF_COLD_BOOT_TIMEOUT: 15,
        CONF_COLD_OS_DELAY: 2,
        CONF_COLD_APP_DELAY: 6,
        CONF_WARM_APP_DELAY: 2,
        CONF_HOT_DELAY: 0,
        CONF_SCAN_INTERVAL: 2,
        CONF_SCAN_TIMEOUT: 30,
        CONF_PLAY_RETRY_COUNT: 3,
        CONF_PLAY_RETRY_DELAY: 2,
        CONF_HANDOFF_REWIND: DEFAULT_HANDOFF_REWIND,
    },
    PROFILE_SAMSUNG_TIZEN: {
        CONF_COLD_BOOT_TIMEOUT: 15,
        CONF_COLD_OS_DELAY: 4,
        CONF_COLD_APP_DELAY: 10,
        CONF_WARM_APP_DELAY: 6,
        CONF_HOT_DELAY: 0,
        CONF_SCAN_INTERVAL: 2,
        CONF_SCAN_TIMEOUT: 45,
        CONF_PLAY_RETRY_COUNT: 6,
        CONF_PLAY_RETRY_DELAY: 5,
        CONF_HANDOFF_REWIND: DEFAULT_HANDOFF_REWIND,
    },
    PROFILE_ANDROID_TV: {
        CONF_COLD_BOOT_TIMEOUT: 6,
        CONF_COLD_OS_DELAY: 2,
        CONF_COLD_APP_DELAY: 3,
        CONF_WARM_APP_DELAY: 2,
        CONF_HOT_DELAY: 0,
        CONF_SCAN_INTERVAL: 2,
        CONF_SCAN_TIMEOUT: 26,
        CONF_PLAY_RETRY_COUNT: 3,
        CONF_PLAY_RETRY_DELAY: 2,
        CONF_HANDOFF_REWIND: DEFAULT_HANDOFF_REWIND,
    },
    PROFILE_STANDARD_MEDIA_PLAYER: {
        CONF_COLD_BOOT_TIMEOUT: 8,
        CONF_COLD_OS_DELAY: 2,
        CONF_COLD_APP_DELAY: 3,
        CONF_WARM_APP_DELAY: 2,
        CONF_HOT_DELAY: 0,
        CONF_SCAN_INTERVAL: 2,
        CONF_SCAN_TIMEOUT: 30,
        CONF_PLAY_RETRY_COUNT: 3,
        CONF_PLAY_RETRY_DELAY: 2,
        CONF_HANDOFF_REWIND: DEFAULT_HANDOFF_REWIND,
    },
    PROFILE_CUSTOM_ACTIONS: {
        CONF_COLD_BOOT_TIMEOUT: 8,
        CONF_COLD_OS_DELAY: 2,
        CONF_COLD_APP_DELAY: 3,
        CONF_WARM_APP_DELAY: 2,
        CONF_HOT_DELAY: 0,
        CONF_SCAN_INTERVAL: 2,
        CONF_SCAN_TIMEOUT: 30,
        CONF_PLAY_RETRY_COUNT: 3,
        CONF_PLAY_RETRY_DELAY: 2,
        CONF_HANDOFF_REWIND: DEFAULT_HANDOFF_REWIND,
    },
}

# Roulette subentry keys
CONF_ROULETTE_NAME = "roulette_name"
CONF_LIBRARIES = "libraries"
CONF_WATCH_FILTER = "watch_filter"
CONF_INCLUDE_GENRES = "include_genres"
CONF_EXCLUDE_GENRES = "exclude_genres"
CONF_MIN_RATING = "min_rating"
CONF_YEAR_FROM = "year_from"
CONF_MAX_DURATION_MIN = "max_duration_min"
CONF_PICK_COUNT = "pick_count"
CONF_COLLAGE_LAYOUT = "collage_layout"
CONF_ANTI_REPEAT_DAYS = "anti_repeat_days"
CONF_DEFAULT_ZONE = "default_zone"

WATCH_FILTER_UNWATCHED = "unwatched"
WATCH_FILTER_IN_PROGRESS = "in_progress"
WATCH_FILTER_WATCHED = "watched"
WATCH_FILTER_ANY = "any"

WATCH_FILTERS: list[str] = [
    WATCH_FILTER_UNWATCHED,
    WATCH_FILTER_IN_PROGRESS,
    WATCH_FILTER_WATCHED,
    WATCH_FILTER_ANY,
]

COLLAGE_LAYOUT_AUTO = "auto"
COLLAGE_LAYOUT_GRID_2X2 = "grid_2x2"
COLLAGE_LAYOUT_ROW = "row"
COLLAGE_LAYOUT_SINGLE = "single"

COLLAGE_LAYOUTS: list[str] = [
    COLLAGE_LAYOUT_AUTO,
    COLLAGE_LAYOUT_GRID_2X2,
    COLLAGE_LAYOUT_ROW,
    COLLAGE_LAYOUT_SINGLE,
]

DEFAULT_WATCH_FILTER = WATCH_FILTER_UNWATCHED
DEFAULT_MIN_RATING = 6.5
DEFAULT_YEAR_FROM = 1990
DEFAULT_MAX_DURATION_MIN = 0
DEFAULT_PICK_COUNT = 4
DEFAULT_COLLAGE_LAYOUT = COLLAGE_LAYOUT_AUTO
DEFAULT_ANTI_REPEAT_DAYS = 14

# Services
SERVICE_COMMAND = "command"
SERVICE_PLAY = "play"
SERVICE_HANDOFF = "handoff"
SERVICE_CONTROL = "control"
SERVICE_SPIN_ROULETTE = "spin_roulette"
SERVICE_REFRESH_CACHE = "refresh_cache"

# Playback control actions and repeat modes
CONTROL_ACTIONS: tuple[str, ...] = (
    "pause",
    "resume_play",
    "play_pause",
    "stop",
    "seek_relative",
    "seek_to",
    "restart",
    "next",
    "previous",
    "set_repeat",
    "set_shuffle",
)
REPEAT_MODES: tuple[str, ...] = ("off", "one", "all")

# Dispatcher signals
SIGNAL_LAST_LAUNCH_UPDATED = f"{DOMAIN}_last_launch_updated"

# States considered offline / ready for Plex clients
OFFLINE_CLIENT_STATES = frozenset({"unavailable", "unknown", "off"})
ACTIVE_PLAYBACK_STATES = frozenset({"playing", "buffering", "paused"})

# HTTP View for roulette collage images
ROULETTE_IMAGE_URL_PREFIX = "/api/plex_smart/roulette"
ROULETTE_MAX_CACHED_IMAGES = 10
ROULETTE_IMAGE_SIGN_TTL_SEC = 300
