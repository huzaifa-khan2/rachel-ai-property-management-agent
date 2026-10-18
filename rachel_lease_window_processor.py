"""Durable daily 90/60/30-day lease-window processing with catch-up."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from rachel_event_memory import initialize_event_memory


DB_PATH = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")
WINDOWS = (90, 60, 30)


class LeaseWindowError(RuntimeError):
    """Sanitized lease-window processing failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_date(value: object) -> Optional[date]:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def discover_due_lease_windows(
    *, db_path: str = DB_PATH, as_of: Optional[date] = None
) -> dict:
    """Persist every crossed threshold, including thresholds missed offline."""
    scan_date = as_of or date.today()
    initialize_event_memory(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "tenants" not in tables:
            return {"due_events": 0, "caught_up_events": 0, "created": []}
        tenant_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tenants)")
        }
        required = {
            "occupancy_id", "lease_type", "lease_end",
            "eligible_increase_on", "is_active",
        }
        if not required.issubset(tenant_columns):
            raise LeaseWindowError("tenant_schema_missing_window_fields")
        has_exclusions = "occupancy_classifications" in tables
        exclusion_clause = (
            "AND NOT EXISTS (SELECT 1 FROM occupancy_classifications oc "
            "WHERE oc.occupancy_id = t.occupancy_id "
            "AND oc.exclude_from_lease_reports = 1)"
            if has_exclusions else ""
        )
        rows = conn.execute(
            "SELECT occupancy_id, lease_type, lease_end, "
            "eligible_increase_on FROM tenants t WHERE is_active = 1 "
            + exclusion_clause
        ).fetchall()
        created = []
        now = _now()
        for row in rows:
            candidates = []
            if str(row["lease_type"] or "").lower() == "fixed":
                candidates.append(("fixed_lease_end", row["lease_end"]))
            if str(row["lease_type"] or "").lower() == "month-to-month":
                candidates.append((
                    "mtm_increase_eligible", row["eligible_increase_on"]
                ))
            occupancy_id = str(row["occupancy_id"] or "").strip()
            if not occupancy_id:
                continue
            for event_kind, raw_target_date in candidates:
                target_date = _parse_date(raw_target_date)
                if not target_date:
                    continue
                for window_days in WINDOWS:
                    due_date = target_date - timedelta(days=window_days)
                    if due_date > scan_date:
                        continue
                    event_id = _digest("|".join((
                        occupancy_id, event_kind, target_date.isoformat(),
                        str(window_days),
                    )))
                    caught_up = int(due_date < scan_date)
                    inserted = conn.execute(
                        """
                        INSERT OR IGNORE INTO lease_window_events (
                            event_id, occupancy_id, event_kind, target_date,
                            window_days, due_date, status, caught_up,
                            detected_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending_notification',
                                  ?, ?, ?)
                        """,
                        (
                            event_id, occupancy_id, event_kind,
                            target_date.isoformat(), window_days,
                            due_date.isoformat(), caught_up, now, now,
                        ),
                    ).rowcount
                    if inserted:
                        created.append({
                            "event_id": event_id,
                            "occupancy_id": occupancy_id,
                            "event_kind": event_kind,
                            "target_date": target_date.isoformat(),
                            "window_days": window_days,
                            "due_date": due_date.isoformat(),
                            "caught_up": bool(caught_up),
                        })
        conn.commit()
    finally:
        conn.close()
    return {
        "due_events": len(created),
        "caught_up_events": sum(1 for event in created if event["caught_up"]),
        "created": created,
    }


def _notification_body(events: list[dict], scan_date: date) -> str:
    lines = [
        f"Rachel lease-window alert for {scan_date.isoformat()}.",
        "The following durable 90/60/30-day thresholds became due:",
        "",
    ]
    for event in sorted(
        events,
        key=lambda item: (
            item["window_days"], item["target_date"], item["occupancy_id"]
        ),
    ):
        catchup = " — CATCH-UP AFTER MISSED RUN" if event["caught_up"] else ""
        tenant = event.get("primary_tenant") or "unknown tenant"
        address = event.get("property_address") or "unknown address"
        unit = event.get("unit") or "unknown unit"
        lines.append(
            f"- {event['window_days']}-day {event['event_kind']} | "
            f"{tenant} | {address}, Unit {unit} | target "
            f"{event['target_date']}{catchup}"
        )
    lines.extend((
        "",
        "Review the current AppFolio lease record before taking any action.",
        "",
        "— Rachel",
        "Assistant Property Manager",
    ))
    return "\n".join(lines)


