"""Durable no-blind-retry claims for consequential AppFolio outbound writes."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone

from rachel_event_memory import initialize_event_memory


class AppFolioWriteIntentError(RuntimeError):
    """Sanitized outbound-intent transition error."""


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_outbound_intent(
    *, operation: str, target_id: str, recipient: str, message: str,
    db_path: str,
) -> dict:
    normalized = {
        "operation": _clean(operation),
        "target_id": _clean(target_id),
        "recipient_digest": _digest(_clean(recipient).lower()),
        "message_digest": _digest(_clean(message)),
    }
    if not normalized["operation"] or not normalized["target_id"]:
        raise AppFolioWriteIntentError("outbound_target_incomplete")
    if not _clean(recipient) or not _clean(message):
        raise AppFolioWriteIntentError("outbound_recipient_or_message_missing")
    idempotency_key = _digest("|".join(normalized.values()))
    intent_id = _digest(f"appfolio-outbound-intent|{idempotency_key}")
    initialize_event_memory(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT * FROM appfolio_outbound_intents "
            "WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if not existing:
            now = _now()
            conn.execute(
                """
                INSERT INTO appfolio_outbound_intents (
                    intent_id, idempotency_key, operation, target_id,
                    recipient_digest, message_digest, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    intent_id, idempotency_key, normalized["operation"],
                    normalized["target_id"], normalized["recipient_digest"],
                    normalized["message_digest"], now, now,
                ),
            )
            conn.commit()
            status = "prepared"
            created = True
            verification_receipt = ""
            failure_reason = ""
        else:
            intent_id = existing["intent_id"]
            status = existing["status"]
            created = False
            verification_receipt = existing["verification_receipt"] or ""
            failure_reason = existing["failure_reason"] or ""
    finally:
        conn.close()
    return {
        "intent_id": intent_id,
        "status": status,
        "created": created,
        "verification_receipt": verification_receipt,
        "failure_reason": failure_reason,
        **normalized,
    }


def claim_outbound_intent(intent_id: str, *, db_path: str) -> dict:
    """Claim immediately before the first form mutation."""
    initialize_event_memory(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM appfolio_outbound_intents WHERE intent_id = ?",
            (_clean(intent_id),),
        ).fetchone()
        if not row:
            raise AppFolioWriteIntentError("outbound_intent_missing")
        if row["status"] != "prepared":
            raise AppFolioWriteIntentError(
                f"outbound_intent_not_prepared_{row['status']}"
            )
        conn.execute(
            "UPDATE appfolio_outbound_intents SET "
            "status = 'sending_unverified', attempt_count = attempt_count + 1, "
            "updated_at = ? WHERE intent_id = ? AND status = 'prepared'",
            (_now(), row["intent_id"]),
        )
        result = dict(conn.execute(
            "SELECT * FROM appfolio_outbound_intents WHERE intent_id = ?",
            (row["intent_id"],),
        ).fetchone())
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_outbound_intent(
    intent_id: str, *, verification_receipt: str, db_path: str
) -> dict:
    receipt = _clean(verification_receipt)
    if not receipt:
        raise AppFolioWriteIntentError("outbound_verification_receipt_missing")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        changed = conn.execute(
            "UPDATE appfolio_outbound_intents SET status = 'sent_verified', "
            "verification_receipt = ?, failure_reason = '', updated_at = ? "
            "WHERE intent_id = ? AND status = 'sending_unverified'",
            (receipt, _now(), _clean(intent_id)),
        ).rowcount
        if changed != 1:
            raise AppFolioWriteIntentError("outbound_completion_state_invalid")
        row = conn.execute(
            "SELECT * FROM appfolio_outbound_intents WHERE intent_id = ?",
            (_clean(intent_id),),
        ).fetchone()
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_outbound_intent_failure(
    intent_id: str, *, reason: str, db_path: str
) -> None:
    """Record sanitized failure while deliberately retaining ambiguous state."""
    sanitized = _clean(reason)
    if not re.fullmatch(r"[a-z0-9_]+", sanitized):
        sanitized = "outbound_transport_failure"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE appfolio_outbound_intents SET failure_reason = ?, "
            "updated_at = ? WHERE intent_id = ? "
            "AND status = 'sending_unverified'",
            (sanitized, _now(), _clean(intent_id)),
        )
        conn.commit()
    finally:
        conn.close()
