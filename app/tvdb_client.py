import logging
import re
import time
import httpx

log = logging.getLogger("anime_watcher.tvdb")

TVDB_BASE = "https://api4.thetvdb.com/v4"

# Module-level token cache: {api_key: {"token": str, "expires_at": float}}
_token_cache: dict[str, dict] = {}

# Token validity window: TVDB tokens last 30 days; we refresh after 25 days
TOKEN_TTL_SECONDS = 25 * 24 * 60 * 60


def get_token(api_key: str) -> str:
    """Returns a cached TVDB v4 bearer token, refreshing it if expired."""
    now = time.time()
    cached = _token_cache.get(api_key)
    if cached and cached["expires_at"] > now:
        return cached["token"]

    response = httpx.post(
        f"{TVDB_BASE}/login",
        json={"apikey": api_key},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    token = data["data"]["token"]

    _token_cache[api_key] = {
        "token": token,
        "expires_at": now + TOKEN_TTL_SECONDS,
    }
    return token


def get_series_info(api_key: str, tvdb_id: int) -> dict:
    """Returns extended series info from TVDB for the given series ID."""
    token = get_token(api_key)
    response = httpx.get(
        f"{TVDB_BASE}/series/{tvdb_id}/extended",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("data", {})


def _tmdb_from_remote_ids(remote_ids: list[dict]) -> str | None:
    """Extract TMDB ID from a TVDB remoteIds array.

    TVDB uses sourceName "TheMovieDB.com" and/or type 12 for TMDB entries.
    """
    for remote in remote_ids:
        source = (remote.get("sourceName") or "").lower()
        if "moviedb" in source or "tmdb" in source or remote.get("type") == 12:
            tmdb_id = str(remote.get("id", ""))
            if tmdb_id:
                return tmdb_id
    return None


def _title_key(title: str) -> str:
    """Lowercase + strip punctuation for fuzzy title matching."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def get_series_movie_tmdb_ids(
    api_key: str, tvdb_id: int
) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (by_tvdb_movie_id, by_normalized_name) → tmdb_id_str for movies linked to this series.

    The series extended endpoint lists linked movies but usually omits remoteIds.
    We therefore fetch each movie's own /v4/movies/{id}/extended endpoint where
    remoteIds (including the TMDB entry) are reliably present.
    """
    token = get_token(api_key)
    series_info = get_series_info(api_key, tvdb_id)

    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}

    movies = series_info.get("movies", [])
    log.debug("TVDB series %s has %d linked movie(s)", tvdb_id, len(movies))

    for movie in movies:
        movie_id = str(movie.get("id", ""))
        if not movie_id:
            continue

        movie_name = (movie.get("name") or "").strip()

        # Try remoteIds on the series-level movie object first (often absent)
        tmdb_id = _tmdb_from_remote_ids(movie.get("remoteIds", []))

        if not tmdb_id:
            # Fetch the individual movie extended record to get full remoteIds
            log.debug("  Movie %s (%s): no remoteIds in series response, fetching extended", movie_id, movie_name)
            try:
                resp = httpx.get(
                    f"{TVDB_BASE}/movies/{movie_id}/extended",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                )
                resp.raise_for_status()
                movie_data = resp.json().get("data", {})
                tmdb_id = _tmdb_from_remote_ids(movie_data.get("remoteIds", []))
                if not movie_name:
                    movie_name = (movie_data.get("name") or "").strip()
            except Exception as exc:
                log.warning("  Movie %s: failed to fetch extended info: %s", movie_id, exc)

        if tmdb_id:
            log.debug("  Movie %s (%s) → TMDB %s", movie_id, movie_name, tmdb_id)
            by_id[movie_id] = tmdb_id
            if movie_name:
                by_name[_title_key(movie_name)] = tmdb_id
        else:
            log.debug("  Movie %s (%s) → no TMDB ID found", movie_id, movie_name)

    log.debug("Series %s: built %d TMDB-by-id and %d TMDB-by-name entries", tvdb_id, len(by_id), len(by_name))
    return by_id, by_name


def enrich_unmatched_specials(
    api_key: str,
    episodes: list[dict],
    tmdb_by_movie_id: dict[str, str],
) -> None:
    """Fetch extended info for unmatched special/movie episodes.

    Modifies each episode dict in-place:
    - Sets '_english_name' if TVDB has an English translation.
    - Sets 'linkedMovies' if found and not already present.

    Also updates `tmdb_by_movie_id` in-place with any newly discovered
    TVDB-movie-ID → TMDB-ID mappings so the caller's lookup tables stay fresh.

    Called once per request for the subset of S00/movie episodes that were not
    found in the show library — typically ≤20 episodes, so the extra API calls
    are acceptable.
    """
    token = get_token(api_key)

    for ep in episodes:
        ep_id = ep.get("id")
        if not ep_id:
            continue

        log.debug("Fetching extended info for special ep %s (%s)", ep_id, ep.get("name"))
        try:
            resp = httpx.get(
                f"{TVDB_BASE}/episodes/{ep_id}/extended",
                headers={"Authorization": f"Bearer {token}"},
                params={"meta": "translations"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except Exception as exc:
            log.warning("  Failed to fetch extended episode %s: %s", ep_id, exc)
            continue

        # English translation
        for trans in (data.get("translations", {}).get("nameTranslations") or []):
            if trans.get("language") == "eng" and trans.get("name"):
                ep["_english_name"] = trans["name"]
                log.debug("  Episode %s English name: %s", ep_id, trans["name"])
                break

        # Linked movies — enrich the episode dict and resolve TMDB IDs
        linked_movies = data.get("linkedMovies") or []
        if linked_movies and not ep.get("linkedMovies"):
            ep["linkedMovies"] = linked_movies

        for linked in linked_movies:
            movie_id = str(linked.get("id", ""))
            if not movie_id or movie_id in tmdb_by_movie_id:
                continue
            log.debug("  Fetching TMDB ID for TVDB movie %s", movie_id)
            try:
                movie_resp = httpx.get(
                    f"{TVDB_BASE}/movies/{movie_id}/extended",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                )
                movie_resp.raise_for_status()
                movie_data = movie_resp.json().get("data", {})
                tmdb_id = _tmdb_from_remote_ids(movie_data.get("remoteIds", []))
                if tmdb_id:
                    tmdb_by_movie_id[movie_id] = tmdb_id
                    log.debug("  Movie %s → TMDB %s", movie_id, tmdb_id)
                else:
                    log.debug("  Movie %s → no TMDB ID found", movie_id)
            except Exception as exc:
                log.warning("  Failed to fetch movie %s: %s", movie_id, exc)


def _fetch_all_episodes(token: str, tvdb_id: int, season_type: str) -> list[dict]:
    """Fetches all episodes for a series/season-type, handling pagination."""
    episodes: list[dict] = []
    page = 0
    while True:
        response = httpx.get(
            f"{TVDB_BASE}/series/{tvdb_id}/episodes/{season_type}",
            headers={"Authorization": f"Bearer {token}"},
            params={"page": page, "lang": "en"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        batch = data.get("data", {}).get("episodes", [])
        if not batch:
            break
        episodes.extend(batch)
        if not data.get("links", {}).get("next"):
            break
        page += 1
    return episodes


def get_absolute_order_episodes(api_key: str, tvdb_id: int) -> list[dict]:
    """Returns all episodes sorted by absolute number, including specials.

    Two sources are merged:
    1. Episodes in TVDB's absolute order season type (already have absoluteNumber).
    2. Season-0 specials from the default order that have airsBeforeEpisode /
       airsAfterEpisode metadata but were not assigned to the absolute order.
       These are interleaved at the correct position (0.5 offset from the
       adjacent regular episode's absolute number).
    """
    token = get_token(api_key)

    # --- Absolute order episodes ---
    abs_episodes = _fetch_all_episodes(token, tvdb_id, "absolute")
    abs_episodes = [ep for ep in abs_episodes if ep.get("absoluteNumber") is not None]

    # Map: TVDB episode ID → absoluteNumber (for reference lookups below)
    id_to_absnum: dict[int, float] = {
        ep["id"]: ep["absoluteNumber"] for ep in abs_episodes
    }

    # --- Default order: find unplaced specials ---
    default_episodes = _fetch_all_episodes(token, tvdb_id, "default")

    # Build (season, episode_number) → absoluteNumber directly for non-special episodes
    # that appear in both the default and absolute orders.  This single map replaces
    # the old two-step pos→id→absnum lookup and lets us resolve placements even when
    # the referenced episode's ID exists in the default order but was never assigned an
    # absoluteNumber (e.g. a partial absolute order on TVDB).
    default_pos_to_absnum: dict[tuple[int, int], float] = {}
    for ep in default_episodes:
        sn = ep.get("seasonNumber")
        num = ep.get("number")
        if sn is not None and num is not None and sn != 0:
            abs_n = id_to_absnum.get(ep["id"])
            if abs_n is not None:
                default_pos_to_absnum[(sn, num)] = abs_n

    # Maximum absolute number across all absolute-order episodes — used as the
    # fallback anchor for specials that only specify airsAfterSeason (no episode).
    max_abs_num: float = max((ep["absoluteNumber"] for ep in abs_episodes), default=0)

    abs_ids = set(id_to_absnum.keys())

    # Counter so multiple end-of-series specials get distinct, ordered positions.
    _end_special_idx = 0

    extra: list[dict] = []
    # Specials/movies with no airs placement data at all — appended after main list.
    unplaced_at_end: list[dict] = []

    for ep in default_episodes:
        sn = ep.get("seasonNumber")
        # isMovie can be 1 (int) or True (bool) depending on the TVDB API version
        is_movie_ep = bool(ep.get("isMovie"))

        # Include Season-0 specials AND movie-tagged episodes from any season.
        # Movie episodes (isMovie=1) may be listed in a non-zero season on TVDB
        # even though they logically belong after a specific point in the series.
        if sn != 0 and not is_movie_ep:
            continue

        if ep["id"] in abs_ids:
            continue  # Already in absolute order

        position: float | None = None
        before_s = ep.get("airsBeforeSeason")
        before_e = ep.get("airsBeforeEpisode")
        after_s = ep.get("airsAfterSeason")
        after_e = ep.get("airsAfterEpisode")

        if before_s is not None and before_e is not None:
            ref_abs = default_pos_to_absnum.get((before_s, before_e))
            if ref_abs is not None:
                position = ref_abs - 0.5
        elif after_s is not None:
            if after_e is not None:
                ref_abs = default_pos_to_absnum.get((after_s, after_e))
            else:
                ref_abs = None

            if ref_abs is None:
                # No exact episode match — fall back to the last absolute number
                # for that season, or the global maximum if season is unknown.
                season_abs = [v for (s, _), v in default_pos_to_absnum.items() if s == after_s]
                ref_abs = max(season_abs) if season_abs else max_abs_num
                # Increment index so each fallback special gets a unique slot.
                _end_special_idx += 1
                position = ref_abs + _end_special_idx * 0.5
            else:
                position = ref_abs + 0.5

        if position is not None:
            placed = dict(ep)
            placed["absoluteNumber"] = position
            extra.append(placed)
        else:
            # No airs placement data whatsoever — queue to append after main list.
            unplaced_at_end.append(ep)

    merged = abs_episodes + extra
    merged.sort(key=lambda ep: ep["absoluteNumber"])

    # Append specials/movies that have no airs data after the last regular episode.
    if unplaced_at_end:
        last_abs: float = merged[-1]["absoluteNumber"] if merged else 0.0
        for i, ep in enumerate(unplaced_at_end):
            placed = dict(ep)
            placed["absoluteNumber"] = last_abs + 1.0 + i * 0.5
            merged.append(placed)

    return merged
