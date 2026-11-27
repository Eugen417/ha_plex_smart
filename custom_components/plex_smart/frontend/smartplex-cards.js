/**
 * SmartPlex Lovelace Cards — Frosted Glass Showcase
 * Provides two independent, freely resizable Home Assistant cards:
 *   1. custom:smartplex-roulette-card ("SmartPlex: Кинорулетка")
 *   2. custom:smartplex-recent-card   ("SmartPlex: Недавно добавленные")
 */

const POSTER_BLOB_CACHE = new Map();
const POSTER_INFLIGHT = new Map();
const MAX_POSTER_BLOBS = 96;

function rememberPosterBlob(key, blobUrl) {
  if (POSTER_BLOB_CACHE.has(key)) {
    POSTER_BLOB_CACHE.delete(key);
  }
  POSTER_BLOB_CACHE.set(key, blobUrl);
  while (POSTER_BLOB_CACHE.size > MAX_POSTER_BLOBS) {
    const oldestKey = POSTER_BLOB_CACHE.keys().next().value;
    const oldUrl = POSTER_BLOB_CACHE.get(oldestKey);
    POSTER_BLOB_CACHE.delete(oldestKey);
    if (oldUrl && oldUrl.startsWith("blob:")) {
      try {
        URL.revokeObjectURL(oldUrl);
      } catch (_e) {
        // ignore
      }
    }
  }
}

async function fetchPosterBlobUrl(hass, entryId, ratingKey) {
  if (!hass || !entryId || !ratingKey) return "";
  const cacheKey = `${entryId}:${ratingKey}`;
  if (POSTER_BLOB_CACHE.has(cacheKey)) {
    return POSTER_BLOB_CACHE.get(cacheKey);
  }
  if (POSTER_INFLIGHT.has(cacheKey)) {
    return POSTER_INFLIGHT.get(cacheKey);
  }
  const path = `/api/plex_smart/poster/${encodeURIComponent(entryId)}/${encodeURIComponent(ratingKey)}.jpg`;
  const promise = (async () => {
    try {
      const resp = hass.fetchWithAuth
        ? await hass.fetchWithAuth(path)
        : await fetch(path, { credentials: "same-origin" });
      if (!resp || !resp.ok) return "";
      const blob = await resp.blob();
      if (!blob || !blob.size) return "";
      const objUrl = URL.createObjectURL(blob);
      rememberPosterBlob(cacheKey, objUrl);
      return objUrl;
    } catch (_err) {
      return "";
    } finally {
      POSTER_INFLIGHT.delete(cacheKey);
    }
  })();
  POSTER_INFLIGHT.set(cacheKey, promise);
  return promise;
}

