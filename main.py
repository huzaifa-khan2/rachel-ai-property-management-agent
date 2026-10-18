"""
Rachel - ClientCo Property Management
Main Scheduler — runs on Render as a persistent Background Worker

One serialized scheduler owns all recurring work:
  - 1st business day at 7:00 AM PT — refresh reports and run lease tracker
  - daily at 8:00 AM PT — durable 90/60/30-day lease-window catch-up
  - daily at 9:00 AM PT — vendor follow-ups
  - every minute — Gmail monitor plus independently due AppFolio polling
"""

import os
import schedule
import time
import subprocess
import logging
import sys
import sqlite3
from datetime import datetime, date, timedelta
import pytz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Rachel] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

PACIFIC = pytz.timezone("America/Los_Angeles")


def env_enabled(name: str, default: bool = False) -> bool:
    """Read a boolean environment safety switch."""
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


RACHEL_ENABLED = env_enabled("RACHEL_ENABLED", default=False)
DB_PATH = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")


def now_pt():
    return datetime.now(PACIFIC)


def is_first_of_month() -> bool:
    return now_pt().day == 1


def is_first_business_day_of_month() -> bool:
    today = now_pt().date()
    if today.day > 3:
        return False
    first = date(today.year, today.month, 1)
    weekday = first.weekday()
    if weekday < 5:
        first_biz = first
    else:
        first_biz = first + timedelta(days=7 - weekday)
    return today == first_biz


