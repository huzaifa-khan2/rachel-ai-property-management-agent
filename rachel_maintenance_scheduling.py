"""Deterministic two-hour maintenance arrival-window state machine."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from rachel_event_memory import initialize_event_memory


TIMEZONE_NAME = "America/Los_Angeles"
TWO_HOUR_MINUTES = 120

AWAITING_TENANT_AVAILABILITY = "awaiting_tenant_availability"
TENANT_AVAILABILITY_RECEIVED = "tenant_availability_received"
AWAITING_VENDOR_WINDOW = "awaiting_vendor_two_hour_window"
AWAITING_TENANT_CONFIRMATION = "awaiting_tenant_two_hour_confirmation"
READY_TO_SCHEDULE = "ready_to_schedule_two_hour_window"
APPFOLIO_SCHEDULE_WRITE_UNVERIFIED = "appfolio_schedule_write_unverified"
SCHEDULED_PENDING_CONFIRMATIONS = "scheduled_pending_final_confirmations"
SCHEDULED_COMPLETE = "scheduled_complete"


class ScheduleWorkflowError(RuntimeError):
    """Sanitized deterministic scheduling-state error."""


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_date(value: str) -> str:
    cleaned = _clean(value)
    for format_string in (
        "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"
    ):
        try:
            return datetime.strptime(cleaned, format_string).date().isoformat()
        except ValueError:
            continue
    raise ScheduleWorkflowError("window_date_invalid_or_ambiguous")


def _normalize_time(value: str) -> tuple[str, int]:
    cleaned = _clean(value).upper().replace(".", "")
    for format_string in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            parsed = datetime.strptime(cleaned, format_string)
            minutes = parsed.hour * 60 + parsed.minute
            return f"{parsed.hour:02d}:{parsed.minute:02d}", minutes
        except ValueError:
            continue
    raise ScheduleWorkflowError("window_time_invalid_or_ambiguous")


def normalize_two_hour_window(
    *, date_value: str, start_value: str, end_value: str
) -> dict:
    """Return a canonical same-day two-hour window or fail closed."""
    normalized_date = _normalize_date(date_value)
    start, start_minutes = _normalize_time(start_value)
    end, end_minutes = _normalize_time(end_value)
    if end_minutes <= start_minutes:
        raise ScheduleWorkflowError("overnight_or_reversed_window_not_allowed")
    if end_minutes - start_minutes != TWO_HOUR_MINUTES:
        raise ScheduleWorkflowError("arrival_window_must_be_exactly_two_hours")
    fingerprint = _digest("|".join((
        normalized_date, start, end, TIMEZONE_NAME
    )))
    return {
        "date": normalized_date,
        "start": start,
        "end": end,
        "duration_minutes": TWO_HOUR_MINUTES,
        "timezone": TIMEZONE_NAME,
        "window_fingerprint": fingerprint,
        "display": (
            f"{normalized_date} from "
            f"{datetime.strptime(start, '%H:%M').strftime('%-I:%M %p')} to "
            f"{datetime.strptime(end, '%H:%M').strftime('%-I:%M %p')}"
        ),
    }


def require_future_two_hour_window(
    window: dict,
    *,
    reference_time: Optional[datetime] = None,
) -> dict:
    """Reject a normalized arrival window once its start has passed."""
    zone = ZoneInfo(TIMEZONE_NAME)
    now = reference_time or datetime.now(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=zone)
    else:
        now = now.astimezone(zone)
    try:
        start = datetime.strptime(
            f"{window['date']} {window['start']}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=zone)
    except (KeyError, TypeError, ValueError):
        raise ScheduleWorkflowError("future_window_context_invalid")
    if start <= now:
        raise ScheduleWorkflowError("arrival_window_start_must_be_future")
    return window


def _connect(db_path: str) -> sqlite3.Connection:
    initialize_event_memory(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_schedule(wo_num: str, *, db_path: str) -> dict:
    normalized_wo = _clean(wo_num)
    if not normalized_wo:
        raise ScheduleWorkflowError("work_order_number_missing")
    now = _now()
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO maintenance_schedule_states (
                wo_num, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(wo_num) DO NOTHING
            """,
            (normalized_wo, AWAITING_TENANT_AVAILABILITY, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM maintenance_schedule_states WHERE wo_num = ?",
            (normalized_wo,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def get_schedule(wo_num: str, *, db_path: str) -> Optional[dict]:
    initialize_event_memory(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM maintenance_schedule_states WHERE wo_num = ?",
            (_clean(wo_num),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _transition(
    wo_num: str,
    *,
    event_kind: str,
    allowed_states: set[str],
    to_state: str,
    updates: dict,
    event_key: str,
    audit_payload: dict,
    db_path: str,
) -> dict:
    current = initialize_schedule(wo_num, db_path=db_path)
    normalized_event_key = _clean(event_key)
    if not normalized_event_key:
        normalized_event_key = _digest(json.dumps(
            {
                "wo_num": _clean(wo_num),
                "event_kind": event_kind,
                "payload": audit_payload,
            },
            sort_keys=True,
        ))
    event_id = _digest(
        f"maintenance-schedule|{_clean(wo_num)}|{event_kind}|"
        f"{normalized_event_key}"
    )
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_event = conn.execute(
            "SELECT to_state, payload_json FROM maintenance_schedule_events "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        row = conn.execute(
            "SELECT * FROM maintenance_schedule_states WHERE wo_num = ?",
            (_clean(wo_num),),
        ).fetchone()
        if existing_event:
            expected_payload = json.dumps(audit_payload, sort_keys=True)
            if (
                existing_event["to_state"] != to_state
                or existing_event["payload_json"] != expected_payload
            ):
                raise ScheduleWorkflowError(
                    "schedule_event_idempotency_conflict"
                )
            conn.commit()
            result = dict(row)
            result["idempotent_replay"] = True
            return result
        if not row or row["state"] not in allowed_states:
            raise ScheduleWorkflowError(
                f"event_{event_kind}_not_allowed_from_{row['state'] if row else 'missing'}"
            )
        assignments = ["state = ?", "revision = revision + 1", "updated_at = ?"]
        values = [to_state, _now()]
        allowed_columns = {
            "resident_availability", "window_date", "window_start",
            "window_end", "window_timezone", "window_fingerprint",
            "vendor_confirmation_at", "tenant_confirmation_at",
            "appfolio_scheduled_at", "appfolio_write_receipt",
            "vendor_final_receipt", "tenant_final_receipt",
        }
        for key, value in updates.items():
            if key not in allowed_columns:
                raise ScheduleWorkflowError("schedule_update_field_invalid")
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(_clean(wo_num))
        conn.execute(
            "UPDATE maintenance_schedule_states SET "
            + ", ".join(assignments)
            + " WHERE wo_num = ?",
            tuple(values),
        )
        conn.execute(
            """
            INSERT INTO maintenance_schedule_events (
                event_id, wo_num, event_kind, from_state, to_state,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, _clean(wo_num), event_kind, row["state"], to_state,
                json.dumps(audit_payload, sort_keys=True), _now(),
            ),
        )
        result_row = conn.execute(
            "SELECT * FROM maintenance_schedule_states WHERE wo_num = ?",
            (_clean(wo_num),),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = dict(result_row)
    result["idempotent_replay"] = False
    return result


def capture_tenant_availability(
    wo_num: str, availability: str, *, event_key: str, db_path: str
) -> dict:
    text = _clean(availability)
    if not text:
        raise ScheduleWorkflowError("tenant_availability_missing")
    return _transition(
        wo_num,
        event_kind="tenant_availability_captured",
        allowed_states={AWAITING_TENANT_AVAILABILITY},
        to_state=TENANT_AVAILABILITY_RECEIVED,
        updates={"resident_availability": text},
        event_key=event_key,
        audit_payload={"availability_digest": _digest(text)},
        db_path=db_path,
    )


def record_vendor_availability_delivery(
    wo_num: str, *, delivery_receipt: str, event_key: str, db_path: str
) -> dict:
    receipt = _clean(delivery_receipt)
    if not receipt:
        raise ScheduleWorkflowError("vendor_delivery_receipt_missing")
    return _transition(
        wo_num,
        event_kind="tenant_availability_delivered_to_vendor",
        allowed_states={TENANT_AVAILABILITY_RECEIVED},
        to_state=AWAITING_VENDOR_WINDOW,
        updates={},
        event_key=event_key,
        audit_payload={"delivery_receipt_digest": _digest(receipt)},
        db_path=db_path,
    )


def record_vendor_two_hour_window(
    wo_num: str,
    *,
    date_value: str,
    start_value: str,
    end_value: str,
    event_key: str,
    db_path: str,
    reference_time: Optional[datetime] = None,
) -> dict:
    window = require_future_two_hour_window(normalize_two_hour_window(
        date_value=date_value,
        start_value=start_value,
        end_value=end_value,
    ), reference_time=reference_time)
    return _transition(
        wo_num,
        event_kind="vendor_two_hour_window_agreed",
        allowed_states={AWAITING_VENDOR_WINDOW},
        to_state=AWAITING_TENANT_CONFIRMATION,
        updates={
            "window_date": window["date"],
            "window_start": window["start"],
            "window_end": window["end"],
            "window_timezone": window["timezone"],
            "window_fingerprint": window["window_fingerprint"],
            "vendor_confirmation_at": _now(),
        },
        event_key=event_key,
        audit_payload={
            "window_fingerprint": window["window_fingerprint"],
            "duration_minutes": TWO_HOUR_MINUTES,
        },
        db_path=db_path,
    )


def record_tenant_window_confirmation(
    wo_num: str,
    *,
    confirmed_window_fingerprint: str,
    explicitly_confirmed: bool,
    event_key: str,
    db_path: str,
    reference_time: Optional[datetime] = None,
) -> dict:
    current = get_schedule(wo_num, db_path=db_path)
    if not explicitly_confirmed:
        raise ScheduleWorkflowError("tenant_confirmation_not_explicit")
    if not current or _clean(confirmed_window_fingerprint) != current.get(
        "window_fingerprint", ""
    ):
        raise ScheduleWorkflowError("tenant_confirmed_different_or_unknown_window")
    require_future_two_hour_window(
        normalize_two_hour_window(
            date_value=current["window_date"],
            start_value=current["window_start"],
            end_value=current["window_end"],
        ),
        reference_time=reference_time,
    )
    return _transition(
        wo_num,
        event_kind="tenant_confirmed_same_two_hour_window",
        allowed_states={AWAITING_TENANT_CONFIRMATION},
        to_state=READY_TO_SCHEDULE,
        updates={"tenant_confirmation_at": _now()},
        event_key=event_key,
        audit_payload={
            "window_fingerprint": current["window_fingerprint"],
            "explicit_confirmation": True,
        },
        db_path=db_path,
    )


def record_tenant_counter_availability(
    wo_num: str, availability: str, *, event_key: str, db_path: str
) -> dict:
    """Reject the pending window and restart vendor coordination safely."""
    text = _clean(availability)
    if not text:
        raise ScheduleWorkflowError("tenant_counter_availability_missing")
    return _transition(
        wo_num,
        event_kind="tenant_rejected_window_with_counter_availability",
        allowed_states={AWAITING_TENANT_CONFIRMATION},
        to_state=TENANT_AVAILABILITY_RECEIVED,
        updates={
            "resident_availability": text,
            "window_date": "",
            "window_start": "",
            "window_end": "",
            "window_timezone": "",
            "window_fingerprint": "",
            "vendor_confirmation_at": "",
            "tenant_confirmation_at": "",
        },
        event_key=event_key,
        audit_payload={"availability_digest": _digest(text)},
        db_path=db_path,
    )


def record_appfolio_schedule_verified(
    wo_num: str,
    *,
    window_fingerprint: str,
    appfolio_write_receipt: str,
    event_key: str,
    db_path: str,
) -> dict:
    current = get_schedule(wo_num, db_path=db_path)
    if not current or _clean(window_fingerprint) != current.get(
        "window_fingerprint", ""
    ):
        raise ScheduleWorkflowError("appfolio_schedule_window_mismatch")
    receipt = _clean(appfolio_write_receipt)
    expected_receipt = (
        "schedule_window_semantically_verified_"
        + current["window_fingerprint"]
    )
    if receipt != expected_receipt:
        raise ScheduleWorkflowError("appfolio_schedule_verification_missing")
    # Revalidate the stored representation before a consequential transition.
    normalize_two_hour_window(
        date_value=current["window_date"],
        start_value=current["window_start"],
        end_value=current["window_end"],
    )
    return _transition(
        wo_num,
        event_kind="appfolio_two_hour_window_scheduled_verified",
        allowed_states={APPFOLIO_SCHEDULE_WRITE_UNVERIFIED},
        to_state=SCHEDULED_PENDING_CONFIRMATIONS,
        updates={
            "appfolio_scheduled_at": _now(),
            "appfolio_write_receipt": receipt,
        },
        event_key=event_key,
        audit_payload={
            "window_fingerprint": current["window_fingerprint"],
            "write_receipt_digest": _digest(receipt),
        },
        db_path=db_path,
    )


def claim_appfolio_schedule_attempt(
    wo_num: str,
    *,
    window_fingerprint: str,
    attempt_id: str,
    event_key: str,
    db_path: str,
) -> dict:
    """Durably claim one write before any AppFolio schedule mutation."""
    current = get_schedule(wo_num, db_path=db_path)
    if not current or _clean(window_fingerprint) != current.get(
        "window_fingerprint", ""
    ):
        raise ScheduleWorkflowError("appfolio_schedule_window_mismatch")
    normalized_attempt = _clean(attempt_id)
    if not normalized_attempt:
        raise ScheduleWorkflowError("appfolio_schedule_attempt_id_missing")
    return _transition(
        wo_num,
        event_kind="appfolio_schedule_write_claimed",
        allowed_states={READY_TO_SCHEDULE},
        to_state=APPFOLIO_SCHEDULE_WRITE_UNVERIFIED,
        updates={},
        event_key=event_key,
        audit_payload={
            "window_fingerprint": current["window_fingerprint"],
            "attempt_id_digest": _digest(normalized_attempt),
        },
        db_path=db_path,
    )


def verify_appfolio_schedule_attempt_claim(
    wo_num: str,
    *,
    window_fingerprint: str,
    attempt_id: str,
    db_path: str,
) -> dict:
    """Prove the exact schedule write was claimed before browser mutation."""
    current = get_schedule(wo_num, db_path=db_path)
    if not current or current.get("state") != APPFOLIO_SCHEDULE_WRITE_UNVERIFIED:
        raise ScheduleWorkflowError("appfolio_schedule_write_not_claimed")
    if _clean(window_fingerprint) != current.get("window_fingerprint", ""):
        raise ScheduleWorkflowError("appfolio_schedule_window_mismatch")
    normalized_attempt = _clean(attempt_id)
    if not normalized_attempt:
        raise ScheduleWorkflowError("appfolio_schedule_attempt_id_missing")
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM maintenance_schedule_events "
            "WHERE wo_num = ? AND event_kind = ? "
            "ORDER BY created_at DESC, event_id DESC LIMIT 1",
            (_clean(wo_num), "appfolio_schedule_write_claimed"),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ScheduleWorkflowError("appfolio_schedule_claim_event_missing")
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    if payload.get("attempt_id_digest") != _digest(normalized_attempt):
        raise ScheduleWorkflowError("appfolio_schedule_attempt_mismatch")
    if payload.get("window_fingerprint") != current["window_fingerprint"]:
        raise ScheduleWorkflowError("appfolio_schedule_claim_window_mismatch")
    normalize_two_hour_window(
        date_value=current["window_date"],
        start_value=current["window_start"],
        end_value=current["window_end"],
    )
    return current


def record_final_confirmation_delivery(
    wo_num: str,
    *,
    audience: str,
    delivery_receipt: str,
    event_key: str,
    db_path: str,
) -> dict:
    normalized_audience = _clean(audience).lower()
    if normalized_audience not in {"vendor", "tenant"}:
        raise ScheduleWorkflowError("confirmation_audience_invalid")
    receipt = _clean(delivery_receipt)
    if not receipt:
        raise ScheduleWorkflowError("confirmation_delivery_receipt_missing")
    current = get_schedule(wo_num, db_path=db_path)
    if not current or current["state"] not in {
        SCHEDULED_PENDING_CONFIRMATIONS, SCHEDULED_COMPLETE
    }:
        raise ScheduleWorkflowError("final_confirmation_before_schedule")
    field = f"{normalized_audience}_final_receipt"
    other = (
        current["tenant_final_receipt"]
        if normalized_audience == "vendor"
        else current["vendor_final_receipt"]
    )
    to_state = SCHEDULED_COMPLETE if other else SCHEDULED_PENDING_CONFIRMATIONS
    return _transition(
        wo_num,
        event_kind=f"{normalized_audience}_final_confirmation_delivered",
        allowed_states={
            SCHEDULED_PENDING_CONFIRMATIONS, SCHEDULED_COMPLETE
        },
        to_state=to_state,
        updates={field: receipt},
        event_key=event_key,
        audit_payload={
            "audience": normalized_audience,
            "delivery_receipt_digest": _digest(receipt),
        },
        db_path=db_path,
    )


def build_vendor_window_request(wo_num: str, availability: str) -> str:
    return (
        f"The resident provided availability for Work Order #{_clean(wo_num)}: "
        f"{_clean(availability)}. Please reply with one specific calendar "
        "date and one two-hour arrival window (for example, 9:00 AM–11:00 "
        "AM). Do not provide an exact appointment time or a one-hour window."
    )


def build_tenant_window_confirmation_request(
    wo_num: str, window: dict
) -> str:
    normalized = normalize_two_hour_window(
        date_value=window["window_date"],
        start_value=window["window_start"],
        end_value=window["window_end"],
    )
    return (
        f"The vendor proposed a two-hour arrival window for Work Order "
        f"#{_clean(wo_num)}: {normalized['display']}. Please explicitly "
        "confirm this same two-hour window or provide different availability."
    )