function escapeHtml(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function findSmartPlexEntityId(hass, cardKind) {
  const states = hass?.states;
  if (!states) return "";
  const entries = Object.entries(states);

  // 1. Exact language-independent attribute marker
  for (const [eid, st] of entries) {
    if (st?.attributes?.ui_card_kind === cardKind) return eid;
  }

  // 2. Attribute signature match (works across RU/EN transliterated entity_ids)
  for (const [eid, st] of entries) {
    const a = st?.attributes;
    if (!a || !Array.isArray(a.items)) continue;
    if (cardKind === "roulette" && eid.startsWith("button.") && ("pick_count" in a || "preset" in a)) {
      return eid;
    }
    if (cardKind === "recent" && eid.startsWith("sensor.") && "entry_id" in a && !("pick_count" in a)) {
      return eid;
    }
  }

  // 3. Slug substring fallback (English & Russian transliteration)
  const patterns =
    cardKind === "roulette"
      ? ["_spin_roulette", "kinorulet", "ruletk", "roulette"]
      : ["_recently_added", "nedavno_dobavlen", "recently_added"];
  for (const pat of patterns) {
    const hit = entries.find(([eid]) =>
      (cardKind === "roulette" ? eid.startsWith("button.") : eid.startsWith("sensor.")) &&
      eid.includes(pat)
    );
    if (hit) return hit[0];
  }
  return "";
}

function findAnySmartPlexMeta(hass) {
  const states = hass?.states;
  if (!states) return { entry_id: "", zones: [], default_zone: "" };
  for (const st of Object.values(states)) {
    const a = st?.attributes;
    if (a?.entry_id && Array.isArray(a.zones)) {
      return {
        entry_id: a.entry_id,
        zones: a.zones,
        default_zone: a.default_zone || a.zones[0] || "",
      };
    }
  }
  return { entry_id: "", zones: [], default_zone: "" };
}

const SHARED_STYLES_PART1 = `
  :host {
    display: block;
    height: 100%;
    --sp-radius: var(--ha-card-border-radius, 22px);
    --sp-tile-radius: 13px;
    --sp-pill-bg: var(--ha-color-fill-neutral-normal-resting, rgba(255, 255, 255, 0.14));
    --sp-pill-hover: var(--ha-color-fill-neutral-normal-hover, rgba(255, 255, 255, 0.24));
    --sp-rim: 0.5px 0.5px 1px 0px rgba(255, 255, 255, 0.36) inset,
              -0.5px -0.5px 1px 0px rgba(255, 255, 255, 0.10) inset;
    --sp-poster-h: 128px;
  }
  ha-card.sp-card {
    position: relative;
    overflow: hidden;
    height: 100%;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    border-radius: var(--sp-radius);
    background: var(--ha-card-background, rgba(24, 25, 32, 0.52));
    backdrop-filter: var(--ha-card-backdrop-filter, blur(22px) saturate(1.45));
    -webkit-backdrop-filter: var(--ha-card-backdrop-filter, blur(22px) saturate(1.45));
    box-shadow: var(
      --ha-card-box-shadow,
      0.5px 0.5px 1px 0px rgba(255, 255, 255, 0.38) inset,
      -0.5px -0.5px 1px 0px rgba(255, 255, 255, 0.10) inset,
      0 8px 24px rgba(0, 0, 0, 0.16)
    );
    border: var(--ha-card-border-width, 0px) solid var(--ha-card-border-color, rgba(255, 255, 255, 0.14));
    color: var(--primary-text-color, #f5f5f7);
    padding: 10px 12px;
    transition: padding 0.24s cubic-bezier(0.22, 1, 0.36, 1);
  }
  ha-card.sp-card.surface-tinted {
    background: rgba(14, 15, 22, 0.78);
    backdrop-filter: blur(26px) saturate(1.55);
    -webkit-backdrop-filter: blur(26px) saturate(1.55);
  }
  ha-card.sp-card.surface-theme {
    background: var(--ha-card-background, var(--card-background-color, #1c1d22));
    backdrop-filter: var(--ha-card-backdrop-filter, none);
    -webkit-backdrop-filter: var(--ha-card-backdrop-filter, none);
  }
  ha-card.sp-card.surface-theme .sp-ambient {
    display: none;
  }
  ha-card.sp-card.is-folded {
    padding: 8px 12px;
    justify-content: center;
  }
  .sp-ambient {
    position: absolute;
    inset: -24px;
    background-size: cover;
    background-position: center;
    filter: blur(36px) saturate(1.7);
    opacity: 0.18;
    pointer-events: none;
    z-index: 0;
    transition: background-image 0.45s ease;
  }
  .sp-header {
    position: relative;
    z-index: 2;
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 30px;
  }
  .sp-badge-icon {
    width: 30px;
    height: 30px;
    border-radius: 999px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    background: color-mix(in srgb, var(--primary-color, #e5a00d) 28%, rgba(255, 255, 255, 0.12));
    box-shadow: var(--sp-rim);
    color: var(--primary-text-color, #fff);
    cursor: pointer;
    user-select: none;
  }
  .sp-badge-icon ha-icon { --mdc-icon-size: 17px; }
  .sp-titles {
    flex: 1;
    min-width: 0;
    cursor: pointer;
  }
  .sp-title {
    font-size: 13.5px;
    font-weight: 600;
    letter-spacing: -0.01em;
    line-height: 1.2;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .sp-sub {
    font-size: 11px;
    color: var(--secondary-text-color, rgba(235, 235, 245, 0.68));
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    line-height: 1.2;
    margin-top: 1px;
  }
  .sp-controls {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-shrink: 0;
  }
  .sp-pill,
  .sp-zone-select {
    appearance: none;
    -webkit-appearance: none;
    border: none;
    outline: none;
    height: 27px;
    border-radius: 999px;
    background: var(--sp-pill-bg);
    box-shadow: var(--sp-rim);
    color: var(--primary-text-color, #fff);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 500;
    padding: 0 9px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    cursor: pointer;
    transition: background 0.18s ease, transform 0.14s ease;
  }
  .sp-zone-select {
    max-width: 96px;
    padding-right: 18px;
    text-overflow: ellipsis;
    background-image: linear-gradient(45deg, transparent 50%, currentColor 50%),
                      linear-gradient(135deg, currentColor 50%, transparent 50%);
    background-position: calc(100% - 10px) 11px, calc(100% - 6px) 11px;
    background-size: 4px 4px, 4px 4px;
    background-repeat: no-repeat;
  }
  .sp-zone-select option { background: #1c1d24; color: #f5f5f7; }
  .sp-pill.icon-only { width: 27px; padding: 0; }
  .sp-pill ha-icon { --mdc-icon-size: 15px; }
  .sp-pill:hover, .sp-zone-select:hover { background-color: var(--sp-pill-hover); }
  .sp-pill:active { transform: scale(0.93); }
  .sp-pill.spinning ha-icon { animation: sp-spin 0.65s linear infinite; }
  @keyframes sp-spin { to { transform: rotate(360deg); } }

  /* Collapsible body */
  .sp-fold {
    position: relative;
    z-index: 2;
    display: grid;
    grid-template-rows: 1fr;
    flex: 1;
    min-height: 0;
    transition: grid-template-rows 0.28s cubic-bezier(0.22, 1, 0.36, 1),
                opacity 0.22s ease, margin-top 0.24s ease;
    margin-top: 9px;
    opacity: 1;
  }
  .sp-fold.folded {
    grid-template-rows: 0fr;
    margin-top: 0;
    opacity: 0;
    pointer-events: none;
  }
  .sp-fold-inner {
    overflow: hidden;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  /* Shelf & Grid layouts */
  .sp-items.layout-shelf {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(calc(var(--sp-poster-h) * 0.67), 1fr);
    gap: 8px;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    scrollbar-width: none;
    padding-bottom: 2px;
    flex: 1;
    align-items: stretch;
  }
  .sp-items.layout-shelf::-webkit-scrollbar { display: none; }
  .sp-items.layout-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(calc(var(--sp-poster-h) * 0.66), 1fr));
    gap: 8px;
    flex: 1;
  }
  .sp-tile {
    position: relative;
    height: var(--sp-poster-h);
    min-height: 84px;
    border-radius: var(--sp-tile-radius);
    overflow: hidden;
    scroll-snap-align: start;
    background: linear-gradient(155deg, rgba(255,255,255,0.12), rgba(255,255,255,0.03));
    box-shadow: var(--sp-rim), 0 4px 12px rgba(0, 0, 0, 0.22);
    cursor: pointer;
    user-select: none;
    transition: transform 0.2s cubic-bezier(0.22, 1, 0.36, 1), box-shadow 0.2s ease;
  }
  .sp-items.layout-shelf .sp-tile {
    height: 100%;
    min-height: var(--sp-poster-h);
  }
  .sp-tile:hover {
    transform: translateY(-2px) scale(1.015);
    box-shadow: var(--sp-rim), 0 8px 18px rgba(0, 0, 0, 0.32);
  }
  .sp-tile:active { transform: scale(0.97); }
  .sp-poster {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    opacity: 0;
    transition: opacity 0.28s ease;
  }
  .sp-poster.loaded { opacity: 1; }
  .sp-fallback {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 8px;
    text-align: center;
    font-size: 11px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.85);
    background: radial-gradient(circle at 30% 20%, rgba(229, 160, 13, 0.28), rgba(28, 30, 40, 0.9));
  }
  .sp-top-badges {
    position: absolute;
    top: 6px;
    left: 6px;
    right: 6px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    pointer-events: none;
    z-index: 2;
  }
  .sp-rate {
    font-size: 10px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 999px;
    background: rgba(12, 13, 18, 0.62);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    color: #ffd60a;
    box-shadow: var(--sp-rim);
  }
  .sp-info-btn {
    pointer-events: auto;
    width: 21px;
    height: 21px;
    border-radius: 999px;
    border: none;
    background: rgba(12, 13, 18, 0.56);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    color: rgba(255, 255, 255, 0.88);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    box-shadow: var(--sp-rim);
  }
  .sp-info-btn:hover { background: rgba(255, 255, 255, 0.28); color: #fff; }
  .sp-scrim {
    position: absolute;
    inset: auto 0 0 0;
    padding: 20px 7px 6px;
    background: linear-gradient(to top, rgba(8, 9, 14, 0.88) 0%, rgba(8, 9, 14, 0.42) 60%, transparent 100%);
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 5px;
    z-index: 2;
  }
  .sp-meta { min-width: 0; flex: 1; }
  .sp-name {
    font-size: 11px;
    font-weight: 600;
    color: #fff;
    line-height: 1.18;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .sp-year {
    font-size: 9.5px;
    color: rgba(235, 235, 245, 0.72);
    margin-top: 1px;
  }
  .sp-play-fab {
    width: 24px;
    height: 24px;
    border-radius: 999px;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.22);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    box-shadow: var(--sp-rim);
    color: #fff;
    transition: background 0.16s ease, transform 0.16s ease;
  }
  .sp-tile:hover .sp-play-fab {
    background: var(--primary-color, #e5a00d);
    color: #111;
    transform: scale(1.08);
  }
  .sp-play-fab ha-icon { --mdc-icon-size: 14px; }

  /* Compact List layout */
  .sp-items.layout-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .sp-row {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 5px 8px 5px 5px;
    border-radius: 13px;
    background: rgba(255, 255, 255, 0.07);
    box-shadow: var(--sp-rim);
    cursor: pointer;
    transition: background 0.16s ease;
  }
  .sp-row:hover { background: rgba(255, 255, 255, 0.14); }
  .sp-row-thumb {
    width: 34px;
    height: 50px;
    border-radius: 8px;
    overflow: hidden;
    flex-shrink: 0;
    position: relative;
    background: rgba(255, 255, 255, 0.08);
  }
  .sp-row-body { flex: 1; min-width: 0; }
  .sp-row-title {
    font-size: 12.5px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .sp-row-sub {
    font-size: 10.5px;
    color: var(--secondary-text-color, rgba(235, 235, 245, 0.65));
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Detail sheet */
  .sp-drawer {
    margin-top: 8px;
    padding: 8px 10px;
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.09);
    box-shadow: var(--sp-rim);
    font-size: 11.5px;
    line-height: 1.38;
  }
  .sp-drawer-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 4px;
  }
  .sp-drawer-title { font-weight: 600; font-size: 12.5px; }
  .sp-drawer-tags {
    font-size: 10.5px;
    color: var(--secondary-text-color, rgba(235, 235, 245, 0.72));
    margin-bottom: 4px;
  }
  .sp-drawer-sum {
    color: var(--primary-text-color, rgba(255, 255, 255, 0.9));
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .sp-toast {
    margin-top: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 500;
    text-align: center;
    background: color-mix(in srgb, var(--primary-color, #30d158) 26%, rgba(255, 255, 255, 0.12));
    box-shadow: var(--sp-rim);
  }
  .sp-empty {
    padding: 12px 6px;
    font-size: 12px;
    text-align: center;
    color: var(--secondary-text-color, rgba(235, 235, 245, 0.65));
  }
`;

function detectIsRu(hassOverride) {
  const haEl =
    typeof document !== "undefined" ? document.querySelector("home-assistant") : null;
  const lang = String(
    hassOverride?.language ||
      haEl?.hass?.language ||
      haEl?.hass?.locale?.language ||
      (typeof navigator !== "undefined" ? navigator.language : "en") ||
      "en"
  ).toLowerCase();
  return lang.startsWith("ru");
}

class SmartPlexBaseGlassCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._folded = false;
    this._spinning = false;
    this._selectedZone = "";
    this._detailKey = null;
    this._toast = "";
    this._toastTimer = null;
    this._lastRenderSig = "";
  }

  get isRu() {
    return detectIsRu(this._hass);
  }

  setConfig(config) {
    this._config = {
      layout: "shelf",
      surface_style: "frosted",
      poster_height: 128,
      max_items: this.cardKind === "roulette" ? 4 : 6,
      start_folded: false,
      show_zone_picker: true,
      ...config,
    };
    this._folded = Boolean(this._config.start_folded);
    if (this._config.default_zone) {
      this._selectedZone = String(this._config.default_zone);
    }
    this._lastRenderSig = "";
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const stateObj = this._resolveEntityState();
    const attrs = stateObj?.attributes || {};
    const itemsSig = (attrs.items || []).map((i) => i.rating_key).join(",");
    const zonesSig = (attrs.zones || []).join(",");
    const sig = `${stateObj?.entity_id || ""}|${stateObj?.state || ""}|${itemsSig}|${zonesSig}`;
    if (sig !== this._lastRenderSig) {
      this._lastRenderSig = sig;
      this._overrideItems = null;
      if (!this._selectedZone) {
        this._selectedZone =
          this._config.default_zone || attrs.zone || attrs.default_zone || (attrs.zones || [])[0] || "";
      }
      this._render();
      if (this.cardKind === "roulette" && !itemsSig && !this._overrideItems && !this._autoSpunOnce) {
        this._autoSpunOnce = true;
        this._handlePrimaryAction({ stopPropagation() {} });
      }
    }
  }

  _resolveEntityState() {
    if (!this._hass?.states) return null;
    if (this._config.entity && this._hass.states[this._config.entity]) {
      return this._hass.states[this._config.entity];
    }
    const autoId = findSmartPlexEntityId(this._hass, this.cardKind);
    return autoId ? this._hass.states[autoId] : null;
  }

  _showToast(msg) {
    this._toast = msg;
    if (this._toastTimer) clearTimeout(this._toastTimer);
    this._render();
    this._toastTimer = setTimeout(() => {
      this._toast = "";
      this._render();
    }, 2800);
  }

  async _handlePrimaryAction(ev) {
    ev.stopPropagation();
    if (!this._hass || this._spinning) return;
    this._spinning = true;
    this._render();
    try {
      if (this.cardKind === "roulette") {
        const data = {};
        if (this._selectedZone) data.zone = this._selectedZone;
        if (this._config.preset) data.preset = this._config.preset;
        if (typeof this._hass.callWS === "function") {
          const wsRes = await this._hass.callWS({
            type: "call_service",
            domain: "plex_smart",
            service: "spin_roulette",
            service_data: data,
            return_response: true,
          });
          const respObj = wsRes?.response || {};
          if (respObj.entry_id || Array.isArray(respObj.zones)) {
            this._overrideMeta = {
              entry_id: respObj.entry_id || "",
              zones: Array.isArray(respObj.zones) ? respObj.zones : [],
              zone: respObj.zone || "",
            };
          }
          const respItems = respObj.items;
          if (Array.isArray(respItems) && respItems.length > 0) {
            this._overrideItems = respItems;
          }
        } else {
          await this._hass.callService("plex_smart", "spin_roulette", data);
        }
      } else {
        await this._hass.callService("plex_smart", "refresh_cache", {});
      }
    } catch (_e) {
      this._showToast(this.isRu ? "Ошибка обновления" : "Refresh failed");
    } finally {
      setTimeout(() => {
        this._spinning = false;
        this._render();
      }, 420);
    }
  }

  async _handlePlayItem(item, ev) {
    ev.stopPropagation();
    if (!this._hass || !item?.rating_key) return;
    const zone = this._selectedZone || this._config.default_zone || "";
    const payload = { rating_key: Number(item.rating_key) };
    if (zone) payload.zone = zone;
    this._showToast(
      this.isRu
        ? `▶ ${item.title}${zone ? ` · ${zone}` : ""}`
        : `▶ ${item.title}${zone ? ` · ${zone}` : ""}`
    );
    try {
      await this._hass.callService("plex_smart", "play", payload);
    } catch (_err) {
      this._showToast(this.isRu ? "Не удалось запустить фильм" : "Playback launch failed");
    }
  }

  _hydratePosters(entryId, items) {
    if (!this._hass || !entryId || !items?.length) return;
    items.forEach((it, idx) => {
      fetchPosterBlobUrl(this._hass, entryId, it.rating_key).then((blobUrl) => {
        if (!blobUrl || !this.shadowRoot) return;
        const imgs = this.shadowRoot.querySelectorAll(`img[data-rk="${it.rating_key}"]`);
        imgs.forEach((img) => {
          img.src = blobUrl;
          img.classList.add("loaded");
          const fb = img.parentElement?.querySelector(".sp-fallback");
          if (fb) fb.style.display = "none";
        });
        if (idx === 0) {
          const amb = this.shadowRoot.querySelector(".sp-ambient");
          if (amb) amb.style.backgroundImage = `url("${blobUrl}")`;
        }
      });
    });
  }

  _render() {
    if (!this.shadowRoot) return;
    const stateObj = this._resolveEntityState();
    const attrs = stateObj?.attributes || {};
    const fallbackMeta = findAnySmartPlexMeta(this._hass);
    const entryId =
      attrs.entry_id || this._overrideMeta?.entry_id || fallbackMeta.entry_id || "default";
    const zones =
      Array.isArray(attrs.zones) && attrs.zones.length
        ? attrs.zones
        : Array.isArray(this._overrideMeta?.zones) && this._overrideMeta.zones.length
          ? this._overrideMeta.zones
          : fallbackMeta.zones;
    if (!this._selectedZone && zones.length) {
      this._selectedZone =
        this._config.default_zone ||
        attrs.zone ||
        this._overrideMeta?.zone ||
        attrs.default_zone ||
        fallbackMeta.default_zone ||
        zones[0];
    }
    const maxItems = Math.max(1, Math.min(10, Number(this._config.max_items) || 4));
    const rawItems = Array.isArray(this._overrideItems)
      ? this._overrideItems
      : Array.isArray(attrs.items)
        ? attrs.items
        : [];
    const items = rawItems.slice(0, maxItems);
    const layout = ["shelf", "grid", "list"].includes(this._config.layout)
      ? this._config.layout
      : "shelf";
    const posterH = Math.max(84, Math.min(260, Number(this._config.poster_height) || 128));

    const defaultTitle =
      this.cardKind === "roulette"
        ? this.isRu ? "Кинорулетка" : "Movie Roulette"
        : this.isRu ? "Недавно добавленные" : "Recently Added";
    const title = this._config.title || defaultTitle;
    const topItem = items[0];
    const subtitle = this._folded
      ? topItem
        ? `${topItem.title}${topItem.year ? ` (${topItem.year})` : ""}`
        : this.isRu ? "Свёрнуто" : "Folded"
      : this.cardKind === "roulette"
        ? attrs.preset && attrs.preset !== "default"
          ? `${attrs.preset} · ${items.length}`
          : this.isRu ? `Подборка · ${items.length}` : `Picks · ${items.length}`
        : this.isRu ? `Новинки Plex · ${items.length}` : `Latest in Plex · ${items.length}`;

    const badgeIcon = this.cardKind === "roulette" ? "mdi:dice-multiple" : "mdi:movie-open-plus";
    const actionIcon = this.cardKind === "roulette" ? "mdi:dice-5" : "mdi:refresh";
    const actionTitle =
      this.cardKind === "roulette"
        ? this.isRu ? "Перекрутить рулетку" : "Spin roulette"
        : this.isRu ? "Обновить медиатеку" : "Refresh library";
    const foldIcon = this._folded ? "mdi:chevron-down" : "mdi:chevron-up";

    const zonePickerHtml =
      this._config.show_zone_picker !== false && zones.length > 0
        ? `<select class="sp-zone-select" title="${this.isRu ? "Зона воспроизведения" : "Target zone"}">
            ${zones
              .map(
                (z) =>
                  `<option value="${escapeHtml(z)}" ${z === this._selectedZone ? "selected" : ""}>${escapeHtml(z)}</option>`
              )
              .join("")}
          </select>`
        : "";

    let itemsHtml = "";
    if (!items.length) {
      itemsHtml = `<div class="sp-empty">${
        this.cardKind === "roulette"
          ? this.isRu ? "Нажмите кубик, чтобы подобрать фильмы" : "Tap the dice to spin movies"
          : this.isRu ? "Список новинок пока пуст" : "No recently added items yet"
      }</div>`;
    } else if (layout === "list") {
      itemsHtml = `<div class="sp-items layout-list">
        ${items
          .map((it) => {
            const metaParts = [
              it.year || "",
              it.rating > 0 ? `★ ${Number(it.rating).toFixed(1)}` : "",
              (it.genres || []).slice(0, 2).join(", "),
              it.duration_min ? `${it.duration_min} ${this.isRu ? "мин" : "m"}` : "",
            ].filter(Boolean);
            return `<div class="sp-row" data-play-rk="${it.rating_key}">
              <div class="sp-row-thumb">
                <div class="sp-fallback">${escapeHtml((it.title || "?").slice(0, 2))}</div>
                <img class="sp-poster" data-rk="${it.rating_key}" alt="${escapeHtml(it.title)}" />
              </div>
              <div class="sp-row-body">
                <div class="sp-row-title">${escapeHtml(it.title)}</div>
                <div class="sp-row-sub">${escapeHtml(metaParts.join(" · "))}</div>
              </div>
              <button class="sp-info-btn" data-info-rk="${it.rating_key}" title="${this.isRu ? "Подробнее" : "Details"}">i</button>
              <span class="sp-play-fab"><ha-icon icon="mdi:play"></ha-icon></span>
            </div>`;
          })
          .join("")}
      </div>`;
    } else {
      itemsHtml = `<div class="sp-items layout-${layout}">
        ${items
          .map(
            (it) => `<div class="sp-tile" data-play-rk="${it.rating_key}">
              <div class="sp-fallback">${escapeHtml(it.title)}</div>
              <img class="sp-poster" data-rk="${it.rating_key}" alt="${escapeHtml(it.title)}" />
              <div class="sp-top-badges">
                ${
                  it.rating > 0
                    ? `<span class="sp-rate">★ ${Number(it.rating).toFixed(1)}</span>`
                    : `<span></span>`
                }
                <button class="sp-info-btn" data-info-rk="${it.rating_key}" title="${this.isRu ? "Подробнее" : "Details"}">i</button>
              </div>
              <div class="sp-scrim">
                <div class="sp-meta">
                  <div class="sp-name">${escapeHtml(it.title)}</div>
                  ${it.year ? `<div class="sp-year">${escapeHtml(it.year)}</div>` : ""}
                </div>
                <span class="sp-play-fab"><ha-icon icon="mdi:play"></ha-icon></span>
              </div>
            </div>`
          )
          .join("")}
      </div>`;
    }

    const detailItem = this._detailKey
      ? items.find((i) => Number(i.rating_key) === Number(this._detailKey))
      : null;
    let drawerHtml = "";
    if (detailItem) {
      const tagParts = [
        detailItem.year || "",
        detailItem.rating > 0 ? `★ ${Number(detailItem.rating).toFixed(1)}` : "",
        detailItem.library || "",
        (detailItem.genres || []).join(", "),
        detailItem.duration_min
          ? `${detailItem.duration_min} ${this.isRu ? "мин" : "min"}`
          : "",
      ].filter(Boolean);
      drawerHtml = `<div class="sp-drawer">
        <div class="sp-drawer-head">
          <div class="sp-drawer-title">${escapeHtml(detailItem.title)}</div>
          <button class="sp-pill" data-drawer-play="${detailItem.rating_key}">
            ▶ ${escapeHtml(this._selectedZone || (this.isRu ? "Смотреть" : "Play"))}
          </button>
        </div>
        <div class="sp-drawer-tags">${escapeHtml(tagParts.join(" · "))}</div>
        ${
          detailItem.summary
            ? `<div class="sp-drawer-sum">${escapeHtml(detailItem.summary)}</div>`
            : ""
        }
      </div>`;
    }

    this.style.setProperty("--sp-poster-h", `${posterH}px`);
    const surfaceStyle = ["frosted", "tinted", "theme"].includes(this._config.surface_style)
      ? this._config.surface_style
      : "frosted";
    this.shadowRoot.innerHTML = `
      <style>${SHARED_STYLES_PART1}</style>
      <ha-card class="sp-card surface-${surfaceStyle} ${this._folded ? "is-folded" : ""}">
        <div class="sp-ambient"></div>
        <div class="sp-header">
          <div class="sp-badge-icon" data-toggle-fold="1">
            <ha-icon icon="${badgeIcon}"></ha-icon>
          </div>
          <div class="sp-titles" data-toggle-fold="1">
            <div class="sp-title">${escapeHtml(title)}</div>
            <div class="sp-sub">${escapeHtml(subtitle)}</div>
          </div>
          <div class="sp-controls">
            ${zonePickerHtml}
            <button class="sp-pill icon-only ${this._spinning ? "spinning" : ""}" data-primary-act="1" title="${escapeHtml(actionTitle)}">
              <ha-icon icon="${actionIcon}"></ha-icon>
            </button>
            <button class="sp-pill icon-only" data-toggle-fold="1" title="${this.isRu ? "Свернуть / развернуть" : "Fold / unfold"}">
              <ha-icon icon="${foldIcon}"></ha-icon>
            </button>
          </div>
        </div>
        <div class="sp-fold ${this._folded ? "folded" : ""}">
          <div class="sp-fold-inner">
            ${itemsHtml}
            ${drawerHtml}
            ${this._toast ? `<div class="sp-toast">${escapeHtml(this._toast)}</div>` : ""}
          </div>
        </div>
      </ha-card>
    `;

    const zoneSel = this.shadowRoot.querySelector(".sp-zone-select");
    if (zoneSel) {
      zoneSel.addEventListener("change", (e) => {
        e.stopPropagation();
        this._selectedZone = e.target.value;
      });
      zoneSel.addEventListener("click", (e) => e.stopPropagation());
    }

    this.shadowRoot.querySelectorAll("[data-toggle-fold]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        this._folded = !this._folded;
        this._render();
      });
    });

    const actBtn = this.shadowRoot.querySelector("[data-primary-act]");
    if (actBtn) {
      actBtn.addEventListener("click", (e) => this._handlePrimaryAction(e));
    }

    this.shadowRoot.querySelectorAll("[data-info-rk]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const rk = Number(btn.getAttribute("data-info-rk"));
        this._detailKey = this._detailKey === rk ? null : rk;
        this._render();
      });
    });

    this.shadowRoot.querySelectorAll("[data-play-rk]").forEach((tile) => {
      tile.addEventListener("click", (e) => {
        const rk = Number(tile.getAttribute("data-play-rk"));
        const item = items.find((i) => Number(i.rating_key) === rk);
        if (item) this._handlePlayItem(item, e);
      });
    });

    const drawerPlay = this.shadowRoot.querySelector("[data-drawer-play]");
    if (drawerPlay && detailItem) {
      drawerPlay.addEventListener("click", (e) => this._handlePlayItem(detailItem, e));
    }

    this._hydratePosters(entryId, items);
  }

  getCardSize() {
    return this._folded ? 1 : 3;
  }

  getGridOptions() {
    return {
      columns: 6,
      min_columns: 3,
      max_columns: 12,
      min_rows: 1,
    };
  }
}

