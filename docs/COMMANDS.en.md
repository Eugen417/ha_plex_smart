# 📖 SmartPlex Commands & Services Reference (`v1.0.3`)

<p align="left">
  <a href="COMMANDS.md">🇷🇺 Русский</a> | <b>🇬🇧 English</b> | <a href="../README.en.md">⬅ Back to README</a>
</p>

This guide documents **all supported voice/text commands, playback controls, YAML services, and Assist LLM API tools** provided by **SmartPlex (`plex_smart`)**.

---

## ⚡ Command Routing (Zero-Token Local vs AI)

SmartPlex automatically picks the fastest path for every request:

| Command Category | Latency | AI Tokens | Engine |
| :--- | :---: | :---: | :--- |
| **Active playback control & seek** *(pause, unpause, stop, seek ±sec/min, jump to timestamp, restart, next/prev, repeat, shuffle)* | `< 1 ms` | `0` | Deterministic `extract_playback_control_intent` parser (in `plex_smart.command` and Assist `ChatLog`) |
| **Pure cross-room transfer** *(“move movie from living room to bedroom”, “switch back to living room”)* | `< 1 ms` | `0` | Deterministic `extract_pure_handoff_intent` interceptor (blocks LLM hallucinations) |
| **Direct title / episode / playlist play** *(“play Interstellar”, “resume Friends season 2 episode 5”)* | `< 1 ms` | `0` | In-memory library resolver |
| **Semantic & filtered discovery** *(2+ actors together, “similar to X”, newest comedies, genre + decade)* | `~1–2 s` | LLM | Native Assist LLM API (`plex_smart_*`) or `ai_task.*` (for `plex_smart.command`) |

---

## 🎬 1. Movies (`START` vs `RESUME`)

| Scenario | Example Phrases | Behavior |
| :--- | :--- | :--- |
| **Start from `0:00`** | *“Play Interstellar”*<br>*“Start Inception in living room”*<br>*“Play Dune from the beginning”* | Resolves exact `rating_key`, wakes TV (cold / warm / hot phase), and starts strictly from **`0:00`** (`resume=0, offset=0`). |
| **Resume from saved position** | *“Resume Interstellar”*<br>*“Continue watching Dune in bedroom”* | Fetches live `viewOffset` directly from PMS (`GET /library/metadata/{id}`), protects against accidental early-second overwrites (`65s` guard + `saved_resume_offsets`), and seeks to the saved timestamp. |
| **Start from exact timestamp** | *“Play Interstellar from minute 45”*<br>*“Start Matrix at 1:15:00”* | Passes explicit `offset_sec` (`2700` / `4500`) and verifies position on the player. |
| **Multi-actor collaborations (`AND`)** | *“Play a movie with Leonardo DiCaprio and Brad Pitt together”* | Strict `actor 1 ∩ actor 2` intersection against full PMS DB (`async_intersect_people`) + `candidate_titles` cross-check. |
| **Similar titles (`similar_to`)** | *“Play something similar to The Gentlemen”* | Queries native `GET /library/metadata/{id}/similar` (preferring unwatched) with local genre/cast fallback. |
| **Filters & sorting** | *“Play a newest sci-fi movie”*<br>*“Play a top rated 90s comedy”* | Supports `genre`, `year`, `decade`, `director`, `studio`, `collection`, `unwatched`, `sort_order` (`newest`, `oldest`, `top_rated`, `random`). |

---

## 📺 2. TV Shows, Seasons & Episodes (`continuous: 1`)

All TV show and episode launches automatically enable `continuous: 1` so `PlayQueue` advances through consecutive episodes.

| Scenario | Example Phrases | Behavior |
| :--- | :--- | :--- |
| **Start show from S01E01** | *“Play Breaking Bad”* | Starts **`S01E01`** (skipping `Season 00` Specials). |
| **Resume show** | *“Continue watching Breaking Bad”* | Resumes the active `Continue Watching` episode (or next unwatched) at its live `viewOffset`. |
| **Specific season / episode** | *“Play Friends season 2 episode 8”*<br>*“Resume season 3 of The Boys”* | Resolves the exact episode `rating_key` (or first in-progress episode of that season). |
| **Random episode in a season** | *“Play a random episode of Friends season 2”* | Shuffles strictly within the requested season. |

