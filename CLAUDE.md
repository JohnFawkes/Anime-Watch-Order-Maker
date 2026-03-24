# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`Anime Watch Order Maker` — a FastAPI web application that provides a UI for creating Plex playlists with TVDB absolute episode ordering for anime. Users browse their Plex anime library, view show details, and generate playlists that follow TVDB's absolute episode numbering (cross-season ordering), including properly interleaved specials. A scheduled cron mode can auto-maintain playlists across the entire library.

## How to Run

### Locally (development)

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The app will be available at `http://localhost:8000`.

### With Docker

```bash
docker compose up --build
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY_FILE` | `/data/secret.key` | Path to the Fernet secret key file (auto-generated on first run) |
| `PORT` | `8000` | Host port exposed by docker-compose |
| `TZ` | `UTC` | Timezone for cron scheduling (e.g. `America/New_York`) |
| `LOG_LEVEL` | `INFO` | Logging level — set to `DEBUG` for trace output |

## Architecture

```
app/
  main.py              # FastAPI app setup, middleware, lifespan (starts scheduler)
  crypto.py            # Fernet encryption/decryption + session secret
  database.py          # SQLAlchemy engine, SessionLocal, Base, init_db(), get_db()
  models.py            # User, Setting, Playlist, ShowSkip ORM models
  plex_client.py       # PlexAPI wrapper functions (build/update playlist, movie index)
  tvdb_client.py       # TVDB v4 API client with token caching
  scheduler.py         # APScheduler background scheduler for cron auto-create
  routes/
    auth.py            # /setup, /login, /logout, /api/plex-libraries
    anime.py           # /, /proxy/thumb, /anime/{key}, /anime/{key}/playlist,
                       # /anime/{key}/playlist/delete, /anime/{key}/skip
                       # + _build_playlist_for_show(), run_auto_playlists()
    settings_routes.py # /settings (GET + POST), /settings/plex-libraries,
                       # /settings/movie-libraries, /cron/run-now
  templates/
    base.html          # Dark violet/zinc-themed base layout with Tailwind + HTMX
    login.html         # Login page (no nav)
    setup.html         # First-run setup (standalone, no nav)
    index.html         # Anime grid with client-side search + skip badges
    anime_detail.html  # Show detail + playlist management + skip toggle
    settings.html      # Settings form + Automation/cron config section
    partials/
      library_select.html      # HTMX partial: anime library dropdown
      movie_library_select.html# HTMX partial: movie library dropdown
      playlist_result.html     # HTMX partial: success/error after playlist create/delete
      coverage_result.html     # HTMX partial: episode coverage table
      cron_status.html         # HTMX partial: cron run-now result
      skip_toggle.html         # HTMX partial: per-show skip toggle button
```

### Request flow

```
routes/ -> plex_client.py / tvdb_client.py -> Plex / TVDB APIs
routes/ -> database.py (SQLAlchemy session) -> SQLite
crypto.py -> Setting.value (all sensitive values are Fernet-encrypted at rest)
scheduler.py -> run_auto_playlists() -> _build_playlist_for_show() (background thread)
```

### Tech stack

- **FastAPI** — web framework
- **Jinja2** — server-side templating
- **HTMX** — dynamic UI updates without full page reloads (OOB swaps for playlist banner)
- **Tailwind CSS** (CDN) — violet/zinc dark-themed styling
- **SQLAlchemy** — ORM over SQLite
- **cryptography (Fernet)** — symmetric encryption for DB values
- **plexapi** — Plex Media Server client
- **passlib[bcrypt]** + **bcrypt==4.0.1** — password hashing (bcrypt pinned for passlib compat)
- **Starlette SessionMiddleware** — cookie-based sessions
- **APScheduler** — background cron scheduler for auto-playlist job

## Data Directory (`/data`)

| File | Purpose |
|---|---|
| `/data/app.db` | SQLite database (users, settings, playlists, skip flags) |
| `/data/secret.key` | Fernet encryption key (auto-generated on first run) |

## First-Run Setup Flow

1. On first request, the app checks if any `User` rows exist in the DB.
2. If none exist, all routes redirect to `/setup`.
3. `/setup` collects username + password, Plex URL + token + library (via HTMX discovery), and TVDB API key.
4. On submit, user and all settings are saved, then redirected to `/login`.

## Database Models

