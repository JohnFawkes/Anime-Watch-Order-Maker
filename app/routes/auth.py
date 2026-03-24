from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.crypto import encrypt
from app.database import get_db
from app.models import Setting, User
from app.plex_client import get_plex_server, get_show_libraries

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request, db: Session = Depends(get_db)):
    user_count = db.query(User).count()
    if user_count > 0:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("setup.html", {"request": request, "error": None})


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    plex_url: str = Form(...),
    plex_token: str = Form(...),
    plex_library: str = Form(...),
    tvdb_api_key: str = Form(...),
    db: Session = Depends(get_db),
):
    # Guard: only allow setup if no users exist
    if db.query(User).count() > 0:
        return RedirectResponse(url="/login", status_code=302)

    if not username or not password:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Username and password are required."},
            status_code=400,
        )

    hashed = pwd_context.hash(password)
    user = User(username=username, hashed_password=hashed)
    db.add(user)

    settings_to_save = {
        "plex_url": plex_url,
        "plex_token": plex_token,
        "plex_library": plex_library,
        "tvdb_api_key": tvdb_api_key,
    }
    for key, value in settings_to_save.items():
        setting = Setting(key=key, value=encrypt(value))
        db.add(setting)

    db.commit()
    return RedirectResponse(url="/login", status_code=302)


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, db: Session = Depends(get_db)):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    if db.query(User).count() == 0:
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password."},
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ---------------------------------------------------------------------------
# HTMX partial: discover Plex libraries
# ---------------------------------------------------------------------------


@router.post("/api/plex-libraries", response_class=HTMLResponse)
async def api_plex_libraries(
    request: Request,
    plex_url: str = Form(...),
    plex_token: str = Form(...),
):
    error = None
    libraries = []
    try:
        server = get_plex_server(plex_url.strip(), plex_token.strip())
        libraries = get_show_libraries(server)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        "partials/library_select.html",
        {"request": request, "libraries": libraries, "error": error},
    )