---

## 🎵 3. Music, Audiobooks & Playlists

| Scenario | Example Phrases | Behavior |
| :--- | :--- | :--- |
| **Artist (shuffled by default)** | *“Play Queen”*<br>*“Play Queen mix in living room”* | Builds a shuffled continuous artist `PlayQueue`. |
| **Specific song (artist optional)** | *“Play song Bohemian Rhapsody”* | Looks up tracks via `/hubs/search` with optional artist/album matching. |
| **Album or audiobook** | *“Play album Dark Side of the Moon”*<br>*“Resume audiobook Dune”* | With `resume=true`, locates the in-progress chapter/track and resumes at its exact offset. |
| **Genre, mood, decade** | *“Play dance music”*<br>*“Play 90s rock”* | Filters tracks by `genre`, `mood`, `album.year`, `album.decade` with `shuffle=1`. |
| **Plex playlist** | *“Play playlist Workout”*<br>*“Shuffle playlist Chill”* | Preserves playlist order unless shuffle/mix keywords are used. |

---

## 🔄 4. Bidirectional Cross-Room Handoff (`HANDOFF`)

| Scenario | Example Phrases | Behavior |
| :--- | :--- | :--- |
| **Forward & reverse room transfer** | *“Move the movie from living room to bedroom”*<br>*“Switch it back from bedroom to living room”* | 1) Captures live playback timestamp BEFORE power-off (PMS session + HA entity state + `saved_resume_offsets`).<br>2) Powers off the source TV (`media_stop` ≤ 2.5s + `turn_off`) in parallel with waking the target TV.<br>3) Starts on the target TV with a **−5s overlap** (`handoff_rewind_sec`) and strict per-zone `machine_id` session filtering so stale source TV sessions never block seeking. |
| **Auto-detected source room** | *“Transfer playback to the bedroom”* | Detects whichever room currently has an active Plex session and powers it off (if `turn_off_on_handoff` is enabled). |
| **Smart “Resume” without title** | *“Resume”* / *“Continue watching”* | If the active room is currently **paused**, unpauses in-place (`media_play`) in 50 ms without restarting from `0:00` or moving to the default room. Otherwise resumes the top `Continue Watching` item. |

---

## ⏯ 5. Playback Control, Seeking, Repeat & Shuffle (`CONTROL`)

When `zone` is omitted, SmartPlex automatically targets whichever room currently has an active or paused Plex session.

| Command | Example Phrases | Action (`plex_smart.control`) |
| :--- | :--- | :--- |
| **Pause** | *“Pause”* / *“Pause in living room”* | `action: pause` (single clean call without double-toggle: `plex_client` → PMS Companion → `hw_entity`; Apple TV goes straight to `hw_entity`). |
| **Unpause** | *“Unpause”* / *“Resume”* *(while paused)* | `action: resume_play` — instant unpause without stream restart. |
| **Stop** | *“Stop playback”* / *“Stop in bedroom”* | `action: stop`. |
| **Relative seek (`±sec / ±min`)** | *“Skip forward 10 seconds”*<br>*“Rewind 5 seconds”*<br>*“Fast forward 2 minutes”* | `action: seek_relative`, `delta_sec: 10` / `-5` / `120` (computes `current_pos + delta_sec` and sends integer `seekTo` / `media_seek`). |
| **Jump to exact timestamp** | *“Seek to 30 minutes”*<br>*“Jump to 1:15:00”* | `action: seek_to`, `offset_sec: 1800` / `4500`. |
| **Restart from `0:00`** | *“Restart from the beginning”* / *“Start over”* | `action: restart` (`offset = 0`). |
| **Next / Previous** | *“Next track”* / *“Next episode”*<br>*“Previous track”* | `action: next` / `previous` (Plex client only — never triggers TV channel buttons). |
| **Repeat mode** | *“Repeat this song”* / *“Repeat all”* / *“Repeat off”* | `action: set_repeat`, `repeat: one` / `all` / `off` (`/player/playback/setParameters?repeat=1|2|0`). |
| **Live shuffle** | *“Turn on shuffle”* / *“Shuffle off”* | `action: set_shuffle`, `shuffle: true` / `false` (`/player/playback/setParameters?shuffle=1|0`). |