| Model | Table | Purpose |
|---|---|---|
| `User` | `users` | Login credentials (bcrypt-hashed password) |
| `Setting` | `settings` | All app settings, Fernet-encrypted at rest |
| `Playlist` | `playlists` | Tracks created playlists (show → playlist rating key mapping) |
| `ShowSkip` | `show_skips` | Shows marked to be skipped by the auto-playlist cron job |

## Playlist Tracking

The `Playlist` table tracks each created playlist:
- `show_rating_key` — Plex rating key of the show (unique: one playlist per show)
- `playlist_rating_key` — Plex rating key of the created playlist (used for update/deletion)
- `playlist_title` / `show_title` — display names

When the cron job or "Create Playlist" button runs on a show with an existing playlist, the app calls `update_plex_playlist()` which:
1. Compares current playlist items to the desired absolute order
2. If identical → returns `"no_change"` (no Plex API calls)
3. If new items are appended at the end → calls `playlist.addItems()` (incremental, preserves history)
4. If items are removed or reordered → deletes and recreates

## Cron / Auto-Playlist

The scheduler (APScheduler) runs `run_auto_playlists()` on a configurable cron expression.

Settings stored in the `Setting` table (all encrypted):

| Key | Values | Description |
|---|---|---|
| `cron_enabled` | `"true"` / `"false"` | Whether the scheduler is active |
| `cron_schedule` | cron expression | Schedule (e.g. `"0 3 * * *"` for 3 AM daily) |
| `skip_no_specials` | `"true"` / `"false"` | Skip shows with no S00/movie episodes on TVDB |
| `skip_no_order_change` | `"true"` / `"false"` | Skip shows where absolute order matches natural Plex order |

The `ShowSkip` table holds per-show skip flags set from the anime detail page UI.
Per-show skips only affect the cron job — manual playlist creation always works.

## Multi-Library Support

If `movie_library` is configured in settings (optional), the playlist builder also searches that library for episodes not found in the anime show library. Matching uses a 6-step chain:
1. TVDB episode ID → `tvdb://` GUID in movie library
2. `linkedMovies` on the episode → TVDB movie ID → TMDB ID → `tmdb://` GUID in Plex
3. Episode name / English translation → TVDB series-movie name → TMDB ID
4. Exact title match against Plex movie titles
5. Normalized fuzzy title match (punctuation-stripped)
6. Prefix/starts-with match (≥8 chars, handles subtitle variants)

## Plex Episode Matching

Episodes are matched between TVDB and Plex by TVDB **episode ID**:

- **Modern Plex agent**: `episode.guids` list contains `tvdb://EPISODE_ID`
- **Legacy agent** (`com.plexapp.agents.thetvdb://seriesid/season/ep`): episode ID cannot be extracted, so legacy-agent episodes are skipped

## Specials / Absolute Order

`tvdb_client.get_absolute_order_episodes()` returns a merged, sorted list:
1. Episodes from TVDB's absolute order season type (already have `absoluteNumber`)
2. Season-0 specials from the default order with `airsBeforeEpisode`/`airsAfterEpisode` that weren't assigned to the absolute order — interleaved using `ref_abs ± 0.5` positioning
3. Movie-type episodes (`isMovie=1`) from any season are also included
4. Unplaced episodes (no airs data) are appended at the end with fractional positions

## TVDB API

- Base URL: `https://api4.thetvdb.com/v4`
- Authentication: POST `/v4/login` with `{"apikey": "..."}` → bearer token
- Token cached per api_key in module-level dict for 25 days
- Absolute order episodes: GET `/v4/series/{id}/episodes/absolute?lang=en&page=N`
- Default order episodes: GET `/v4/series/{id}/episodes/default?lang=en&page=N`
- Series info (slug, linked movies): GET `/v4/series/{id}/extended`
- Episode extended (translations, linkedMovies): GET `/v4/episodes/{id}/extended?meta=translations`
- Movie extended (remoteIds for TMDB): GET `/v4/movies/{id}/extended`

## Encrypted Settings

| Key | Description |
|---|---|
| `plex_url` | Plex server base URL |
| `plex_token` | X-Plex-Token for authentication |
| `plex_library` | Name of the primary anime show library |
| `movie_library` | Name of the optional movie library for specials (empty string if not set) |
| `tvdb_api_key` | TVDB v4 API key |
| `cron_enabled` | Cron scheduler enabled flag |
| `cron_schedule` | Cron expression for auto-playlist schedule |
| `skip_no_specials` | Auto-skip filter flag |
| `skip_no_order_change` | Auto-skip filter flag |

All settings are Fernet-encrypted in the `Setting` table regardless of sensitivity.
