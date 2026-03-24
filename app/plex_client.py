import logging
import re

from plexapi.server import PlexServer

log = logging.getLogger("anime_watcher.plex")


def get_plex_server(url: str, token: str) -> PlexServer:
    """Returns an authenticated PlexServer instance."""
    return PlexServer(url, token)


def get_show_libraries(server: PlexServer) -> list[dict]:
    """Returns all Show-type libraries as [{"title": ..., "key": ...}]."""
    libraries = []
    for section in server.library.sections():
        if section.type == "show":
            libraries.append({"title": section.title, "key": section.key})
    return libraries


def get_movie_libraries(server: PlexServer) -> list[dict]:
    """Returns all Movie-type libraries as [{"title": ..., "key": ...}]."""
    libraries = []
    for section in server.library.sections():
        if section.type == "movie":
            libraries.append({"title": section.title, "key": section.key})
    return libraries


def _extract_tvdb_id(show) -> str | None:
    """Extract TVDB series ID from a Plex show item."""
    # Modern agent: check guids list for tvdb:// entries
    if hasattr(show, "guids") and show.guids:
        for guid in show.guids:
            guid_id = guid.id if hasattr(guid, "id") else str(guid)
            if guid_id.startswith("tvdb://"):
                return guid_id.replace("tvdb://", "").strip()

    # Legacy agent: parse from show.guid string
    # Format: com.plexapp.agents.thetvdb://SERIES_ID/...
    if hasattr(show, "guid") and show.guid and "thetvdb" in show.guid:
        parts = show.guid.split("://")
        if len(parts) > 1:
            series_id = parts[1].split("/")[0]
            if series_id:
                return series_id

    return None


def get_shows_from_library(server: PlexServer, library_name: str) -> list[dict]:
    """Returns a sorted list of shows from the given library.

    Each entry contains: rating_key, title, thumb, year, tvdb_id.
    """
    section = server.library.section(library_name)
    shows = section.all()
    result = []
    for show in shows:
        tvdb_id = _extract_tvdb_id(show)
        result.append(
            {
                "rating_key": show.ratingKey,
                "title": show.title,
                "thumb": show.thumb,
                "year": show.year,
                "tvdb_id": tvdb_id,
            }
        )
    result.sort(key=lambda s: s["title"].lower())
    return result


def _extract_tvdb_episode_id(episode) -> str | None:
    """Extract TVDB episode ID from a Plex episode item."""
    # Modern agent: check guids list
    if hasattr(episode, "guids") and episode.guids:
        for guid in episode.guids:
            guid_id = guid.id if hasattr(guid, "id") else str(guid)
            if guid_id.startswith("tvdb://"):
                return guid_id.replace("tvdb://", "").strip()

    # Legacy agent: com.plexapp.agents.thetvdb://seriesid/season/ep
    # Cannot reliably extract the TVDB episode ID from this format — skip.
    return None


def get_show_detail(server: PlexServer, rating_key: int) -> dict:
    """Returns show info including all episodes with tvdb_episode_id extracted from guids."""
    show = server.fetchItem(rating_key)
    tvdb_id = _extract_tvdb_id(show)

    episodes = []
    for ep in show.episodes():
        tvdb_episode_id = _extract_tvdb_episode_id(ep)
        episodes.append(
            {
                "rating_key": ep.ratingKey,
                "title": ep.title,
                "season_number": ep.seasonNumber,
                "episode_number": ep.index,
                "thumb": ep.thumb,
                "tvdb_episode_id": tvdb_episode_id,
            }
        )

    return {
        "rating_key": show.ratingKey,
        "title": show.title,
        "thumb": show.thumb,
        "year": show.year,
        "summary": show.summary,
        "tvdb_id": tvdb_id,
        "episodes": episodes,
    }


def delete_plex_playlist(server: PlexServer, playlist_rating_key: int) -> None:
    """Deletes a Plex playlist by its ratingKey."""
    playlist = server.fetchItem(playlist_rating_key)
    playlist.delete()


