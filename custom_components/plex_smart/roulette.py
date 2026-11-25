"""Configurable Movie Roulette: filtering, anti-repeat storage, Pillow collage, and HTTP view."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
import io
import json
import logging
import os
import random
import secrets
import time
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    COLLAGE_LAYOUT_AUTO,
    COLLAGE_LAYOUT_GRID_2X2,
    COLLAGE_LAYOUT_ROW,
    COLLAGE_LAYOUT_SINGLE,
    CONF_ANTI_REPEAT_DAYS,
    CONF_COLLAGE_LAYOUT,
    CONF_DEFAULT_ZONE,
    CONF_EXCLUDE_GENRES,
    CONF_INCLUDE_GENRES,
    CONF_LIBRARIES,
    CONF_MAX_DURATION_MIN,
    CONF_MIN_RATING,
    CONF_PICK_COUNT,
    CONF_ROULETTE_NAME,
    CONF_WATCH_FILTER,
    CONF_YEAR_FROM,
    CONF_ZONE_NAME,
    DEFAULT_ANTI_REPEAT_DAYS,
    DEFAULT_COLLAGE_LAYOUT,
    DEFAULT_MAX_DURATION_MIN,
    DEFAULT_MIN_RATING,
    DEFAULT_PICK_COUNT,
    DEFAULT_WATCH_FILTER,
    DEFAULT_YEAR_FROM,
    DOMAIN,
    ROULETTE_IMAGE_URL_PREFIX,
    ROULETTE_MAX_CACHED_IMAGES,
    WATCH_FILTER_ANY,
    WATCH_FILTER_IN_PROGRESS,
    WATCH_FILTER_UNWATCHED,
    WATCH_FILTER_WATCHED,
)
from .models import PlexMediaItem
from .search import normalize_title, resolve_roulette_preset, resolve_zone

_LOGGER = logging.getLogger(__name__)


class RouletteHistoryStore:
    """Persistent anti-repeat store tracking recently suggested rating_keys."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, float]] = Store(
            hass, 1, f"{DOMAIN}_roulette_{entry_id}"
        )
        self._history: dict[str, float] = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._history = {str(k): float(v) for k, v in data.items()}
        self._loaded = True

    def get_excluded_keys(self, days: int) -> set[int]:
        if days <= 0:
            return set()
        cutoff = time.time() - (days * 86400)
        return {
            int(k)
            for k, ts in self._history.items()
            if k.isdigit() and ts >= cutoff
        }

    async def async_record_shown(
        self, rating_keys: list[int], keep_days: int = 30
    ) -> None:
        await self.async_load()
        now = time.time()
        cutoff = now - (max(keep_days, 1) * 86400)
        self._history = {
            k: ts for k, ts in self._history.items() if ts >= cutoff
        }
        for rk in rating_keys:
            self._history[str(rk)] = now
        await self._store.async_save(self._history)


def filter_roulette_candidates(
    items: list[PlexMediaItem],
    *,
    libraries: list[str] | None = None,
    watch_filter: str = DEFAULT_WATCH_FILTER,
    include_genres: list[str] | None = None,
    exclude_genres: list[str] | None = None,
    min_rating: float = DEFAULT_MIN_RATING,
    year_from: int = DEFAULT_YEAR_FROM,
    max_duration_min: int = DEFAULT_MAX_DURATION_MIN,
    excluded_keys: set[int] | None = None,
) -> list[PlexMediaItem]:
    """Filter media items according to configurable roulette constraints."""
    lib_set = {normalize_title(x) for x in (libraries or []) if normalize_title(x)}
    inc_set = {normalize_title(x) for x in (include_genres or []) if normalize_title(x)}
    exc_set = {normalize_title(x) for x in (exclude_genres or []) if normalize_title(x)}
    excl_keys = excluded_keys or set()

    def _matches(item: PlexMediaItem, apply_anti_repeat: bool) -> bool:
        if apply_anti_repeat and item.rating_key in excl_keys:
            return False
        if lib_set and normalize_title(item.library_title) not in lib_set:
            return False
        if watch_filter == WATCH_FILTER_UNWATCHED and item.view_count > 0:
            return False
        if watch_filter == WATCH_FILTER_IN_PROGRESS and item.view_offset_ms <= 0:
            return False
        if watch_filter == WATCH_FILTER_WATCHED and item.view_count <= 0:
            return False
        if min_rating > 0 and item.rating > 0 and item.rating < min_rating:
            return False
        if year_from > 0 and item.year is not None and item.year < year_from:
            return False
        if max_duration_min > 0 and item.duration_ms > 0:
            if (item.duration_ms / 60000.0) > max_duration_min:
                return False

        item_genres = {normalize_title(g) for g in item.genres}
        if inc_set and not (item_genres & inc_set):
            return False
        if exc_set and (item_genres & exc_set):
            return False
        return True

    filtered = [it for it in items if _matches(it, apply_anti_repeat=True)]
    if not filtered and excl_keys:
        # Fallback if user exhausted the pool within the anti-repeat window
        filtered = [it for it in items if _matches(it, apply_anti_repeat=False)]
    return filtered


