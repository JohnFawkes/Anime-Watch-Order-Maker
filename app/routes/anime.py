import logging
import re

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.crypto import decrypt
from app.database import get_db
from app.models import Playlist, Setting, ShowSkip
from app.plex_client import (
    build_ordered_playlist,
    delete_plex_playlist,
    get_movie_library_index,
    get_plex_server,
    get_show_detail,
    get_shows_from_library,
    update_plex_playlist,
)
from app.tvdb_client import (
    enrich_unmatched_specials,
    get_absolute_order_episodes,
    get_series_info,
    get_series_movie_tmdb_ids,
)

log = logging.getLogger("anime_watcher.anime")

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _require_auth(request: Request):
    """Returns user_id from session or None if not authenticated."""
    return request.session.get("user_id")


def _title_key(title: str) -> str:
    """Normalize a title for fuzzy matching: lowercase, strip punctuation/spaces."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _match_movie(
    tvdb_ep: dict,
    series_movie_tmdb_by_id: dict[str, str],
    series_movie_tmdb_by_name: dict[str, str],
    movie_by_tvdb_id: dict[str, int],
    movie_by_title: dict[str, int],
    movie_by_tmdb_id: dict[str, int],
    movie_norm_titles: dict[str, int],
) -> int | None:
    """Try to find a movie library rating key for a TVDB episode.

    Lookup order:
    1. TVDB episode ID → movie library tvdb:// GUID (rare but possible)
    2. linkedMovies on the episode → TVDB movie ID → TMDB ID → Plex tmdb:// GUID
    3. Episode name → TVDB series-movie name match → TMDB ID → Plex tmdb:// GUID
       (handles cases where bulk episode API omits linkedMovies)
    4. Exact episode title match against Plex movie titles
    5. Normalized fuzzy title match (punctuation-stripped)
    """
    ep_id = str(tvdb_ep.get("id", ""))
    ep_name = (tvdb_ep.get("name") or "").strip()
    # _english_name is set by enrich_unmatched_specials when TVDB has a translation
    english_name = (tvdb_ep.get("_english_name") or "").strip()
    abs_num = tvdb_ep.get("absoluteNumber", "?")

    log.debug("  [movie-match] ep %s (%s / eng: %s) abs=%s — searching movie library",
              ep_id, ep_name, english_name or "—", abs_num)

    # 1. TVDB episode ID directly in the movie library (unlikely but cheap)
    rk = movie_by_tvdb_id.get(ep_id)
    if rk:
        log.debug("    → matched via tvdb episode-ID GUID (rk=%s)", rk)
        return rk

    # 2. linkedMovies field on the episode → TVDB movie ID → TMDB ID → Plex
    #    enrich_unmatched_specials populates this when the bulk endpoint omits it.
    for linked in tvdb_ep.get("linkedMovies") or []:
        movie_tvdb_id = str(linked.get("id", ""))
        tmdb_id = series_movie_tmdb_by_id.get(movie_tvdb_id)
        log.debug("    linkedMovie tvdb_movie_id=%s → tmdb_id=%s", movie_tvdb_id, tmdb_id)
        if tmdb_id:
            rk = movie_by_tmdb_id.get(tmdb_id)
            if rk:
                log.debug("    → matched via linkedMovies→TMDB (rk=%s)", rk)
                return rk

    # 3. Try both the episode name and its English translation against the
    #    TVDB series-movie name → TMDB ID mapping.
    for candidate in dict.fromkeys([english_name, ep_name]):  # dedup, preserve order
        if not candidate:
            continue
        name_key = _title_key(candidate)
        tmdb_id = series_movie_tmdb_by_name.get(name_key)
        log.debug("    name key '%s' → tmdb_id=%s", name_key, tmdb_id)
        if tmdb_id:
            rk = movie_by_tmdb_id.get(tmdb_id)
            if rk:
                log.debug("    → matched via name→TMDB (rk=%s)", rk)
                return rk
            log.debug("    TMDB id %s not found in Plex movie library index", tmdb_id)

    # 4 & 5. Direct title match against Plex movie library (exact then fuzzy).
    #         Try English name first, then original episode name.
    for candidate in dict.fromkeys([english_name, ep_name]):
        if not candidate:
            continue
        rk = movie_by_title.get(candidate.lower())
        if rk:
            log.debug("    → matched via exact title '%s' (rk=%s)", candidate, rk)
            return rk
        rk = movie_norm_titles.get(_title_key(candidate))
        if rk:
            log.debug("    → matched via fuzzy title '%s' (rk=%s)", candidate, rk)
            return rk

    # 6. Prefix/substring match — handles cases where one side has extra subtitle
    #    detail the other doesn't.
    #    e.g. TVDB: "JUJUTSU KAISEN: Execution"
    #         Plex: "JUJUTSU KAISEN: Execution -Shibuya Incident x The Culling Game Begins-"
    #    Require ≥ 8 chars on the shorter side to avoid short-name false positives.
    for candidate in dict.fromkeys([english_name, ep_name]):
        if not candidate:
            continue
        ckey = _title_key(candidate)
        if len(ckey) < 8:
            continue
        for movie_key, rk in movie_norm_titles.items():
            if movie_key.startswith(ckey) and len(ckey) >= 8:
                log.debug("    → prefix match: plex '%s' starts with '%s' (rk=%s)", movie_key, ckey, rk)
                return rk
            if ckey.startswith(movie_key) and len(movie_key) >= 8:
                log.debug("    → prefix match: tvdb '%s' starts with plex '%s' (rk=%s)", ckey, movie_key, rk)
                return rk

    log.debug("    → no match found in movie library")
    return None


def _english_title(tvdb_name: str | None, is_special: bool, abs_num_display: str) -> str:
    """Returns a display title for a TVDB episode.

    The bulk episode endpoint is called with lang=en, so tvdb_name should
    already be in English when TVDB has a translation. Only falls back to a
    numbered generic label when TVDB has no name at all for the episode.
    """
    if tvdb_name:
        return tvdb_name
    return f"Special {abs_num_display}" if is_special else f"Episode {abs_num_display}"


def get_all_settings(db: Session) -> dict:
    """Decrypts all Setting rows and returns them as a plain dict."""
    result = {}
    for row in db.query(Setting).all():
        try:
            result[row.key] = decrypt(row.value)
        except Exception:
            result[row.key] = ""
    return result


# ---------------------------------------------------------------------------
# Shared playlist-building helper (used by HTTP handler and cron job)
# ---------------------------------------------------------------------------


def _build_playlist_for_show(
    rating_key: int,
    settings: dict,
    db: Session,
    server,  # PlexServer
    skip_no_specials: bool = False,
    skip_no_order_change: bool = False,
    prebuilt_movie_index: tuple | None = None,
) -> dict:
    """Create or recreate the absolute-order playlist for one show.

    Returns a dict with keys:
      status            – "created" | "skipped" | "error"
      skip_reason       – "no_specials" | "no_order_change" | None
      playlist_title    – str | None
      playlist_rating_key – int | None
      show_title        – str | None
      matched           – int
      total             – int
      error             – str | None
    """
    _empty = dict(
        skip_reason=None, playlist_title=None, playlist_rating_key=None,
        show_title=None, matched=0, total=0, error=None,
    )
    try:
        show = get_show_detail(server, rating_key)
        tvdb_id = show.get("tvdb_id")
        if not tvdb_id:
            return {**_empty, "status": "error", "show_title": show.get("title"),
                    "error": "No TVDB ID found for this show — cannot build absolute order."}

        tvdb_episodes = get_absolute_order_episodes(settings["tvdb_api_key"], int(tvdb_id))
        total = len(tvdb_episodes)
        log.debug("BUILD PLAYLIST: '%s' (tvdb=%s) — %d TVDB episodes", show["title"], tvdb_id, total)

        # Skip: no specials
        if skip_no_specials:
            has_specials = any(
                ep.get("seasonNumber") == 0 or bool(ep.get("isMovie"))
                for ep in tvdb_episodes
            )
            if not has_specials:
                log.debug("  Skipping '%s': no specials on TVDB", show["title"])
                return {**_empty, "status": "skipped", "skip_reason": "no_specials",
                        "show_title": show["title"], "total": total}

        # Index Plex show episodes by TVDB episode ID
        tvdb_ep_id_to_rk: dict[str, int] = {}
        for ep in show["episodes"]:
            ep_tvdb_id = ep.get("tvdb_episode_id")
            if ep_tvdb_id:
                tvdb_ep_id_to_rk[str(ep_tvdb_id)] = ep["rating_key"]
        log.debug("Show library: %d episodes with TVDB episode IDs", len(tvdb_ep_id_to_rk))

        # Movie library index (pre-built for cron job, or built per-show for HTTP)
        if prebuilt_movie_index is not None:
            movie_by_tvdb_id, movie_by_title, movie_by_tmdb_id, movie_norm_titles = prebuilt_movie_index
        elif settings.get("movie_library"):
            log.debug("Movie library configured: '%s' — indexing…", settings["movie_library"])
            movie_by_tvdb_id, movie_by_title, movie_by_tmdb_id, movie_norm_titles = (
                get_movie_library_index(server, settings["movie_library"])
            )
        else:
            movie_by_tvdb_id = movie_by_title = movie_by_tmdb_id = movie_norm_titles = {}

        series_movie_tmdb_by_id: dict[str, str] = {}
        series_movie_tmdb_by_name: dict[str, str] = {}
        if settings.get("movie_library"):
            try:
                series_movie_tmdb_by_id, series_movie_tmdb_by_name = (
                    get_series_movie_tmdb_ids(settings["tvdb_api_key"], int(tvdb_id))
                )
            except Exception as exc:
                log.warning("Failed to fetch series movie TMDB IDs for '%s': %s", show["title"], exc)

            unmatched_specials = [
                ep for ep in tvdb_episodes
                if str(ep.get("id", "")) not in tvdb_ep_id_to_rk
                and (ep.get("seasonNumber") == 0 or bool(ep.get("isMovie")))
            ]
            if unmatched_specials:
                log.debug("Enriching %d unmatched special/movie episodes via TVDB extended API",
                          len(unmatched_specials))
                enrich_unmatched_specials(
                    settings["tvdb_api_key"], unmatched_specials, series_movie_tmdb_by_id
                )
        else:
            log.debug("No movie library configured — skipping movie library lookup")

        # Build ordered list of Plex rating keys following TVDB absolute order
        ordered_rating_keys: list[int] = []
        for tvdb_ep in tvdb_episodes:
            ep_id = str(tvdb_ep.get("id", ""))
            rk = tvdb_ep_id_to_rk.get(ep_id)
            if rk:
                log.debug("ep %s (%s) abs=%s → show library (rk=%s)",
                          ep_id, tvdb_ep.get("name"), tvdb_ep.get("absoluteNumber"), rk)
            if rk is None and settings.get("movie_library"):
                rk = _match_movie(
                    tvdb_ep, series_movie_tmdb_by_id, series_movie_tmdb_by_name,
                    movie_by_tvdb_id, movie_by_title, movie_by_tmdb_id, movie_norm_titles,
                )
            if rk is None:
                log.debug("ep %s (%s) abs=%s → NOT FOUND in any library",
                          ep_id, tvdb_ep.get("name"), tvdb_ep.get("absoluteNumber"))
            if rk is not None:
                ordered_rating_keys.append(rk)

        matched = len(ordered_rating_keys)
        if matched == 0:
            return {**_empty, "status": "error", "show_title": show["title"], "total": total,
                    "error": "No Plex episodes could be matched to TVDB absolute order entries. "
                             "Ensure your Plex library uses the TVDB agent with episode GUIDs."}

        # Skip: watch order wouldn't change (compare absolute order vs Plex natural order)
        if skip_no_order_change:
            # Only compare show-library episodes (movies are always an "addition")
            abs_show_rks = [
                tvdb_ep_id_to_rk[str(ep.get("id", ""))]
                for ep in tvdb_episodes
                if str(ep.get("id", "")) in tvdb_ep_id_to_rk
            ]
            natural_rks = [
                ep["rating_key"]
                for ep in sorted(
                    show["episodes"], key=lambda e: (e["season_number"], e["episode_number"])
                )
                if ep.get("tvdb_episode_id") and str(ep["tvdb_episode_id"]) in tvdb_ep_id_to_rk
            ]
            if abs_show_rks == natural_rks and not any(
                rk for rk in ordered_rating_keys if rk not in tvdb_ep_id_to_rk.values()
            ):
                log.debug("  Skipping '%s': absolute order matches natural order", show["title"])
                return {**_empty, "status": "skipped", "skip_reason": "no_order_change",
                        "show_title": show["title"], "matched": matched, "total": total}

        # Create or incrementally update the playlist in Plex
        playlist_title = f"{show['title']} — Absolute Order"
        existing = db.query(Playlist).filter(Playlist.show_rating_key == rating_key).first()

        update_type = None
        if existing:
            try:
                plex_playlist, update_type = update_plex_playlist(
                    server, rating_key, existing.playlist_rating_key,
                    ordered_rating_keys, playlist_title,
                )
                if update_type == "no_change":
                    return {**_empty, "status": "no_change",
                            "playlist_title": existing.playlist_title,
                            "playlist_rating_key": existing.playlist_rating_key,
                            "show_title": show["title"], "matched": matched, "total": total}
                # Playlist was appended-to or recreated — update DB record
                new_playlist_rk = plex_playlist.ratingKey
                existing.playlist_rating_key = new_playlist_rk
                existing.playlist_title = playlist_title
                db.commit()
            except Exception as exc:
                # Playlist was deleted from Plex externally — create a fresh one
                log.debug("Could not update existing playlist for '%s' (%s) — creating new",
                          show["title"], exc)
                db.delete(existing)
                db.flush()
                plex_playlist = build_ordered_playlist(
                    server, rating_key, ordered_rating_keys, playlist_title
                )
                new_playlist_rk = plex_playlist.ratingKey
                db.add(Playlist(
                    show_rating_key=rating_key, show_title=show["title"],
                    playlist_rating_key=new_playlist_rk, playlist_title=playlist_title,
                ))
                db.commit()
        else:
            plex_playlist = build_ordered_playlist(
                server, rating_key, ordered_rating_keys, playlist_title
            )
            new_playlist_rk = plex_playlist.ratingKey
            db.add(Playlist(
                show_rating_key=rating_key, show_title=show["title"],
                playlist_rating_key=new_playlist_rk, playlist_title=playlist_title,
            ))
            db.commit()

        return {
            "status": "created",
            "skip_reason": None,
            "update_type": update_type,  # "appended" | "recreated" | None (new)
            "playlist_title": playlist_title,
            "playlist_rating_key": new_playlist_rk,
            "show_title": show["title"],
            "matched": matched,
            "total": total,
            "error": None,
        }

    except Exception as exc:
        db.rollback()
        return {**_empty, "status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Auto-playlist job (called by cron scheduler and "Run Now" button)
# ---------------------------------------------------------------------------


def run_auto_playlists(db: Session) -> dict:
    """Iterate all shows in the library and create/update playlists.

    Respects the ShowSkip table and skip_no_specials / skip_no_order_change
    settings flags.  Returns a summary dict with created/skipped/errors counts.
    """
    log.info("Auto-playlist job starting")
    settings = get_all_settings(db)

    skip_no_specials = settings.get("skip_no_specials") == "true"
    skip_no_order_change = settings.get("skip_no_order_change") == "true"

    try:
        server = get_plex_server(settings["plex_url"], settings["plex_token"])
        shows = get_shows_from_library(server, settings["plex_library"])
    except Exception as exc:
        log.error("Auto-playlist job: could not connect to Plex: %s", exc)
        return {"created": 0, "skipped": 0, "errors": 1, "error": str(exc)}

    # Pre-index the movie library once for the whole job
    prebuilt_movie_index: tuple | None = None
    if settings.get("movie_library"):
        log.debug("Pre-indexing movie library '%s'", settings["movie_library"])
        prebuilt_movie_index = get_movie_library_index(server, settings["movie_library"])

    skipped_rks = {row.show_rating_key for row in db.query(ShowSkip).all()}

    created = skipped = up_to_date = errors = 0

    for show in shows:
        rk = show["rating_key"]
        if rk in skipped_rks:
            log.debug("Skipping '%s' (user-marked skip)", show["title"])
            skipped += 1
            continue

        log.info("Auto-playlist: processing '%s'", show["title"])
        result = _build_playlist_for_show(
            rk, settings, db, server,
            skip_no_specials=skip_no_specials,
            skip_no_order_change=skip_no_order_change,
            prebuilt_movie_index=prebuilt_movie_index,
        )

        status = result["status"]
        if status == "created":
            created += 1
            ut = result.get("update_type")
            if ut == "appended":
                log.info("  Updated (appended) playlist for '%s' (%d/%d matched)",
                         result["show_title"], result["matched"], result["total"])
            elif ut == "recreated":
                log.info("  Recreated playlist for '%s' (%d/%d matched)",
                         result["show_title"], result["matched"], result["total"])
            else:
                log.info("  Created new playlist for '%s' (%d/%d matched)",
                         result["show_title"], result["matched"], result["total"])
        elif status == "no_change":
            up_to_date += 1
            log.debug("  '%s': playlist already up to date", show["title"])
        elif status == "skipped":
            skipped += 1
            log.debug("  Skipped '%s' (reason: %s)", show["title"], result["skip_reason"])
        else:
            errors += 1
            log.warning("  Error for '%s': %s", show["title"], result["error"])

    log.info(
        "Auto-playlist job complete: %d updated, %d up-to-date, %d skipped, %d errors",
        created, up_to_date, skipped, errors,
    )
    return {"created": created, "up_to_date": up_to_date, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Index — anime grid
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    settings = get_all_settings(db)
    shows = []
    error = None

    try:
        server = get_plex_server(settings["plex_url"], settings["plex_token"])
        shows = get_shows_from_library(server, settings["plex_library"])
    except Exception as exc:
        error = str(exc)

    skipped_keys = {row.show_rating_key for row in db.query(ShowSkip).all()}

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "shows": shows, "error": error, "skipped_keys": skipped_keys},
    )


# ---------------------------------------------------------------------------
# Thumbnail proxy
# ---------------------------------------------------------------------------


@router.get("/proxy/thumb", response_class=Response)
async def proxy_thumb(request: Request, path: str, db: Session = Depends(get_db)):
    if not _require_auth(request):
        return Response(status_code=401)

    settings = get_all_settings(db)
    plex_url = settings.get("plex_url", "").rstrip("/")
    plex_token = settings.get("plex_token", "")

    url = f"{plex_url}{path}?X-Plex-Token={plex_token}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        return Response(status_code=502)


# ---------------------------------------------------------------------------
# Anime detail
# ---------------------------------------------------------------------------


@router.get("/anime/{rating_key}", response_class=HTMLResponse)
async def anime_detail(
    rating_key: int, request: Request, db: Session = Depends(get_db)
):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    settings = get_all_settings(db)
    error = None
    show = None
    tvdb_slug = None

    try:
        server = get_plex_server(settings["plex_url"], settings["plex_token"])
        show = get_show_detail(server, rating_key)
    except Exception as exc:
        error = str(exc)

    if show and show.get("tvdb_id"):
        try:
            series_info = get_series_info(settings["tvdb_api_key"], int(show["tvdb_id"]))
            tvdb_slug = series_info.get("slug")
        except Exception:
            pass

    existing_playlist = (
        db.query(Playlist).filter(Playlist.show_rating_key == rating_key).first()
    )
    skipped = db.query(ShowSkip).filter(ShowSkip.show_rating_key == rating_key).first() is not None

    return templates.TemplateResponse(
        "anime_detail.html",
        {
            "request": request,
            "show": show,
            "tvdb_slug": tvdb_slug,
            "existing_playlist": existing_playlist,
            "skipped": skipped,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Create absolute order playlist (HTMX POST)
# ---------------------------------------------------------------------------


@router.post("/anime/{rating_key}/playlist", response_class=HTMLResponse)
async def create_playlist(
    rating_key: int, request: Request, db: Session = Depends(get_db)
):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    settings = get_all_settings(db)
    result: dict = {"status": "error", "error": "Could not connect to Plex.",
                    "playlist_title": None, "playlist_rating_key": None,
                    "matched": 0, "total": 0}

    try:
        server = get_plex_server(settings["plex_url"], settings["plex_token"])
        result = _build_playlist_for_show(rating_key, settings, db, server)
    except Exception as exc:
        result = {**result, "error": str(exc)}

    status = result.get("status")
    return templates.TemplateResponse(
        "partials/playlist_result.html",
        {
            "request": request,
            "success": status in ("created", "no_change"),
            "deleted": False,
            "update_type": result.get("update_type"),   # "appended" | "recreated" | None (new)
            "no_change": status == "no_change",
            "playlist_title": result.get("playlist_title"),
            "playlist_rating_key": result.get("playlist_rating_key"),
            "show_rating_key": rating_key,
            "error": result.get("error"),
            "matched": result.get("matched", 0),
            "total": result.get("total", 0),
        },
    )


# ---------------------------------------------------------------------------
# Episode coverage check (HTMX POST)
# ---------------------------------------------------------------------------


@router.post("/anime/{rating_key}/coverage", response_class=HTMLResponse)
async def episode_coverage(
    rating_key: int, request: Request, db: Session = Depends(get_db)
):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    settings = get_all_settings(db)
    error = None
    coverage_items: list[dict] = []

    try:
        server = get_plex_server(settings["plex_url"], settings["plex_token"])
        show = get_show_detail(server, rating_key)

        tvdb_id = show.get("tvdb_id")
        if not tvdb_id:
            raise ValueError("No TVDB ID found for this show.")

        tvdb_episodes = get_absolute_order_episodes(settings["tvdb_api_key"], int(tvdb_id))
        log.debug("COVERAGE: '%s' (tvdb=%s) — %d TVDB episodes", show["title"], tvdb_id, len(tvdb_episodes))

        # Index Plex show episodes by TVDB episode ID → rating key + English title
        tvdb_ep_id_to_rk: dict[str, int] = {}
        tvdb_ep_id_to_plex_title: dict[str, str] = {}
        for ep in show["episodes"]:
            ep_tvdb_id = ep.get("tvdb_episode_id")
            if ep_tvdb_id:
                tvdb_ep_id_to_rk[str(ep_tvdb_id)] = ep["rating_key"]
                if ep.get("title"):
                    tvdb_ep_id_to_plex_title[str(ep_tvdb_id)] = ep["title"]
        log.debug("Show library: %d episodes with TVDB episode IDs", len(tvdb_ep_id_to_rk))

        # Optionally index movie library (tvdb, tmdb, exact title, fuzzy title)
        movie_by_tvdb_id: dict[str, int] = {}
        movie_by_title: dict[str, int] = {}
        movie_by_tmdb_id: dict[str, int] = {}
        movie_norm_titles: dict[str, int] = {}
        series_movie_tmdb_by_id: dict[str, str] = {}
        series_movie_tmdb_by_name: dict[str, str] = {}
        if settings.get("movie_library"):
            log.debug("Movie library configured: '%s' — indexing…", settings["movie_library"])
            movie_by_tvdb_id, movie_by_title, movie_by_tmdb_id, movie_norm_titles = (
                get_movie_library_index(server, settings["movie_library"])
            )
            try:
                series_movie_tmdb_by_id, series_movie_tmdb_by_name = (
                    get_series_movie_tmdb_ids(settings["tvdb_api_key"], int(tvdb_id))
                )
            except Exception as exc:
                log.warning("Failed to fetch series movie TMDB IDs: %s", exc)

            # Enrich unmatched S00/movie episodes with linkedMovies + English
            # translations fetched from the TVDB episode extended endpoint.
            unmatched_specials = [
                ep for ep in tvdb_episodes
                if str(ep.get("id", "")) not in tvdb_ep_id_to_rk
                and (ep.get("seasonNumber") == 0 or bool(ep.get("isMovie")))
            ]
            if unmatched_specials:
                log.debug("Enriching %d unmatched special/movie episodes via TVDB extended API",
                          len(unmatched_specials))
                enrich_unmatched_specials(
                    settings["tvdb_api_key"], unmatched_specials, series_movie_tmdb_by_id
                )
        else:
            log.debug("No movie library configured — skipping movie library lookup")

        for tvdb_ep in tvdb_episodes:
            ep_id = str(tvdb_ep.get("id", ""))
            abs_num = tvdb_ep.get("absoluteNumber", 0)

            source = None
            source_lib = None

            if tvdb_ep_id_to_rk.get(ep_id):
                source = "show"
                source_lib = settings.get("plex_library", "Show Library")
            elif settings.get("movie_library") and _match_movie(
                tvdb_ep,
                series_movie_tmdb_by_id,
                series_movie_tmdb_by_name,
                movie_by_tvdb_id,
                movie_by_title,
                movie_by_tmdb_id,
                movie_norm_titles,
            ):
                source = "movie"
                source_lib = settings.get("movie_library", "Movie Library")

            # Specials interleaved from default order get fractional abs numbers (e.g. 9.5)
            is_special = float(abs_num) != int(float(abs_num))
            abs_num_display = str(abs_num) if is_special else str(int(float(abs_num)))

            # Title priority: Plex (always English) → TVDB English translation
            # (set by enrich_unmatched_specials) → raw TVDB name → generic fallback.
            plex_title = tvdb_ep_id_to_plex_title.get(ep_id)
            if plex_title:
                display_name = plex_title
            else:
                best_name = tvdb_ep.get("_english_name") or tvdb_ep.get("name")
                display_name = _english_title(best_name, is_special, abs_num_display)

            coverage_items.append({
                "abs_num": abs_num,
                "abs_num_display": abs_num_display,
                "name": display_name,
                "is_special": is_special,
                "source": source,
                "source_lib": source_lib,
            })

    except Exception as exc:
        error = str(exc)

    matched_show = sum(1 for i in coverage_items if i["source"] == "show")
    matched_movie = sum(1 for i in coverage_items if i["source"] == "movie")
    missing_count = sum(1 for i in coverage_items if i["source"] is None)

    return templates.TemplateResponse(
        "partials/coverage_result.html",
        {
            "request": request,
            "coverage_items": coverage_items,
            "show_library": settings.get("plex_library", ""),
            "movie_library": settings.get("movie_library", ""),
            "total": len(coverage_items),
            "matched_show": matched_show,
            "matched_movie": matched_movie,
            "missing": missing_count,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Delete playlist (HTMX POST)
# ---------------------------------------------------------------------------


@router.post("/anime/{rating_key}/playlist/delete", response_class=HTMLResponse)
async def delete_playlist(
    rating_key: int, request: Request, db: Session = Depends(get_db)
):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    settings = get_all_settings(db)
    error = None
    playlist_title = None

    try:
        existing = db.query(Playlist).filter(Playlist.show_rating_key == rating_key).first()
        if not existing:
            raise ValueError("No tracked playlist found for this show.")

        playlist_title = existing.playlist_title
        server = get_plex_server(settings["plex_url"], settings["plex_token"])

        try:
            delete_plex_playlist(server, existing.playlist_rating_key)
        except Exception:
            pass  # Already deleted from Plex

        db.delete(existing)
        db.commit()

    except Exception as exc:
        error = str(exc)
        db.rollback()

    return templates.TemplateResponse(
        "partials/playlist_result.html",
        {
            "request": request,
            "success": error is None,
            "deleted": error is None,
            "playlist_title": playlist_title,
            "playlist_rating_key": None,
            "show_rating_key": rating_key,
            "error": error,
            "matched": 0,
            "total": 0,
        },
    )


# ---------------------------------------------------------------------------
# Skip toggle (HTMX POST) — marks/unmarks a show for auto-playlist skipping
# ---------------------------------------------------------------------------


@router.post("/anime/{rating_key}/skip", response_class=HTMLResponse)
async def toggle_skip(
    rating_key: int,
    request: Request,
    show_title: str = Form(""),
    db: Session = Depends(get_db),
):
    if not _require_auth(request):
        return Response(status_code=401)

    existing = db.query(ShowSkip).filter(ShowSkip.show_rating_key == rating_key).first()
    if existing:
        db.delete(existing)
        skipped = False
    else:
        db.add(ShowSkip(show_rating_key=rating_key, show_title=show_title or str(rating_key)))
        skipped = True
    db.commit()

    return templates.TemplateResponse(
        "partials/skip_toggle.html",
        {"request": request, "rating_key": rating_key, "show_title": show_title, "skipped": skipped},
    )
