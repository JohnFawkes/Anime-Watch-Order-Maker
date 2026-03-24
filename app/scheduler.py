import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("anime_watcher.scheduler")

_scheduler: BackgroundScheduler | None = None


def _auto_playlists_job() -> None:
    """Background job: create playlists for all shows in library."""
    from app.database import SessionLocal
    from app.routes.anime import run_auto_playlists

    db = SessionLocal()
    try:
        run_auto_playlists(db)
    except Exception as exc:
        log.error("Auto-playlist job raised an unhandled exception: %s", exc)
    finally:
        db.close()


def start_scheduler(cron_schedule: str) -> None:
    global _scheduler
    stop_scheduler()
    _scheduler = BackgroundScheduler(daemon=True)
    trigger = CronTrigger.from_crontab(cron_schedule)
    _scheduler.add_job(_auto_playlists_job, trigger, id="auto_playlists")
    _scheduler.start()
    log.info("Auto-playlist scheduler started with schedule: %s", cron_schedule)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")
    _scheduler = None


def update_scheduler(cron_enabled: bool, cron_schedule: str) -> None:
    """Start or stop the scheduler based on current settings."""
    if cron_enabled and cron_schedule:
        start_scheduler(cron_schedule)
    else:
        stop_scheduler()


def is_running() -> bool:
    return _scheduler is not None and _scheduler.running