def _find_system_bold_font_path() -> str | None:
    """Locate a clean bold TrueType font across Alpine (HAOS), Debian, and macOS."""
    for path in (
        "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        if os.path.exists(path):
            return path

    for root_dir in ("/usr/share/fonts", "/usr/local/share/fonts"):
        if not os.path.isdir(root_dir):
            continue
        try:
            for dirpath, _, filenames in os.walk(root_dir):
                for fname in sorted(filenames):
                    low = fname.lower()
                    if low.endswith(".ttf") and "bold" in low and "italic" not in low:
                        return os.path.join(dirpath, fname)
        except OSError:
            continue
    return None


def build_poster_collage_sync(
    posters: list[tuple[bytes | None, int, str, float]],
    layout: str = DEFAULT_COLLAGE_LAYOUT,
) -> bytes:
    """Build a crisp numbered JPEG poster showcase using Pillow (runs in executor)."""
    from PIL import Image, ImageDraw, ImageFont

    count = max(1, len(posters))
    if layout == COLLAGE_LAYOUT_SINGLE or count == 1:
        cols, rows, tile_w, tile_h = 1, 1, 600, 900
    elif layout == COLLAGE_LAYOUT_ROW or (layout == COLLAGE_LAYOUT_AUTO and count <= 3):
        cols, rows, tile_w, tile_h = count, 1, 400, 600
    elif layout == COLLAGE_LAYOUT_GRID_2X2 or (layout == COLLAGE_LAYOUT_AUTO and count == 4):
        cols, rows, tile_w, tile_h = 2, 2, 400, 600
    else:
        cols, rows, tile_w, tile_h = 3, 2, 360, 540

    pad = 16
    canvas_w = cols * tile_w + (cols + 1) * pad
    canvas_h = rows * tile_h + (rows + 1) * pad
    canvas = Image.new("RGB", (canvas_w, canvas_h), (20, 20, 24))

    badge_size = max(68, round(tile_w * 0.20))
    margin = max(14, round(tile_w * 0.035))
    radius = max(14, round(badge_size * 0.24))
    border_w = max(3, round(badge_size * 0.045))
    num_font_size = max(44, round(badge_size * 0.66))
    stub_font_size = max(24, round(tile_w * 0.07))

    font_num: Any = None
    font_stub: Any = None
    num_stroke = 0
    is_tiny_bitmap_font = False

    bold_path = _find_system_bold_font_path()
    if bold_path:
        try:
            font_num = ImageFont.truetype(bold_path, num_font_size)
            font_stub = ImageFont.truetype(bold_path, stub_font_size)
            num_stroke = 1
        except OSError:
            font_num = None

    if font_num is None:
        try:
            font_num = ImageFont.load_default(size=num_font_size)
            font_stub = ImageFont.load_default(size=stub_font_size)
            num_stroke = max(2, round(num_font_size * 0.045))
        except (TypeError, ImportError, OSError):
            font_num = ImageFont.load_default()
            font_stub = font_num
            is_tiny_bitmap_font = True

    for idx, (raw_bytes, number, title, _rating) in enumerate(posters[: cols * rows]):
        col = idx % cols
        row = idx // cols
        x0 = pad + col * (tile_w + pad)
        y0 = pad + row * (tile_h + pad)

        tile: Image.Image | None = None
        if raw_bytes:
            try:
                with Image.open(io.BytesIO(raw_bytes)) as src:
                    tile = src.convert("RGB").resize(
                        (tile_w, tile_h), Image.Resampling.LANCZOS
                    )
            except Exception:  # noqa: BLE001
                tile = None

        if tile is None:
            tile = Image.new("RGB", (tile_w, tile_h), (42, 44, 54))
            d_stub = ImageDraw.Draw(tile)
            d_stub.text(
                (24, tile_h // 2 - 20),
                title[:28],
                fill=(235, 235, 245),
                font=font_stub,
            )

        canvas.paste(tile, (x0, y0))
        draw = ImageDraw.Draw(canvas, "RGBA")

        bx0 = x0 + margin
        by0 = y0 + margin
        bx1 = bx0 + badge_size
        by1 = by0 + badge_size

        # High-contrast drop shadow + Plex-gold rounded number badge with dark outline
        draw.rounded_rectangle(
            [bx0 + 3, by0 + 4, bx1 + 3, by1 + 4],
            radius=radius,
            fill=(0, 0, 0, 155),
        )
        draw.rounded_rectangle(
            [bx0, by0, bx1, by1],
            radius=radius,
            fill=(235, 168, 16, 255),
            outline=(18, 18, 24, 255),
            width=border_w,
        )

        num_str = str(number)
        if is_tiny_bitmap_font:
            tmp = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((2, 2), num_str, fill=(18, 18, 22, 255), font=font_num)
            crop_box = tmp.getbbox()
            if crop_box:
                glyph = tmp.crop(crop_box)
                target_h = max(32, round(badge_size * 0.56))
                target_w = max(16, round(glyph.width * (target_h / max(1, glyph.height))))
                scaled = glyph.resize((target_w, target_h), Image.Resampling.NEAREST)
                canvas.paste(
                    scaled,
                    (bx0 + (badge_size - target_w) // 2, by0 + (badge_size - target_h) // 2),
                    scaled,
                )
        else:
            bbox = draw.textbbox((0, 0), num_str, font=font_num, stroke_width=num_stroke)
            tx = (bx0 + bx1) / 2.0 - (bbox[0] + bbox[2]) / 2.0
            ty = (by0 + by1) / 2.0 - (bbox[1] + bbox[3]) / 2.0
            draw.text(
                (tx, ty),
                num_str,
                fill=(18, 18, 22, 255),
                font=font_num,
                stroke_width=num_stroke,
                stroke_fill=(18, 18, 22, 255),
            )

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()


class SmartPlexRouletteImageView(HomeAssistantView):
    """Serves generated roulette collage images without exposing Plex tokens."""

    url = f"{ROULETTE_IMAGE_URL_PREFIX}/{{image_id}}.jpg"
    name = "api:plex_smart:roulette"
    requires_auth = False

    def __init__(self) -> None:
        self._images: OrderedDict[str, bytes] = OrderedDict()

    def store_image(self, jpeg_bytes: bytes) -> str:
        image_id = secrets.token_urlsafe(16)
        self._images[image_id] = jpeg_bytes
        while len(self._images) > ROULETTE_MAX_CACHED_IMAGES:
            self._images.popitem(last=False)
        return image_id

    async def get(self, request: web.Request, image_id: str) -> web.Response:
        img = self._images.get(image_id)
        if img is None:
            return web.Response(status=404, text="Not found")
        return web.Response(
            body=img,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )


def _format_duration_human(duration_ms: int, is_ru: bool) -> str:
    if duration_ms <= 0:
        return ""
    total_min = int(round(duration_ms / 60000.0))
    hours, mins = divmod(total_min, 60)
    if is_ru:
        return f"{hours}ч {mins}м" if hours else f"{mins}м"
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _build_roulette_image_url(hass: HomeAssistant, local_url: str) -> str:
    """Build direct loopback HTTP(S) URL to SmartPlexRouletteImageView.

    Using the internal HTTP view URL avoids Home Assistant's allowlist_external_dirs
    restriction on /config/www in both vk_notify and telegram_bot without exposing
    any Plex tokens or relying on external reverse proxies / hairpin NAT.
    """
    http_comp = getattr(hass, "http", None)
    scheme = "https" if getattr(http_comp, "ssl_certificate", None) else "http"
    port = int(getattr(http_comp, "server_port", 8123) or 8123)
    host = "127.0.0.1"
    raw_hosts = getattr(http_comp, "server_host", None)
    if raw_hosts:
        hosts = [raw_hosts] if isinstance(raw_hosts, str) else list(raw_hosts)
        if hosts and not any(
            h in ("0.0.0.0", "::", "127.0.0.1", "localhost") for h in hosts
        ):
            first_host = str(hosts[0]).strip()
            if first_host:
                host = (
                    f"[{first_host}]"
                    if ":" in first_host and not first_host.startswith("[")
                    else first_host
                )
    path = local_url if local_url.startswith("/") else f"/{local_url}"
    return f"{scheme}://{host}:{port}{path}"


def _sanitize_cb_token(value: str | None) -> str:
    if not value:
        return ""
    return str(value).replace(":", " ").strip()


def _fit_telegram_callback_data(
    prefix: str,
    parts: list[tuple[str, str | None]],
    max_bytes: int = 64,
) -> str:
    """Pack callback command within Telegram's strict 64-byte UTF-8 limit."""
    chosen = [_sanitize_cb_token(pref) for pref, _ in parts]

    def _assemble(tokens: list[str]) -> str:
        trimmed = list(tokens)
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        return ":".join([prefix, *trimmed]) if trimmed else f"{prefix}:"

    candidate = _assemble(chosen)
    if len(candidate.encode("utf-8")) <= max_bytes:
        return candidate

    for idx in range(len(parts) - 1, -1, -1):
        _, fallback = parts[idx]
        chosen[idx] = _sanitize_cb_token(fallback)
        candidate = _assemble(chosen)
        if len(candidate.encode("utf-8")) <= max_bytes:
            return candidate

    for idx in range(len(chosen) - 1, -1, -1):
        if idx == 0 and prefix == "/plex_play":
            break
        chosen[idx] = ""
        candidate = _assemble(chosen)
        if len(candidate.encode("utf-8")) <= max_bytes:
            return candidate

    return candidate.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def build_vk_keyboard_dict(
    picked: list[PlexMediaItem],
    zone_name: str,
    preset_name: str,
    is_ru: bool,
) -> dict[str, Any]:
    """Build native VK keyboard dict for vk_notify (`keyboard` parameter)."""
    vk_play_row: list[dict[str, Any]] = []
    for idx, item in enumerate(picked, start=1):
        vk_play_row.append(
            {
                "action": {
                    "type": "callback",
                    "label": f"▶ {idx}",
                    "payload": {
                        "cmd": "plex_play",
                        "key": item.rating_key,
                        "zone": zone_name,
                    },
                },
                "color": "positive",
            }
        )

    vk_rows: list[list[dict[str, Any]]] = []
    for i in range(0, len(vk_play_row), 4):
        vk_rows.append(vk_play_row[i : i + 4])
    vk_rows.append(
        [
            {
                "action": {
                    "type": "callback",
                    "label": "🔄 Другие варианты" if is_ru else "🔄 Reroll",
                    "payload": {
                        "cmd": "plex_reroll",
                        "preset": preset_name,
                        "zone": zone_name,
                    },
                },
                "color": "secondary",
            }
        ]
    )
    return {"inline": True, "buttons": vk_rows}


def build_roulette_keyboards(
    picked: list[PlexMediaItem],
    zone_name: str,
    preset_name: str,
    is_ru: bool,
    *,
    zone_id: str | None = None,
    preset_id: str | None = None,
) -> tuple[str, list[list[list[str]]]]:
    """Build inline keyboards for VK (JSON string) and Telegram Bot (3D button array)."""
    vk_kb_dict = build_vk_keyboard_dict(picked, zone_name, preset_name, is_ru)
    vk_keyboard_json = json.dumps(vk_kb_dict, ensure_ascii=False)

    tg_play_btns: list[list[str]] = [
        [
            f"▶ {idx}",
            _fit_telegram_callback_data(
                "/plex_play",
                [(str(item.rating_key), str(item.rating_key)), (zone_name, zone_id)],
            ),
        ]
        for idx, item in enumerate(picked, start=1)
    ]
    tg_rows: list[list[list[str]]] = []
    for i in range(0, len(tg_play_btns), 4):
        tg_rows.append(tg_play_btns[i : i + 4])
    reroll_label = "🔄 Другие варианты" if is_ru else "🔄 Reroll"
    tg_rows.append(
        [
            [
                reroll_label,
                _fit_telegram_callback_data(
                    "/plex_reroll",
                    [(preset_name, preset_id), (zone_name, zone_id)],
                ),
            ]
        ]
    )

    return vk_keyboard_json, tg_rows


def _save_jpeg_sync(file_path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as fp:
        fp.write(data)


async def async_spin_roulette(
    hass: HomeAssistant,
    entry: ConfigEntry,
    service_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Generate configurable movie roulette showcase with collage and keyboards."""
    import asyncio

    runtime = entry.runtime_data
    index = runtime.coordinator.data
    is_ru = (hass.config.language or "en").lower().startswith("ru")

    preset_query = service_data.get("preset")
    preset = resolve_roulette_preset(entry, preset_query)
    p_data = preset.data if preset else {}
    preset_name = str(
        p_data.get(CONF_ROULETTE_NAME)
        or (preset.title if preset else (preset_query or "default"))
    )

    zone_query = service_data.get("zone") or p_data.get(CONF_DEFAULT_ZONE)
    zone = resolve_zone(entry, hass, zone_query)
    zone_name = str(zone.data.get(CONF_ZONE_NAME) or zone.title) if zone else "default"

    def _opt(key: str, conf_key: str, default: Any) -> Any:
        val = service_data.get(key)
        return val if val is not None else p_data.get(conf_key, default)

    def _list_opt(key: str, conf_key: str) -> list[str]:
        val = service_data.get(key) or p_data.get(conf_key) or []
        return list(val) if isinstance(val, list) else [str(val)]

    watch_filter = str(_opt("watch_filter", CONF_WATCH_FILTER, DEFAULT_WATCH_FILTER))
    min_rating = float(_opt("min_rating", CONF_MIN_RATING, DEFAULT_MIN_RATING))
    year_from = int(_opt("year_from", CONF_YEAR_FROM, DEFAULT_YEAR_FROM))
    max_dur = int(_opt("max_duration_min", CONF_MAX_DURATION_MIN, DEFAULT_MAX_DURATION_MIN))
    pick_count = max(1, min(6, int(_opt("pick_count", CONF_PICK_COUNT, DEFAULT_PICK_COUNT))))
    layout = str(_opt("collage_layout", CONF_COLLAGE_LAYOUT, DEFAULT_COLLAGE_LAYOUT))
    anti_days = int(_opt("anti_repeat_days", CONF_ANTI_REPEAT_DAYS, DEFAULT_ANTI_REPEAT_DAYS))

    await runtime.roulette_store.async_load()
    excluded_keys = runtime.roulette_store.get_excluded_keys(anti_days)

    movies = list(index.items_by_type.get("movie", [])) if index else []
    candidates = filter_roulette_candidates(
        movies,
        libraries=_list_opt("libraries", CONF_LIBRARIES),
        watch_filter=watch_filter,
        include_genres=_list_opt("include_genres", CONF_INCLUDE_GENRES),
        exclude_genres=_list_opt("exclude_genres", CONF_EXCLUDE_GENRES),
        min_rating=min_rating,
        year_from=year_from,
        max_duration_min=max_dur,
        excluded_keys=excluded_keys,
    )

    if not candidates:
        return {
            "success": False,
            "zone": zone_name,
            "preset": preset_name,
            "pick_count": 0,
            "file_path": "",
            "local_url": "",
            "image_url": "",
            "message": (
                "Не найдено фильмов по заданным фильтрам рулетки."
                if is_ru
                else "No movies matched the roulette filters."
            ),
            "items": [],
            "vk_keyboard": {"inline": True, "buttons": []},
            "vk_inline_keyboard": "{}",
            "telegram_inline_keyboard": [],
        }

    picked = random.sample(candidates, k=min(pick_count, len(candidates)))
    await runtime.roulette_store.async_record_shown(
        [it.rating_key for it in picked], keep_days=max(anti_days, 14)
    )

    poster_bytes_list = await asyncio.gather(
        *(runtime.api.async_fetch_poster_bytes(it.thumb) for it in picked)
    )
    collage_inputs = [
        (p_bytes, idx, it.title, it.rating)
        for idx, (it, p_bytes) in enumerate(
            zip(picked, poster_bytes_list, strict=True), start=1
        )
    ]
    jpeg_bytes = await hass.async_add_executor_job(
        build_poster_collage_sync, collage_inputs, layout
    )

    file_path = hass.config.path("www", DOMAIN, "roulette_latest.jpg")
    await hass.async_add_executor_job(_save_jpeg_sync, file_path, jpeg_bytes)

    view: SmartPlexRouletteImageView | None = hass.data.get(DOMAIN, {}).get(
        "roulette_view"
    )
    local_url = (
        f"{ROULETTE_IMAGE_URL_PREFIX}/{view.store_image(jpeg_bytes)}.jpg"
        if view is not None
        else f"/local/{DOMAIN}/roulette_latest.jpg"
    )
    image_url = _build_roulette_image_url(hass, local_url)

    header = (
        f"🍿 Что посмотрим сегодня? ({zone_name})"
        if is_ru
        else f"🍿 Movie Roulette ({zone_name})"
    )
    lines = [header, ""]
    items_out: list[dict[str, Any]] = []
    for idx, it in enumerate(picked, start=1):
        year_s = f" ({it.year})" if it.year else ""
        rating_s = f" · ★ {it.rating:.1f}" if it.rating > 0 else ""
        dur_s = _format_duration_human(it.duration_ms, is_ru)
        genres_s = ", ".join(it.genres[:3])
        meta_parts = [p for p in (genres_s, dur_s) if p]
        meta_line = f" · {' · '.join(meta_parts)}" if meta_parts else ""
        lines.append(f"{idx}. {it.title}{year_s}{rating_s}{meta_line}")
        if pick_count == 1 and it.summary:
            lines.append(it.summary[:320])
        items_out.append(
            {
                "index": idx,
                "rating_key": it.rating_key,
                "title": it.title,
                "year": it.year,
                "rating": it.rating,
                "genres": list(it.genres),
                "duration_min": int(round(it.duration_ms / 60000.0))
                if it.duration_ms
                else 0,
            }
        )

    vk_kb_dict = build_vk_keyboard_dict(picked, zone_name, preset_name, is_ru)
    vk_kb, tg_kb = build_roulette_keyboards(
        picked,
        zone_name,
        preset_name,
        is_ru,
        zone_id=zone.subentry_id if zone else None,
        preset_id=preset.subentry_id if preset else None,
    )
    return {
        "success": True,
        "zone": zone_name,
        "preset": preset_name,
        "pick_count": len(picked),
        "file_path": file_path,
        "local_url": local_url,
        "image_url": image_url,
        "message": "\n".join(lines),
        "items": items_out,
        "vk_keyboard": vk_kb_dict,
        "vk_inline_keyboard": vk_kb,
        "telegram_inline_keyboard": tg_kb,
    }



