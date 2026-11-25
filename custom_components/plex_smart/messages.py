"""Localized response formatter with optional customizable user address."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_USER_TITLE

_TEMPLATES_RU: dict[str, str] = {
    "playing": "Включаю «{title}» ({zone}).",
    "starting_optimistic": "Включаю ТВ и запускаю «{title}» ({zone}).",
    "failed_play": "Не удалось запустить «{title}» ({zone}): {error}",
    "not_found": "«{query}» не найдено в медиатеке Plex.",
    "handoff_ok": "Переношу «{title}» из {source} в {zone} ({position}){off_note}.",
    "handoff_failed": "Не удалось перенести «{title}» в {zone}: {error}",
    "resume_ok": "Продолжаю «{title}» ({zone}).",
    "resume_failed": "Не удалось продолжить «{title}» ({zone}): {error}",
    "nothing_to_resume": "Нет активных сессий и недосмотренного контента на Plex.",
    "paused_ok": "Поставил на паузу ({zone}).",
    "unpaused_ok": "Продолжаю воспроизведение ({zone}).",
    "stopped_ok": "Остановил воспроизведение ({zone}).",
    "seek_ok": "Перемотал на {position} ({zone}).",
    "restarted_ok": "Запускаю с начала ({zone}).",
    "track_next_ok": "Переключил на следующий элемент ({zone}).",
    "track_prev_ok": "Переключил на предыдущий элемент ({zone}).",
    "repeat_ok": "Режим повтора: {mode} ({zone}).",
    "shuffle_ok": "Случайный порядок: {mode} ({zone}).",
    "no_active_playback": "Сейчас ничего не воспроизводится ({zone}).",
    "control_failed": "Не удалось выполнить команду управления ({zone}): {error}",
}

_TEMPLATES_EN: dict[str, str] = {
    "playing": "Playing '{title}' in {zone}.",
    "starting_optimistic": "Starting TV and playing '{title}' in {zone}.",
    "failed_play": "Failed to play '{title}' in {zone}: {error}",
    "not_found": "'{query}' not found in Plex library.",
    "handoff_ok": "Transferred '{title}' from {source} to {zone} at {position}{off_note}.",
    "handoff_failed": "Failed to transfer '{title}' to {zone}: {error}",
    "resume_ok": "Resuming '{title}' in {zone}.",
    "resume_failed": "Failed to resume '{title}' in {zone}: {error}",
    "nothing_to_resume": "No active sessions and no Continue Watching items found on Plex.",
    "paused_ok": "Paused playback in {zone}.",
    "unpaused_ok": "Resumed playback in {zone}.",
    "stopped_ok": "Stopped playback in {zone}.",
    "seek_ok": "Seeked to {position} in {zone}.",
    "restarted_ok": "Restarted from the beginning in {zone}.",
    "track_next_ok": "Skipped to next item in {zone}.",
    "track_prev_ok": "Skipped to previous item in {zone}.",
    "repeat_ok": "Repeat mode set to {mode} in {zone}.",
    "shuffle_ok": "Shuffle set to {mode} in {zone}.",
    "no_active_playback": "Nothing is currently playing in {zone}.",
    "control_failed": "Playback control failed in {zone}: {error}",
}


def format_response(
    hass: HomeAssistant,
    entry_data: Mapping[str, Any],
    key: str,
    **kwargs: Any,
) -> str:
    """Format a localized user-facing response with optional custom user_title prefix."""
    is_ru = (getattr(hass.config, "language", None) or "en").lower().startswith("ru")
    catalog = _TEMPLATES_RU if is_ru else _TEMPLATES_EN
    template = catalog.get(key, "{title}")
    body = template.format(**kwargs)

    user_title = str(entry_data.get(CONF_USER_TITLE) or "").strip()
    if not user_title:
        return body

    if body and body[0].isalpha() and not (len(body) > 1 and body[1].isupper()):
        body = body[0].lower() + body[1:]
    return f"{user_title}, {body}"
