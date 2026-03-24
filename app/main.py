import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.crypto import get_session_secret
from app.database import init_db
from app.routes.auth import router as auth_router
from app.routes.anime import router as anime_router
from app.routes.settings_routes import router as settings_router

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
# Set LOG_LEVEL=DEBUG in the environment (or docker-compose) to enable trace
# logging.  Defaults to INFO so normal operation stays quiet.
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("anime_watcher")
log.info("Log level: %s", _log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Start the auto-playlist scheduler if cron is enabled in settings
    from app import scheduler as sched
    from app.database import SessionLocal
    from app.routes.anime import get_all_settings

    try:
        db = SessionLocal()
        try:
            settings = get_all_settings(db)
            cron_enabled = settings.get("cron_enabled") == "true"
            cron_schedule = settings.get("cron_schedule") or "0 3 * * *"
            sched.update_scheduler(cron_enabled, cron_schedule)
        finally:
            db.close()
    except Exception as exc:
        log.warning("Could not start scheduler on startup: %s", exc)

    yield

    sched.stop_scheduler()


app = FastAPI(title="Kometa Playlist Maker", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    max_age=86400 * 30,
)

app.include_router(auth_router)
app.include_router(anime_router)
app.include_router(settings_router)
