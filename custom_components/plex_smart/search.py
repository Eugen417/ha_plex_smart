"""Multilingual normalization, fuzzy media matching, and zone resolution."""

from __future__ import annotations

from collections.abc import Mapping
from difflib import SequenceMatcher
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .const import (
    CONF_ALIASES,
    CONF_AREA_ID,
    CONF_IS_DEFAULT,
    CONF_ROULETTE_NAME,
    CONF_ZONE_NAME,
    SUBENTRY_TYPE_ROULETTE,
    SUBENTRY_TYPE_ZONE,
)
from .models import PlexMediaItem, PlexPlaylistItem

_QUALITY_TAG_RE = re.compile(
    r"\b(4k|uhd|2160p|1080p|720p|hdr10\+?|hdr|dolby\s*vision|dv|imax|remux|extended|directors?\s*cut)\b",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def normalize_title(text: str | None) -> str:
    """Normalize media title or query for language-agnostic matching."""
    if not text:
        return ""
    cleaned = text.strip().lower().replace("ё", "е")
    cleaned = _QUALITY_TAG_RE.sub(" ", cleaned)
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    cleaned = _SPACES_RE.sub(" ", cleaned).strip()
    return cleaned


def parse_aliases(raw: Any) -> list[str]:
    """Parse zone aliases stored as comma-separated string or list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _pair_similarity(query_norm: str, candidate_norm: str) -> float:
    """Compute similarity score between two normalized strings."""
    if not query_norm or not candidate_norm:
        return 0.0
    if query_norm == candidate_norm:
        return 1.0
    ratio = SequenceMatcher(None, query_norm, candidate_norm).ratio()
    shorter_s, longer_s = (
        (query_norm, candidate_norm)
        if len(query_norm) <= len(candidate_norm)
        else (candidate_norm, query_norm)
    )
    shorter = len(shorter_s)
    longer = len(longer_s)
    if shorter >= 3 and shorter_s in longer_s:
        whole_words = f" {shorter_s} " in f" {longer_s} "
        single_prefix = (
            " " not in shorter_s
            and " " not in longer_s
            and shorter >= 4
            and longer_s.startswith(shorter_s)
        )
        if whole_words or single_prefix:
            if len(candidate_norm) < len(query_norm) and (shorter / longer) < 0.45:
                sub_score = 0.55 * (shorter / longer)
            else:
                sub_score = 0.78 + 0.20 * (shorter / longer)
            ratio = max(ratio, sub_score)
    return ratio


def find_best_media(
    items: list[PlexMediaItem],
    queries: list[str],
    year: int | None = None,
    library_filter: str | None = None,
    threshold: float = 0.62,
    secondary_queries: list[str] | None = None,
) -> tuple[PlexMediaItem | None, list[PlexMediaItem]]:
    """Find best matching media item across all libraries of that type.

    Primary queries (user's exact title) have full weight 1.0; secondary queries
    (AI-translated original_title) are weighted 0.82 so an LLM franchise hallucination
    cannot override a direct match on the user's actual words.
    """
    primary_norm = [q for q in (normalize_title(raw) for raw in queries) if q]
    secondary_norm = [
        q
        for q in (normalize_title(raw) for raw in (secondary_queries or []))
        if q and q not in primary_norm
    ]
    if not items or (not primary_norm and not secondary_norm):
        return None, []

    lib_norm = normalize_title(library_filter) if library_filter else ""
    scored: list[tuple[float, PlexMediaItem]] = []

    for item in items:
        if lib_norm and normalize_title(item.library_title) != lib_norm:
            continue
        best_q_score = 0.0
        for q_norm in primary_norm:
            s_title = _pair_similarity(q_norm, item.title_norm)
            s_orig = _pair_similarity(q_norm, item.orig_norm) if item.orig_norm else 0.0
            s = max(s_title, s_orig)
            if s > best_q_score:
                best_q_score = s

        for q_norm in secondary_norm:
            s_title = _pair_similarity(q_norm, item.title_norm) * 0.82
            s_orig = (
                (_pair_similarity(q_norm, item.orig_norm) * 0.82)
                if item.orig_norm
                else 0.0
            )
            s = max(s_title, s_orig)
            if s > best_q_score:
                best_q_score = s

        if best_q_score <= 0.35:
            continue

        total_score = best_q_score
        if year is not None and item.year is not None:
            if abs(item.year - year) <= 1:
                total_score += 0.08
            elif abs(item.year - year) >= 10:
                total_score -= 0.05

        scored.append((total_score, item))

    if not scored:
        return None, []

    scored.sort(key=lambda pair: (pair[0], pair[1].rating), reverse=True)
    top_suggestions = [item for _, item in scored[:3]]
    best_score, best_item = scored[0]

    if best_score >= threshold:
        return best_item, top_suggestions
    return None, top_suggestions


def find_best_playlist(
    playlists: list[PlexPlaylistItem],
    queries: list[str],
    threshold: float = 0.60,
) -> tuple[PlexPlaylistItem | None, list[PlexPlaylistItem]]:
    """Find best matching playlist by fuzzy title."""
    norm_queries = [q for q in (normalize_title(raw) for raw in queries) if q]
    if not playlists or not norm_queries:
        return None, []

    scored: list[tuple[float, PlexPlaylistItem]] = []
    for pl in playlists:
        score = max(_pair_similarity(q, pl.title_norm) for q in norm_queries)
        if score > 0.35:
            scored.append((score, pl))

    if not scored:
        return None, []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    suggestions = [pl for _, pl in scored[:3]]
    if scored[0][0] >= threshold:
        return scored[0][1], suggestions
    return None, suggestions


def resolve_genre_tag(raw_genre: str | None, available_genres: list[str]) -> str | None:
    """Map an AI or user genre string to the closest real genre tag on Plex."""
    if not raw_genre:
        return None
    q_norm = normalize_title(raw_genre)
    if not q_norm or not available_genres:
        return raw_genre.strip()

    best_tag: str | None = None
    best_score = 0.0
    for tag in available_genres:
        tag_norm = normalize_title(tag)
        if tag_norm == q_norm:
            return tag
        score = _pair_similarity(q_norm, tag_norm)
        if score > best_score:
            best_score = score
            best_tag = tag

    if best_tag is not None and best_score >= 0.72:
        return best_tag
    return raw_genre.strip()


def get_zone_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """Return all configured media zone subentries for an entry."""
    return [
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_ZONE
    ]


def get_roulette_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """Return all configured movie roulette preset subentries for an entry."""
    return [
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_ROULETTE
    ]


def resolve_roulette_preset(
    entry: ConfigEntry, preset_query: str | None
) -> ConfigSubentry | None:
    """Resolve a roulette preset subentry by subentry_id or name."""
    presets = get_roulette_subentries(entry)
    if not presets:
        return None
    if not preset_query:
        return presets[0]

    q_raw = preset_query.strip()
    q_norm = normalize_title(q_raw)

    for sub in presets:
        if sub.subentry_id == q_raw:
            return sub
        name = sub.data.get(CONF_ROULETTE_NAME) or sub.title
        if normalize_title(name) == q_norm:
            return sub

    best_sub: ConfigSubentry | None = None
    best_score = 0.0
    for sub in presets:
        name = sub.data.get(CONF_ROULETTE_NAME) or sub.title
        score = _pair_similarity(q_norm, normalize_title(name))
        if score > best_score:
            best_score = score
            best_sub = sub

    return best_sub if best_score >= 0.60 else None


def get_default_zone(
    zones: list[ConfigSubentry], area_id: str | None = None
) -> ConfigSubentry | None:
    """Pick the default zone for an area (if provided) or globally across the home."""
    if not zones:
        return None
    if area_id:
        in_area = [z for z in zones if z.data.get(CONF_AREA_ID) == area_id]
        if in_area:
            for z in in_area:
                if bool(z.data.get(CONF_IS_DEFAULT)):
                    return z
            return in_area[0]
    for z in zones:
        if bool(z.data.get(CONF_IS_DEFAULT)):
            return z
    return zones[0]


def resolve_zone(
    entry: ConfigEntry,
    hass: HomeAssistant,
    zone_query: str | None = None,
    area_id: str | None = None,
) -> ConfigSubentry | None:
    """Resolve target zone subentry by ID, name, alias, HA Area, or fuzzy match.

    When multiple zones share the same HA Area (e.g., LG TV + Apple TV in Living Room),
    an explicit zone name/alias picks that specific player, while a room-only or empty
    query always prefers the zone marked `is_default=True`.
    """
    zones = get_zone_subentries(entry)
    if not zones:
        return None

    area_reg = ar.async_get(hass)

    def _split_keywords(sub: ConfigSubentry) -> tuple[list[str], list[str]]:
        specific: list[str] = []
        area_kws: list[str] = []
        z_name = sub.data.get(CONF_ZONE_NAME) or sub.title
        if z_name:
            specific.append(str(z_name))
        specific.extend(parse_aliases(sub.data.get(CONF_ALIASES)))
        z_area_id = sub.data.get(CONF_AREA_ID)
        if z_area_id:
            area_kws.append(str(z_area_id))
            area_entry = area_reg.async_get_area(z_area_id)
            if area_entry:
                if area_entry.name:
                    area_kws.append(area_entry.name)
                if getattr(area_entry, "aliases", None):
                    area_kws.extend(str(a) for a in area_entry.aliases)
        return specific, area_kws

    if zone_query and zone_query.strip():
        q_raw = zone_query.strip()
        q_norm = normalize_title(q_raw)

        # 1. Exact subentry_id match
        for sub in zones:
            if sub.subentry_id == q_raw:
                return sub

        # 2. Exact match on specific zone name/alias (prefer is_default on ties)
        exact_specific = [
            sub
            for sub in zones
            if any(normalize_title(kw) == q_norm for kw in _split_keywords(sub)[0])
        ]
        if exact_specific:
            return get_default_zone(exact_specific)

        # 3. Exact match on HA Area name/ID (prefer is_default zone inside that room!)
        exact_area = [
            sub
            for sub in zones
            if any(normalize_title(kw) == q_norm for kw in _split_keywords(sub)[1])
        ]
        if exact_area:
            return get_default_zone(exact_area)

        # 4. Fuzzy match across specific names/aliases (+0.05 bonus) and area names,
        #    tie-breaking in favor of is_default=True
        scored: list[tuple[float, int, ConfigSubentry]] = []
        for sub in zones:
            spec_kws, area_kws = _split_keywords(sub)
            best_s = max(
                (_pair_similarity(q_norm, normalize_title(kw)) for kw in spec_kws),
                default=0.0,
            )
            best_a = max(
                (_pair_similarity(q_norm, normalize_title(kw)) for kw in area_kws),
                default=0.0,
            )
            # Slight boost for specific device name over shared room name
            score = max(best_s + (0.04 if best_s >= 0.62 else 0.0), best_a)
            if score >= 0.62:
                scored.append((round(score, 3), 1 if sub.data.get(CONF_IS_DEFAULT) else 0, sub))
        if scored:
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return scored[0][2]

    return get_default_zone(zones, area_id=area_id)


_PLAYLIST_HINT_RE = re.compile(
    r"\b(плейлист\w*|подборк\w*|playlist\w*)\b", re.IGNORECASE | re.UNICODE
)
_MUSIC_HINT_RE = re.compile(
    r"\b(музык\w*|песн\w*|песен\w*|трек\w*|альбом\w*|исполнител\w*|групп\w*|клип\w*|music|song\w*|track\w*|album\w*|artist\w*)\b",
    re.IGNORECASE | re.UNICODE,
)
_SHOW_HINT_RE = re.compile(
    r"\b(сериал\w*|сезон\w*|сери[яиюйе]\w*|эпизод\w*|series|tv\s*show\w*|show\w*|season\w*|episode\w*)\b",
    re.IGNORECASE | re.UNICODE,
)
_MOVIE_HINT_RE = re.compile(
    r"\b(фильм\w*|кино\w*|мульт\w*|аниме\w*|movie\w*|film\w*|cartoon\w*|anime\w*)\b",
    re.IGNORECASE | re.UNICODE,
)


def pre_detect_media_type(command_text: str | None) -> str | None:
    """Fast local detection of explicit media type keywords to save AI tokens."""
    norm = normalize_title(command_text)
    if not norm:
        return None
    if _PLAYLIST_HINT_RE.search(norm):
        return "playlist"
    if _MUSIC_HINT_RE.search(norm):
        return "music"
    if _SHOW_HINT_RE.search(norm):
        return "show"
    if _MOVIE_HINT_RE.search(norm):
        return "movie"
    return None


def _stem_token(word: str) -> str:
    """Light Russian/English inflection stemmer for fast room-name pre-matching."""
    if len(word) >= 6:
        return word[:-2]
    if len(word) >= 4 and word[-1] in "аяеиоуыэю":
        return word[:-1]
    return word


_DEVICE_BRAND_TOKENS = frozenset(
    {
        "lg", "samsung", "самсунг", "sony", "сони", "philips", "филипс",
        "tcl", "hisense", "xiaomi", "сяоми", "apple", "эпл", "shield",
        "tv", "тв", "телевизор", "oled", "qled", "приставка", "плеер",
        "webos", "tizen", "android", "plex",
    }
)


def _strip_device_brand_tokens(tokens: list[str]) -> list[str]:
    """Extract room-only tokens from composite zone names like 'LG Зал' -> ['зал']."""
    if len(tokens) < 2:
        return []
    room_tokens = [t for t in tokens if t not in _DEVICE_BRAND_TOKENS and not t.isdigit()]
    if room_tokens and len(room_tokens) < len(tokens):
        return room_tokens
    return []


def pre_resolve_zone_from_text(
    entry: ConfigEntry,
    hass: HomeAssistant,
    command_text: str | None,
    *,
    explicit_zone: str | None = None,
    area_id: str | None = None,
) -> ConfigSubentry | None:
    """Detect target TV zone locally (<1 ms) BEFORE calling AI Task so TV boots in parallel."""
    zones = get_zone_subentries(entry)
    if not zones:
        return None
    if explicit_zone and explicit_zone.strip():
        return resolve_zone(entry, hass, explicit_zone, area_id=area_id)
    if len(zones) == 1:
        return zones[0]

    cmd_norm = normalize_title(command_text)
    if cmd_norm:
        cmd_words = cmd_norm.split()
        area_reg = ar.async_get(hass)
        scored_pre: list[tuple[int, int, ConfigSubentry]] = []

        for sub in zones:
            spec_kws: list[str] = []
            area_kws: list[str] = []
            z_name = sub.data.get(CONF_ZONE_NAME) or sub.title
            if z_name:
                spec_kws.append(str(z_name))
            spec_kws.extend(parse_aliases(sub.data.get(CONF_ALIASES)))
            z_area_id = sub.data.get(CONF_AREA_ID)
            if z_area_id:
                area_entry = area_reg.async_get_area(z_area_id)
                if area_entry:
                    if area_entry.name:
                        area_kws.append(area_entry.name)
                    if getattr(area_entry, "aliases", None):
                        area_kws.extend(str(a) for a in area_entry.aliases)

            best_w = 0
            weighted_kws: list[tuple[str, int]] = [(k, 15) for k in spec_kws] + [
                (k, 0) for k in area_kws
            ]
            for sk in spec_kws:
                stripped = _strip_device_brand_tokens(normalize_title(sk).split())
                if stripped:
                    weighted_kws.append((" ".join(stripped), 5))
            for kw, bonus in weighted_kws:
                kw_norm = normalize_title(kw)
                if not kw_norm:
                    continue
                kw_tokens = kw_norm.split()
                if all(
                    any(cw.startswith(_stem_token(kt)) for cw in cmd_words)
                    for kt in kw_tokens
                ):
                    w = len(kw_norm) + len(kw_tokens) * 10 + bonus
                    if w > best_w:
                        best_w = w
            if best_w > 0:
                scored_pre.append((best_w, 1 if sub.data.get(CONF_IS_DEFAULT) else 0, sub))

        if scored_pre:
            scored_pre.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return scored_pre[0][2]

    return get_default_zone(zones, area_id=area_id)


_PEOPLE_SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|\+|\bи\b|\bда\b|\band\b|\bwith\b)\s*",
    re.IGNORECASE | re.UNICODE,
)


def split_people_list(raw: Any) -> list[str]:
    """Split a person string or list into clean individual actor/director names."""
    if not raw:
        return []
    raw_items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for entry in raw_items:
        text = str(entry or "").strip()
        if not text:
            continue
        for part in _PEOPLE_SPLIT_RE.split(text):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def build_actor_groups(params: Mapping[str, Any]) -> list[list[str]]:
    """Build bilingual variant groups per actor (each group must match via AND)."""
    primary = split_people_list(params.get("actors")) or split_people_list(
        params.get("actor")
    )
    orig = split_people_list(params.get("actors_original")) or split_people_list(
        params.get("actor_original")
    )
    if not primary and not orig:
        return []
    if primary and orig:
        if len(primary) == len(orig):
            return [
                list(dict.fromkeys([p, o])) for p, o in zip(primary, orig, strict=False)
            ]
        if len(primary) == 1:
            return [list(dict.fromkeys(primary + orig))]
    if primary:
        return [[p] for p in primary]
    return [[o] for o in orig]


def person_matches(
    query_variants: list[str], item_people: tuple[str, ...] | list[str]
) -> bool:
    """Match bilingual actor/director names (handles 'Ди Каприо' == 'ДиКаприо', surnames, inflections)."""
    if not query_variants or not item_people:
        return False

    prepared_people: list[tuple[str, str, list[str]]] = []
    for raw_p in item_people:
        p_norm = normalize_title(raw_p)
        if p_norm:
            prepared_people.append(
                (p_norm, p_norm.replace(" ", ""), p_norm.split())
            )
    if not prepared_people:
        return False

    for raw_q in query_variants:
        q_norm = normalize_title(raw_q)
        if not q_norm:
            continue
        q_compact = q_norm.replace(" ", "")
        q_tokens = q_norm.split()

        for p_norm, p_compact, p_tokens in prepared_people:
            if q_norm == p_norm or q_compact == p_compact:
                return True

            # Surname-only or compound-surname query (e.g. "Питт", "Ди Каприо", "Де Ниро")
            if len(q_compact) >= 4 and (
                q_norm in p_tokens
                or q_compact in p_tokens
                or (len(q_compact) >= 5 and p_compact.endswith(q_compact))
            ):
                return True

            if len(q_tokens) == 1 and len(q_compact) >= 5:
                if any(
                    SequenceMatcher(None, q_compact, pt).ratio() >= 0.82
                    for pt in p_tokens
                    if len(pt) >= 4
                ):
                    return True

            # Full-name fuzzy/inflected match ("Брэдом Питтом" -> "Брэд Питт", never "Брэд Гарретт")
            if (
                len(q_compact) >= 5
                and SequenceMatcher(None, q_compact, p_compact).ratio() >= 0.80
                and all(
                    qt in p_compact
                    or any(
                        SequenceMatcher(None, qt, pt).ratio() >= 0.72
                        for pt in p_tokens
                    )
                    for qt in q_tokens
                    if len(qt) >= 3
                )
            ):
                return True

    return False


def filter_items_by_people_local(
    items: list[PlexMediaItem],
    actor_groups: list[list[str]],
    director_variants: list[str] | None = None,
) -> list[PlexMediaItem]:
    """Filter cached items requiring ALL actors in actor_groups (strict AND) and optional director."""
    if not actor_groups and not director_variants:
        return list(items)
    out: list[PlexMediaItem] = []
    for item in items:
        if actor_groups and not all(
            person_matches(grp, item.actors) for grp in actor_groups
        ):
            continue
        if director_variants and not person_matches(director_variants, item.directors):
            continue
        out.append(item)
    return out


_SEMANTIC_OR_FILTER_RE = re.compile(
    r"\b("
    r"свеж\w*|нов[ыоау]\w*|последн\w*|стар\w*|ранн\w*|классик\w*|лучш\w*|популярн\w*|"
    r"случайн\w*|любо\w*|любу\w*|какой[\s-]нибудь|что[\s-]нибудь|"
    r"жанр\w*|комеди\w*|боевик\w*|ужас\w*|триллер\w*|драм\w*|фантаст\w*|детектив\w*|мелодрам\w*|"
    r"актер\w*|режиссер\w*|студи\w*|коллекц\w*|год[ау]?\b|десятилет\w*|"
    r"снима\w*|сыграл\w*|играет|играют|играл\w*|вместе\s+с|два\s+актер\w*|двумя\s+актер\w*|"
    r"(?:фильм\w*|кино|сериал\w*)\s+с\s+\w+|"
    r"похож\w*|типа\s+\w+|вроде\s+\w+|в\s+духе\s+\w+|"
    r"перенес\w*|перенос\w*|переключ\w*|перевед\w*|перевод\w*|перекин\w*|верн\w*|возврат\w*|продолжи\s+просмотр|продолжи\s+в\b|"
    r"пауз\w*|стоп\b|останов\w*|перемот\w*|отмот\w*|промот\w*|репит\w*|зацикл\w*|следующ\w*|предыдущ\w*|"
    r"про\s+\w+|где\s+\w+|о\s+том\b|с\s+участием\b|"
    r"newest|fresh|recent|oldest|classic|best|top\s*rated|random|anything|genre|actor|actors|director|"
    r"starring|featuring|together\s+with|(?:movie|film|show)\s+with\s+\w+|similar\s+to|something\s+like|"
    r"transfer|move\s+to|continue\s+watching|about\s+\w+|pause|unpause|stop|seek|rewind|forward|repeat"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)
_RESUME_HINT_RE = re.compile(
    r"\b(продолж\w*|досмотр\w*|resume)\b", re.IGNORECASE | re.UNICODE
)
_SHUFFLE_HINT_RE = re.compile(
    r"\b(микс\w*|вперемешку|вразброс|перемеш\w*|шафф?л\w*|shuffle|mix)\b",
    re.IGNORECASE | re.UNICODE,
)
_REPEAT_HINT_RE = re.compile(
    r"\b(на\s+повтор\w*|с\s+повтор\w*|на\s+репит\w*|повтор\w*|репит\w*|зацикл\w*|repeat|loop)\b",
    re.IGNORECASE | re.UNICODE,
)
_SE_CODE_RE = re.compile(r"\bs(\d{1,2})\s*e(\d{1,3})\b", re.IGNORECASE)
_SEASON_RE = re.compile(
    r"(?:(\d{1,2})\s*(?:сезон\w*|season)|(?:сезон\w*|season)\s*(\d{1,2}))",
    re.IGNORECASE | re.UNICODE,
)
_EPISODE_RE = re.compile(
    r"(?:(\d{1,3})\s*(?:сери[яюией]\w*|эпизод\w*|episode)|(?:сери[яюией]\w*|эпизод\w*|episode)\s*(\d{1,3}))",
    re.IGNORECASE | re.UNICODE,
)
_STOP_WORDS = frozenset(
    {
        "включи", "включить", "поставь", "поставить", "запусти", "запустить",
        "открой", "открыть", "найди", "покажи", "смотреть", "посмотреть",
        "пожалуйста", "продолжи", "продолжить", "досмотри", "досмотреть",
        "фильм", "фильмы", "кино", "сериал", "сериалы", "мультфильм", "мультик",
        "аниме", "песня", "песню", "трек", "музыка", "музыку", "альбом", "плейлист",
        "на", "в", "во", "для", "с", "по", "тв", "телевизор", "телевизоре",
        "приставка", "приставке", "play", "watch", "start", "open", "launch",
        "resume", "please", "movie", "film", "show", "series", "song", "track",
        "music", "playlist", "on", "in", "the",
    }
)


def try_direct_local_resolve(
    entry: ConfigEntry,
    hass: HomeAssistant,
    command_text: str | None,
    *,
    explicit_zone: str | None = None,
    area_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve exact title/episode requests directly against the in-memory Plex index (<1 ms, 0 LLM tokens)."""
    if not command_text or not command_text.strip():
        return None
    norm = normalize_title(command_text)
    if not norm or _SEMANTIC_OR_FILTER_RE.search(norm):
        return None

    runtime = getattr(entry, "runtime_data", None)
    index = (
        runtime.coordinator.data
        if runtime and getattr(runtime, "coordinator", None)
        else None
    )
    if index is None:
        return None

    resume = bool(_RESUME_HINT_RE.search(norm))
    shuffle = bool(_SHUFFLE_HINT_RE.search(norm))
    season: int | None = None
    episode: int | None = None

    cleaned = _SHUFFLE_HINT_RE.sub(" ", norm) if shuffle else norm
    if m_se := _SE_CODE_RE.search(cleaned):
        season, episode = int(m_se.group(1)), int(m_se.group(2))
        cleaned = _SE_CODE_RE.sub(" ", cleaned)
    else:
        if m_s := _SEASON_RE.search(cleaned):
            season = int(m_s.group(1) or m_s.group(2))
            cleaned = _SEASON_RE.sub(" ", cleaned)
        if m_e := _EPISODE_RE.search(cleaned):
            episode = int(m_e.group(1) or m_e.group(2))
            cleaned = _EPISODE_RE.sub(" ", cleaned)

    zone_sub = pre_resolve_zone_from_text(
        entry, hass, command_text, explicit_zone=explicit_zone, area_id=area_id
    )
    zone_name = (
        str(zone_sub.data.get(CONF_ZONE_NAME) or zone_sub.title)
        if zone_sub is not None
        else None
    )

    zone_stems: set[str] = set()
    if zone_sub is not None:
        area_reg = ar.async_get(hass)
        z_kws = [str(zone_sub.data.get(CONF_ZONE_NAME) or zone_sub.title)]
        z_kws.extend(parse_aliases(zone_sub.data.get(CONF_ALIASES)))
        z_aid = zone_sub.data.get(CONF_AREA_ID)
        if z_aid:
            a_ent = area_reg.async_get_area(z_aid)
            if a_ent and a_ent.name:
                z_kws.append(a_ent.name)
        for kw in z_kws:
            for tok in normalize_title(kw).split():
                if tok:
                    zone_stems.add(_stem_token(tok))

    remaining_tokens = [
        tok
        for tok in cleaned.split()
        if tok not in _STOP_WORDS
        and not any(tok.startswith(st) and len(st) >= 2 for st in zone_stems)
    ]
    title_candidate = " ".join(remaining_tokens).strip()
    if len(title_candidate) < 2:
        return None

    detected_type = pre_detect_media_type(command_text)
    if season is not None or episode is not None:
        search_order = ["show"]
    elif detected_type in ("movie", "show", "music", "playlist"):
        search_order = [detected_type]
    else:
        search_order = ["movie", "show", "music", "playlist"]

    for m_type in search_order:
        if m_type == "playlist":
            pl, _ = find_best_playlist(
                index.playlists, [title_candidate], threshold=0.80
            )
            if pl is not None:
                return {
                    "zone": explicit_zone or zone_name,
                    "media_type": "playlist",
                    "title": pl.title,
                    "resume": resume,
                    "shuffle": shuffle,
                    "_direct_local": True,
                }
            continue

        pool = index.items_by_type.get(m_type, [])
        item, _ = find_best_media(pool, [title_candidate], threshold=0.78)
        if item is None:
            continue

        if m_type == "show":
            params: dict[str, Any] = {
                "zone": explicit_zone or zone_name,
                "media_type": "show",
                "show_name": item.title,
                "resume": resume,
                "shuffle": shuffle,
                "_direct_local": True,
            }
            if season is not None:
                params["season"] = season
            if episode is not None:
                params["episode"] = episode
            return params

        return {
            "zone": explicit_zone or zone_name,
            "media_type": m_type,
            "rating_key": item.rating_key,
            "title": item.title,
            "resume": resume,
            "shuffle": shuffle,
            "_direct_local": True,
        }

    return None


_HANDOFF_ACTION_RE = re.compile(
    r"\b(перенес\w*|перенос\w*|переключ\w*|перевед\w*|перевод\w*|перекин\w*|верн\w*|возвращ\w*|возврат\w*|отправ\w*|transfer\w*|handoff|move)\b",
    re.IGNORECASE | re.UNICODE,
)
_START_OVER_HINT_RE = re.compile(
    r"\b(с\s+начала|сначала|заново|в\s+начало|на\s+начало|с\s+первой\s+минут\w*|from\s+the\s+beginning|from\s+start|start\s+over|to\s+the\s+beginning)\b",
    re.IGNORECASE | re.UNICODE,
)
_SOURCE_PREPS = frozenset({"из", "с", "со", "от", "from"})
_TARGET_PREPS = frozenset({"в", "во", "на", "к", "to", "into", "in", "on"})
_PREP_DEVICE_FILLERS = frozenset(
    {
        "тв", "телевизор", "телевизора", "телевизору", "телевизоре",
        "приставка", "приставки", "приставку", "приставке",
        "плеер", "плеера", "плеере", "комната", "комнаты", "комнату", "комнате",
        "the", "tv", "player", "room",
    }
)
_HANDOFF_ALLOWED_FILLER_RE = re.compile(
    r"^("
    r"перенес\w*|перенос\w*|переключ\w*|перевед\w*|перевод\w*|перекин\w*|верн\w*|возвращ\w*|возврат\w*|отправ\w*|"
    r"продолж\w*|досмотр\w*|возобнов\w*|включ\w*|запуст\w*|постав\w*|"
    r"смотр\w*|игра\w*|идет|шел|шло|шла|котор\w*|сейчас|тут|там|дальше|место|места|останов\w*|"
    r"обратн\w*|назад|сюда|туда|оттуда|отсюда|снова|опять|теперь|уже|просто|быстро|сразу|"
    r"фильм\w*|кино\w*|кинцо|сериал\w*|сери[яиюей]\w*|эпизод\w*|мульт\w*|аниме\w*|видео|"
    r"просмотр\w*|музык\w*|песн\w*|трек\w*|альбом\w*|"
    r"тв|телевизор\w*|приставк\w*|экран\w*|плеер\w*|комнат\w*|друг\w*|"
    r"из|с|со|от|в|во|на|к|по|за|у|о|об|и|а|же|ли|бы|"
    r"мне|нам|это|этот|эту|тот|ту|мой|моя|мое|мои|мою|наш|нашу|"
    r"пожалуйста|плиз|давай|нужно|надо|хочу|"
    r"transfer\w*|handoff|move|moving|switch\w*|return\w*|back|again|here|there|"
    r"continue|resume|keep|watching|playing|playback|"
    r"movie\w*|film\w*|show\w*|series|episode\w*|video\w*|music|tv|player|room|another|"
    r"from|to|into|in|on|at|the|a|an|my|our|this|that|please"
    r")$",
    re.IGNORECASE | re.UNICODE,
)


def _match_zones_with_prepositions(
    entry: ConfigEntry, hass: HomeAssistant, words: list[str]
) -> tuple[ConfigSubentry | None, ConfigSubentry | None, set[int]]:
    """Scan normalized word list for source ('из зала') and target ('в спальню') zones."""
    zones = get_zone_subentries(entry)
    if not zones or not words:
        return None, None, set()

    area_reg = ar.async_get(hass)
    source_hits: list[tuple[int, int, ConfigSubentry]] = []
    target_hits: list[tuple[int, int, ConfigSubentry]] = []
    consumed: set[int] = set()

    for sub in zones:
        kws: list[tuple[list[str], int]] = []
        z_name = sub.data.get(CONF_ZONE_NAME) or sub.title
        if z_name:
            tks = normalize_title(str(z_name)).split()
            if tks:
                kws.append((tks, 15))
                stripped_tks = _strip_device_brand_tokens(tks)
                if stripped_tks:
                    kws.append((stripped_tks, 5))
        for al in parse_aliases(sub.data.get(CONF_ALIASES)):
            tks = normalize_title(al).split()
            if tks:
                kws.append((tks, 15))
        z_aid = sub.data.get(CONF_AREA_ID)
        if z_aid:
            a_ent = area_reg.async_get_area(z_aid)
            if a_ent:
                if a_ent.name:
                    tks = normalize_title(a_ent.name).split()
                    if tks:
                        kws.append((tks, 0))
                for a_al in getattr(a_ent, "aliases", None) or ():
                    tks = normalize_title(str(a_al)).split()
                    if tks:
                        kws.append((tks, 0))

        is_def = 1 if sub.data.get(CONF_IS_DEFAULT) else 0
        for kw_tokens, bonus in kws:
            klen = len(kw_tokens)
            for idx in range(0, len(words) - klen + 1):
                if not all(
                    words[idx + off].startswith(_stem_token(kt))
                    for off, kt in enumerate(kw_tokens)
                ):
                    continue
                prep: str | None = None
                back = idx - 1
                while back >= 0 and back >= idx - 3:
                    w_back = words[back]
                    if w_back in _PREP_DEVICE_FILLERS:
                        back -= 1
                        continue
                    if w_back in _SOURCE_PREPS or w_back in _TARGET_PREPS:
                        prep = w_back
                    break
                weight = sum(len(t) for t in kw_tokens) + klen * 10 + bonus
                for pos in range(idx, idx + klen):
                    consumed.add(pos)
                if prep in _SOURCE_PREPS:
                    source_hits.append((weight, is_def, sub))
                else:
                    target_hits.append((weight, is_def, sub))

    source_sub: ConfigSubentry | None = None
    target_sub: ConfigSubentry | None = None
    if source_hits:
        source_hits.sort(key=lambda t: (t[0], t[1]), reverse=True)
        source_sub = source_hits[0][2]
    if target_hits:
        target_hits.sort(key=lambda t: (t[0], t[1]), reverse=True)
        for _, _, cand in target_hits:
            if source_sub is None or cand.subentry_id != source_sub.subentry_id:
                target_sub = cand
                break
        if target_sub is None:
            target_sub = target_hits[0][2]

    return source_sub, target_sub, consumed


def extract_source_zone_from_transfer_text(
    entry: ConfigEntry, hass: HomeAssistant, text: str | None
) -> str | None:
    """Return source zone name when user explicitly says 'перенеси/перенос ... из <zone> в ...'."""
    norm = normalize_title(text)
    if not norm or not _HANDOFF_ACTION_RE.search(norm):
        return None
    source_sub, _, _ = _match_zones_with_prepositions(entry, hass, norm.split())
    if source_sub is None:
        return None
    return str(source_sub.data.get(CONF_ZONE_NAME) or source_sub.title)


def extract_pure_handoff_intent(
    entry: ConfigEntry, hass: HomeAssistant, text: str | None
) -> dict[str, Any] | None:
    """Deterministically detect pure room-to-room transfer or generic resume with NO movie title."""
    norm = normalize_title(text)
    if not norm or _START_OVER_HINT_RE.search(norm):
        return None
    has_transfer = bool(_HANDOFF_ACTION_RE.search(norm))
    has_resume = bool(_RESUME_HINT_RE.search(norm))
    if not has_transfer and not has_resume:
        return None

    words = norm.split()
    source_sub, target_sub, consumed_idx = _match_zones_with_prepositions(
        entry, hass, words
    )

    for idx, tok in enumerate(words):
        if idx in consumed_idx:
            continue
        if not _HANDOFF_ALLOWED_FILLER_RE.match(tok):
            return None

    detected_type = pre_detect_media_type(text)
    return {
        "source_zone": (
            str(source_sub.data.get(CONF_ZONE_NAME) or source_sub.title)
            if source_sub is not None
            else None
        ),
        "target_zone": (
            str(target_sub.data.get(CONF_ZONE_NAME) or target_sub.title)
            if target_sub is not None
            else None
        ),
        "media_type": (
            detected_type if detected_type in ("movie", "show", "music") else None
        ),
        "is_transfer": has_transfer,
    }


_TIMECODE_HMS_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
_HOURS_NUM_RE = re.compile(
    r"\b(\d{1,2})\s*(?:час\w*|ч\b|h(?:ou)?r?s?)\b", re.IGNORECASE | re.UNICODE
)
_MINS_NUM_RE = re.compile(
    r"\b(\d{1,3})(?:\s*[-–]?\s*[юейоаыя]{1,3})?\s*(?:минут\w*|мин\b|m(?:in(?:ute)?s?)?)\b",
    re.IGNORECASE | re.UNICODE,
)
_SECS_NUM_RE = re.compile(
    r"\b(\d{1,4})(?:\s*[-–]?\s*[юейоаыя]{1,3})?\s*(?:секунд\w*|сек\b|s(?:ec(?:ond)?s?)?)\b",
    re.IGNORECASE | re.UNICODE,
)
_SEEK_VERB_RE = re.compile(
    r"\b(перемот\w*|отмот\w*|промот\w*|мотай\w*|мотан\w*|перепрыг\w*|перейд\w*|seek|rewind|fast\s*forward|forward|skip)\b",
    re.IGNORECASE | re.UNICODE,
)
_SEEK_FORWARD_RE = re.compile(
    r"\b(вперед|дальше|плюс|forward|ahead|fast\s*forward)\b",
    re.IGNORECASE | re.UNICODE,
)
_SEEK_BACKWARD_RE = re.compile(
    r"\b(назад|обратно|отмот\w*|минус|back|backward|rewind)\b",
    re.IGNORECASE | re.UNICODE,
)
_PAUSE_RE = re.compile(r"\b(пауз\w*|приостанов\w*|pause)\b", re.IGNORECASE | re.UNICODE)
_UNPAUSE_RE = re.compile(
    r"\b(сним\w*\s+с\s+пауз\w*|убер\w*\s+пауз\w*|выключ\w*\s+пауз\w*|отжм\w*\s+пауз\w*|unpause)\b",
    re.IGNORECASE | re.UNICODE,
)
_STOP_CTRL_RE = re.compile(r"\b(стоп|останов\w*|stop)\b", re.IGNORECASE | re.UNICODE)
_NEXT_CTRL_RE = re.compile(
    r"\b(следующ\w*|дальше\s+трек\w*|next\s+(?:track|song|episode)|next)\b",
    re.IGNORECASE | re.UNICODE,
)
_PREV_CTRL_RE = re.compile(
    r"\b(предыдущ\w*|прошл\w*\s+(?:трек\w*|песн\w*|сери\w*)|previous\s+(?:track|song|episode)|previous|prev)\b",
    re.IGNORECASE | re.UNICODE,
)
_OFF_HINT_RE = re.compile(
    r"\b(выключ\w*|отключ\w*|убер\w*|сброс\w*|без|off|disable|none)\b",
    re.IGNORECASE | re.UNICODE,
)
_REPEAT_ONE_RE = re.compile(
    r"\b(одн\w*|один|этот\s+трек|эту\s+песн\w*|трек\w*|песн\w*|one|single|track|song)\b",
    re.IGNORECASE | re.UNICODE,
)
_CONTROL_ALLOWED_FILLER_RE = re.compile(
    r"^("
    r"постав\w*|включ\w*|выключ\w*|отключ\w*|сним\w*|убер\w*|отжм\w*|сброс\w*|начн\w*|запуст\w*|"
    r"пауз\w*|приостанов\w*|стоп|останов\w*|продолж\w*|воспроизвед\w*|просмотр\w*|"
    r"перемот\w*|отмот\w*|промот\w*|мотай\w*|мотан\w*|перепрыг\w*|перейд\w*|переключ\w*|"
    r"вперед|назад|обратно|дальше|плюс|минус|начал\w*|сначала|заново|перв\w*|"
    r"час\w*|ч|минут\w*|мин|секунд\w*|сек|мест\w*|момент\w*|позиц\w*|отметк\w*|"
    r"следующ\w*|предыдущ\w*|прошл\w*|"
    r"повтор\w*|репит\w*|зацикл\w*|микс\w*|вперемешку|вразброс|перемеш\w*|шафф?л\w*|случайн\w*|поряд\w*|режим\w*|"
    r"одн\w*|один|все|всю|весь|очеред\w*|альбом\w*|плейлист\w*|"
    r"фильм\w*|кино\w*|сериал\w*|сери[яиюей]\w*|эпизод\w*|видео|музык\w*|песн\w*|трек\w*|"
    r"тв|телевизор\w*|приставк\w*|плеер\w*|комнат\w*|экран\w*|"
    r"на|в|во|с|со|к|по|до|из|от|у|за|без|и|а|же|мне|нам|это|этот|эту|тут|там|сейчас|пожалуйста|плиз|"
    r"pause|unpause|stop|play|resume|seek|rewind|forward|fast|skip|back|backward|ahead|restart|start|over|beginning|"
    r"next|previous|prev|repeat|loop|shuffle|mix|random|mode|one|single|all|off|on|disable|enable|"
    r"hour\w*|hr\w*|min\w*|sec\w*|movie|film|show|episode|track|song|music|video|playlist|album|queue|"
    r"to|from|in|on|at|by|for|the|a|an|of|please|\d+"
    r")$",
    re.IGNORECASE | re.UNICODE,
)


def parse_duration_seconds(text: str) -> int | None:
    """Parse spoken or numeric timestamps ('30 минуту', '1:15:00', '10 секунд', '2 минуты 30 секунд')."""
    if not text:
        return None
    raw = text.strip().lower().replace("ё", "е")

    if m_tc := _TIMECODE_HMS_RE.search(raw):
        p1, p2, p3 = int(m_tc.group(1)), int(m_tc.group(2)), m_tc.group(3)
        if p3 is not None:
            return p1 * 3600 + p2 * 60 + int(p3)
        return p1 * 60 + p2

    total = 0
    matched = False
    if m_h := _HOURS_NUM_RE.search(raw):
        total += int(m_h.group(1)) * 3600
        matched = True
    if m_m := _MINS_NUM_RE.search(raw):
        total += int(m_m.group(1)) * 60
        matched = True
    if m_s := _SECS_NUM_RE.search(raw):
        total += int(m_s.group(1))
        matched = True
    if matched:
        return total

    if "полтора часа" in raw:
        return 5400
    if "полчаса" in raw or "пол часа" in raw:
        return 1800
    return None


def extract_playback_control_intent(
    entry: ConfigEntry, hass: HomeAssistant, text: str | None
) -> dict[str, Any] | None:
    """Deterministically detect pure transport/seek/repeat/shuffle commands (<1 ms, 0 LLM tokens)."""
    if not text or not text.strip():
        return None
    raw_low = text.strip().lower().replace("ё", "е")
    norm = normalize_title(text)
    if not norm:
        return None

    words = norm.split()
    src_sub, tgt_sub, consumed_idx = _match_zones_with_prepositions(entry, hass, words)
    if src_sub is not None and tgt_sub is not None and src_sub.subentry_id != tgt_sub.subentry_id:
        return None

    for idx, tok in enumerate(words):
        if idx in consumed_idx:
            continue
        if not _CONTROL_ALLOWED_FILLER_RE.match(tok):
            return None

    zone_sub = tgt_sub or src_sub
    zone_name = (
        str(zone_sub.data.get(CONF_ZONE_NAME) or zone_sub.title)
        if zone_sub is not None
        else None
    )

    if _UNPAUSE_RE.search(norm):
        return {"action": "resume_play", "zone": zone_name}
    if _PAUSE_RE.search(norm):
        return {"action": "pause", "zone": zone_name}
    if _STOP_CTRL_RE.search(norm) and not _SEEK_VERB_RE.search(norm):
        return {"action": "stop", "zone": zone_name}

    if _SEEK_VERB_RE.search(norm) or _START_OVER_HINT_RE.search(norm):
        if _START_OVER_HINT_RE.search(norm):
            return {"action": "restart", "zone": zone_name}
        sec_val = parse_duration_seconds(raw_low)
        is_fwd = bool(_SEEK_FORWARD_RE.search(norm))
        is_bwd = bool(_SEEK_BACKWARD_RE.search(norm))
        if is_fwd or is_bwd:
            step = sec_val if (sec_val is not None and sec_val > 0) else 10
            delta = -step if (is_bwd and not is_fwd) else step
            return {"action": "seek_relative", "delta_sec": delta, "zone": zone_name}
        if sec_val is not None:
            return {"action": "seek_to", "offset_sec": sec_val, "zone": zone_name}

    if _NEXT_CTRL_RE.search(norm):
        return {"action": "next", "zone": zone_name}
    if _PREV_CTRL_RE.search(norm):
        return {"action": "previous", "zone": zone_name}

    if _REPEAT_HINT_RE.search(norm):
        if _OFF_HINT_RE.search(norm):
            mode = "off"
        elif _REPEAT_ONE_RE.search(norm):
            mode = "one"
        else:
            mode = "all"
        return {"action": "set_repeat", "repeat": mode, "zone": zone_name}

    if _SHUFFLE_HINT_RE.search(norm):
        enabled = not bool(_OFF_HINT_RE.search(norm))
        return {"action": "set_shuffle", "shuffle": enabled, "zone": zone_name}

    return None


