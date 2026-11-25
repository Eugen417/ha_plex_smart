"""Unit tests for SmartPlex (search, hardware state machine, handoff, roulette, i18n)."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "plex_smart"


def _install_ha_stubs() -> None:
    """Install lightweight Home Assistant & aiohttp stubs for isolated stdlib unit testing."""
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    ha_const = types.ModuleType("homeassistant.const")
    ha_const.Platform = types.SimpleNamespace(BUTTON="button", SENSOR="sensor")
    ha_const.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")

    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = object
    ha_core.Event = object
    ha_core.ServiceCall = object
    ha_core.SupportsResponse = types.SimpleNamespace(OPTIONAL="optional", ONLY="only")
    ha_core.callback = lambda fn: fn

    ha_ce = types.ModuleType("homeassistant.config_entries")
    ha_ce.ConfigEntry = object
    ha_ce.ConfigSubentry = object
    ha_ce.ConfigEntryState = types.SimpleNamespace(LOADED="loaded")
    ha_ce.ConfigFlow = object
    ha_ce.ConfigFlowResult = dict
    ha_ce.ConfigSubentryFlow = object
    ha_ce.SubentryFlowResult = dict

    ha_exc = types.ModuleType("homeassistant.exceptions")
    ha_exc.HomeAssistantError = RuntimeError
    ha_exc.ConfigEntryAuthFailed = RuntimeError

    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_ar = types.ModuleType("homeassistant.helpers.area_registry")
    ha_ar.async_get = lambda hass: types.SimpleNamespace(async_get_area=lambda aid: None)

    ha_dr = types.ModuleType("homeassistant.helpers.device_registry")
    ha_dr.async_get = lambda hass: types.SimpleNamespace(async_get=lambda did: None)
    ha_dr.DeviceEntryType = types.SimpleNamespace(SERVICE="service")
    ha_dr.DeviceInfo = dict

    ha_er = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_er.async_get = lambda hass: types.SimpleNamespace(async_get=lambda eid: None)
    ha_er.async_entries_for_device = lambda reg, dev_id: []

    ha_llm = types.ModuleType("homeassistant.helpers.llm")

    class _FakeToolResult:
        def __init__(self, data, error=False):
            self.data = data
            self.error = error

    class _FakeTool:
        integration = None

    class _FakeAPI:
        def __init__(self, hass, id, name):
            self.hass = hass
            self.id = id
            self.name = name

    class _FakeToolAnnotations:
        def __init__(self, read_only=False, destructive=True, idempotent=False, open_world=True):
            self.read_only = read_only
            self.destructive = destructive
            self.idempotent = idempotent
            self.open_world = open_world

    ha_llm.Tool = _FakeTool
    ha_llm.ToolResult = _FakeToolResult
    ha_llm.ToolAnnotations = _FakeToolAnnotations
    ha_llm.ToolInput = object
    ha_llm.LLMContext = object
    ha_llm.API = _FakeAPI
    ha_llm.APIInstance = dict

    ha_diag = types.ModuleType("homeassistant.components.diagnostics")

    def _fake_redact(data, to_redact):
        if isinstance(data, dict):
            return {
                k: ("**REDACTED**" if k in to_redact else _fake_redact(v, to_redact))
                for k, v in data.items()
            }
        return data

    ha_diag.async_redact_data = _fake_redact

    ha_util_json = types.ModuleType("homeassistant.util.json")
    ha_util_json.JsonObjectType = dict

    vol_mod = types.ModuleType("voluptuous")
    vol_mod.Schema = lambda s: s
    vol_mod.Optional = lambda k, default=None: k
    vol_mod.Required = lambda k, default=None: k
    vol_mod.In = lambda vals: vals
    vol_mod.Coerce = lambda t: t
    vol_mod.All = lambda *validators: validators
    vol_mod.Range = lambda **kw: kw

    ha_cv = types.ModuleType("homeassistant.helpers.config_validation")
    ha_cv.string = str
    ha_cv.boolean = bool
    ha_cv.entity_id = str
    ha_cv.ensure_list = lambda v: v if isinstance(v, list) else [v]

    ha_disp = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_disp.async_dispatcher_send = lambda *a, **kw: None

    ha_event = types.ModuleType("homeassistant.helpers.event")

    def _fake_track(hass, entities, action):
        hass._trackers.append((list(entities), action))

        def _unsub():
            hass._trackers = [t for t in hass._trackers if t[1] is not action]

        return _unsub

    ha_event.async_track_state_change_event = _fake_track

    ha_script = types.ModuleType("homeassistant.helpers.script")

    class _FakeScript:
        def __init__(self, hass, actions, name, domain):
            self.hass = hass
            self.actions = actions

        async def async_run(self, context=None):
            self.hass.custom_runs.append(self.actions)

    ha_script.Script = _FakeScript

    ha_storage = types.ModuleType("homeassistant.helpers.storage")

    class _FakeStore:
        def __init__(self, hass, version, key):
            self._data = None

        async def async_load(self):
            return self._data

        async def async_save(self, data):
            self._data = dict(data)

    ha_storage.Store = _FakeStore

    ha_aio = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ha_aio.async_get_clientsession = lambda hass, verify_ssl=True: None

    ha_comp = types.ModuleType("homeassistant.components")
    ha_http = types.ModuleType("homeassistant.components.http")
    ha_http.HomeAssistantView = object

    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    import datetime as _dt

    ha_dt.now = lambda: _dt.datetime(2026, 11, 20, 20, 0, 0)

    aiohttp_mod = types.ModuleType("aiohttp")
    aiohttp_mod.ClientSession = object
    aiohttp_mod.ClientTimeout = lambda **kw: kw
    aiohttp_mod.ClientError = Exception
    aiohttp_web = types.ModuleType("aiohttp.web")
    aiohttp_web.Request = object
    aiohttp_web.Response = lambda **kw: kw

    for name, mod in {
        "homeassistant": ha,
        "homeassistant.const": ha_const,
        "homeassistant.core": ha_core,
        "homeassistant.config_entries": ha_ce,
        "homeassistant.exceptions": ha_exc,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.area_registry": ha_ar,
        "homeassistant.helpers.config_validation": ha_cv,
        "homeassistant.helpers.device_registry": ha_dr,
        "homeassistant.helpers.dispatcher": ha_disp,
        "homeassistant.helpers.entity_registry": ha_er,
        "homeassistant.helpers.llm": ha_llm,
        "homeassistant.helpers.event": ha_event,
        "homeassistant.helpers.script": ha_script,
        "homeassistant.helpers.storage": ha_storage,
        "homeassistant.helpers.aiohttp_client": ha_aio,
        "homeassistant.components": ha_comp,
        "homeassistant.components.diagnostics": ha_diag,
        "homeassistant.components.http": ha_http,
        "homeassistant.util": ha_util,
        "homeassistant.util.dt": ha_dt,
        "homeassistant.util.json": ha_util_json,
        "voluptuous": vol_mod,
        "aiohttp": aiohttp_mod,
        "aiohttp.web": aiohttp_web,
    }.items():
        sys.modules[name] = mod

    ha_helpers.area_registry = ha_ar
    ha_helpers.config_validation = ha_cv
    ha_helpers.device_registry = ha_dr
    ha_helpers.dispatcher = ha_disp
    ha_helpers.entity_registry = ha_er
    ha_helpers.llm = ha_llm
    ha_util.dt = ha_dt
    aiohttp_mod.web = aiohttp_web


_install_ha_stubs()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Avoid executing __init__.py (which imports full HA platforms) when importing submodules
pkg_stub = types.ModuleType("custom_components.plex_smart")
pkg_stub.__path__ = [str(PKG_DIR)]
sys.modules["custom_components"] = types.ModuleType("custom_components")
sys.modules["custom_components"].__path__ = [str(ROOT / "custom_components")]
sys.modules["custom_components.plex_smart"] = pkg_stub

from custom_components.plex_smart import ai_parser, api, const, diagnostics, hardware, llm_api, messages, models, payload_templates, privacy, roulette, search, services  # noqa: E402


def _collect_keys(obj: object, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys.update(_collect_keys(v, full))
            else:
                keys.add(full)
    return keys


class _FakeState:
    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


class _FakeHass:
    def __init__(self, initial_states: dict[str, _FakeState] | None = None) -> None:
        self._states: dict[str, _FakeState] = dict(initial_states or {})
        self._trackers: list[tuple[list[str], object]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.custom_runs: list = []
        self.config = types.SimpleNamespace(language="ru")

        class _States:
            def __init__(self, outer):
                self._outer = outer

            def get(self, eid):
                return self._outer._states.get(eid)

        class _Services:
            def __init__(self, outer):
                self._outer = outer

            async def async_call(self, domain, service, data, blocking=True, return_response=False):
                self._outer.calls.append((domain, service, dict(data)))
                if domain == "button" and service == "press":
                    # Simulate plex client coming online after Scan press
                    for eid in list(self._outer._states):
                        if eid.startswith("media_player.plex_") and self._outer._states[eid].state == "unavailable":
                            self._outer.set_state(eid, "idle")
                elif domain == "media_player" and service == "play_media":
                    eid = data.get("entity_id")
                    if data.get("media_content_type") == "url":
                        pass  # Deep link opens card in idle until remote select
                    elif eid and getattr(self._outer, "fail_first_play", False):
                        self._outer.fail_first_play = False
                    elif eid:
                        self._outer.set_state(eid, "playing")
                elif domain == "media_player" and service == "media_seek":
                    eid = data.get("entity_id")
                    if eid and getattr(self._outer, "ignore_next_seek", 0) > 0:
                        self._outer.ignore_next_seek -= 1
                    elif eid and eid in self._outer._states:
                        cur = self._outer._states[eid]
                        attrs = dict(cur.attributes)
                        attrs["media_position"] = data.get("seek_position")
                        self._outer.set_state(eid, cur.state, attrs)
                return {}

        self.states = _States(self)
        self.services = _Services(self)

    def set_state(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        new_st = _FakeState(state, attributes)
        self._states[entity_id] = new_st
        ev = types.SimpleNamespace(data={"new_state": new_st})
        for entities, cb in list(self._trackers):
            if entity_id in entities:
                cb(ev)


class TestSearchAndI18n(unittest.TestCase):
    def test_normalization_and_multi_library_search(self) -> None:
        self.assertEqual(
            search.normalize_title("Ёлки 4K HDR IMAX — Remux!"),
            "елки",
        )
        items = [
            models.PlexMediaItem(
                rating_key=101,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Интерстеллар",
                title_norm=search.normalize_title("Интерстеллар"),
                original_title="Interstellar",
                orig_norm=search.normalize_title("Interstellar"),
                year=2014,
                rating=8.7,
            ),
            models.PlexMediaItem(
                rating_key=202,
                section_id="2",
                library_title="Мультфильмы",
                media_type="movie",
                title="Шрек",
                title_norm=search.normalize_title("Шрек"),
                original_title="Shrek",
                orig_norm=search.normalize_title("Shrek"),
                year=2001,
                rating=8.1,
            ),
        ]
        # Must find movie in the second movie library ("Мультфильмы") via English original title
        best, _ = search.find_best_media(items, ["Shrek"], year=2001)
        self.assertIsNotNone(best)
        self.assertEqual(best.rating_key, 202)
        self.assertEqual(best.library_title, "Мультфильмы")

        # Must return None (pre-flight miss) instead of waking TV for missing movie
        missing, sugg = search.find_best_media(items, ["Аватар 5"])
        self.assertIsNone(missing)

    def test_genre_mapping_and_ai_json_parser(self) -> None:
        tag = search.resolve_genre_tag("комедия", ["Драма", "Комедия", "Фантастика"])
        self.assertEqual(tag, "Комедия")
        parsed = ai_parser.parse_ai_json_response('```json\n{"control": {"intent": "handoff"}}\n```')
        self.assertEqual(parsed["control"]["intent"], "handoff")

    def test_translations_and_manifests_parity(self) -> None:
        strings = json.loads((PKG_DIR / "strings.json").read_text(encoding="utf-8"))
        en = json.loads((PKG_DIR / "translations" / "en.json").read_text(encoding="utf-8"))
        ru = json.loads((PKG_DIR / "translations" / "ru.json").read_text(encoding="utf-8"))

        k_str = _collect_keys(strings)
        k_en = _collect_keys(en)
        k_ru = _collect_keys(ru)
        self.assertEqual(k_str, k_en)
        self.assertEqual(k_str, k_ru)
        self.assertGreater(len(k_str), 80)

        manifest = json.loads((PKG_DIR / "manifest.json").read_text(encoding="utf-8"))
        hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["domain"], "plex_smart")
        self.assertEqual(hacs["homeassistant"], "2026.9.0")
        self.assertTrue(hacs["zip_release"])
        self.assertEqual(hacs["filename"], "plex_smart.zip")

        # Every PNG in repo (icons + docs screenshots) must be stripped of EXIF/XMP/iCCP/tEXt
        import struct

        allowed_chunks = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}
        png_files = list(ROOT.rglob("*.png"))
        self.assertGreaterEqual(len(png_files), 12)
        for pf in png_files:
            raw = pf.read_bytes()
            self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n", str(pf))
            pos = 8
            while pos < len(raw):
                length = struct.unpack(">I", raw[pos : pos + 4])[0]
                ctype = raw[pos + 4 : pos + 8]
                self.assertIn(ctype, allowed_chunks, f"Forbidden PNG chunk {ctype!r} in {pf}")
                pos += 12 + length


class TestHardwareAndHandoff(unittest.IsolatedAsyncioTestCase):
    async def test_hot_start_skips_tv_boot(self) -> None:
        hass = _FakeHass(
            {
                "media_player.plex_tv": _FakeState("idle"),
                "media_player.lg_tv": _FakeState(
                    "on", {"source": "Plex", "source_list": ["Live TV", "Plex"]}
                ),
            }
        )
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_tv",
            const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            const.CONF_HARDWARE_ENTITY: "media_player.lg_tv",
            const.CONF_APP_SOURCE: "Plex",
            const.CONF_COLD_BOOT_TIMEOUT: 5,
            const.CONF_COLD_OS_DELAY: 2,
            const.CONF_COLD_APP_DELAY: 4,
            const.CONF_HOT_DELAY: 0,
        }
        phase, elapsed = await hardware.async_ensure_zone_ready(hass, {}, zone)
        self.assertEqual(phase, const.START_PHASE_HOT)
        self.assertEqual(elapsed, 0.0)
        self.assertEqual(hass.calls, [])

    async def test_false_hot_start_rejected_when_tv_is_off(self) -> None:
        hass = _FakeHass(
            {
                "media_player.plex_tv": _FakeState("idle"),
                "media_player.lg_tv": _FakeState("off"),
            }
        )
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_tv",
            const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            const.CONF_HARDWARE_DEVICE_ID: "dev_lg_77",
            const.CONF_HARDWARE_ENTITY: "media_player.lg_tv",
            const.CONF_APP_SOURCE: "Plex",
            const.CONF_COLD_BOOT_TIMEOUT: 0,
            const.CONF_COLD_OS_DELAY: 0,
            const.CONF_COLD_APP_DELAY: 0,
        }
        self.assertFalse(hardware.is_zone_hot_ready(hass, zone))
        phase, _ = await hardware.async_ensure_zone_ready(hass, {}, zone)
        self.assertEqual(phase, const.START_PHASE_COLD)
        self.assertIn(("media_player", "turn_on", {"device_id": "dev_lg_77"}), hass.calls)
        self.assertIn(
            ("media_player", "select_source", {"device_id": "dev_lg_77", "source": "Plex"}),
            hass.calls,
        )

    async def test_samsung_tizen_launches_plex_by_app_id(self) -> None:
        hass = _FakeHass(
            {
                "media_player.samsung_tv": _FakeState(
                    "on", {"source": "TV", "source_list": ["TV", "HDMI"]}
                ),
                "media_player.plex_samsung": _FakeState("unavailable"),
            }
        )
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_samsung",
            const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
            const.CONF_HARDWARE_ENTITY: "media_player.samsung_tv",
            const.CONF_APP_SOURCE: "Plex",
            const.CONF_WARM_APP_DELAY: 0,
            const.CONF_SCAN_INTERVAL: 1,
            const.CONF_SCAN_TIMEOUT: 2,
        }
        phase, _ = await hardware.async_ensure_zone_ready(
            hass, {const.CONF_SCAN_BUTTON: "button.plex_scan"}, zone
        )
        self.assertEqual(phase, const.START_PHASE_WARM)
        self.assertIn(
            (
                "media_player",
                "play_media",
                {
                    "entity_id": "media_player.samsung_tv",
                    "media_content_type": "app",
                    "media_content_id": const.SAMSUNG_TIZEN_PLEX_APP_ID,
                },
            ),
            hass.calls,
        )

    async def test_cold_start_and_play_retry(self) -> None:
        hass = _FakeHass(
            {
                "media_player.lg_tv": _FakeState("off"),
                "media_player.plex_lg": _FakeState("unavailable"),
            }
        )
        hass.fail_first_play = True
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_lg",
            const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            const.CONF_HARDWARE_ENTITY: "media_player.lg_tv",
            const.CONF_APP_SOURCE: "Plex",
            const.CONF_COLD_BOOT_TIMEOUT: 0,
            const.CONF_COLD_OS_DELAY: 0,
            const.CONF_COLD_APP_DELAY: 0,
            const.CONF_SCAN_INTERVAL: 1,
            const.CONF_SCAN_TIMEOUT: 5,
            const.CONF_PLAY_RETRY_COUNT: 2,
            const.CONF_PLAY_RETRY_DELAY: 0.05,
        }
        entry_data = {
            const.CONF_SCAN_BUTTON: "button.plex_scan_clients",
            const.CONF_SERVER_ID: "srv123",
        }
        await hardware.async_ensure_zone_ready(hass, entry_data, zone)
        called_services = [(d, s) for d, s, _ in hass.calls]
        self.assertIn(("media_player", "turn_on"), called_services)
        self.assertIn(("media_player", "select_source"), called_services)
        self.assertIn(("button", "press"), called_services)

        ok, retries, err = await hardware.async_play_on_zone_with_retry(
            hass, entry_data, zone, {"plex_key": 777}, "MOVIE"
        )
        self.assertTrue(ok)
        self.assertEqual(retries, 1)
        self.assertEqual(err, "")
        self.assertEqual(hass.states.get("media_player.plex_lg").state, "playing")
        play_calls = [
            d for dom, srv, d in hass.calls if dom == "media_player" and srv == "play_media"
        ]
        self.assertEqual(play_calls[-1]["media_content_id"], "plex://srv123/777")
        self.assertEqual(play_calls[-1]["media_content_type"], "movie")

    async def test_handoff_turns_off_source_tv_and_rewinds_offset(self) -> None:
        hass = _FakeHass(
            {
                "media_player.plex_hall": _FakeState(
                    "playing", {"friendly_name": "Apple TV"}
                ),
                "media_player.apple_tv": _FakeState("on"),
                "media_player.plex_bedroom": _FakeState("idle"),
                "media_player.samsung_tv": _FakeState("on"),
            }
        )
        hall_sub = types.SimpleNamespace(
            subentry_id="z_hall",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Зал",
            data={
                const.CONF_ZONE_NAME: "Зал",
                const.CONF_PLEX_CLIENT: "media_player.plex_hall",
                const.CONF_HARDWARE_ENTITY: "media_player.apple_tv",
                const.CONF_POWER_PROFILE: const.PROFILE_APPLE_TV,
                const.CONF_TURN_OFF_ON_HANDOFF: True,
            },
        )
        bed_sub = types.SimpleNamespace(
            subentry_id="z_bed",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Спальня",
            data={
                const.CONF_ZONE_NAME: "Спальня",
                const.CONF_PLEX_CLIENT: "media_player.plex_bedroom",
                const.CONF_HARDWARE_ENTITY: "media_player.samsung_tv",
                const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
                const.CONF_HANDOFF_REWIND: 5,
                const.CONF_PLAY_RETRY_COUNT: 0,
            },
        )

        class _FakeApi:
            async def async_get_active_sessions(self):
                return [
                    models.PlexActiveSession(
                        session_key="1",
                        rating_key=999,
                        media_type="movie",
                        title="Джентльмены",
                        display_title="Джентльмены",
                        view_offset_ms=605_000,
                        player_machine_id="",
                        player_title="Apple TV",
                        player_state="playing",
                    )
                ]

        entry = types.SimpleNamespace(
            entry_id="e1",
            data={},
            subentries={"z_hall": hall_sub, "z_bed": bed_sub},
            runtime_data=types.SimpleNamespace(api=_FakeApi()),
        )
        res = await hardware.async_handoff_or_resume(hass, entry, target_zone=bed_sub)
        self.assertTrue(res.success)
        self.assertEqual(res.handoff_from, "Зал")
        self.assertEqual(res.rating_key, 999)
        self.assertIn(
            ("media_player", "media_stop", {"entity_id": "media_player.plex_hall"}),
            hass.calls,
        )
        self.assertIn(
            ("media_player", "turn_off", {"entity_id": "media_player.apple_tv"}),
            hass.calls,
        )
        play_calls = [
            d for dom, srv, d in hass.calls if dom == "media_player" and srv == "play_media"
        ]
        raw_cid = play_calls[0]["media_content_id"]
        self.assertTrue(raw_cid.startswith("plex://{"))
        payload = json.loads(raw_cid.removeprefix("plex://"))
        self.assertEqual(payload["plex_key"], 999)
        self.assertEqual(payload["offset"], 600)


class TestV101Optimizations(unittest.TestCase):
    def test_pre_detect_media_type_and_scoped_prompt(self) -> None:
        self.assertEqual(search.pre_detect_media_type("поставь песню Linkin Park в спальне"), "music")
        self.assertEqual(search.pre_detect_media_type("включи 2 сезон сериала Укрытие"), "show")
        self.assertEqual(search.pre_detect_media_type("включи свежий фильм в зале"), "movie")
        self.assertEqual(search.pre_detect_media_type("включи плейлист на вечер"), "playlist")
        self.assertIsNone(search.pre_detect_media_type("включи Интерстеллар в зале"))

        hass = _FakeHass()
        index = models.PlexIndexData(
            sections=[
                models.PlexLibrarySection("1", "1) Фильмы", "movie", ["Боевик", "Комедия"]),
                models.PlexLibrarySection("4", "2) Телесериалы", "show", ["Драма"]),
                models.PlexLibrarySection("5", "3) Музыка", "music", ["Rock", "Electronic"]),
            ],
            all_genres=["Боевик", "Комедия", "Драма", "Rock", "Electronic"],
        )
        z_hall = types.SimpleNamespace(
            subentry_id="z1",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Зал",
            data={const.CONF_ZONE_NAME: "Зал", const.CONF_ALIASES: "гостиная"},
        )
        z_bed = types.SimpleNamespace(
            subentry_id="z2",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Спальня",
            data={const.CONF_ZONE_NAME: "Спальня", const.CONF_ALIASES: ""},
        )
        entry = types.SimpleNamespace(
            subentries={"z1": z_hall, "z2": z_bed},
            runtime_data=types.SimpleNamespace(
                coordinator=types.SimpleNamespace(data=index)
            ),
        )

        # Pre-detect target room from Russian prepositional case ("в спальне")
        pre_z = search.pre_resolve_zone_from_text(
            entry, hass, "поставь песню Linkin Park в спальне"
        )
        self.assertEqual(pre_z.subentry_id, "z2")

        # Scoped music prompt contains ONLY music library, music genres, and music JSON keys
        prompt = ai_parser.build_ai_parser_prompt(
            hass, entry, "поставь песню Linkin Park в спальне"
        )
        self.assertIn("3) Музыка [music]", prompt)
        self.assertNotIn("1) Фильмы", prompt)
        self.assertNotIn("2) Телесериалы", prompt)
        self.assertIn("Rock, Electronic", prompt)
        self.assertNotIn("Боевик", prompt)
        self.assertNotIn('"season"', prompt)
        self.assertIn("LOCKED MEDIA TYPE: 'music'", prompt)

    def test_llm_tools_ha_2026_compliance(self) -> None:
        self.assertEqual(llm_api.SmartPlexPlayTool.integration, const.DOMAIN)
        self.assertEqual(llm_api.SmartPlexHandoffTool.integration, const.DOMAIN)
        self.assertEqual(llm_api.SmartPlexSearchTool.integration, const.DOMAIN)
        self.assertTrue(llm_api.SmartPlexSearchTool.annotations.read_only)
        self.assertFalse(llm_api.SmartPlexPlayTool.annotations.destructive)
        wrapped = llm_api._wrap_tool_result({"ok": True})
        self.assertEqual(wrapped.data, {"ok": True})
        self.assertFalse(wrapped.error)


class TestV102PhasesAndPrivacy(unittest.IsolatedAsyncioTestCase):
    async def test_reactive_cold_wakeup_exits_early_before_timeout(self) -> None:
        hass = _FakeHass(
            {
                "media_player.lg_tv": _FakeState("off"),
                "media_player.plex_lg": _FakeState("unavailable"),
            }
        )
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_lg",
            const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            const.CONF_HARDWARE_ENTITY: "media_player.lg_tv",
            const.CONF_APP_SOURCE: "Plex",
            const.CONF_COLD_BOOT_TIMEOUT: 5,
            const.CONF_COLD_OS_DELAY: 0,
            const.CONF_COLD_APP_DELAY: 0,
            const.CONF_SCAN_INTERVAL: 1,
            const.CONF_SCAN_TIMEOUT: 3,
        }

        async def _wake_tv_soon() -> None:
            await asyncio.sleep(0.05)
            hass.set_state(
                "media_player.lg_tv", "on", {"source": "Live TV", "source_list": ["Live TV", "Plex"]}
            )

        wake_task = asyncio.create_task(_wake_tv_soon())
        phase, elapsed = await hardware.async_ensure_zone_ready(
            hass, {const.CONF_SCAN_BUTTON: "button.plex_scan"}, zone
        )
        await wake_task
        self.assertEqual(phase, const.START_PHASE_COLD)
        self.assertLess(elapsed, 1.5)

    async def test_privacy_redactor_and_diagnostics(self) -> None:
        leaked = (
            "(404) not_found; https://198-51-100-10.abc123def.plex.direct:32400/player/playback/playMedia"
            "?key=%2Flibrary%2Fmetadata%2F7642&machineIdentifier=secretid123"
            "&token=transient-00000000-1111-2222-3333-444444444444 from 198.51.100.10"
        )
        cleaned = privacy.redact_sensitive_text(leaked)
        self.assertNotIn("198-51-100-10", cleaned)
        self.assertNotIn("198.51.100.10", cleaned)
        self.assertNotIn("plex.direct", cleaned)
        self.assertNotIn("secretid123", cleaned)
        self.assertNotIn("transient-00000000", cleaned)

        zone_sub = types.SimpleNamespace(
            subentry_id="z1",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            data={
                const.CONF_ZONE_NAME: "Секретная спальня",
                const.CONF_PLEX_CLIENT: "media_player.plex_secret",
                const.CONF_COLD_BOOT_TIMEOUT: 10,
            },
        )
        entry = types.SimpleNamespace(
            entry_id="e1",
            version=1,
            data={
                const.CONF_URL: "https://10.0.0.5:32400",
                const.CONF_TOKEN: "super_secret_token",
                const.CONF_SERVER_NAME: "My Home Plex",
            },
            subentries={"z1": zone_sub},
            runtime_data=types.SimpleNamespace(
                coordinator=types.SimpleNamespace(data=models.PlexIndexData()),
                last_launch=models.LaunchResult(
                    success=True,
                    zone_name="Секретная спальня",
                    matched_title="Секретный фильм (2025)",
                    rating_key=555,
                    media_type="movie",
                    boot_seconds=4.2,
                    retries_used=1,
                    handoff_from=None,
                    message="ok",
                    start_phase="cold",
                ),
            ),
        )
        diag = await diagnostics.async_get_config_entry_diagnostics(_FakeHass(), entry)
        dumped = json.dumps(diag, ensure_ascii=False)
        self.assertNotIn("super_secret_token", dumped)
        self.assertNotIn("10.0.0.5", dumped)
        self.assertNotIn("My Home Plex", dumped)
        self.assertNotIn("Секретная спальня", dumped)
        self.assertNotIn("Секретный фильм", dumped)
        self.assertEqual(diag["last_launch"]["start_phase"], "cold")

    async def test_apple_tv_deeplink_start_vs_resume_and_paired_display(self) -> None:
        hass = _FakeHass(
            {
                "media_player.apple_tv_4k": _FakeState("off"),
                "remote.apple_tv_4k": _FakeState("on"),
                "media_player.plex_plex_for_apple_tv_apple_tv": _FakeState("unavailable"),
                "media_player.tv_display": _FakeState(
                    "off", {"source": "Live TV", "source_list": ["Live TV", "Apple TV"]}
                ),
            }
        )
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_plex_for_apple_tv_apple_tv",
            const.CONF_POWER_PROFILE: const.PROFILE_APPLE_TV,
            const.CONF_HARDWARE_ENTITY: "media_player.apple_tv_4k",
            const.CONF_REMOTE_ENTITY: "remote.apple_tv_4k",
            const.CONF_DISPLAY_DEVICE_ID: "dev_tv_screen",
            const.CONF_DISPLAY_ENTITY: "media_player.tv_display",
            const.CONF_DISPLAY_SOURCE: "Apple TV",
            const.CONF_COLD_BOOT_TIMEOUT: 0,
            const.CONF_COLD_OS_DELAY: 0,
            const.CONF_WARM_APP_DELAY: 0,
            const.CONF_ATV_AUTO_SELECT: True,
        }
        entry_data = {const.CONF_SERVER_ID: "srv999"}

        # 1. Boot wakes Apple TV + paired display and switches HDMI input to "Apple TV"
        phase, _ = await hardware.async_ensure_zone_ready(hass, entry_data, zone)
        self.assertEqual(phase, const.START_PHASE_COLD)
        self.assertIn(("media_player", "turn_on", {"device_id": "dev_tv_screen"}), hass.calls)
        self.assertIn(
            ("media_player", "select_source", {"device_id": "dev_tv_screen", "source": "Apple TV"}),
            hass.calls,
        )

        # 2. Start from beginning (resume=False): resets server progress to 0, opens verified deep link, sends select
        progress_calls: list[tuple[int, int]] = []

        class _FakeProgressApi:
            async def async_set_playback_progress(self, rk: int, offset_ms: int) -> bool:
                progress_calls.append((rk, offset_ms))
                return True

        hass.calls.clear()
        hass.set_state("media_player.apple_tv_4k", "idle")
        ok, _, err = await hardware.async_play_on_zone_with_retry(
            hass,
            entry_data,
            zone,
            {"plex_key": 8274, "resume": 0, "offset": 0},
            "movie",
            api=_FakeProgressApi(),
        )
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(progress_calls, [(8274, 0)])
        self.assertIn(
            (
                "media_player",
                "play_media",
                {
                    "entity_id": "media_player.apple_tv_4k",
                    "media_content_type": "url",
                    "media_content_id": "plex://libraries/srv999/unmatched/%2Flibrary%2Fmetadata%2F8274",
                },
            ),
            hass.calls,
        )
        self.assertIn(
            ("remote", "send_command", {"entity_id": "remote.apple_tv_4k", "command": "select"}),
            hass.calls,
        )

        # 3. Resume with offset=300s syncs 300_000ms to Plex server before opening card
        progress_calls.clear()
        hass.set_state("media_player.apple_tv_4k", "playing")
        ok2, _, _ = await hardware.async_play_on_zone_with_retry(
            hass,
            entry_data,
            zone,
            {"plex_key": 8274, "resume": 1, "offset": 300},
            "movie",
            api=_FakeProgressApi(),
        )
        self.assertTrue(ok2)
        self.assertEqual(progress_calls, [(8274, 300_000)])

        # 4. Power off turns off paired display as well
        hass.calls.clear()
        await hardware.async_power_off_zone(hass, zone)
        self.assertIn(("media_player", "turn_off", {"device_id": "dev_tv_screen"}), hass.calls)

    async def test_apple_tv_second_command_uses_deeplink_not_port_32500(self) -> None:
        """Even after 1st movie makes plex_client 'playing', 2nd command must still use Deep Link."""
        hass = _FakeHass(
            {
                "media_player.apple_tv_4k": _FakeState(
                    "playing", {"source": "Plex", "source_list": ["Plex", "YouTube"]}
                ),
                "remote.apple_tv_4k": _FakeState("on"),
                "media_player.plex_plex_for_apple_tv_apple_tv": _FakeState("playing"),
            }
        )
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_plex_for_apple_tv_apple_tv",
            const.CONF_POWER_PROFILE: const.PROFILE_APPLE_TV,
            const.CONF_HARDWARE_ENTITY: "media_player.apple_tv_4k",
            const.CONF_REMOTE_ENTITY: "remote.apple_tv_4k",
            const.CONF_WARM_APP_DELAY: 0,
            const.CONF_ATV_AUTO_SELECT: True,
        }
        entry_data = {const.CONF_SERVER_ID: "srv999"}
        ok, _, err = await hardware.async_play_on_zone_with_retry(
            hass,
            entry_data,
            zone,
            {"plex_key": 8298, "resume": 0, "offset": 0},
            "movie",
        )
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertIn(
            ("remote", "send_command", {"entity_id": "remote.apple_tv_4k", "command": "menu"}),
            hass.calls,
        )
        self.assertIn(
            (
                "media_player",
                "play_media",
                {
                    "entity_id": "media_player.apple_tv_4k",
                    "media_content_type": "url",
                    "media_content_id": "plex://libraries/srv999/unmatched/%2Flibrary%2Fmetadata%2F8298",
                },
            ),
            hass.calls,
        )
        plex_direct_calls = [
            d
            for dom, srv, d in hass.calls
            if dom == "media_player"
            and srv == "play_media"
            and d.get("entity_id") == "media_player.plex_plex_for_apple_tv_apple_tv"
        ]
        self.assertEqual(plex_direct_calls, [])

    async def test_direct_local_resolve_and_franchise_hallucination_guard(self) -> None:
        movies = [
            models.PlexMediaItem(
                rating_key=501,
                section_id="1",
                library_title="1) Фильмы",
                media_type="movie",
                title="Хищник: Планета смерти",
                title_norm=search.normalize_title("Хищник: Планета смерти"),
                original_title="Predator: Badlands",
                orig_norm=search.normalize_title("Predator: Badlands"),
                year=2025,
                rating=7.5,
            ),
            models.PlexMediaItem(
                rating_key=502,
                section_id="1",
                library_title="1) Фильмы",
                media_type="movie",
                title="Добыча",
                title_norm=search.normalize_title("Добыча"),
                original_title="Prey",
                orig_norm=search.normalize_title("Prey"),
                year=2022,
                rating=7.2,
            ),
        ]
        shows = [
            models.PlexMediaItem(
                rating_key=601,
                section_id="4",
                library_title="2) Телесериалы",
                media_type="show",
                title="Гангстерленд",
                title_norm=search.normalize_title("Гангстерленд"),
                original_title="MobLand",
                orig_norm=search.normalize_title("MobLand"),
                year=2025,
                rating=8.4,
            )
        ]

        best, _ = search.find_best_media(
            movies,
            ["Планета смерти"],
            secondary_queries=["Prey"],
        )
        self.assertIsNotNone(best)
        self.assertEqual(best.rating_key, 501)

        index = models.PlexIndexData(
            items_by_type={"movie": movies, "show": shows, "music": []},
            items_by_key={501: movies[0], 502: movies[1], 601: shows[0]},
        )
        z_lg = types.SimpleNamespace(
            subentry_id="z_lg",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="LG Зал",
            data={const.CONF_ZONE_NAME: "LG Зал", const.CONF_ALIASES: "lg, лыжа"},
        )
        entry = types.SimpleNamespace(
            subentries={"z_lg": z_lg},
            runtime_data=types.SimpleNamespace(
                coordinator=types.SimpleNamespace(data=index)
            ),
        )
        hass = _FakeHass()

        direct_movie = search.try_direct_local_resolve(
            entry, hass, "включи фильм планета смерти на lg"
        )
        self.assertIsNotNone(direct_movie)
        self.assertEqual(direct_movie["rating_key"], 501)
        self.assertEqual(direct_movie["zone"], "LG Зал")

        direct_ep = search.try_direct_local_resolve(
            entry, hass, "включи на lg 8 серию 2 сезона Гангстерленд"
        )
        self.assertIsNotNone(direct_ep)
        self.assertEqual(direct_ep["media_type"], "show")
        self.assertEqual(direct_ep["show_name"], "Гангстерленд")
        self.assertEqual(direct_ep["season"], 2)
        self.assertEqual(direct_ep["episode"], 8)

        self.assertIsNone(
            search.try_direct_local_resolve(entry, hass, "включи свежую комедию на lg")
        )

    async def test_lg_read_timeout_does_not_restart_already_playing_episode(self) -> None:
        """If plexapi times out after LG webOS already transitioned to playing, do NOT retry."""
        hass = _FakeHass({"media_player.plex_lg77": _FakeState("idle")})
        play_attempts = 0

        async def _slow_timeout_call(domain, service, data, blocking=True, return_response=False):
            nonlocal play_attempts
            hass.calls.append((domain, service, dict(data)))
            if domain == "media_player" and service == "play_media":
                play_attempts += 1
                # TV starts playing quickly over WebSocket, then HTTP proxy raises ReadTimeout
                hass.set_state("media_player.plex_lg77", "playing")
                raise TimeoutError("Read timed out. (read timeout=30)")
            return {}

        hass.services.async_call = _slow_timeout_call
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_lg77",
            const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            const.CONF_PLAY_RETRY_COUNT: 3,
            const.CONF_PLAY_RETRY_DELAY: 0.05,
        }
        ok, retries, err = await hardware.async_play_on_zone_with_retry(
            hass, {const.CONF_SERVER_ID: "srv1"}, zone, {"plex_key": 777}, "episode"
        )
        self.assertTrue(ok)
        self.assertEqual(retries, 0)
        self.assertEqual(err, "")
        self.assertEqual(play_attempts, 1)

    async def test_default_zone_priority_in_same_room_and_globally(self) -> None:
        """When a room has both Apple TV and LG (is_default=True), LG wins unless Apple TV is named."""
        z_atv = types.SimpleNamespace(
            subentry_id="z_atv",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Apple TV",
            data={
                const.CONF_ZONE_NAME: "Apple TV",
                const.CONF_AREA_ID: "gostinaia",
                const.CONF_ALIASES: "приставка, эпл тв",
                const.CONF_IS_DEFAULT: False,
            },
        )
        z_lg = types.SimpleNamespace(
            subentry_id="z_lg",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="LG 77",
            data={
                const.CONF_ZONE_NAME: "LG 77",
                const.CONF_AREA_ID: "gostinaia",
                const.CONF_ALIASES: "лыжа, телевизор",
                const.CONF_IS_DEFAULT: True,
            },
        )
        entry = types.SimpleNamespace(subentries={"z_atv": z_atv, "z_lg": z_lg})
        hass = _FakeHass()

        # Patch area_registry to return Area(name="Зал") for "gostinaia"
        fake_area = types.SimpleNamespace(name="Зал", aliases=[])
        orig_ar = sys.modules["homeassistant.helpers.area_registry"].async_get
        sys.modules["homeassistant.helpers.area_registry"].async_get = lambda h: types.SimpleNamespace(
            async_get_area=lambda aid: fake_area if aid == "gostinaia" else None
        )
        try:
            # 1. No room mentioned -> default zone (LG 77) even though Apple TV is listed first
            self.assertEqual(search.resolve_zone(entry, hass, None).subentry_id, "z_lg")
            # 2. Shared room name "Зал" -> default zone (LG 77)
            self.assertEqual(search.resolve_zone(entry, hass, "Зал").subentry_id, "z_lg")
            self.assertEqual(
                search.pre_resolve_zone_from_text(entry, hass, "включи сериал в зале").subentry_id,
                "z_lg",
            )
            # 3. Explicit device name/alias -> Apple TV
            self.assertEqual(search.resolve_zone(entry, hass, "Apple TV").subentry_id, "z_atv")
            self.assertEqual(
                search.pre_resolve_zone_from_text(entry, hass, "включи фильм на приставке").subentry_id,
                "z_atv",
            )
        finally:
            sys.modules["homeassistant.helpers.area_registry"].async_get = orig_ar

    async def test_localized_messages_and_custom_user_title_privacy(self) -> None:
        hass_ru = _FakeHass()
        hass_ru.config.language = "ru"
        hass_en = _FakeHass()
        hass_en.config.language = "en"

        # 1. Default (empty user_title): neutral localized replies with NO hardcoded "Сэр"/"Sir"
        msg_ru = messages.format_response(
            hass_ru, {}, "starting_optimistic", title="Интерстеллар", zone="Зал"
        )
        self.assertEqual(msg_ru, "Включаю ТВ и запускаю «Интерстеллар» (Зал).")
        self.assertNotIn("Сэр", msg_ru)

        msg_en = messages.format_response(
            hass_en, {}, "starting_optimistic", title="Interstellar", zone="Living Room"
        )
        self.assertEqual(msg_en, "Starting TV and playing 'Interstellar' in Living Room.")
        self.assertNotIn("Sir", msg_en)

        # 2. Custom user_title configured
        msg_ru_sir = messages.format_response(
            hass_ru,
            {const.CONF_USER_TITLE: "Сэр"},
            "starting_optimistic",
            title="Интерстеллар",
            zone="Зал",
        )
        self.assertEqual(msg_ru_sir, "Сэр, включаю ТВ и запускаю «Интерстеллар» (Зал).")

        msg_en_cap = messages.format_response(
            hass_en,
            {const.CONF_USER_TITLE: "Captain"},
            "playing",
            title="Dune",
            zone="Cinema",
        )
        self.assertEqual(msg_en_cap, "Captain, playing 'Dune' in Cinema.")

        # 3. user_title is redacted in diagnostics
        entry = types.SimpleNamespace(
            entry_id="e1",
            version=1,
            data={const.CONF_USER_TITLE: "Сэр Капитан"},
            subentries={},
            runtime_data=None,
        )
        diag = await diagnostics.async_get_config_entry_diagnostics(hass_ru, entry)
        self.assertNotIn("Сэр Капитан", json.dumps(diag, ensure_ascii=False))

    async def test_multi_actor_intersection_bilingual_and_candidate_titles_fallback(
        self,
    ) -> None:
        movies = [
            models.PlexMediaItem(
                rating_key=10,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Бойцовский клуб",
                title_norm=search.normalize_title("Бойцовский клуб"),
                original_title="Fight Club",
                orig_norm=search.normalize_title("Fight Club"),
                year=1999,
                rating=8.8,
                actors=("Брэд Питт", "Эдвард Нортон"),
            ),
            models.PlexMediaItem(
                rating_key=20,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Начало",
                title_norm=search.normalize_title("Начало"),
                original_title="Inception",
                orig_norm=search.normalize_title("Inception"),
                year=2010,
                rating=8.8,
                actors=("Леонардо ДиКаприо", "Том Харди"),
            ),
            models.PlexMediaItem(
                rating_key=30,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Однажды в… Голливуде",
                title_norm=search.normalize_title("Однажды в… Голливуде"),
                original_title="Once Upon a Time in... Hollywood",
                orig_norm=search.normalize_title("Once Upon a Time in... Hollywood"),
                year=2019,
                rating=7.7,
                actors=("Леонардо ДиКаприо", "Brad Pitt", "Марго Робби"),
                directors=("Квентин Тарантино",),
            ),
            models.PlexMediaItem(
                rating_key=40,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Схватка",
                title_norm=search.normalize_title("Схватка"),
                original_title="Heat",
                orig_norm=search.normalize_title("Heat"),
                year=1995,
                rating=8.3,
                actors=(),  # Simulate rip with missing cast tags in Plex
            ),
        ]

        # 1. Strict AND across 2 actors (with "Ди Каприо" vs "ДиКаприо" + RU/EN mix)
        groups = search.build_actor_groups(
            {
                "actors": ["Брэд Питт", "Леонардо Ди Каприо"],
                "actors_original": ["Brad Pitt", "Leonardo DiCaprio"],
            }
        )
        hits = search.filter_items_by_people_local(movies, groups)
        self.assertEqual([m.rating_key for m in hits], [30])

        # Ensure "Брэд Питт" never false-matches "Брэд Гарретт"
        self.assertFalse(search.person_matches(["Брэд Питт"], ("Брэд Гарретт",)))
        # Ensure surname-only ("Ди Каприо", "Питт") and inflected ("Брэдом Питтом") match
        self.assertTrue(search.person_matches(["Ди Каприо"], ("Леонардо ДиКаприо",)))
        self.assertTrue(search.person_matches(["Брэдом Питтом"], ("Брэд Питт",)))

        # 2. Auto-split singular actor="Роберт Де Ниро и Аль Пачино" + candidate_titles fallback
        fake_api = types.SimpleNamespace(
            async_intersect_people=lambda *a, **kw: asyncio.sleep(0, result=set())
        )
        index = models.PlexIndexData(
            sections=[models.PlexLibrarySection("1", "Фильмы", "movie")],
            items_by_type={"movie": movies, "show": [], "music": []},
            items_by_key={m.rating_key: m for m in movies},
        )
        runtime = types.SimpleNamespace(
            api=fake_api,
            coordinator=types.SimpleNamespace(data=index),
        )
        params_heat = {
            "actor": "Роберт Де Ниро и Аль Пачино",
            "candidate_titles": ["Схватка", "Право на убийство"],
        }
        payload, matched_title, rk, not_found = await services._build_movie_payload(
            runtime, params_heat, {}, [], None, None, None, False
        )
        self.assertIsNone(not_found)
        self.assertEqual(rk, 40)
        self.assertEqual(payload["plex_key"], 40)
        self.assertIn("Схватка", matched_title)

    async def test_v108_live_resume_and_media_templates(self) -> None:
        war_dogs = models.PlexMediaItem(
            rating_key=500,
            section_id="1",
            library_title="Фильмы",
            media_type="movie",
            title="Парни со стволами",
            title_norm=search.normalize_title("Парни со стволами"),
            original_title="War Dogs",
            orig_norm=search.normalize_title("War Dogs"),
            year=2016,
            rating=7.1,
            view_offset_ms=0,  # Stale 60-min startup cache!
            genres=("Комедия", "Криминал"),
        )
        similar_movie = models.PlexMediaItem(
            rating_key=501,
            section_id="1",
            library_title="Фильмы",
            media_type="movie",
            title="Оружейный барон",
            title_norm=search.normalize_title("Оружейный барон"),
            original_title="Lord of War",
            orig_norm=search.normalize_title("Lord of War"),
            year=2005,
            rating=7.6,
            view_count=0,
            genres=("Криминал", "Драма"),
        )
        ted_lasso = models.PlexMediaItem(
            rating_key=700,
            section_id="2",
            library_title="Сериалы",
            media_type="show",
            title="Тед Лассо",
            title_norm=search.normalize_title("Тед Лассо"),
            original_title="Ted Lasso",
            orig_norm=search.normalize_title("Ted Lasso"),
            year=2020,
        )

        async def _fake_state(rk: int):
            return (2460000, 6840000) if rk == 500 else (0, 7000000)

        async def _fake_similar(rk: int, count: int = 20):
            return [501] if rk == 500 else []

        async def _fake_music_target(**kw):
            if kw.get("track") == "Believer":
                return (901, "track", "Imagine Dragons — Believer", 0)
            return None

        async def _fake_ep(show_rk: int, season=None, episode=None, resume_unwatched=False):
            if resume_unwatched:
                return (702, "Тед Лассо S02E05 — Радуга", 615)
            return (701, "Тед Лассо S01E01 — Пилот", 0)

        index = models.PlexIndexData(
            sections=[
                models.PlexLibrarySection("1", "Фильмы", "movie"),
                models.PlexLibrarySection("2", "Сериалы", "show"),
                models.PlexLibrarySection("3", "Музыка", "music"),
            ],
            items_by_type={"movie": [war_dogs, similar_movie], "show": [ted_lasso], "music": []},
            items_by_key={500: war_dogs, 501: similar_movie, 700: ted_lasso},
        )
        fake_api = types.SimpleNamespace(
            async_get_item_playback_state=_fake_state,
            async_get_similar_item_keys=_fake_similar,
            async_resolve_music_target=_fake_music_target,
            async_resolve_episode=_fake_ep,
            async_hub_search=lambda *a, **kw: asyncio.sleep(0, result=[]),
        )
        runtime = types.SimpleNamespace(
            api=fake_api,
            coordinator=types.SimpleNamespace(data=index),
            recent_zone_launches={},
            last_launch=None,
        )

        p_res, _, rk_res, nf = await services._build_movie_payload(
            runtime, {"resume": True}, {}, ["Парни со стволами"], None, None, None, True
        )
        self.assertIsNone(nf)
        self.assertEqual(rk_res, 500)
        self.assertEqual(p_res, {"plex_key": 500, "resume": 1, "offset": 2460})

        sig_start = services._build_launch_signature(
            {"title": "Парни со стволами", "resume": False}, "movie"
        )
        sig_resume = services._build_launch_signature(
            {"title": "Парни со стволами", "resume": True}, "movie"
        )
        self.assertNotEqual(sig_start, sig_resume)

        ep_start, lbl_start, _, _ = await services._build_show_payload(
            runtime, {}, {}, ["Тед Лассо"], None, None, None, False
        )
        self.assertEqual(ep_start, {"plex_key": 701, "resume": 0, "offset": 0, "continuous": 1})
        self.assertIn("S01E01", lbl_start)

        ep_res, lbl_res, _, _ = await services._build_show_payload(
            runtime, {"resume": True}, {}, ["Тед Лассо"], None, None, None, True
        )
        self.assertEqual(ep_res, {"plex_key": 702, "resume": 1, "offset": 615, "continuous": 1})
        self.assertIn("S02E05", lbl_res)

        cid_ep, _ = hardware.build_plex_media_args(
            {const.CONF_SERVER_ID: "srv1", const.CONF_SERVER_NAME: "HomePlex"},
            {"plex_key": 701, "resume": 0, "offset": 0, "continuous": 1, "_is_show_root": True},
            "episode",
        )
        self.assertTrue(cid_ep.startswith("plex://{"))
        self.assertIn('"continuous": 1', cid_ep)
        self.assertNotIn("_is_show_root", cid_ep)

        m_track, m_lbl, m_rk = await services._build_music_payload(
            runtime, {"track": "Believer"}, {}, None, None, None, resume=False
        )
        self.assertEqual(m_rk, 901)
        self.assertEqual(m_track, {"plex_key": 901, "continuous": 1, "resume": 0, "offset": 0})
        self.assertIn("Believer", m_lbl)

        m_dec, _, _ = await services._build_music_payload(
            runtime, {"decade": 1990}, {}, None, None, "Rock", resume=False
        )
        self.assertEqual(m_dec["libtype"], "track")
        self.assertEqual(m_dec["album.decade"], 1990)
        self.assertEqual(m_dec["shuffle"], 1)

        _, t_sim, rk_sim, nf_sim = await services._build_movie_payload(
            runtime, {"similar_to": "Парни со стволами"}, {}, [], None, None, None, False
        )
        self.assertIsNone(nf_sim)
        self.assertEqual(rk_sim, 501)
        self.assertIn("Оружейный барон", t_sim)

    async def test_v108_handoff_title_guard_and_clean_labels(self) -> None:
        war_dogs = models.PlexMediaItem(
            rating_key=500,
            section_id="1",
            library_title="Фильмы",
            media_type="movie",
            title="Парни со стволами",
            title_norm=search.normalize_title("Парни со стволами"),
            original_title="War Dogs",
            orig_norm=search.normalize_title("War Dogs"),
            year=2016,
            view_offset_ms=0,
        )
        index = models.PlexIndexData(
            sections=[models.PlexLibrarySection("1", "Фильмы", "movie")],
            items_by_type={"movie": [war_dogs], "show": [], "music": []},
            items_by_key={500: war_dogs},
        )
        fake_api = types.SimpleNamespace(
            async_get_item_playback_state=lambda rk: asyncio.sleep(0, result=(2460000, 6840000)),
            async_hub_search=lambda *a, **kw: asyncio.sleep(0, result=[]),
        )
        runtime = types.SimpleNamespace(
            api=fake_api,
            coordinator=types.SimpleNamespace(data=index),
            recent_zone_launches={},
            last_launch=None,
        )
        hass = _FakeHass()
        hass.set_state(
            "media_player.plex_lg",
            "playing",
            {"friendly_name": "Plex LG", "media_position": 2460},
        )
        hass.set_state(
            "media_player.lg_tv",
            "on",
            {"source": "Plex", "source_list": ["Plex"]},
        )
        zone_sub = types.SimpleNamespace(
            subentry_id="z1",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Зал",
            data={
                const.CONF_ZONE_NAME: "Зал",
                const.CONF_IS_DEFAULT: True,
                const.CONF_PLEX_CLIENT: "media_player.plex_lg",
                const.CONF_HARDWARE_ENTITY: "media_player.lg_tv",
                const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            },
        )
        entry = types.SimpleNamespace(
            entry_id="e1",
            data={const.CONF_SERVER_ID: "srv1", const.CONF_SERVER_NAME: "HomePlex"},
            subentries={"z1": zone_sub},
            runtime_data=runtime,
        )
        hass.config_entries = types.SimpleNamespace(async_entries=lambda d: [entry])
        orig_er = sys.modules["homeassistant.helpers.entity_registry"].async_get
        sys.modules["homeassistant.helpers.entity_registry"].async_get = lambda h: types.SimpleNamespace(
            async_get=lambda eid: types.SimpleNamespace(platform="plex", device_id="d1")
        )
        try:
            handoff_tool = llm_api.SmartPlexHandoffTool()
            tool_in = types.SimpleNamespace(
                tool_args={"title": "Парни со стволами", "target_zone": "Зал"}
            )
            ctx = types.SimpleNamespace(device_id=None)
            wrapped = await handoff_tool.async_call(hass, tool_in, ctx)
            self.assertTrue(wrapped.data["success"])
            self.assertEqual(wrapped.data["rating_key"], 500)
            play_calls = [c for c in hass.calls if c[:2] == ("media_player", "play_media")]
            self.assertEqual(len(play_calls), 1)
            # v1.0.19: Preserves integer offset in playMedia AND schedules post-proxy verified seek
            cid_json = json.loads(play_calls[0][2]["media_content_id"].removeprefix("plex://"))
            self.assertEqual(cid_json["plex_key"], 500)
            self.assertEqual(cid_json["offset"], 2460)
            self.assertEqual(cid_json["resume"], 1)
            self.assertIn("media_player.plex_lg", hardware._get_seek_tasks_map(hass))
            hardware._cancel_pending_seek_task(hass, "media_player.plex_lg")
        finally:
            sys.modules["homeassistant.helpers.entity_registry"].async_get = orig_er

        ru_data = json.loads((PKG_DIR / "translations" / "ru.json").read_text("utf-8"))
        en_data = json.loads((PKG_DIR / "strings.json").read_text("utf-8"))
        for label in ru_data["config"]["step"]["manual"]["data"].values():
            self.assertNotIn("(опционально)", label.lower())
        for label in en_data["config"]["step"]["manual"]["data"].values():
            self.assertNotIn("(optional)", label.lower())

    async def test_v113_saved_offset_guard_and_post_proxy_safe_seek(
        self,
    ) -> None:
        fake_api = types.SimpleNamespace(
            async_get_item_playback_state=lambda rk: asyncio.sleep(
                0, result=(10000, 6840000)
            ),
        )
        runtime = types.SimpleNamespace(
            api=fake_api,
            saved_resume_offsets={500: 2460},
        )
        protected_sec = await services._resolve_live_resume_offset_sec(
            runtime, 500, cached_offset_ms=0, explicit_offset_sec=None
        )
        self.assertEqual(protected_sec, 2460)

        # v1.0.19: Verify _async_ensure_resume_seek strictly awaits play_call_task completion
        # (never sending seekTo inside an open 30s proxy socket!) AND waits for 'playing'!
        hass = _FakeHass()
        hass.set_state("media_player.plex_lg", "buffering", {"media_position": 0})
        proxy_gate = asyncio.Event()

        async def _slow_proxy_play():
            await proxy_gate.wait()
            raise TimeoutError("Read timed out (read timeout=30)")

        play_task = asyncio.create_task(_slow_proxy_play())
        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.002, **kw)
        try:
            seek_worker = asyncio.create_task(
                hardware._async_ensure_resume_seek(
                    hass, "media_player.plex_lg", 2460, play_call_task=play_task
                )
            )
            await orig_sleep(0.01)
            # While proxy is open (and buffering), ZERO seeks may fire!
            self.assertEqual(
                [c for c in hass.calls if c[:2] == ("media_player", "media_seek")], []
            )
            hass.set_state("media_player.plex_lg", "playing", {"media_position": 1})
            await orig_sleep(0.01)
            # Even after 'playing', while play_task proxy is still open, ZERO seeks fire!
            self.assertEqual(
                [c for c in hass.calls if c[:2] == ("media_player", "media_seek")], []
            )
            # Proxy closes -> seek fires immediately!
            proxy_gate.set()
            await asyncio.wait_for(seek_worker, timeout=1.0)
        finally:
            hardware.asyncio.sleep = orig_sleep
            proxy_gate.set()
            await asyncio.gather(play_task, return_exceptions=True)

        seek_calls = [c for c in hass.calls if c[:2] == ("media_player", "media_seek")]
        self.assertEqual(
            seek_calls,
            [("media_player", "media_seek", {"entity_id": "media_player.plex_lg", "seek_position": 2460})],
        )

    async def test_v112_chat_log_forces_resume_when_llm_forgets_flag(self) -> None:
        import contextvars

        fake_chat_var: contextvars.ContextVar = contextvars.ContextVar(
            "current_chat_log", default=None
        )
        conv_mod = types.ModuleType("homeassistant.components.conversation")
        chat_log_mod = types.ModuleType("homeassistant.components.conversation.chat_log")
        chat_log_mod.current_chat_log = fake_chat_var
        sys.modules["homeassistant.components.conversation"] = conv_mod
        sys.modules["homeassistant.components.conversation.chat_log"] = chat_log_mod

        movie_222 = models.PlexMediaItem(
            rating_key=888,
            section_id="1",
            library_title="Фильмы",
            media_type="movie",
            title="2:22",
            title_norm=search.normalize_title("2:22"),
            original_title="2:22",
            orig_norm=search.normalize_title("2:22"),
            year=2017,
            view_offset_ms=0,
        )
        index = models.PlexIndexData(
            sections=[models.PlexLibrarySection("1", "Фильмы", "movie")],
            items_by_type={"movie": [movie_222], "show": [], "music": []},
            items_by_key={888: movie_222},
        )
        fake_api = types.SimpleNamespace(
            async_get_item_playback_state=lambda rk: asyncio.sleep(0, result=(1850000, 5800000)),
            async_hub_search=lambda *a, **kw: asyncio.sleep(0, result=[]),
        )
        runtime = types.SimpleNamespace(
            api=fake_api,
            coordinator=types.SimpleNamespace(data=index),
            recent_zone_launches={},
            saved_resume_offsets={},
            last_launch=None,
        )
        hass = _FakeHass()
        hass.set_state("media_player.plex_lg", "playing", {"media_position": 1850})
        hass.set_state("media_player.lg_tv", "on", {"source": "Plex", "source_list": ["Plex"]})
        zone_sub = types.SimpleNamespace(
            subentry_id="z1",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Зал",
            data={
                const.CONF_ZONE_NAME: "Зал",
                const.CONF_IS_DEFAULT: True,
                const.CONF_PLEX_CLIENT: "media_player.plex_lg",
                const.CONF_HARDWARE_ENTITY: "media_player.lg_tv",
                const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            },
        )
        entry = types.SimpleNamespace(
            entry_id="e1",
            data={const.CONF_SERVER_ID: "srv1", const.CONF_SERVER_NAME: "HomePlex"},
            subentries={"z1": zone_sub},
            runtime_data=runtime,
        )
        hass.config_entries = types.SimpleNamespace(async_entries=lambda d: [entry])
        orig_er = sys.modules["homeassistant.helpers.entity_registry"].async_get
        sys.modules["homeassistant.helpers.entity_registry"].async_get = (
            lambda h: types.SimpleNamespace(
                async_get=lambda eid: types.SimpleNamespace(
                    platform="plex", unique_id="srv1:lg_id", device_id="d1"
                )
            )
        )
        token = fake_chat_var.set(
            types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(role="user", content="продолжи фильм 2:22")
                ]
            )
        )
        try:
            play_tool = llm_api.SmartPlexPlayTool()
            # Simulate GigaChat forgetting resume=True (passing resume=False!)
            tool_in = types.SimpleNamespace(
                tool_args={"media_type": "movie", "title": "2:22", "resume": False}
            )
            ctx = types.SimpleNamespace(device_id=None)
            wrapped = await play_tool.async_call(hass, tool_in, ctx)
            self.assertTrue(wrapped.data["success"])
            play_calls = [c for c in hass.calls if c[:2] == ("media_player", "play_media")]
            self.assertEqual(len(play_calls), 1)
            # v1.0.19: ChatLog forced RESUME -> passes integer offset=1850 + schedules verified seek
            cid_888 = json.loads(play_calls[0][2]["media_content_id"].removeprefix("plex://"))
            self.assertEqual(cid_888["plex_key"], 888)
            self.assertEqual(cid_888["offset"], 1850)
            self.assertEqual(cid_888["resume"], 1)
            self.assertIn("media_player.plex_lg", hardware._get_seek_tasks_map(hass))
            hardware._cancel_pending_seek_task(hass, "media_player.plex_lg")
        finally:
            fake_chat_var.reset(token)
            sys.modules["homeassistant.helpers.entity_registry"].async_get = orig_er


class TestRoulette(unittest.IsolatedAsyncioTestCase):
    def test_roulette_filters_anti_repeat_and_keyboards(self) -> None:
        movies = [
            models.PlexMediaItem(
                rating_key=1,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Хороший фильм",
                title_norm="хороший фильм",
                original_title="Good Movie",
                orig_norm="good movie",
                year=2020,
                rating=8.2,
                duration_ms=100 * 60_000,
                view_count=0,
                genres=("Комедия", "Криминал"),
            ),
            models.PlexMediaItem(
                rating_key=2,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Страшный ужастик",
                title_norm="страшный ужастик",
                original_title="Scary Horror",
                orig_norm="scary horror",
                year=2021,
                rating=7.9,
                duration_ms=95 * 60_000,
                view_count=0,
                genres=("Ужасы",),
            ),
            models.PlexMediaItem(
                rating_key=3,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Очень длинный фильм",
                title_norm="очень длинный фильм",
                original_title="Long Epic",
                orig_norm="long epic",
                year=2022,
                rating=8.9,
                duration_ms=210 * 60_000,
                view_count=0,
                genres=("Драма",),
            ),
        ]
        filtered = roulette.filter_roulette_candidates(
            movies,
            libraries=["Фильмы"],
            watch_filter="unwatched",
            exclude_genres=["Ужасы"],
            min_rating=7.0,
            year_from=2000,
            max_duration_min=120,
        )
        self.assertEqual([m.rating_key for m in filtered], [1])

        # Anti-repeat fallback when all matching movies were already shown
        fallback = roulette.filter_roulette_candidates(
            movies,
            exclude_genres=["Ужасы"],
            max_duration_min=120,
            excluded_keys={1},
        )
        self.assertEqual([m.rating_key for m in fallback], [1])

        vk_dict = roulette.build_vk_keyboard_dict(
            filtered, zone_name="Зал", preset_name="Вечернее кино", is_ru=True
        )
        self.assertIsInstance(vk_dict, dict)
        self.assertTrue(vk_dict["inline"])
        self.assertEqual(vk_dict["buttons"][0][0]["action"]["payload"]["key"], 1)

        vk_kb, tg_kb = roulette.build_roulette_keyboards(
            filtered, zone_name="Зал", preset_name="Вечернее кино", is_ru=True
        )
        vk_parsed = json.loads(vk_kb)
        self.assertEqual(vk_parsed, vk_dict)
        self.assertEqual(
            vk_parsed["buttons"][1][0]["action"]["payload"]["cmd"], "plex_reroll"
        )
        # Telegram keyboard must be a 3D list of [label, callback_data] pairs
        self.assertEqual(tg_kb[0], [["▶ 1", "/plex_play:1:Зал"]])
        self.assertEqual(
            tg_kb[1], [["🔄 Другие варианты", "/plex_reroll:Вечернее кино:Зал"]]
        )

        # Long Cyrillic preset + zone with ':' and ',' must fit strictly into 64 UTF-8 bytes
        _, tg_kb_long = roulette.build_roulette_keyboards(
            filtered,
            zone_name="Большая гостиная: ТВ, ресивер и проектор",
            preset_name="Вечернее семейное кино: комедии, приключения",
            is_ru=True,
            zone_id="z_living",
            preset_id="p_family",
        )
        play_cb = tg_kb_long[0][0][1]
        reroll_cb = tg_kb_long[1][0][1]
        self.assertLessEqual(len(play_cb.encode("utf-8")), 64)
        self.assertLessEqual(len(reroll_cb.encode("utf-8")), 64)
        self.assertEqual(reroll_cb, "/plex_reroll:p_family:z_living")

    async def test_v114_roulette_image_url_and_blueprints_contracts(self) -> None:
        import tempfile
        from unittest import mock

        hass_default = types.SimpleNamespace(http=None)
        self.assertEqual(
            roulette._build_roulette_image_url(
                hass_default, "/api/plex_smart/roulette/abc.jpg"
            ),
            "http://127.0.0.1:8123/api/plex_smart/roulette/abc.jpg",
        )
        hass_ssl = types.SimpleNamespace(
            http=types.SimpleNamespace(
                ssl_certificate="/ssl/fullchain.pem",
                server_port=8443,
                server_host=["0.0.0.0"],
            )
        )
        self.assertEqual(
            roulette._build_roulette_image_url(
                hass_ssl, "/api/plex_smart/roulette/abc.jpg"
            ),
            "https://127.0.0.1:8443/api/plex_smart/roulette/abc.jpg",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            movie = models.PlexMediaItem(
                rating_key=42,
                section_id="1",
                library_title="Фильмы",
                media_type="movie",
                title="Тестовый фильм",
                title_norm="тестовый фильм",
                original_title="Test Movie",
                orig_norm="test movie",
                year=2024,
                rating=8.4,
                duration_ms=110 * 60_000,
                view_count=0,
                genres=("Фантастика",),
            )
            index = models.PlexIndexData(
                items_by_type={"movie": [movie]},
                items_by_key={42: movie},
            )
            view = roulette.SmartPlexRouletteImageView()

            class _RouletteHass:
                def __init__(self) -> None:
                    self.config = types.SimpleNamespace(
                        language="ru",
                        path=lambda *parts: str(pathlib.Path(tmpdir, *parts)),
                    )
                    self.data = {const.DOMAIN: {"roulette_view": view}}
                    self.http = types.SimpleNamespace(
                        ssl_certificate=None, server_port=8123, server_host=None
                    )

                async def async_add_executor_job(self, fn, *args):
                    return fn(*args)

            hass = _RouletteHass()
            zone_sub = types.SimpleNamespace(
                subentry_id="z_hall",
                subentry_type=const.SUBENTRY_TYPE_ZONE,
                title="Зал",
                data={const.CONF_ZONE_NAME: "Зал", const.CONF_IS_DEFAULT: True},
            )
            preset_sub = types.SimpleNamespace(
                subentry_id="p_eve",
                subentry_type=const.SUBENTRY_TYPE_ROULETTE,
                title="Вечернее кино",
                data={const.CONF_ROULETTE_NAME: "Вечернее кино"},
            )
            runtime = types.SimpleNamespace(
                coordinator=types.SimpleNamespace(data=index),
                roulette_store=roulette.RouletteHistoryStore(hass, "e1"),
                api=types.SimpleNamespace(
                    async_fetch_poster_bytes=lambda _t: asyncio.sleep(0, result=None)
                ),
            )
            entry = types.SimpleNamespace(
                entry_id="e1",
                subentries={"z_hall": zone_sub, "p_eve": preset_sub},
                runtime_data=runtime,
            )

            with mock.patch.object(
                roulette, "build_poster_collage_sync", return_value=b"\xff\xd8\xff\xd9"
            ):
                res = await roulette.async_spin_roulette(hass, entry, {})
            self.assertTrue(res["success"])
            self.assertTrue(
                res["image_url"].startswith(
                    "http://127.0.0.1:8123/api/plex_smart/roulette/"
                )
            )
            self.assertIsInstance(res["vk_keyboard"], dict)
            self.assertTrue(res["vk_keyboard"]["inline"])
            self.assertEqual(
                res["telegram_inline_keyboard"][0], [["▶ 1", "/plex_play:42:Зал"]]
            )

            empty_res = await roulette.async_spin_roulette(
                hass, entry, {"min_rating": 9.9}
            )
            self.assertFalse(empty_res["success"])
            self.assertEqual(empty_res["image_url"], "")
            self.assertEqual(empty_res["vk_keyboard"], {"inline": True, "buttons": []})

        vk_files = [
            ROOT / "blueprints" / "automation" / "Eugen417" / "smartplex_vk_movie_roulette.yaml",
            ROOT / "examples" / "vk_movie_roulette.yaml",
        ]
        for vf in vk_files:
            txt = vf.read_text(encoding="utf-8")
            self.assertNotIn("peer_id:", txt)
            self.assertNotIn("file_path:", txt)
            self.assertNotIn("caption:", txt)
            self.assertIn('keyboard: "{{ roulette.vk_keyboard }}"', txt)
            self.assertIn('url: "{{ roulette.image_url }}"', txt)

        tg_files = [
            ROOT / "blueprints" / "automation" / "Eugen417" / "smartplex_telegram_movie_roulette.yaml",
            ROOT / "blueprints" / "automation" / "Eugen417" / "smartplex_telegram_movie_roulette_en.yaml",
            ROOT / "examples" / "telegram_movie_roulette.yaml",
            ROOT / "examples" / "en" / "telegram_movie_roulette.yaml",
        ]
        for tf in tg_files:
            txt = tf.read_text(encoding="utf-8")
            self.assertNotIn("target:", txt)
            self.assertIn("config_entry_id:", txt)
            self.assertIn("chat_id:", txt)
            self.assertIn("entity_id:", txt)
            self.assertIn('url: "{{ roulette.image_url }}"', txt)
            self.assertIn("parse_mode: plain_text", txt)


    async def test_v116_samsung_tizen_wake_false_hot_and_30s_boot_recovery(self) -> None:
        hass = _FakeHass(
            {
                "media_player.samsung_frame": _FakeState("off"),
                "remote.samsung_frame": _FakeState("off"),
                "media_player.plex_samsung": _FakeState("unavailable"),
            }
        )
        orig_call = hass.services.async_call

        async def _samsung_wake_call(domain, service, data, blocking=True, return_response=False):
            if domain == "remote" and service == "turn_on":
                raise RuntimeError("Unable to turn on remote.samsung_frame")
            return await orig_call(domain, service, data, blocking=blocking, return_response=return_response)

        hass.services.async_call = _samsung_wake_call
        zone_cold = {
            const.CONF_PLEX_CLIENT: "media_player.plex_samsung",
            const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
            const.CONF_HARDWARE_ENTITY: "media_player.samsung_frame",
            const.CONF_REMOTE_ENTITY: "remote.samsung_frame",
            const.CONF_COLD_BOOT_TIMEOUT: 0,
            const.CONF_COLD_OS_DELAY: 0,
            const.CONF_COLD_APP_DELAY: 0,
        }
        self.assertTrue(await hardware._async_power_on_hardware(hass, zone_cold))
        self.assertIn(
            ("media_player", "turn_on", {"entity_id": "media_player.samsung_frame"}),
            hass.calls,
        )

        # False HOT guard: TV on Live TV with stale idle Plex -> WARM, not HOT
        hass.set_state(
            "media_player.samsung_frame",
            "on",
            {"source": "TV", "source_list": ["TV", "HDMI"]},
        )
        hass.set_state("media_player.plex_samsung", "idle")
        self.assertFalse(hardware.is_zone_hot_ready(hass, zone_cold))
        self.assertEqual(
            hardware.detect_zone_start_phase(hass, zone_cold), const.START_PHASE_WARM
        )
        hass.set_state(
            "media_player.samsung_frame",
            "on",
            {"source": "TV", "source_list": ["TV", "HDMI"], "app_id": const.SAMSUNG_TIZEN_PLEX_APP_ID},
        )
        self.assertTrue(hardware.is_zone_hot_ready(hass, zone_cold))

        # 30s Tizen splash-screen boot: 404 -> unavailable -> 500x3 -> ReadTimeout + PMS session
        hass2 = _FakeHass(
            {
                "media_player.samsung_frame": _FakeState(
                    "on", {"source": "TV", "source_list": ["TV", "HDMI"]}
                ),
                "media_player.plex_samsung": _FakeState("idle"),
            }
        )
        play_step = 0
        active_sessions: list[models.PlexActiveSession] = []

        async def _tizen_boot_seq(domain, service, data, blocking=True, return_response=False):
            nonlocal play_step
            hass2.calls.append((domain, service, dict(data)))
            if domain == "button" and service == "press":
                if hass2.states.get("media_player.plex_samsung").state == "unavailable":
                    hass2.set_state("media_player.plex_samsung", "idle")
                return {}
            if (
                domain == "media_player"
                and service == "play_media"
                and data.get("entity_id") == "media_player.plex_samsung"
            ):
                play_step += 1
                if play_step == 1:
                    hass2.set_state("media_player.plex_samsung", "unavailable")
                    raise RuntimeError("(404) not_found; /player/playback/playMedia")
                if play_step in (2, 3, 4):
                    raise RuntimeError("(500) internal_server_error; /player/playback/playMedia")
                active_sessions.append(
                    models.PlexActiveSession(
                        session_key="99",
                        rating_key=7642,
                        media_type="movie",
                        title="Хищник: Планета смерти",
                        display_title="Хищник: Планета смерти",
                        view_offset_ms=2000,
                        player_machine_id="sam2019",
                        player_title="Samsung TV 2019",
                        player_state="playing",
                    )
                )
                raise RuntimeError("Read timed out. (read timeout=30)")
            return {}

        hass2.services.async_call = _tizen_boot_seq
        fake_api = types.SimpleNamespace(
            async_get_active_sessions=lambda: asyncio.sleep(0, result=active_sessions)
        )
        saved_old_zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_samsung",
            const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
            const.CONF_HARDWARE_ENTITY: "media_player.samsung_frame",
            const.CONF_PLAY_RETRY_COUNT: 3,
            const.CONF_PLAY_RETRY_DELAY: 0.02,
        }
        ok, retries, err = await hardware.async_play_on_zone_with_retry(
            hass2,
            {const.CONF_SERVER_ID: "srv1", const.CONF_SCAN_BUTTON: "button.plex_scan"},
            saved_old_zone,
            {"plex_key": 7642},
            "movie",
            api=fake_api,
        )
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(play_step, 5)
        self.assertGreaterEqual(retries, 4)
        self.assertIn(
            (
                "media_player",
                "play_media",
                {
                    "entity_id": "media_player.samsung_frame",
                    "media_content_type": "app",
                    "media_content_id": const.SAMSUNG_TIZEN_PLEX_APP_ID,
                },
            ),
            hass2.calls,
        )


    async def test_v116_overlapping_zone_launch_cancels_superseded_task(self) -> None:
        hass = _FakeHass(
            {
                "media_player.samsung_frame": _FakeState("off"),
                "media_player.plex_samsung": _FakeState("unavailable"),
            }
        )
        m1 = models.PlexMediaItem(8298, "1", "Фильмы", "movie", "Один дома", "один дома", "Home Alone", "home alone", 1990)
        m2 = models.PlexMediaItem(7642, "1", "Фильмы", "movie", "Хищник", "хищник", "Predator", "predator", 2025)
        index = models.PlexIndexData(items_by_type={"movie": [m1, m2]}, items_by_key={8298: m1, 7642: m2})
        zone_sub = types.SimpleNamespace(
            subentry_id="z_bed",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Спальня",
            data={
                const.CONF_ZONE_NAME: "Спальня",
                const.CONF_IS_DEFAULT: True,
                const.CONF_PLEX_CLIENT: "media_player.plex_samsung",
                const.CONF_HARDWARE_ENTITY: "media_player.samsung_frame",
                const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
                const.CONF_OPTIMISTIC_MODE: True,
            },
        )
        runtime = types.SimpleNamespace(
            coordinator=types.SimpleNamespace(data=index),
            api=types.SimpleNamespace(),
            recent_zone_launches={},
            active_zone_tasks={},
            saved_resume_offsets={},
            last_launch=None,
        )
        entry = types.SimpleNamespace(
            entry_id="e1",
            data={const.CONF_SERVER_ID: "srv1"},
            subentries={"z_bed": zone_sub},
            runtime_data=runtime,
        )
        gate = asyncio.Event()

        async def _slow_boot(*_a, **_kw):
            await gate.wait()
            return const.START_PHASE_COLD, 1.0

        orig_boot = services.async_ensure_zone_ready
        services.async_ensure_zone_ready = _slow_boot
        try:
            res1 = await services.async_execute_play_request(hass, entry, {"rating_key": 8298, "zone": "Спальня"})
            self.assertTrue(res1.success)
            task1 = runtime.active_zone_tasks.get("z_bed")
            self.assertIsNotNone(task1)
            self.assertFalse(task1.done())

            res2 = await services.async_execute_play_request(hass, entry, {"rating_key": 7642, "zone": "Спальня"})
            self.assertTrue(res2.success)
            self.assertTrue(task1.cancelled())
            task2 = runtime.active_zone_tasks.get("z_bed")
            self.assertIsNotNone(task2)
            self.assertIsNot(task1, task2)
            await services._cancel_zone_active_task(runtime, "z_bed")
            self.assertTrue(task2.cancelled())
        finally:
            services.async_ensure_zone_ready = orig_boot


    def test_v116_roulette_collage_clean_number_badges_and_docs(self) -> None:
        drawn_rects: list[tuple[list[int], dict]] = []
        drawn_texts: list[tuple[str, dict]] = []

        class _StubImg:
            def __init__(self, mode="RGB", size=(400, 600), color=None):
                self.mode, self.size = mode, size
                self.width, self.height = size

            def convert(self, mode):
                return _StubImg(mode, self.size)

            def resize(self, size, resample=None):
                return _StubImg(self.mode, size)

            def paste(self, im, box=None, mask=None):
                return None

            def getbbox(self):
                return (0, 0, 12, 16)

            def crop(self, box):
                return _StubImg(self.mode, (12, 16))

            def save(self, fp, format="JPEG", **kw):
                fp.write(b"\xff\xd8\xff\xe0JFIF\xff\xd9")

        class _StubDraw:
            def __init__(self, im, mode=None):
                self._im = im

            def rounded_rectangle(self, xy, **kw):
                drawn_rects.append((list(xy), kw))

            def textbbox(self, xy, text, font=None, stroke_width=0):
                return (0, 0, 28 * len(str(text)), 42)

            def text(self, xy, text, **kw):
                drawn_texts.append((str(text), kw))

        pil_mod = types.ModuleType("PIL")
        pil_img = types.ModuleType("PIL.Image")
        pil_img.new = lambda mode, size, color=None: _StubImg(mode, size, color)
        pil_img.open = lambda fp: _StubImg("RGB", (400, 600))
        pil_img.Resampling = types.SimpleNamespace(LANCZOS=1, NEAREST=0)
        pil_draw = types.ModuleType("PIL.ImageDraw")
        pil_draw.Draw = lambda im, mode=None: _StubDraw(im, mode)
        pil_font = types.ModuleType("PIL.ImageFont")
        pil_font.truetype = lambda path, size: ("ttf", size)
        pil_font.load_default = lambda size=None: ("default", size)
        pil_mod.Image, pil_mod.ImageDraw, pil_mod.ImageFont = pil_img, pil_draw, pil_font

        keys = ("PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont")
        saved_pil = {k: sys.modules.get(k) for k in keys}
        for k, m in zip(keys, (pil_mod, pil_img, pil_draw, pil_font), strict=True):
            sys.modules[k] = m
        try:
            jpeg = roulette.build_poster_collage_sync(
                [(None, 1, "Логан", 9.1), (None, 2, "Аквамен", 7.2)],
                layout=const.COLLAGE_LAYOUT_ROW,
            )
            self.assertTrue(jpeg.startswith(b"\xff\xd8"))
            self.assertEqual(len(drawn_rects), 4)
            badge_box, badge_kw = drawn_rects[1]
            self.assertEqual(badge_box[2] - badge_box[0], 80)
            self.assertGreaterEqual(badge_kw.get("width", 0), 3)
            rendered = [t for t, _ in drawn_texts]
            self.assertIn("1", rendered)
            self.assertIn("2", rendered)
            self.assertFalse(any("9.1" in s or "7.2" in s or "★" in s for s in rendered))
        finally:
            for k, old_v in saved_pil.items():
                if old_v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = old_v

        readme_ru = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_en = (ROOT / "README.en.md").read_text(encoding="utf-8")
        for doc in (readme_ru, readme_en):
            self.assertIn("https://github.com/Eugen417/HA-Yandex-generative_ai_conversation", doc)
            self.assertIn("https://github.com/Eugen417/HA-GigaChat-generative_ai_conversation", doc)
        self.assertNotIn("под капотом", readme_ru.lower())
        self.assertNotIn("уникальные инженерные решения", readme_ru.lower())
        self.assertNotIn("under-the-hood", readme_en.lower())
        self.assertNotIn("under the hood", readme_en.lower())


    async def test_v117_samsung_tizen_closed_loop_resume_seek_and_cancel(self) -> None:
        # Simulate Samsung Tizen starting movie 7926 at 0:00 in 'buffering', ignoring 1st early seek,
        # then accepting 2nd verified media_seek -> 5142s confirmed via PMS /status/sessions!
        hass = _FakeHass(
            {
                "media_player.plex_samsung": _FakeState(
                    "buffering", {"media_content_id": "8298", "media_position": 120}
                )
            }
        )
        hass.ignore_next_seek = 1
        pms_sessions: list[models.PlexActiveSession] = []

        orig_call = hass.services.async_call

        async def _tizen_seek_call(domain, service, data, blocking=True, return_response=False):
            if domain == "media_player" and service == "media_seek":
                hass.calls.append((domain, service, dict(data)))
                if hass.ignore_next_seek > 0:
                    hass.ignore_next_seek -= 1
                    # Tizen AVPlay was still initializing and dropped 1st seek (pos stays at 2s)
                    hass.set_state(
                        "media_player.plex_samsung",
                        "playing",
                        {"media_content_id": "7926", "media_position": 2},
                    )
                else:
                    # 2nd seek succeeds -> PMS session updates to 5142s even before HA entity polls!
                    pms_sessions.append(
                        models.PlexActiveSession(
                            session_key="77",
                            rating_key=7926,
                            media_type="movie",
                            title="Голодные игры: И вспыхнет пламя",
                            display_title="Голодные игры: И вспыхнет пламя",
                            view_offset_ms=5_142_000,
                            player_machine_id="sam2019",
                            player_title="Samsung TV 2019",
                            player_state="playing",
                        )
                    )
                return {}
            return await orig_call(domain, service, data, blocking=blocking, return_response=return_response)

        hass.services.async_call = _tizen_seek_call
        async def _get_pms_sessions():
            return list(pms_sessions)

        fake_api = types.SimpleNamespace(async_get_active_sessions=_get_pms_sessions)
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_samsung",
            const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
        }

        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.002, **kw)
        try:
            worker = asyncio.create_task(
                hardware._async_ensure_resume_seek(
                    hass,
                    "media_player.plex_samsung",
                    5142,
                    zone_data=zone,
                    payload={"plex_key": 7926, "resume": 1, "offset": 5142},
                    api=fake_api,
                )
            )
            # While still 'buffering' on old item 8298, no seek fires yet
            await orig_sleep(0.001)
            self.assertEqual(
                [c for c in hass.calls if c[:2] == ("media_player", "media_seek")], []
            )
            # Transition Tizen to 'playing' new item 7926 at 0s -> triggers closed-loop seek!
            hass.set_state(
                "media_player.plex_samsung",
                "playing",
                {"media_content_id": "7926", "media_position": 0},
            )
            await worker
        finally:
            hardware.asyncio.sleep = orig_sleep

        seeks = [c for c in hass.calls if c[:2] == ("media_player", "media_seek")]
        self.assertEqual(len(seeks), 2)
        self.assertEqual(
            seeks[-1],
            ("media_player", "media_seek", {"entity_id": "media_player.plex_samsung", "seek_position": 5142}),
        )

        # Starting a new movie from 0:00 must cancel any pending resume-seek task on that client
        hang_gate = asyncio.Event()
        fake_seek_task = asyncio.create_task(hang_gate.wait())
        hardware._get_seek_tasks_map(hass)["media_player.plex_samsung"] = fake_seek_task
        hardware._cancel_pending_seek_task(hass, "media_player.plex_samsung")
        await asyncio.sleep(0)
        self.assertTrue(fake_seek_task.cancelled())


class TestV1018FastSeekAndSpinup(unittest.IsolatedAsyncioTestCase):
    async def test_lg_and_samsung_preserve_offset_and_seek_strictly_after_proxy(self) -> None:
        """v1.0.19: Preserve integer offset in playMedia AND seek strictly after proxy completion."""
        entry_data = {
            const.CONF_SERVER_ID: "0123456789abcdef0123456789abcdef01234567",
            const.CONF_SERVER_NAME: "HomeNAS",
        }
        movie_payload = {"plex_key": 7563, "resume": 1, "offset": 1801}

        for prof in (const.PROFILE_LG_WEBOS, const.PROFILE_SAMSUNG_TIZEN):
            cid, ctype = hardware.build_plex_media_args(
                entry_data, movie_payload, "movie", power_profile=prof
            )
            parsed_m = json.loads(cid.removeprefix("plex://"))
            self.assertEqual(parsed_m["plex_key"], 7563)
            self.assertEqual(parsed_m["offset"], 1801)
            self.assertEqual(parsed_m["resume"], 1)
            self.assertEqual(ctype, "movie")

        start_cid, _ = hardware.build_plex_media_args(
            entry_data, {"plex_key": 7563, "resume": 0, "offset": 0}, "movie", power_profile=const.PROFILE_LG_WEBOS
        )
        self.assertEqual(start_cid, "plex://0123456789abcdef0123456789abcdef01234567/7563")

        ep_payload = {"plex_key": 8801, "continuous": 1, "resume": 1, "offset": 920}
        ep_cid, ep_type = hardware.build_plex_media_args(
            entry_data, ep_payload, "episode", power_profile=const.PROFILE_LG_WEBOS
        )
        self.assertEqual(ep_type, "episode")
        ep_json = json.loads(ep_cid.removeprefix("plex://"))
        self.assertEqual(ep_json["plex_key"], 8801)
        self.assertEqual(ep_json["continuous"], 1)
        self.assertEqual(ep_json["offset"], 920)
        self.assertEqual(ep_json["resume"], 1)

        hass = _FakeHass(
            {
                "media_player.plex_samsung_3": _FakeState(
                    "playing", {"media_content_id": "7563", "media_position": 0.5}
                )
            }
        )
        hanging_play_gate = asyncio.Event()
        hanging_play_task = asyncio.create_task(hanging_play_gate.wait())
        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.002, **kw)
        try:
            seek_task = asyncio.create_task(
                hardware._async_ensure_resume_seek(
                    hass,
                    "media_player.plex_samsung_3",
                    1801,
                    play_call_task=hanging_play_task,
                    zone_data={const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN},
                    payload=movie_payload,
                )
            )
            await orig_sleep(0.01)
            self.assertEqual([c for c in hass.calls if c[:2] == ("media_player", "media_seek")], [])
            hanging_play_gate.set()
            await asyncio.wait_for(seek_task, timeout=1.0)
        finally:
            hardware.asyncio.sleep = orig_sleep
            hanging_play_gate.set()
            await asyncio.gather(hanging_play_task, return_exceptions=True)

        seeks = [c for c in hass.calls if c[:2] == ("media_player", "media_seek")]
        self.assertEqual(len(seeks), 1)
        self.assertEqual(seeks[0][2]["seek_position"], 1801)

    async def test_sleeping_nas_disks_spinup_waits_for_playing_before_seek(self) -> None:
        """While NAS disks spin up ('idle'/'buffering'), seek waits reactively until 'playing'."""
        hass = _FakeHass(
            {"media_player.plex_lg": _FakeState("buffering", {"media_content_id": "7563"})}
        )
        pms_state = {"playing": False}

        class _SpinupApi:
            async def async_get_active_sessions(self):
                if not pms_state["playing"]:
                    return []
                return [
                    models.PlexActiveSession(
                        session_key="10",
                        rating_key=7563,
                        media_type="movie",
                        title="28 лет спустя",
                        display_title="28 лет спустя",
                        view_offset_ms=1000,
                        player_machine_id="lg77",
                        player_title="LG OLED77G1RLA",
                        player_state="playing",
                    )
                ]

        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.005, **kw)
        try:
            seek_worker = asyncio.create_task(
                hardware._async_ensure_resume_seek(
                    hass,
                    "media_player.plex_lg",
                    1801,
                    zone_data={const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS},
                    payload={"plex_key": 7563, "resume": 1, "offset": 1801},
                    api=_SpinupApi(),
                )
            )
            await orig_sleep(0.015)
            self.assertEqual(
                [c for c in hass.calls if c[:2] == ("media_player", "media_seek")], []
            )
            pms_state["playing"] = True
            await asyncio.wait_for(seek_worker, timeout=1.0)
        finally:
            hardware.asyncio.sleep = orig_sleep

        seeks = [c for c in hass.calls if c[:2] == ("media_player", "media_seek")]
        self.assertGreaterEqual(len(seeks), 1)
        self.assertEqual(seeks[0][2]["seek_position"], 1801)

    async def test_plex_client_suffix_auto_resolution_and_bounded_media_stop(self) -> None:
        """Auto-follow _3 recreated Plex client entity and never hang 30s on media_stop."""
        hass = _FakeHass(
            {
                "media_player.plex_plex_for_samsung_tv_2019": _FakeState("unavailable"),
                "media_player.plex_plex_for_samsung_tv_2019_2": _FakeState("unavailable"),
                "media_player.plex_plex_for_samsung_tv_2019_3": _FakeState("playing"),
                "media_player.samsung_tv": _FakeState("on"),
            }
        )
        hass.states.async_all = lambda dom=None: [
            types.SimpleNamespace(entity_id=eid, state=st.state, attributes=st.attributes)
            for eid, st in hass._states.items()
        ]
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_plex_for_samsung_tv_2019",
            const.CONF_HARDWARE_ENTITY: "media_player.samsung_tv",
            const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
        }
        resolved = hardware.resolve_plex_client(hass, zone)
        self.assertEqual(resolved, "media_player.plex_plex_for_samsung_tv_2019_3")

        stop_hang = asyncio.Event()
        orig_call = hass.services.async_call

        async def _hanging_stop(domain, service, data, blocking=True, return_response=False):
            if domain == "media_player" and service == "media_stop":
                hass.calls.append((domain, service, dict(data)))
                await stop_hang.wait()
                return {}
            return await orig_call(domain, service, data, blocking=blocking, return_response=return_response)

        hass.services.async_call = _hanging_stop
        orig_stop_to = hardware.MEDIA_STOP_TIMEOUT_SEC
        hardware.MEDIA_STOP_TIMEOUT_SEC = 0.03
        try:
            await asyncio.wait_for(hardware.async_power_off_zone(hass, zone), timeout=0.5)
        finally:
            hardware.MEDIA_STOP_TIMEOUT_SEC = orig_stop_to
            stop_hang.set()
            await asyncio.sleep(0)

        self.assertIn(
            ("media_player", "media_stop", {"entity_id": "media_player.plex_plex_for_samsung_tv_2019_3"}),
            hass.calls,
        )
        self.assertIn(
            ("media_player", "turn_off", {"entity_id": "media_player.samsung_tv"}),
            hass.calls,
        )

    async def test_handoff_media_type_filter_and_season_shuffle(self) -> None:
        """'Continue my show' skips movies in On Deck; season shuffle picks within that season."""
        hass = _FakeHass({"media_player.plex_lg": _FakeState("idle")})
        lg_sub = types.SimpleNamespace(
            subentry_id="z_lg",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Зал",
            data={
                const.CONF_ZONE_NAME: "Зал",
                const.CONF_PLEX_CLIENT: "media_player.plex_lg",
                const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
                const.CONF_PLAY_RETRY_COUNT: 0,
            },
        )

        class _DeckApi:
            async def async_get_active_sessions(self):
                return []

            async def async_get_continue_watching(self):
                return [
                    models.PlexMediaItem(
                        rating_key=111,
                        section_id="1",
                        library_title="Фильмы",
                        media_type="movie",
                        title="Случайный фильм",
                        title_norm="случайный фильм",
                        original_title="",
                        orig_norm="",
                        view_offset_ms=120_000,
                    ),
                    models.PlexMediaItem(
                        rating_key=222,
                        section_id="2",
                        library_title="Сериалы",
                        media_type="episode",
                        title="Пилот",
                        title_norm="пилот",
                        original_title="",
                        orig_norm="",
                        grandparent_title="Укрытие",
                        season_index=2,
                        episode_index=4,
                        view_offset_ms=600_000,
                    ),
                ]

        entry = types.SimpleNamespace(
            entry_id="e1",
            data={const.CONF_SERVER_ID: "srv1"},
            subentries={"z_lg": lg_sub},
            runtime_data=types.SimpleNamespace(api=_DeckApi()),
        )
        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.001, **kw)
        try:
            res = await hardware.async_handoff_or_resume(
                hass, entry, target_zone=lg_sub, media_type="show"
            )
            await orig_sleep(0.02)
        finally:
            hardware.asyncio.sleep = orig_sleep

        self.assertTrue(res.success)
        self.assertEqual(res.rating_key, 222)
        self.assertIn("Укрытие S02E04", res.matched_title)

        client = api.PlexApiClient(hass, "http://127.0.0.1:32400", "tok")

        async def _fake_leaves(_path, params=None):
            return {
                "Metadata": [
                    {"ratingKey": "10", "grandparentTitle": "Friends", "title": "Ep1", "parentIndex": "1", "index": "1"},
                    {"ratingKey": "21", "grandparentTitle": "Friends", "title": "S2E1", "parentIndex": "2", "index": "1"},
                    {"ratingKey": "22", "grandparentTitle": "Friends", "title": "S2E2", "parentIndex": "2", "index": "2"},
                ]
            }

        client._async_get_container = _fake_leaves
        picked_keys = set()
        for _ in range(25):
            ep = await client.async_resolve_episode(99, season=2, shuffle=True)
            self.assertIsNotNone(ep)
            self.assertIn(ep[0], (21, 22))
            picked_keys.add(ep[0])
        self.assertEqual(picked_keys, {21, 22})


class TestV1019ColdResumeAndHandoff(unittest.IsolatedAsyncioTestCase):
    async def test_direct_integer_seek_to_avoids_ha_float_bug(self) -> None:
        """PlexApiClient.async_seek_client_player must send pure integer ms ('1801000', never '1801000.0')."""
        captured: list[tuple[str, dict, dict]] = []

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            def get(self, url, headers=None, params=None, timeout=None):
                captured.append((url, dict(headers or {}), dict(params or {})))
                return _Resp()

        client = api.PlexApiClient(_FakeHass(), "http://192.0.2.10:32400", "tok123")
        client._session = lambda: _Sess()
        ok = await client.async_seek_client_player("sam_machine_3", 1801 * 1000, "movie")
        self.assertTrue(ok)
        self.assertEqual(len(captured), 1)
        url, headers, params = captured[0]
        self.assertTrue(url.endswith("/player/playback/seekTo"))
        self.assertEqual(headers["X-Plex-Target-Client-Identifier"], "sam_machine_3")
        self.assertEqual(params["offset"], "1801000")
        self.assertNotIn(".", params["offset"])
        self.assertEqual(params["type"], "video")
        self.assertEqual(params["commandID"], "1")

    async def test_lg_cold_start_purges_stale_idle_and_seeks_integer_after_proxy(self) -> None:
        """Cold LG webOS with stale overnight 'idle' refreshes via scan_clients and seeks after 30s proxy."""
        hass = _FakeHass(
            {
                "media_player.lg_77": _FakeState("off"),
                "media_player.plex_lg": _FakeState("idle"),
            }
        )
        scan_presses = 0
        orig_call = hass.services.async_call

        async def _lg_cold_calls(domain, service, data, blocking=True, return_response=False):
            nonlocal scan_presses
            if domain == "media_player" and service == "turn_on":
                hass.calls.append((domain, service, dict(data)))
                hass.set_state(
                    "media_player.lg_77",
                    "on",
                    {"source": "Live TV", "source_list": ["Live TV", "Plex"]},
                )
                return {}
            if domain == "button" and service == "press":
                scan_presses += 1
                hass.calls.append((domain, service, dict(data)))
                if scan_presses == 1:
                    hass.set_state("media_player.plex_lg", "unavailable")
                else:
                    hass.set_state("media_player.plex_lg", "idle")
                return {}
            return await orig_call(domain, service, data, blocking=blocking, return_response=return_response)

        hass.services.async_call = _lg_cold_calls
        zone = {
            const.CONF_PLEX_CLIENT: "media_player.plex_lg",
            const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
            const.CONF_HARDWARE_ENTITY: "media_player.lg_77",
            const.CONF_COLD_BOOT_TIMEOUT: 1,
            const.CONF_COLD_OS_DELAY: 0,
            const.CONF_COLD_APP_DELAY: 0,
            const.CONF_SCAN_INTERVAL: 1,
            const.CONF_SCAN_TIMEOUT: 4,
        }
        phase, _ = await hardware.async_ensure_zone_ready(
            hass, {const.CONF_SCAN_BUTTON: "button.plex_scan"}, zone
        )
        self.assertEqual(phase, const.START_PHASE_COLD)
        self.assertGreaterEqual(scan_presses, 2)
        self.assertEqual(hass.states.get("media_player.plex_lg").state, "idle")

        direct_seeks: list[tuple[str, int, str]] = []
        cur_offset_ms = 2000

        class _LgApi:
            async def async_get_active_sessions(self):
                return [
                    models.PlexActiveSession(
                        session_key="1",
                        rating_key=7563,
                        media_type="movie",
                        title="28 лет спустя",
                        display_title="28 лет спустя",
                        view_offset_ms=cur_offset_ms,
                        player_machine_id="lg_machine_77",
                        player_title="LG OLED77",
                        player_state="playing",
                    )
                ]

            async def async_seek_client_player(self, mid: str, off_ms: int, mtype: str = "video") -> bool:
                nonlocal cur_offset_ms
                direct_seeks.append((mid, off_ms, mtype))
                cur_offset_ms = off_ms
                return True

        hass.set_state("media_player.plex_lg", "playing", {"media_content_id": "7563", "media_position": 2})
        proxy_gate = asyncio.Event()
        proxy_task = asyncio.create_task(proxy_gate.wait())
        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.002, **kw)
        try:
            w = asyncio.create_task(
                hardware._async_ensure_resume_seek(
                    hass,
                    "media_player.plex_lg",
                    1801,
                    play_call_task=proxy_task,
                    zone_data=zone,
                    payload={"plex_key": 7563, "resume": 1, "offset": 1801},
                    media_type="movie",
                    api=_LgApi(),
                )
            )
            await orig_sleep(0.01)
            self.assertEqual(direct_seeks, [])
            proxy_gate.set()
            await asyncio.wait_for(w, timeout=1.0)
        finally:
            hardware.asyncio.sleep = orig_sleep

        self.assertEqual(direct_seeks, [("lg_machine_77", 1801000, "movie")])

    async def test_samsung_cold_boot_follows_suffix_3_and_handoff_blocks_ai_glitch(self) -> None:
        """Samsung switches to _3 when _3 wakes, and 'перенос фильма из зала в спальню' turns off LG & resumes 7563."""
        import contextvars

        short_movies = [
            models.PlexMediaItem(1, "1", "1) Фильмы", "movie", "Зал", "зал", "Hall", "hall", 2020),
            models.PlexMediaItem(2, "1", "1) Фильмы", "movie", "Нос", "нос", "Nose", "nose", 2019),
            models.PlexMediaItem(
                7563, "1", "1) Фильмы", "movie", "28 лет спустя", "28 лет спустя", "28 Years Later", "28 years later", 2025
            ),
            models.PlexMediaItem(
                8277, "1", "1) Фильмы", "movie", "3 дня на убийство", "3 дня на убийство", "3 Days to Kill", "3 days to kill", 2014
            ),
        ]
        hit_bad, _ = search.find_best_media(short_movies, ["перенос фильма из зала"], threshold=0.78)
        self.assertIsNone(hit_bad)

        hass = _FakeHass(
            {
                "media_player.apple_tv": _FakeState("off"),
                "media_player.plex_atv": _FakeState("unavailable"),
                "media_player.lg_77": _FakeState("on", {"source": "Plex", "source_list": ["Plex"]}),
                "media_player.plex_lg": _FakeState("idle"),
                "media_player.samsung_tv": _FakeState("off"),
                "media_player.plex_plex_for_samsung_tv_2019": _FakeState("unavailable"),
                "media_player.plex_plex_for_samsung_tv_2019_3": _FakeState("unavailable"),
            }
        )
        hass.states.async_all = lambda dom=None: [
            types.SimpleNamespace(entity_id=eid, state=st.state, attributes=st.attributes)
            for eid, st in hass._states.items()
        ]
        orig_call = hass.services.async_call

        async def _custom_calls(domain, service, data, blocking=True, return_response=False):
            if domain == "media_player" and service == "turn_off" and "device_id" in data:
                raise RuntimeError("DLNA entity does not support media_player.turn_off")
            if domain == "media_player" and service == "turn_on":
                hass.calls.append((domain, service, dict(data)))
                hass.set_state("media_player.samsung_tv", "on", {"source": "TV", "source_list": ["TV", "HDMI"]})
                return {}
            if domain == "button" and service == "press":
                hass.calls.append((domain, service, dict(data)))
                hass.set_state("media_player.plex_plex_for_samsung_tv_2019_3", "idle")
                return {}
            return await orig_call(domain, service, data, blocking=blocking, return_response=return_response)

        hass.services.async_call = _custom_calls

        z_atv = types.SimpleNamespace(
            subentry_id="z_atv",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Apple TV",
            data={
                const.CONF_ZONE_NAME: "Apple TV",
                const.CONF_AREA_ID: "area_zal",
                const.CONF_PLEX_CLIENT: "media_player.plex_atv",
                const.CONF_HARDWARE_ENTITY: "media_player.apple_tv",
                const.CONF_POWER_PROFILE: const.PROFILE_APPLE_TV,
                const.CONF_IS_DEFAULT: False,
            },
        )
        z_lg = types.SimpleNamespace(
            subentry_id="z_lg",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="LG Зал",
            data={
                const.CONF_ZONE_NAME: "LG Зал",
                const.CONF_AREA_ID: "area_zal",
                const.CONF_ALIASES: "зал, lg",
                const.CONF_PLEX_CLIENT: "media_player.plex_lg",
                const.CONF_HARDWARE_DEVICE_ID: "dev_lg_77",
                const.CONF_HARDWARE_ENTITY: "media_player.lg_77",
                const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
                const.CONF_IS_DEFAULT: True,
                const.CONF_TURN_OFF_ON_HANDOFF: True,
            },
        )
        z_bed = types.SimpleNamespace(
            subentry_id="z_bed",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Спальня",
            data={
                const.CONF_ZONE_NAME: "Спальня",
                const.CONF_AREA_ID: "area_bed",
                const.CONF_ALIASES: "самсунг",
                const.CONF_PLEX_CLIENT: "media_player.plex_plex_for_samsung_tv_2019",
                const.CONF_HARDWARE_ENTITY: "media_player.samsung_tv",
                const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
                const.CONF_COLD_BOOT_TIMEOUT: 1,
                const.CONF_COLD_OS_DELAY: 0,
                const.CONF_COLD_APP_DELAY: 0,
                const.CONF_SCAN_INTERVAL: 1,
                const.CONF_SCAN_TIMEOUT: 3,
                const.CONF_PLAY_RETRY_COUNT: 0,
                const.CONF_HANDOFF_REWIND: 5,
            },
        )

        direct_seeks: list[tuple[str, int]] = []
        sam_pos_ms = 1000

        class _HandoffApi:
            async def async_get_active_sessions(self):
                if hass.states.get("media_player.plex_plex_for_samsung_tv_2019_3").state == "playing":
                    return [
                        models.PlexActiveSession(
                            session_key="55",
                            rating_key=7563,
                            media_type="movie",
                            title="28 лет спустя",
                            display_title="28 лет спустя",
                            view_offset_ms=sam_pos_ms,
                            player_machine_id="sam_2019_3_id",
                            player_title="Samsung TV 2019",
                            player_state="playing",
                        )
                    ]
                return []

            async def async_get_item_playback_state(self, rk: int):
                return (12_000, 7_200_000)

            async def async_get_continue_watching(self):
                return [short_movies[3]]

            async def async_seek_client_player(self, mid: str, off_ms: int, mtype: str = "video") -> bool:
                nonlocal sam_pos_ms
                direct_seeks.append((mid, off_ms))
                sam_pos_ms = off_ms
                return True

        index = models.PlexIndexData(
            items_by_type={"movie": short_movies, "show": [], "music": []},
            items_by_key={m.rating_key: m for m in short_movies},
        )
        runtime = models.PlexSmartRuntimeData(
            coordinator=types.SimpleNamespace(data=index),
            api=_HandoffApi(),
            roulette_store=types.SimpleNamespace(),
            last_launch=models.LaunchResult(
                success=True,
                zone_name="LG Зал",
                matched_title="28 лет спустя (2025)",
                rating_key=7563,
                media_type="movie",
                boot_seconds=2.0,
                retries_used=0,
                handoff_from=None,
                message="ok",
            ),
            saved_resume_offsets={7563: 1801},
        )
        entry = types.SimpleNamespace(
            entry_id="e1",
            data={
                const.CONF_SERVER_ID: "0123456789abcdef0123456789abcdef01234567",
                const.CONF_SERVER_NAME: "HomeNAS",
                const.CONF_SCAN_BUTTON: "button.plex_scan",
            },
            subentries={"z_atv": z_atv, "z_lg": z_lg, "z_bed": z_bed},
            runtime_data=runtime,
        )
        hass.config_entries = types.SimpleNamespace(async_entries=lambda d: [entry])

        fake_chat_var: contextvars.ContextVar = contextvars.ContextVar("current_chat_log", default=None)
        conv_mod = types.ModuleType("homeassistant.components.conversation")
        chat_log_mod = types.ModuleType("homeassistant.components.conversation.chat_log")
        chat_log_mod.current_chat_log = fake_chat_var
        sys.modules["homeassistant.components.conversation"] = conv_mod
        sys.modules["homeassistant.components.conversation.chat_log"] = chat_log_mod

        fake_area_map = {
            "area_zal": types.SimpleNamespace(name="Зал", aliases=["гостиная"]),
            "area_bed": types.SimpleNamespace(name="Спальня", aliases=[]),
        }
        orig_ar = sys.modules["homeassistant.helpers.area_registry"].async_get
        sys.modules["homeassistant.helpers.area_registry"].async_get = lambda h: types.SimpleNamespace(
            async_get_area=lambda aid: fake_area_map.get(aid)
        )
        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.002, **kw)
        token = fake_chat_var.set(
            types.SimpleNamespace(
                content=[types.SimpleNamespace(role="user", content="перенос фильма из зала в спальню")]
            )
        )
        try:
            play_tool = llm_api.SmartPlexPlayTool()
            tool_in = types.SimpleNamespace(
                tool_args={"media_type": "movie", "title": "3 дня на убийство", "zone": "Спальня", "resume": False}
            )
            wrapped = await play_tool.async_call(hass, tool_in, types.SimpleNamespace(device_id=None))
            self.assertTrue(wrapped.data["success"])
            self.assertEqual(wrapped.data["rating_key"], 7563)
            self.assertIn("28 лет спустя", wrapped.data["matched_title"])
            self.assertEqual(wrapped.data["handoff_from"], "LG Зал")

            seek_task = hardware._get_seek_tasks_map(hass).get("media_player.plex_plex_for_samsung_tv_2019_3")
            if seek_task is not None:
                await asyncio.wait_for(seek_task, timeout=1.0)
        finally:
            fake_chat_var.reset(token)
            hardware.asyncio.sleep = orig_sleep
            sys.modules["homeassistant.helpers.area_registry"].async_get = orig_ar

        self.assertIn(
            ("media_player", "turn_off", {"entity_id": "media_player.lg_77"}),
            hass.calls,
        )
        sam_plays = [
            d
            for dom, srv, d in hass.calls
            if dom == "media_player"
            and srv == "play_media"
            and d.get("entity_id") == "media_player.plex_plex_for_samsung_tv_2019_3"
        ]
        self.assertEqual(len(sam_plays), 1)
        sam_payload = json.loads(sam_plays[0]["media_content_id"].removeprefix("plex://"))
        self.assertEqual(sam_payload["plex_key"], 7563)
        self.assertEqual(sam_payload["offset"], 1796)
        self.assertEqual(direct_seeks, [("sam_2019_3_id", 1796000)])


class TestV1020ReverseHandoffAndControl(unittest.IsolatedAsyncioTestCase):
    def _make_zones(self):
        z_lg = types.SimpleNamespace(
            subentry_id="z_lg",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="LG Зал",
            data={
                const.CONF_ZONE_NAME: "LG Зал",
                const.CONF_PLEX_CLIENT: "media_player.plex_lg",
                const.CONF_HARDWARE_ENTITY: "media_player.televizor",
                const.CONF_POWER_PROFILE: const.PROFILE_LG_WEBOS,
                const.CONF_IS_DEFAULT: True,
                const.CONF_COLD_BOOT_TIMEOUT: 1,
                const.CONF_COLD_OS_DELAY: 0,
                const.CONF_COLD_APP_DELAY: 0,
                const.CONF_PLAY_RETRY_COUNT: 2,
                const.CONF_PLAY_RETRY_DELAY: 0.01,
                const.CONF_HANDOFF_REWIND: 5,
            },
        )
        z_bed = types.SimpleNamespace(
            subentry_id="z_bed",
            subentry_type=const.SUBENTRY_TYPE_ZONE,
            title="Спальня",
            data={
                const.CONF_ZONE_NAME: "Спальня",
                const.CONF_PLEX_CLIENT: "media_player.plex_sam_3",
                const.CONF_HARDWARE_ENTITY: "media_player.samsung_tv",
                const.CONF_POWER_PROFILE: const.PROFILE_SAMSUNG_TIZEN,
                const.CONF_IS_DEFAULT: False,
                const.CONF_TURN_OFF_ON_HANDOFF: True,
            },
        )
        return z_lg, z_bed

    async def test_reverse_handoff_bedroom_to_cold_lg_ignores_stale_samsung_session(self) -> None:
        """Live 25.11 scenario: Bedroom Samsung -> Cold LG with ghost TV 2019 session & 404."""
        hass = _FakeHass({
            "media_player.televizor": _FakeState("off"),
            "media_player.plex_lg": _FakeState("idle"),
            "media_player.samsung_tv": _FakeState("on", {"source": "Plex"}),
            "media_player.plex_sam_3": _FakeState("playing", {"media_content_id": "7865", "media_position": 3430}),
        })
        hass.states.async_all = lambda dom=None: [
            types.SimpleNamespace(entity_id=eid, state=st.state, attributes=st.attributes)
            for eid, st in hass._states.items()
        ]
        play_attempts, lg_pos_ms = 0, 2000
        orig_call = hass.services.async_call

        async def _live_calls(dom, srv, data, blocking=True, return_response=False):
            nonlocal play_attempts
            if dom == "media_player" and srv == "turn_on":
                hass.calls.append((dom, srv, dict(data)))
                hass.set_state("media_player.televizor", "on", {"source": "Live TV", "source_list": ["Live TV", "Plex"]})
                return {}
            if dom == "media_player" and srv == "select_source":
                hass.calls.append((dom, srv, dict(data)))
                hass.set_state("media_player.televizor", "on", {"source": "Plex", "source_list": ["Live TV", "Plex"]})
                return {}
            if dom == "media_player" and srv == "play_media" and data.get("entity_id") == "media_player.plex_lg":
                hass.calls.append((dom, srv, dict(data)))
                play_attempts += 1
                if play_attempts == 1:
                    raise RuntimeError("(404) not_found; /player/playback/playMedia")
                hass.set_state("media_player.plex_lg", "playing", {"media_content_id": "7865", "media_position": 2})
                return {}
            return await orig_call(dom, srv, data, blocking=blocking, return_response=return_response)

        hass.services.async_call = _live_calls
        fake_ents = {
            "media_player.plex_lg": types.SimpleNamespace(
                entity_id="media_player.plex_lg", domain="media_player", platform="plex",
                disabled=False, unique_id="srv1:lg_mid_77", device_id="dev_lg",
            ),
            "media_player.plex_sam_3": types.SimpleNamespace(
                entity_id="media_player.plex_sam_3", domain="media_player", platform="plex",
                disabled=False, unique_id="srv1:sam_mid_3", device_id="dev_sam",
            ),
        }
        orig_er = sys.modules["homeassistant.helpers.entity_registry"].async_get
        sys.modules["homeassistant.helpers.entity_registry"].async_get = lambda h: types.SimpleNamespace(
            async_get=lambda eid: fake_ents.get(eid), entities=fake_ents,
        )
        z_lg, z_bed = self._make_zones()
        direct_seeks: list[tuple[str, int]] = []

        class _GhostApi:
            async def async_get_active_sessions(self):
                out = [models.PlexActiveSession("10", 7865, "movie", "Битва", "Битва", 3_430_000, "sam_mid_3", "TV 2019", "playing")]
                if hass.states.get("media_player.plex_lg").state == "playing":
                    out.append(models.PlexActiveSession("11", 7865, "movie", "Битва", "Битва", lg_pos_ms, "lg_mid_77", "LG OLED77", "playing"))
                return out

            async def async_seek_client_player(self, mid: str, off_ms: int, mtype: str = "video") -> bool:
                nonlocal lg_pos_ms
                direct_seeks.append((mid, off_ms))
                if mid == "lg_mid_77":
                    lg_pos_ms = off_ms
                return True

        entry = types.SimpleNamespace(
            entry_id="e1", data={const.CONF_SERVER_ID: "srv1", const.CONF_SERVER_NAME: "HomeNAS"},
            subentries={"z_lg": z_lg, "z_bed": z_bed},
            runtime_data=models.PlexSmartRuntimeData(
                coordinator=types.SimpleNamespace(data=models.PlexIndexData()),
                api=_GhostApi(), roulette_store=types.SimpleNamespace(),
            ),
        )
        orig_sleep = hardware.asyncio.sleep
        hardware.asyncio.sleep = lambda _s, **kw: orig_sleep(0.002, **kw)
        try:
            res = await hardware.async_handoff_or_resume(hass, entry, target_zone=z_lg, source_zone=z_bed, turn_off_source=True)
            st = hardware._get_seek_tasks_map(hass).get("media_player.plex_lg")
            if st is not None:
                await asyncio.wait_for(st, timeout=1.0)
        finally:
            hardware.asyncio.sleep = orig_sleep
            sys.modules["homeassistant.helpers.entity_registry"].async_get = orig_er

        self.assertTrue(res.success)
        self.assertEqual(play_attempts, 2)
        self.assertEqual(direct_seeks, [("lg_mid_77", 3425000)])

    async def test_paused_resume_and_control_intents_seek_repeat_shuffle(self) -> None:
        """Pause/unpause/seek/repeat/shuffle + 'продолжи' on paused Bedroom stays in Bedroom."""
        hass = _FakeHass({
            "media_player.televizor": _FakeState("off"),
            "media_player.plex_lg": _FakeState("unavailable"),
            "media_player.samsung_tv": _FakeState("on"),
            "media_player.plex_sam_3": _FakeState("paused", {"media_content_id": "7865", "media_position": 600}),
        })
        z_lg, z_bed = self._make_zones()
        sent_cmds: list[tuple[str, str, dict]] = []
        seeks: list[tuple[str, int]] = []

        class _CtrlApi:
            async def async_get_active_sessions(self):
                return [models.PlexActiveSession("1", 7865, "movie", "Битва", "Битва", 600_000, "sam_mid_3", "TV 2019", "paused")]

            async def async_send_client_playback_command(self, mid, cmd, params=None, media_type="video"):
                sent_cmds.append((mid, cmd, dict(params or {})))
                return True

            async def async_seek_client_player(self, mid, off_ms, media_type="video"):
                seeks.append((mid, off_ms))
                return True

        runtime = models.PlexSmartRuntimeData(
            coordinator=types.SimpleNamespace(data=models.PlexIndexData()),
            api=_CtrlApi(), roulette_store=types.SimpleNamespace(),
            saved_resume_offsets={7865: 3425},
        )
        entry = types.SimpleNamespace(
            entry_id="e1", data={const.CONF_SERVER_ID: "srv1"},
            subentries={"z_lg": z_lg, "z_bed": z_bed}, runtime_data=runtime,
        )

        # 1. Zero dead zone in 30..64s protection
        self.assertEqual(hardware.resolve_protected_offset_sec(runtime.saved_resume_offsets, 7865, 38), 3425)

        # 2. Reverse handoff phrasing with 'обратно' and composite 'LG Зал' -> 'зал'
        rev = search.extract_pure_handoff_intent(entry, hass, "переключи фильм обратно из спальне в зал")
        self.assertIsNotNone(rev)
        self.assertEqual(rev["source_zone"], "Спальня")
        self.assertEqual(rev["target_zone"], "LG Зал")
        self.assertTrue(rev["is_transfer"])

        # 3. Deterministic <1ms transport & seek parser
        self.assertEqual(search.extract_playback_control_intent(entry, hass, "поставь на паузу")["action"], "pause")
        self.assertEqual(search.extract_playback_control_intent(entry, hass, "перемотай вперед на 10 секунд"), {"action": "seek_relative", "delta_sec": 10, "zone": None})
        self.assertEqual(search.extract_playback_control_intent(entry, hass, "отмотай назад на 5 секунд"), {"action": "seek_relative", "delta_sec": -5, "zone": None})
        self.assertEqual(search.extract_playback_control_intent(entry, hass, "перемотай на 30 минуту"), {"action": "seek_to", "offset_sec": 1800, "zone": None})
        self.assertEqual(search.extract_playback_control_intent(entry, hass, "перемотай в начало"), {"action": "restart", "zone": None})
        self.assertEqual(search.extract_playback_control_intent(entry, hass, "включи повтор трека"), {"action": "set_repeat", "repeat": "one", "zone": None})
        self.assertEqual(search.extract_playback_control_intent(entry, hass, "включи микс"), {"action": "set_shuffle", "shuffle": True, "zone": None})

        # 4. Saying 'продолжи' while Bedroom is paused picks Bedroom and calls media_play (NO TV power-off!)
        active_z = await hardware.async_resolve_active_playback_zone(entry, hass, None)
        self.assertEqual(active_z.subentry_id, "z_bed")
        res_unpause = await hardware.async_handoff_or_resume(hass, entry, target_zone=active_z)
        self.assertTrue(res_unpause.success)
        self.assertIn(("media_player", "media_play", {"entity_id": "media_player.plex_sam_3"}), hass.calls)
        self.assertNotIn(("media_player", "turn_off", {"entity_id": "media_player.samsung_tv"}), hass.calls)

        # 5. Relative seek (+10s -> 610000ms), absolute seek (1800000ms), restart (0ms), repeat & shuffle
        await hardware.async_control_zone_playback(hass, entry, z_bed, "seek_relative", delta_sec=10)
        await hardware.async_control_zone_playback(hass, entry, z_bed, "seek_to", offset_sec=1800)
        await hardware.async_control_zone_playback(hass, entry, z_bed, "restart")
        await hardware.async_control_zone_playback(hass, entry, z_bed, "set_repeat", repeat="one")
        await hardware.async_control_zone_playback(hass, entry, z_bed, "set_shuffle", shuffle=True)
        self.assertEqual(seeks, [("sam_mid_3", 610000), ("sam_mid_3", 1800000), ("sam_mid_3", 0)])
        self.assertEqual(
            sent_cmds,
            [
                ("sam_mid_3", "setParameters", {"repeat": "1"}),
                ("sam_mid_3", "setParameters", {"shuffle": "1"}),
            ],
        )



if __name__ == "__main__":
    unittest.main()