def process_due_lease_windows(
    *,
    db_path: str = DB_PATH,
    as_of: Optional[date] = None,
    notifier: Callable[[str, str], bool],
) -> dict:
    """Claim, notify, and verify new thresholds without duplicate retries."""
    scan_date = as_of or date.today()
    discovery = discover_due_lease_windows(
        db_path=db_path, as_of=scan_date
    )
    initialize_event_memory(db_path)
    run_id = _digest(f"lease-window-run|{scan_date.isoformat()}|{_now()}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        pending_rows = conn.execute(
            "SELECT lease_window_events.*, tenants.property_address, "
            "tenants.unit, tenants.primary_tenant FROM lease_window_events "
            "LEFT JOIN tenants ON tenants.occupancy_id = "
            "lease_window_events.occupancy_id "
            "WHERE lease_window_events.status = 'pending_notification' "
            "ORDER BY due_date, event_id"
        ).fetchall()
        pending = [dict(row) for row in pending_rows]
        for event in pending:
            event["caught_up"] = bool(event["caught_up"])
        conn.execute(
            """
            INSERT INTO lease_window_runs (
                run_id, as_of_date, started_at, due_events,
                caught_up_events, status
            ) VALUES (?, ?, ?, ?, ?, 'running')
            """,
            (
                run_id, scan_date.isoformat(), _now(), len(pending),
                sum(1 for event in pending if event["caught_up"]),
            ),
        )
        if pending:
            event_ids = [event["event_id"] for event in pending]
            placeholders = ",".join("?" for _event_id in event_ids)
            changed = conn.execute(
                f"UPDATE lease_window_events SET "
                f"status = 'notification_unverified', "
                f"delivery_attempts = delivery_attempts + 1, updated_at = ? "
                f"WHERE event_id IN ({placeholders}) "
                f"AND status = 'pending_notification'",
                (_now(), *event_ids),
            ).rowcount
            if changed != len(event_ids):
                raise LeaseWindowError("lease_window_notification_claim_race")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    finally:
        if conn:
            conn.close()

    delivered = False
    failure_reason = ""
    if pending:
        subject = (
            f"Rachel Lease Windows — {len(pending)} threshold"
            f"{'s' if len(pending) != 1 else ''} due"
        )
        try:
            delivered = bool(notifier(
                subject, _notification_body(pending, scan_date)
            ))
            if not delivered:
                failure_reason = "notification_not_verified"
        except Exception as error:
            failure_reason = f"notification_{type(error).__name__}"

    receipt = (
        "verified-notification:" + _digest("|".join(
            event["event_id"] for event in pending
        )) if pending and delivered else ""
    )
    conn = sqlite3.connect(db_path)
    try:
        if pending:
            event_ids = [event["event_id"] for event in pending]
            placeholders = ",".join("?" for _event_id in event_ids)
            if delivered:
                conn.execute(
                    f"UPDATE lease_window_events SET status = 'notified', "
                    f"delivery_receipt = ?, failure_reason = '', updated_at = ? "
                    f"WHERE event_id IN ({placeholders}) "
                    f"AND status = 'notification_unverified'",
                    (receipt, _now(), *event_ids),
                )
            else:
                conn.execute(
                    f"UPDATE lease_window_events SET failure_reason = ?, "
                    f"updated_at = ? WHERE event_id IN ({placeholders}) "
                    f"AND status = 'notification_unverified'",
                    (failure_reason, _now(), *event_ids),
                )
        conn.execute(
            "UPDATE lease_window_runs SET completed_at = ?, "
            "delivered_events = ?, unverified_events = ?, status = ? "
            "WHERE run_id = ?",
            (
                _now(), len(pending) if delivered else 0,
                len(pending) if pending and not delivered else 0,
                "complete" if delivered or not pending else "notification_unverified",
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "run_id": run_id,
        "new_events_discovered": discovery["due_events"],
        "due_events": len(pending),
        "caught_up_events": sum(
            1 for event in pending if event["caught_up"]
        ),
        "delivered_events": len(pending) if delivered else 0,
        "notification_unverified_events": (
            len(pending) if pending and not delivered else 0
        ),
        "status": (
            "complete" if delivered or not pending
            else "notification_unverified"
        ),
    }


def _load_tracker_email_sender():
    tracker_path = Path(__file__).with_name("rachel_lease_tracker_v2.1.py")
    spec = importlib.util.spec_from_file_location(
        "rachel_lease_tracker_runtime", tracker_path
    )
    if spec is None or spec.loader is None:
        raise LeaseWindowError("lease_tracker_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.send_email


def run_lease_window_cycle(
    *, db_path: str = DB_PATH, as_of: Optional[date] = None,
    notifier: Optional[Callable[[str, str], bool]] = None,
    delivery_enabled: Optional[bool] = None,
) -> dict:
    """Discover safely in dry-run mode; claim only when delivery is enabled."""
    enabled = delivery_enabled
    if enabled is None:
        enabled = os.getenv(
            "RACHEL_LEASE_EMAIL_DRY_RUN", "false"
        ).strip().lower() not in {"1", "true", "yes", "on"}
    if enabled:
        return process_due_lease_windows(
            db_path=db_path,
            as_of=as_of,
            notifier=notifier or _load_tracker_email_sender(),
        )

    discovery = discover_due_lease_windows(db_path=db_path, as_of=as_of)
    conn = sqlite3.connect(db_path)
    try:
        pending = int(conn.execute(
            "SELECT COUNT(*) FROM lease_window_events "
            "WHERE status = 'pending_notification'"
        ).fetchone()[0])
    finally:
        conn.close()
    return {
        "new_events_discovered": discovery["due_events"],
        "due_events": pending,
        "caught_up_events": discovery["caught_up_events"],
        "delivered_events": 0,
        "notification_unverified_events": 0,
        "delivery_suppressed": True,
        "status": "delivery_suppressed_pending",
    }


def main() -> int:
    result = run_lease_window_cycle(db_path=DB_PATH)
    print("[LeaseWindowMetrics] " + " ".join(
        f"{key}={value}" for key, value in result.items()
        if key != "run_id"
    ))
    return 0 if result["status"] in {
        "complete", "delivery_suppressed_pending"
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