def _first_business_day(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first if first.weekday() < 5 else first + timedelta(
        days=7 - first.weekday()
    )


def _monthly_lease_cycle_due(*, as_of: date = None, db_path: str = DB_PATH) -> bool:
    """Catch up a missed monthly cycle after the first business day."""
    today = as_of or now_pt().date()
    if today < _first_business_day(today.year, today.month):
        return False
    month_key = today.strftime("%Y-%m")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rachel_runtime_state ("
            "state_key TEXT PRIMARY KEY, state_value TEXT, updated_at TEXT)"
        )
        conn.commit()
        row = conn.execute(
            "SELECT state_value FROM rachel_runtime_state "
            "WHERE state_key = 'last_successful_monthly_lease_cycle'"
        ).fetchone()
    finally:
        conn.close()
    return not row or row[0] != month_key


def _mark_monthly_lease_cycle_completed(
    *, as_of: date = None, db_path: str = DB_PATH
) -> None:
    today = as_of or now_pt().date()
    now = datetime.now(pytz.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rachel_runtime_state ("
            "state_key TEXT PRIMARY KEY, state_value TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO rachel_runtime_state (state_key, state_value, updated_at) "
            "VALUES ('last_successful_monthly_lease_cycle', ?, ?) "
            "ON CONFLICT(state_key) DO UPDATE SET "
            "state_value = excluded.state_value, updated_at = excluded.updated_at",
            (today.strftime("%Y-%m"), now),
        )
        conn.commit()
    finally:
        conn.close()


def run_script(script_name: str, args: list = None) -> bool:
    cmd = [sys.executable, script_name] + (args or [])
    log.info(f"Starting: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.stdout:
            log.info(f"Output:\n{result.stdout[-2000:]}")
        if result.stderr:
            log.warning(f"Stderr:\n{result.stderr[-500:]}")
        if result.returncode == 0:
            log.info(f"✅ {script_name} completed")
            return True

        log.error(f"❌ {script_name} failed (exit {result.returncode})")
        return False

    except subprocess.TimeoutExpired:
        log.error(f"❌ {script_name} timed out")
        return False

    except Exception as e:
        log.error(f"❌ {script_name} error: {e}")
        return False


# ── Monthly jobs ───────────────────────────────────────────────────────────

def job_monthly_lease_cycle():
    """
    First business day monthly workflow:

    1. Download fresh AppFolio reports.
    2. Parse and update Rachel's database.
    3. Run the lease and rent-increase tracker.
    4. Email Manager and Owner.
    """
    if not _monthly_lease_cycle_due():
        return True

    # A dry run is deliberately not a completed monthly delivery. Leave the
    # durable month marker untouched and avoid an otherwise wasted AppFolio
    # report refresh; the next enabled run will perform the complete cycle.
    if env_enabled("RACHEL_LEASE_EMAIL_DRY_RUN", default=True):
        log.warning(
            "Monthly lease cycle remains pending because "
            "RACHEL_LEASE_EMAIL_DRY_RUN=true."
        )
        return False

    log.info("=== Monthly Lease and Rent Increase Cycle ===")

    try:
        from rachel_scraper_v2 import run as run_appfolio_scraper

        scraper_ok = bool(run_appfolio_scraper())
    except Exception:
        log.exception("AppFolio report refresh failed")
        scraper_ok = False

    if not scraper_ok:
        log.error(
            "❌ AppFolio report refresh failed. "
            "Lease tracker will not run with stale data."
        )
        return False

    tracker_ok = run_script(
        "rachel_lease_tracker_v2.1.py"
    )

    if not tracker_ok:
        log.error(
            "❌ Lease tracker failed after the AppFolio "
            "reports were refreshed."
        )
        return False

    _mark_monthly_lease_cycle_completed()
    log.info(
        "✅ Monthly lease and rent increase cycle completed."
    )
    return True


def job_vendor_followups():
    """Daily 9 AM PT — nudge vendors who haven't sent estimates/schedules."""
    log.info("=== Daily Vendor Follow-up Check ===")
    try:
        from rachel_vendor_lifecycle import check_vendor_followups

        check_vendor_followups()
    except Exception:
        log.exception("Daily vendor follow-up check failed")


def job_daily_lease_windows():
    """Daily durable 90/60/30-day detection, including downtime catch-up."""
    log.info("=== Daily Lease Window Check ===")
    if not run_script("rachel_lease_window_processor.py"):
        log.error(
            "Lease-window processing did not verify notification delivery; "
            "ambiguous delivery will not be retried automatically."
        )


# Schedule in Pacific time so daylight-saving changes do not shift operations.
schedule.every().day.at("07:00", "America/Los_Angeles").do(
    job_monthly_lease_cycle
)
schedule.every().day.at("08:00", "America/Los_Angeles").do(
    job_daily_lease_windows
)
schedule.every().day.at("09:00", "America/Los_Angeles").do(
    job_vendor_followups
)


# ── Inbox monitor — one persistent in-process loop ────────────────────────

def inbox_monitor_loop():
    """Run one inbox cycle without creating a fresh Python process."""
    try:
        from rachel_inbox_monitor import run_once

        run_once()
    except Exception:
        log.exception("Inbox monitor cycle failed")


# ── Main ───────────────────────────────────────────────────────────────────

def run_scheduler():
    log.info("Rachel Scheduler started ✅")

    # Initialize and migrate every Rachel database table before any worker starts.
    log.info("Initializing Rachel database...")
    database_ready = run_script("rachel_db_init.py")

    if not database_ready:
        raise RuntimeError(
            "Rachel database initialization failed. "
            "Scheduler startup stopped to prevent partial operation."
        )

    log.info("✅ Rachel database initialized")

    if not RACHEL_ENABLED:
        log.warning(
            "⛔ RACHEL_ENABLED=false — all scheduled jobs and inbox "
            "automation are paused."
        )
        while True:
            time.sleep(60)

    log.info("✅ RACHEL_ENABLED=true — automation permitted")

    # Seed vendor data after the complete database schema is ready.
    log.info("Seeding database...")
    if run_script("rachel_seed_vendors.py"):
        log.info("✅ Database seeded")
    else:
        log.warning(
            "Vendor seed did not complete; existing durable vendor data "
            "is preserved and automated recommendations may be incomplete."
        )

    # A restart after the scheduled 7 AM slot must not lose the monthly cycle.
    job_monthly_lease_cycle()

    # Lease-window discovery is idempotent and durable. Run it once at startup
    # so a worker restart after the scheduled 8 AM slot catches thresholds
    # immediately instead of waiting until the following day.
    job_daily_lease_windows()

    log.info("Lease cycle: 1st business day at 7:00 AM PT")
    log.info("Lease windows: Daily durable 90/60/30-day catch-up at 8:00 AM PT")
    log.info("Inbox:    Every 1 minute 24/7")
    log.info("Vendors:  Follow-up check daily at 9:00 AM PT")

    # Keep scheduling and browser work on one thread. Playwright's synchronous
    # API is thread-affine, and the manager serializes AppFolio operations.
    log.info("📬 Inbox monitor started — checking Gmail every 1 minute")
    while True:
        schedule.run_pending()
        inbox_monitor_loop()
        time.sleep(60)


if __name__ == "__main__":
    run_scheduler()