function buildCardConfigForm(isRoulette) {
  const ru = detectIsRu();
  const labels = {
    entity: ru ? "Источник данных (автовыбор, если пусто)" : "Source entity (auto if empty)",
    title: ru ? "Заголовок карточки" : "Card title",
    layout: ru ? "Режим отображения" : "Layout mode",
    surface_style: ru ? "Стиль подложки" : "Surface style",
    poster_height: ru ? "Высота обложек (px)" : "Poster height (px)",
    max_items: ru ? "Количество элементов" : "Number of items",
    default_zone: ru ? "Медиа-зона по умолчанию" : "Default playback zone",
    preset: ru ? "Пресет Кинорулетки" : "Movie Roulette preset",
    show_zone_picker: ru ? "Показывать выбор зоны в шапке" : "Show zone selector in header",
    start_folded: ru ? "Сворачивать карточку при открытии" : "Start collapsed",
  };
  const helpers = {
    entity: ru
      ? "Оставьте пустым для автоопределения или укажите сущность конкретного сервера SmartPlex."
      : "Leave empty for automatic detection or select a specific SmartPlex entity.",
    title: ru
      ? "Необязательно: своё название вместо стандартного заголовка."
      : "Optional custom title shown in the card header.",
    poster_height: ru
      ? "Компактный размер по умолчанию — 128 px."
      : "Default compact height is 128 px.",
    default_zone: ru
      ? "Необязательно: имя зоны (например, Зал или Спальня)."
      : "Optional zone name override.",
    preset: ru
      ? "Необязательно: название настроенного пресета Кинорулетки."
      : "Optional configured Movie Roulette preset name.",
  };

  const schema = [
    {
      name: "entity",
      selector: {
        entity: {
          integration: "plex_smart",
          domain: isRoulette ? ["button"] : ["sensor"],
        },
      },
    },
    { name: "title", selector: { text: {} } },
    {
      name: "layout",
      selector: {
        select: {
          mode: "dropdown",
          options: [
            { value: "shelf", label: ru ? "Полка (горизонтальный ряд)" : "Poster Shelf (horizontal row)" },
            { value: "grid", label: ru ? "Плитка (адаптивная сетка)" : "Poster Grid (auto-fit)" },
            { value: "list", label: ru ? "Компактный список" : "Compact List" },
          ],
        },
      },
    },
    {
      name: "surface_style",
      selector: {
        select: {
          mode: "dropdown",
          options: [
            { value: "frosted", label: ru ? "Матовое стекло" : "Frosted Glass" },
            { value: "tinted", label: ru ? "Тёмный акрил" : "Dark Acrylic" },
            { value: "theme", label: ru ? "Системная тема" : "Theme Default" },
          ],
        },
      },
    },
    {
      name: "poster_height",
      selector: { number: { min: 84, max: 240, step: 6, mode: "slider" } },
    },
    {
      name: "max_items",
      selector: { number: { min: 2, max: 10, step: 1, mode: "slider" } },
    },
    { name: "default_zone", selector: { text: {} } },
  ];
  if (isRoulette) {
    schema.push({ name: "preset", selector: { text: {} } });
  }
  schema.push(
    { name: "show_zone_picker", selector: { boolean: {} } },
    { name: "start_folded", selector: { boolean: {} } }
  );
  return {
    schema,
    computeLabel: (s) => labels[s.name] || s.name,
    computeHelper: (s) => helpers[s.name] || "",
  };
}