---

## 🔍 6. Zero-Wake Library Search & Movie Roulette

* **Read-only search (`plex_smart_search`):** *“What movies do I have with Keanu Reeves?”*, *“What's in my Continue Watching?”* — queries the cached index and PMS without turning on any TV.
* **Movie Roulette (`plex_smart.spin_roulette`):** Picks `1..6` unwatched movies with anti-repeat memory, renders a numbered poster collage, and returns native inline keyboards for **Telegram Bot** and **VK Notify** (see [`examples/en/README.md`](../examples/en/README.md)).

---

## 🛠 7. YAML Examples for All 6 SmartPlex Services

```yaml
# 1. Natural language (<1 ms local or via ai_task.*)
action: plex_smart.command
data:
  command_text: "seek to 30 minutes in living room"

# 2. Structured start / resume / start at exact timestamp
action: plex_smart.play
data:
  zone: "Living Room"
  media_type: movie
  title: "Interstellar"
  resume: false
  offset_sec: 1800

# 3. Cross-room handoff
action: plex_smart.handoff
data:
  source_zone: "Bedroom"
  target_zone: "Living Room"
  turn_off_source: true

# 4. Pause, relative seek, exact seek, restart, repeat & shuffle
action: plex_smart.control
data:
  action: seek_to # pause | resume_play | play_pause | stop | seek_relative | seek_to | restart | next | previous | set_repeat | set_shuffle
  zone: "Living Room" # optional: auto-detects currently active zone
  offset_sec: 1800    # for seek_to (30:00)
  # delta_sec: -10    # for seek_relative (-10s)
  # repeat: one       # for set_repeat (off | one | all)
  # shuffle: true     # for set_shuffle

# 5. Movie Roulette collage & inline keyboards
action: plex_smart.spin_roulette
data:
  preset: "Evening Movie"
  zone: "Living Room"
response_variable: roulette

# 6. Manual library index refresh
action: plex_smart.refresh_cache
```

### Assist LLM API Tools (`SmartPlex`)
Enabling **SmartPlex** in your Assist conversation agent exposes 4 native tools:
1. `plex_smart_play` — play/resume movies, shows, episodes, music, playlists, multi-actor collaborations, similar items, and explicit `offset_sec`.
2. `plex_smart_transfer_or_resume` — cross-room handoff and Continue Watching resume.
3. `plex_smart_control` — pause, unpause, stop, relative/absolute seek, restart, next/previous, repeat, and shuffle.
4. `plex_smart_search` — read-only library & Continue Watching search without waking any TV.

---

## 🖼 8. Built-in Lovelace Dashboard Cards

SmartPlex bundles **two independent, freely resizable Lovelace cards** directly inside the integration (available right in Home Assistant's **Add Card** picker with a visual UI editor and 3–12 column Section drag-resizing — no third-party HACS plugins required):
* `custom:smartplex-roulette-card` — **SmartPlex: Movie Roulette**: *Poster Shelf / Poster Grid / Compact List* layouts, *Frosted Glass / Dark Acrylic / Theme Default* surface styles, inline zone selector, `🎲` reroll button, collapsible compact bar, and 1-tap poster playback.
* `custom:smartplex-recent-card` — **SmartPlex: Recently Added**: compact showcase of the newest movies and shows in Plex with expandable `i` details and 1-tap playback.
* **Browser / App Cache Note:** after installing or updating SmartPlex, perform a hard refresh in your browser (**`Ctrl+F5`** on Windows/Linux or **`Cmd+Shift+R`** on macOS) — or choose *Settings → Companion App → Debugging → Reset frontend cache* in the Home Assistant mobile app — so the latest card bundle loads immediately.
* Sample YAML configurations: [`../examples/lovelace_ui_cards.yaml`](../examples/lovelace_ui_cards.yaml).