def _title_key(title: str) -> str:
    """Normalize a title for fuzzy matching: lowercase, strip punctuation/spaces."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def get_movie_library_index(
    server: PlexServer, movie_library_name: str
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, str]]:
    """Indexes a movie library for cross-library playlist matching.

    Returns:
        by_tvdb_id:    {tvdb_id_str: rating_key}   — TVDB movie ID (tvdb:// GUID)
        by_title:      {exact_lower_title: rating_key}
        by_tmdb_id:    {tmdb_id_str: rating_key}   — TMDB ID (tmdb:// GUID)
        norm_titles:   {normalized_title: rating_key} — punctuation-stripped for fuzzy match
    """
    try:
        section = server.library.section(movie_library_name)
    except Exception:
        return {}, {}, {}, {}

    by_tvdb_id: dict[str, int] = {}
    by_title: dict[str, int] = {}
    by_tmdb_id: dict[str, int] = {}
    norm_titles: dict[str, int] = {}
    for movie in section.all():
        rk = movie.ratingKey
        exact = movie.title.lower().strip()
        by_title[exact] = rk
        norm_titles[_title_key(movie.title)] = rk
        if hasattr(movie, "guids") and movie.guids:
            for guid in movie.guids:
                gid = guid.id if hasattr(guid, "id") else str(guid)
                if gid.startswith("tvdb://"):
                    by_tvdb_id[gid.replace("tvdb://", "").strip()] = rk
                elif gid.startswith("tmdb://"):
                    by_tmdb_id[gid.replace("tmdb://", "").strip()] = rk

    log.debug(
        "Movie library '%s': %d movies indexed — %d tvdb IDs, %d tmdb IDs, %d titles",
        movie_library_name, len(by_title), len(by_tvdb_id), len(by_tmdb_id), len(norm_titles),
    )
    return by_tvdb_id, by_title, by_tmdb_id, norm_titles


def _fetch_plex_items(
    server: PlexServer, show_rating_key: int, ordered_rating_keys: list[int]
) -> list:
    """Resolve a list of rating keys to Plex media objects.

    Fetches all show episodes in a single call for efficiency; items not found
    there (e.g. from a movie library) are fetched individually.
    """
    show = server.fetchItem(show_rating_key)
    ep_by_rk = {ep.ratingKey: ep for ep in show.episodes()}

    items = []
    for rk in ordered_rating_keys:
        if rk in ep_by_rk:
            items.append(ep_by_rk[rk])
        else:
            try:
                items.append(server.fetchItem(rk))
            except Exception:
                pass
    return items


def build_ordered_playlist(
    server: PlexServer,
    show_rating_key: int,
    ordered_rating_keys: list[int],
    title: str,
):
    """Creates a new Plex playlist in the given episode order."""
    items = _fetch_plex_items(server, show_rating_key, ordered_rating_keys)
    return server.createPlaylist(title, items=items)


def update_plex_playlist(
    server: PlexServer,
    show_rating_key: int,
    playlist_rating_key: int,
    ordered_rating_keys: list[int],
    title: str,
) -> tuple[object, str]:
    """Update an existing playlist to match ordered_rating_keys.

    Returns (playlist, update_type) where update_type is:
      "no_change"  — already up to date, nothing modified
      "appended"   — new items appended to the end (incremental update)
      "recreated"  — old playlist deleted and new one created (reorder/removal)

    Raises if the playlist cannot be fetched from Plex (e.g. already deleted).
    """
    playlist = server.fetchItem(playlist_rating_key)
    current_rks = [item.ratingKey for item in playlist.items()]
    desired_rks = list(ordered_rating_keys)

    if current_rks == desired_rks:
        log.debug("Playlist '%s': already up to date (%d items)", title, len(current_rks))
        return playlist, "no_change"

    n = len(current_rks)
    if len(desired_rks) > n and desired_rks[:n] == current_rks:
        # Pure append — existing items unchanged, only new ones at the end
        new_rks = desired_rks[n:]
        log.debug("Playlist '%s': appending %d new item(s)", title, len(new_rks))
        new_items = _fetch_plex_items(server, show_rating_key, new_rks)
        if new_items:
            playlist.addItems(new_items)
        return playlist, "appended"

    # Items removed, reordered, or inserted in middle → full recreate
    log.debug(
        "Playlist '%s': order changed (current %d items, desired %d) — recreating",
        title, len(current_rks), len(desired_rks),
    )
    playlist.delete()
    new_playlist = build_ordered_playlist(server, show_rating_key, desired_rks, title)
    return new_playlist, "recreated"
