# Anime Watch Order Maker

A self-hosted web application that creates and maintains Plex playlists following TVDB absolute episode ordering for anime — automatically interleaving specials, OVAs, and movies at their correct positions in the watch order.

[![Docker Build & Publish](https://github.com/JohnFawkes/anime-watch-order-maker/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/JohnFawkes/anime-watch-order-maker/actions/workflows/docker-publish.yml)

---

## Features

- **Absolute order playlists** — orders all episodes (including specials) by TVDB's absolute numbering, interleaving them at the correct position between regular episodes
- **Movie library support** — finds specials and movies stored in a separate Plex movie library using a multi-step TVDB/TMDB matching chain
- **Episode coverage checker** — shows which TVDB episodes are matched in Plex before creating a playlist
- **Incremental playlist updates** — appends only new episodes rather than deleting and recreating; detects when no changes are needed
- **Scheduled auto-create** — configurable cron job to auto-create/update playlists for every show in your library
- **Per-show skip flag** — mark individual shows to be excluded from the auto-create job
- **Auto-skip filters** — optionally skip shows with no specials on TVDB, or shows where the absolute order matches the natural Plex viewing order
- **Dark UI** — violet/zinc-themed interface with HTMX-powered dynamic updates
- **Single-user, self-hosted** — simple setup with one admin account, all credentials encrypted at rest

---

## Quick Start

### Docker Compose (recommended)

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` to set your timezone and port:
   ```env
   PORT=8000
   TZ=America/New_York
   LOG_LEVEL=INFO
   ```

3. Start the container:
   ```bash
   docker compose up -d
   ```

4. Open `http://localhost:8000` in your browser and complete the first-run setup.

### Docker (pre-built image)

```bash
docker run -d \
  --name anime-watch-order-maker \
  -p 8000:8000 \
  -v ./data:/data \
  -e TZ=America/New_York \
  ghcr.io/johnfawkes/anime-watch-order-maker:latest
```

### Local Development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

---

## First-Run Setup

On first visit the app redirects to `/setup` where you configure:

| Field | Description |
|---|---|
| Username / Password | Admin login credentials |
| Plex Server URL | e.g. `http://192.168.1.100:32400` |
| Plex Token | Your X-Plex-Token ([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| Anime Library | Name of your Plex show library containing anime |
| TVDB API Key | Free key from [thetvdb.com/api-information](https://thetvdb.com/api-information) |

The movie library (for specials/OVAs stored as movies) can be added later in Settings.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Host port |
| `TZ` | `UTC` | Timezone for cron scheduling |
| `LOG_LEVEL` | `INFO` | Log verbosity — `DEBUG` enables detailed matching traces |
| `SECRET_KEY_FILE` | `/data/secret.key` | Path to Fernet encryption key (auto-generated) |

---

## Scheduled Auto-Create

The Automation section in Settings lets you configure a cron job that runs across your entire library:

- **Enable scheduled auto-create** — toggle the background scheduler on/off
- **Schedule** — standard cron expression (e.g. `0 3 * * *` for 3 AM daily)
- **Skip series with no specials on TVDB** — avoids creating playlists for shows that don't need them
- **Skip series where watch order wouldn't change** — avoids creating playlists when absolute order matches natural Plex order
- **Run Now** — trigger an immediate run from the UI

The scheduler respects per-show skip flags set on individual show pages. Skipped shows are shown with a badge on the library grid.

### Playlist Update Strategy

When a playlist already exists and the cron job (or the Create button) runs:

| Situation | Action |
|---|---|
| No new episodes | No changes made |
| New episodes appended at end | `addItems()` called — history preserved |
| Episodes removed or reordered | Full delete + recreate |

---

## Movie Library Matching

When a separate movie library is configured, unmatched TVDB specials/movies are looked up using this chain:

1. TVDB episode ID → `tvdb://` GUID on Plex movie
2. `linkedMovies` on the TVDB episode → TVDB movie ID → TMDB ID → `tmdb://` GUID on Plex movie
3. Episode English name → TVDB series movie name → TMDB ID
4. Exact title match
5. Normalized fuzzy title match (punctuation-stripped)
6. Prefix/starts-with match (handles subtitle variants, minimum 8 characters)

No TMDB API key is required — TMDB IDs are sourced from TVDB's `remoteIds` data.

---

## Data Storage

All persistent data lives in the `/data` directory (mount a volume here):

| File | Contents |
|---|---|
| `/data/app.db` | SQLite database — users, settings, playlist records, skip flags |
| `/data/secret.key` | Fernet encryption key — auto-generated on first run |

All sensitive settings (Plex URL, token, TVDB key, etc.) are Fernet-encrypted at rest in the database.

---

## Requirements

- Plex Media Server with the **TVDB agent** — episodes must have TVDB episode GUIDs (`tvdb://EPISODE_ID`) for matching to work. The legacy `com.plexapp.agents.thetvdb` agent is not supported.
- A free [TVDB API key](https://thetvdb.com/api-information)

---

## Architecture

```
FastAPI + Jinja2 + HTMX + Tailwind CSS
SQLAlchemy → SQLite (/data/app.db)
PlexAPI → Plex Media Server
TVDB v4 REST API → episode/series/movie data
APScheduler → background cron job
Fernet → encrypted settings at rest
```

See [CLAUDE.md](CLAUDE.md) for full developer documentation.