class SmartPlexRouletteCard extends SmartPlexBaseGlassCard {
  get cardKind() {
    return "roulette";
  }

  static getStubConfig(hass) {
    const entity = findSmartPlexEntityId(hass, "roulette");
    return { entity, layout: "shelf", surface_style: "frosted", poster_height: 128, max_items: 4 };
  }

  static getConfigForm() {
    return buildCardConfigForm(true);
  }
}

class SmartPlexRecentCard extends SmartPlexBaseGlassCard {
  get cardKind() {
    return "recent";
  }

  static getStubConfig(hass) {
    const entity = findSmartPlexEntityId(hass, "recent");
    return { entity, layout: "shelf", surface_style: "frosted", poster_height: 128, max_items: 6 };
  }

  static getConfigForm() {
    return buildCardConfigForm(false);
  }
}

if (!customElements.get("smartplex-roulette-card")) {
  customElements.define("smartplex-roulette-card", SmartPlexRouletteCard);
}
if (!customElements.get("smartplex-recent-card")) {
  customElements.define("smartplex-recent-card", SmartPlexRecentCard);
}

window.customCards = window.customCards || [];
const CARD_DEFS = [
  {
    type: "smartplex-roulette-card",
    get name() {
      return detectIsRu() ? "SmartPlex: Кинорулетка" : "SmartPlex: Movie Roulette";
    },
    get description() {
      return detectIsRu()
        ? "Компактная витрина Кинорулетки с матовым стеклом, выбором зоны и запуском в 1 клик."
        : "Compact frosted-glass Movie Roulette showcase with zone selector and 1-tap play.";
    },
    preview: true,
  },
  {
    type: "smartplex-recent-card",
    get name() {
      return detectIsRu() ? "SmartPlex: Недавно добавленные" : "SmartPlex: Recently Added";
    },
    get description() {
      return detectIsRu()
        ? "Компактная витрина недавно добавленных новинок Plex с запуском в 1 клик."
        : "Compact frosted-glass showcase of recently added Plex items with 1-tap play.";
    },
    preview: true,
  },
];
for (const def of CARD_DEFS) {
  if (!window.customCards.some((c) => c.type === def.type)) {
    window.customCards.push(def);
  }
}






