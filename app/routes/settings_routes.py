import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app import scheduler as sched
from app.crypto import decrypt, encrypt
from app.database import SessionLocal, get_db
from app.models import Setting, User
from app.plex_client import get_movie_libraries, get_plex_server, get_show_libraries
from app.routes.anime import get_all_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _require_auth(request: Request):
    return request.session.get("user_id")


def _mask(value: str) -> str:
    """Returns a masked version of a sensitive string (last 4 chars visible)."""
    if not value or len(value) <= 4:
        return "****"
    return "****" + value[-4:]


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request, db: Session = Depends(get_db)):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    raw = get_all_settings(db)
    masked = {
        "plex_url": raw.get("plex_url", ""),
        "plex_token": _mask(raw.get("plex_token", "")),
        "plex_library": raw.get("plex_library", ""),
        "movie_library": raw.get("movie_library", ""),
        "tvdb_api_key": _mask(raw.get("tvdb_api_key", "")),
        # Cron / automation settings (shown as-is, not sensitive)
        "cron_enabled": raw.get("cron_enabled", "false"),
        "cron_schedule": raw.get("cron_schedule", "0 3 * * *"),
        "skip_no_specials": raw.get("skip_no_specials", "false"),
        "skip_no_order_change": raw.get("skip_no_order_change", "false"),
        "cron_running": sched.is_running(),
    }
    flash = request.session.pop("flash", None)
    flash_error = request.session.pop("flash_error", None)

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": masked,
            "flash": flash,
            "flash_error": flash_error,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_post(
    request: Request,
    plex_url: str = Form(""),
    plex_token: str = Form(""),
    plex_library: str = Form(""),
    movie_library: str = Form(""),
    tvdb_api_key: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    # Automation / cron settings
    cron_enabled: str = Form(""),
    cron_schedule: str = Form(""),
    skip_no_specials: str = Form(""),
    skip_no_order_change: str = Form(""),
    db: Session = Depends(get_db),
):
    user_id = _require_auth(request)
    if not user_id:
        return RedirectResponse(url="/login", status_code=302)

    # Sensitive settings — only update when a non-empty value is submitted
    sensitive = {
        "plex_url": plex_url.strip(),
        "plex_token": plex_token.strip(),
        "plex_library": plex_library.strip(),
        "movie_library": movie_library.strip(),
        "tvdb_api_key": tvdb_api_key.strip(),
    }
    for key, value in sensitive.items():
        if not value:
            continue
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = encrypt(value)
        else:
            db.add(Setting(key=key, value=encrypt(value)))

    # Cron / automation settings — always save (checkboxes send nothing when off)
    cron_enabled_val = "true" if cron_enabled else "false"
    cron_schedule_val = cron_schedule.strip() or "0 3 * * *"
    skip_no_specials_val = "true" if skip_no_specials else "false"
    skip_no_order_change_val = "true" if skip_no_order_change else "false"

    for key, value in [
        ("cron_enabled", cron_enabled_val),
        ("cron_schedule", cron_schedule_val),
        ("skip_no_specials", skip_no_specials_val),
        ("skip_no_order_change", skip_no_order_change_val),
    ]:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = encrypt(value)
        else:
            db.add(Setting(key=key, value=encrypt(value)))

    # Optional password change
    if new_password:
        if new_password != confirm_password:
            request.session["flash_error"] = "Passwords do not match."
            db.rollback()
            return RedirectResponse(url="/settings", status_code=302)
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.hashed_password = pwd_context.hash(new_password)

    db.commit()

    # Apply scheduler changes immediately
    sched.update_scheduler(cron_enabled_val == "true", cron_schedule_val)

    request.session["flash"] = "Settings saved successfully."
    return RedirectResponse(url="/settings", status_code=302)


# ---------------------------------------------------------------------------
# HTMX partial: discover Plex libraries using stored credentials
# ---------------------------------------------------------------------------


@router.post("/settings/plex-libraries", response_class=HTMLResponse)
async def settings_plex_libraries(
    request: Request,
    plex_url: str = Form(""),
    db: Session = Depends(get_db),
):
    """Library discovery for the settings page.

    Uses the Plex URL from the form (so edits take effect immediately) but
    always reads the token from the database so the hidden token never needs
    to be round-tripped through the browser.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    raw = get_all_settings(db)
    url = plex_url.strip() or raw.get("plex_url", "")
    token = raw.get("plex_token", "")

    error = None
    libraries = []
    try:
        server = get_plex_server(url, token)
        libraries = get_show_libraries(server)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        "partials/library_select.html",
        {"request": request, "libraries": libraries, "error": error},
    )


@router.post("/settings/movie-libraries", response_class=HTMLResponse)
async def settings_movie_libraries(
    request: Request,
    plex_url: str = Form(""),
    db: Session = Depends(get_db),
):
    """Movie library discovery for the settings page.

    Reads the Plex token from the database so it never needs to be
    round-tripped through the browser.
    """
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    raw = get_all_settings(db)
    url = plex_url.strip() or raw.get("plex_url", "")
    token = raw.get("plex_token", "")

    error = None
    libraries = []
    try:
        server = get_plex_server(url, token)
        libraries = get_movie_libraries(server)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        "partials/movie_library_select.html",
        {"request": request, "libraries": libraries, "error": error},
    )


# ---------------------------------------------------------------------------
# "Run Now" — triggers the auto-playlist job immediately (HTMX POST)
# ---------------------------------------------------------------------------


@router.post("/cron/run-now", response_class=HTMLResponse)
async def cron_run_now(request: Request):
    if not _require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    from app.routes.anime import run_auto_playlists

    error = None
    result = None

    def _job():
        db = SessionLocal()
        try:
            return run_auto_playlists(db)
        finally:
            db.close()

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _job)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        "partials/cron_status.html",
        {"request": request, "result": result, "error": error},
    )
