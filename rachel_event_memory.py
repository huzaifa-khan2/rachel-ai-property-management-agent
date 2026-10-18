"""Durable company-wide event memory for communications and verified writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional


DB_PATH = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")

# Every wo_triage.status value under which a tenant/vendor may still
# reasonably reply about a work order. Deliberately excludes only the
# genuinely terminal/synthetic statuses (complete, resolved_troubleshooting,
# escalated_to_manager, failed, qa_management_decision_complete). Verified
# against every literal status string actually written across
# rachel_wo_triage.py / rachel_vendor_lifecycle.py / rachel_inbox_monitor.py —
# 'scheduled' was never one of them (the real value is 'appointment_scheduled'),
# and 'waiting_for_manager_vendor_approval' / 'in_house_assigned' were missing
# entirely despite being two of the most common real-world statuses.
ACTIVE_WO_TRIAGE_STATUSES = (
    "awaiting_reply",
    "waiting_for_manager_vendor_approval",
    "waiting_for_tenant_availability",
    "waiting_for_tenant_confirmation",
    "waiting_for_tenant_cancellation",
    "satisfaction_check_sent",
    "vendor_assigned",
    "in_house_assigned",
    "waiting_for_vendor_availability",
    "waiting_for_vendor_confirmation",
    "vendor_schedule_needs_review",
    "vendor_stalled",
    "getting_other_estimates",
    "estimate_pending_approval",
    "work_approved",
    "appointment_scheduled",
    "appointment_scheduled_confirmation_issue",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def initialize_event_memory(db_path: Optional[str] = None) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS internal_users (
                user_id          TEXT PRIMARY KEY,
                display_name     TEXT NOT NULL,
                email            TEXT NOT NULL UNIQUE,
                role             TEXT NOT NULL,
                portfolio_scope  TEXT NOT NULL DEFAULT '[]',
                permission_scope TEXT NOT NULL DEFAULT '[]',
                is_active        INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS property_portfolio_assignments (
                property_key     TEXT PRIMARY KEY,
                property_group   TEXT NOT NULL DEFAULT '',
                manager_user_id  TEXT NOT NULL DEFAULT '',
                source           TEXT NOT NULL DEFAULT 'configuration',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS appfolio_communication_state (
                event_key         TEXT PRIMARY KEY,
                thread_id         TEXT NOT NULL,
                source_message_id TEXT,
                href              TEXT,
                preview           TEXT,
                observed_at       TEXT,
                first_seen_at     TEXT NOT NULL,
                last_seen_at      TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                communication_id  TEXT,
                error_type        TEXT
            );

            CREATE TABLE IF NOT EXISTS appfolio_thread_selector_state (
                selector_key      TEXT PRIMARY KEY,
                thread_id         TEXT NOT NULL,
                latest_event_key  TEXT NOT NULL DEFAULT '',
                last_unread       INTEGER NOT NULL DEFAULT 0,
                processing_status TEXT NOT NULL,
                last_error        TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL,
                conversation_identity_key TEXT NOT NULL DEFAULT '',
                latest_state_key  TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS communications (
                communication_id  TEXT PRIMARY KEY,
                source            TEXT NOT NULL,
                source_thread_id  TEXT,
                source_message_id TEXT,
                contact_name      TEXT,
                contact_role      TEXT,
                property_address  TEXT,
                unit              TEXT,
                occurred_at       TEXT,
                direction         TEXT NOT NULL,
                message_text      TEXT,
                preview           TEXT,
                summary           TEXT,
                topic             TEXT,
                related_wo        TEXT,
                related_tenant    TEXT,
                related_vendor    TEXT,
                internal_user_id  TEXT,
                first_seen_at     TEXT NOT NULL,
                last_updated_at   TEXT NOT NULL,
                processing_status TEXT NOT NULL,
                metadata_json     TEXT NOT NULL DEFAULT '{}',
                conversation_identity_key TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS conversation_identity_links (
                legacy_thread_id          TEXT PRIMARY KEY,
                conversation_identity_key TEXT NOT NULL,
                first_linked_at           TEXT NOT NULL,
                last_seen_at              TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS appfolio_action_audit (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                operation           TEXT NOT NULL,
                target_type         TEXT NOT NULL,
                target_id           TEXT NOT NULL,
                internal_user_id    TEXT,
                source              TEXT NOT NULL,
                previous_state_json TEXT NOT NULL DEFAULT '{}',
                new_state_json      TEXT NOT NULL DEFAULT '{}',
                performed_at        TEXT NOT NULL,
                verified_at         TEXT NOT NULL,
                verification_result TEXT NOT NULL,
                details_json        TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS communication_drafts (
                draft_id                 TEXT PRIMARY KEY,
                idempotency_key          TEXT NOT NULL UNIQUE,
                source_thread_id         TEXT NOT NULL,
                latest_communication_id  TEXT NOT NULL,
                context_digest           TEXT NOT NULL,
                ownership_classification TEXT NOT NULL,
                authorization_reason     TEXT NOT NULL,
                draft_text               TEXT NOT NULL,
                status                   TEXT NOT NULL DEFAULT 'draft_only',
                created_at               TEXT NOT NULL,
                updated_at               TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS communication_send_intents (
                intent_id                    TEXT PRIMARY KEY,
                idempotency_key              TEXT NOT NULL UNIQUE,
                draft_id                    TEXT NOT NULL,
                source_thread_id            TEXT NOT NULL,
                target_selector_key         TEXT NOT NULL,
                recipient_identity_digest   TEXT NOT NULL,
                reply_to_message_fingerprint TEXT NOT NULL,
                context_digest              TEXT NOT NULL,
                message_digest              TEXT NOT NULL,
                authorization_reason        TEXT NOT NULL,
                authorization_evidence_id   TEXT NOT NULL,
                status                       TEXT NOT NULL,
                attempt_count                INTEGER NOT NULL DEFAULT 0,
                sent_message_fingerprint     TEXT NOT NULL DEFAULT '',
                failure_reason               TEXT NOT NULL DEFAULT '',
                created_at                   TEXT NOT NULL,
                updated_at                   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS appfolio_outbound_intents (
                intent_id          TEXT PRIMARY KEY,
                idempotency_key    TEXT NOT NULL UNIQUE,
                operation          TEXT NOT NULL,
                target_id          TEXT NOT NULL,
                recipient_digest   TEXT NOT NULL,
                message_digest     TEXT NOT NULL,
                status             TEXT NOT NULL,
                attempt_count      INTEGER NOT NULL DEFAULT 0,
                verification_receipt TEXT NOT NULL DEFAULT '',
                failure_reason     TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS maintenance_schedule_states (
                wo_num                    TEXT PRIMARY KEY,
                state                     TEXT NOT NULL,
                resident_availability     TEXT NOT NULL DEFAULT '',
                window_date               TEXT NOT NULL DEFAULT '',
                window_start              TEXT NOT NULL DEFAULT '',
                window_end                TEXT NOT NULL DEFAULT '',
                window_timezone           TEXT NOT NULL DEFAULT 'America/Los_Angeles',
                window_fingerprint        TEXT NOT NULL DEFAULT '',
                vendor_confirmation_at    TEXT NOT NULL DEFAULT '',
                tenant_confirmation_at    TEXT NOT NULL DEFAULT '',
                appfolio_scheduled_at     TEXT NOT NULL DEFAULT '',
                appfolio_write_receipt    TEXT NOT NULL DEFAULT '',
                vendor_final_receipt      TEXT NOT NULL DEFAULT '',
                tenant_final_receipt      TEXT NOT NULL DEFAULT '',
                revision                  INTEGER NOT NULL DEFAULT 0,
                created_at                TEXT NOT NULL,
                updated_at                TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS maintenance_schedule_events (
                event_id       TEXT PRIMARY KEY,
                wo_num         TEXT NOT NULL,
                event_kind     TEXT NOT NULL,
                from_state     TEXT NOT NULL,
                to_state       TEXT NOT NULL,
                payload_json   TEXT NOT NULL DEFAULT '{}',
                created_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lease_window_events (
                event_id          TEXT PRIMARY KEY,
                occupancy_id      TEXT NOT NULL,
                event_kind        TEXT NOT NULL,
                target_date       TEXT NOT NULL,
                window_days       INTEGER NOT NULL,
                due_date          TEXT NOT NULL,
                status            TEXT NOT NULL,
                caught_up         INTEGER NOT NULL DEFAULT 0,
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                delivery_receipt  TEXT NOT NULL DEFAULT '',
                failure_reason    TEXT NOT NULL DEFAULT '',
                detected_at       TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                UNIQUE(occupancy_id, event_kind, target_date, window_days)
            );

            CREATE TABLE IF NOT EXISTS lease_window_runs (
                run_id          TEXT PRIMARY KEY,
                as_of_date      TEXT NOT NULL,
                started_at      TEXT NOT NULL,
                completed_at    TEXT NOT NULL DEFAULT '',
                due_events      INTEGER NOT NULL DEFAULT 0,
                caught_up_events INTEGER NOT NULL DEFAULT 0,
                delivered_events INTEGER NOT NULL DEFAULT 0,
                unverified_events INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rachel_runtime_state (
                state_key   TEXT PRIMARY KEY,
                state_value TEXT,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS appfolio_feature_health (
                feature              TEXT PRIMARY KEY,
                status               TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_reason          TEXT NOT NULL DEFAULT '',
                last_failure_at      TEXT NOT NULL DEFAULT '',
                retry_after          TEXT NOT NULL DEFAULT '',
                last_success_at      TEXT NOT NULL DEFAULT '',
                updated_at           TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_communications_thread
            ON communications(source, source_thread_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_communications_related_wo
            ON communications(related_wo, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_communications_contact
            ON communications(contact_name, property_address, unit);
            CREATE INDEX IF NOT EXISTS idx_communications_internal_user
            ON communications(internal_user_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_appfolio_comm_state_thread
            ON appfolio_communication_state(thread_id, last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_appfolio_selector_thread
            ON appfolio_thread_selector_state(thread_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_appfolio_action_target
            ON appfolio_action_audit(target_type, target_id, verified_at);
            CREATE INDEX IF NOT EXISTS idx_portfolio_manager
            ON property_portfolio_assignments(manager_user_id, property_group);
            CREATE INDEX IF NOT EXISTS idx_communication_drafts_thread
            ON communication_drafts(source_thread_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_communication_send_intents_thread
            ON communication_send_intents(source_thread_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_appfolio_outbound_intent_target
            ON appfolio_outbound_intents(operation, target_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_maintenance_schedule_state
            ON maintenance_schedule_states(state, updated_at);
            CREATE INDEX IF NOT EXISTS idx_maintenance_schedule_events_wo
            ON maintenance_schedule_events(wo_num, created_at);
            CREATE INDEX IF NOT EXISTS idx_lease_window_event_status
            ON lease_window_events(status, due_date);
            CREATE INDEX IF NOT EXISTS idx_lease_window_event_occupancy
            ON lease_window_events(occupancy_id, target_date);
            CREATE INDEX IF NOT EXISTS idx_appfolio_feature_status
            ON appfolio_feature_health(status, retry_after);
            CREATE INDEX IF NOT EXISTS idx_conversation_identity_links_identity
            ON conversation_identity_links(conversation_identity_key);
            """
        )
        communications_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(communications)"
            ).fetchall()
        }
        if "conversation_identity_key" not in communications_columns:
            conn.execute(
                "ALTER TABLE communications ADD COLUMN "
                "conversation_identity_key TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_communications_conversation_identity "
            "ON communications(conversation_identity_key, occurred_at)"
        )
        selector_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(appfolio_thread_selector_state)"
            ).fetchall()
        }
        if "latest_event_key" not in selector_columns:
            conn.execute(
                "ALTER TABLE appfolio_thread_selector_state ADD COLUMN "
                "latest_event_key TEXT NOT NULL DEFAULT ''"
            )
        if "last_unread" not in selector_columns:
            conn.execute(
                "ALTER TABLE appfolio_thread_selector_state ADD COLUMN "
                "last_unread INTEGER NOT NULL DEFAULT 0"
            )
        if "conversation_identity_key" not in selector_columns:
            conn.execute(
                "ALTER TABLE appfolio_thread_selector_state ADD COLUMN "
                "conversation_identity_key TEXT NOT NULL DEFAULT ''"
            )
        if "latest_state_key" not in selector_columns:
            conn.execute(
                "ALTER TABLE appfolio_thread_selector_state ADD COLUMN "
                "latest_state_key TEXT NOT NULL DEFAULT ''"
            )
        # A stable durable conversation may be re-verified many times as new
        # messages arrive (selector_key is intentionally volatile - see
        # qa_authorization_fingerprint - so it cannot serve as this table's
        # routing key). One row per stable identity; empty identities from
        # rows written before this migration are excluded so they never
        # collide with each other under the partial unique index.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_appfolio_thread_selector_state_identity "
            "ON appfolio_thread_selector_state(conversation_identity_key) "
            "WHERE conversation_identity_key != ''"
        )
        now = _now()
        conn.execute(
            """
            INSERT INTO internal_users (
                user_id, display_name, email, role, portfolio_scope,
                permission_scope, is_active, created_at, updated_at
            ) VALUES (
                'rachel-asst', 'Rachel Asst', 'rachel@clientco.example',
                'assistant_property_manager', '["*"]',
                '["communication_read", "communication_draft"]',
                1, ?, ?
            )
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                role = excluded.role,
                portfolio_scope = excluded.portfolio_scope,
                permission_scope = excluded.permission_scope,
                updated_at = excluded.updated_at
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO internal_users (
                user_id, display_name, email, role, portfolio_scope,
                permission_scope, is_active, created_at, updated_at
            ) VALUES (
                'regional-manager', 'Regional Manager', ?, 'property_manager',
                '["*"]', '["property_management"]', 1, ?, ?
            )
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                email = excluded.email,
                updated_at = excluded.updated_at
            """,
            (os.getenv("MANAGER_EMAIL", "manager@clientco.example"), now, now),
        )
        conn.execute(
            """
            INSERT INTO internal_users (
                user_id, display_name, email, role, portfolio_scope,
                permission_scope, is_active, created_at, updated_at
            ) VALUES (
                'owner', 'Client Owner', ?, 'administrator',
                '["*"]', '["property_management", "system_administration"]',
                1, ?, ?
            )
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                email = excluded.email,
                updated_at = excluded.updated_at
            """,
            (os.getenv("OWNER_EMAIL", "owner@clientco.example"), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_internal_user_context(
    user_id: str, db_path: Optional[str] = None
) -> Optional[dict]:
    initialize_event_memory(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM internal_users WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["portfolio_scope"] = json.loads(result["portfolio_scope"] or "[]")
        result["permission_scope"] = json.loads(result["permission_scope"] or "[]")
        return result
    finally:
        conn.close()


def resolve_internal_user(
    email_address: str, db_path: Optional[str] = None
) -> Optional[dict]:
    initialize_event_memory(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM internal_users WHERE LOWER(email) = LOWER(?) "
            "AND is_active = 1",
            (_clean(email_address),),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["portfolio_scope"] = json.loads(result["portfolio_scope"] or "[]")
        result["permission_scope"] = json.loads(result["permission_scope"] or "[]")
        return result
    finally:
        conn.close()


def resolve_internal_user_identity(
    identity: str, db_path: Optional[str] = None
) -> Optional[dict]:
    """Resolve exactly one active internal user by email or display name."""
    normalized = _clean(identity)
    if not normalized:
        return None
    initialize_event_memory(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM internal_users WHERE is_active = 1 AND "
            "(LOWER(email) = LOWER(?) OR LOWER(display_name) = LOWER(?))",
            (normalized, normalized),
        ).fetchall()
        if len(rows) != 1:
            return None
        result = dict(rows[0])
        result["portfolio_scope"] = json.loads(
            result["portfolio_scope"] or "[]"
        )
        result["permission_scope"] = json.loads(
            result["permission_scope"] or "[]"
        )
        return result
    finally:
        conn.close()


def internal_user_has_access(
    user_id: str,
    *,
    permission: str,
    property_key: str = "",
    property_group: str = "",
    db_path: Optional[str] = None,
) -> bool:
    """Enforce role configuration and portfolio scope for future PM actions."""
    user = get_internal_user_context(user_id, db_path)
    if not user or permission not in user["permission_scope"]:
        return False
    portfolio = {_clean(value).lower() for value in user["portfolio_scope"]}
    if "*" in portfolio:
        return True
    requested_values = {
        value for value in (
            _clean(property_key).lower(),
            _clean(property_group).lower(),
        ) if value
    }
    return bool(
        requested_values
        and any(
            scope in requested or requested in scope
            for scope in portfolio for requested in requested_values
        )
    )


def inbox_event_key(row: dict) -> str:
    """Identify the latest visible event using stable thread/list metadata."""
    identity = "|".join(
        _clean(value)
        for value in (
            row.get("thread_id") or row.get("href"),
            row.get("source_message_id"),
            row.get("timestamp"),
            row.get("preview") or row.get("text"),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def inbox_selector_key(row: dict) -> str:
    """
    One-time, preview-sensitive fingerprint for controlled QA/canary
    authorization only - NOT Rachel's durable conversation identity.

    Timestamp formatting and unread state are intentionally excluded, but the
    latest-message preview remains included so a changed event cannot inherit
    authorization granted for earlier content in the same conversation. That
    same property makes this unsuitable as a durable primary key: a new
    inbound message changes this value, which would make an already-known
    conversation look brand new. Durable routing/state tracking must use
    conversation_identity_key() instead; qa_authorization_fingerprint() is
    the explicit alias for this function's actual use case.
    """
    identity = "|".join(
        _clean(value).lower()
        for value in (
            row.get("contact_name"),
            row.get("contact_role"),
            row.get("list_detail"),
            row.get("property"),
            row.get("unit"),
            row.get("preview") or row.get("text"),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def qa_authorization_fingerprint(row: dict) -> str:
    """Explicit name for the volatile, preview-sensitive QA/canary
    fingerprint. Never use this as Rachel's durable conversation identity -
    see conversation_identity_key() and conversation_state_key()."""
    return inbox_selector_key(row)


def conversation_identity_key(row: dict) -> str:
    """
    Durable conversation identity: stable across new incoming messages.

    Prefers AppFolio's own native thread id (recognized by the absence of
    the synthetic "list-" prefix _normalize_lightweight_rows attaches when
    no native id could be read from the row). Falls back to a hash of only
    contact name, contact role, and property/unit context - never message
    preview, timestamp, unread state, or other volatile fields. Two rows
    that share a phone number but are genuinely different contacts must not
    collide here: nothing about phone number feeds this key at all, only
    the contact identity AppFolio itself renders.
    """
    existing_thread_id = _clean(row.get("thread_id")).strip()
    if existing_thread_id and not existing_thread_id.startswith("list-"):
        return existing_thread_id
    basis = "|".join(
        _clean(value).lower()
        for value in (
            row.get("contact_name"),
            row.get("contact_role"),
            row.get("property"),
            row.get("unit"),
        )
    )
    return "identity-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def conversation_state_key(row: dict) -> str:
    """
    Volatile conversation state fingerprint: changes when new activity
    arrives in an otherwise-unchanged conversation (same
    conversation_identity_key). Deliberately excludes contact/role/property
    fields, which belong to identity, not state.
    """
    basis = "|".join(
        _clean(value).lower()
        for value in (
            row.get("preview") or row.get("text"),
            row.get("timestamp"),
            row.get("source_message_id"),
        )
    )
    return "state-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def unseen_communication_rows(
    rows: Iterable[dict], db_path: Optional[str] = None
) -> list[dict]:
    initialize_event_memory(db_path)
    result = []
    conn = _connect(db_path)
    try:
        for raw in rows:
            row = dict(raw)
            row["event_key"] = inbox_event_key(row)
            existing = conn.execute(
                "SELECT processing_status FROM appfolio_communication_state "
                "WHERE event_key = ?",
                (row["event_key"],),
            ).fetchone()
            if not existing or existing["processing_status"] == "failed":
                result.append(row)
    finally:
        conn.close()
    return result


def record_communication_state(
    row: dict,
    status: str,
    *,
    communication_id: str = "",
    error_type: str = "",
    db_path: Optional[str] = None,
) -> str:
    initialize_event_memory(db_path)
    now = _now()
    event_key = row.get("event_key") or inbox_event_key(row)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO appfolio_communication_state (
                event_key, thread_id, source_message_id, href, preview,
                observed_at, first_seen_at, last_seen_at, processing_status,
                communication_id, error_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                processing_status = excluded.processing_status,
                communication_id = excluded.communication_id,
                error_type = excluded.error_type
            """,
            (
                event_key,
                _clean(row.get("thread_id") or row.get("href")),
                _clean(row.get("source_message_id")),
                _clean(row.get("href")),
                _clean(row.get("preview") or row.get("text")),
                _clean(row.get("timestamp")),
                now, now, status, communication_id, error_type,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return event_key


def _communication_id(data: dict) -> str:
    source = _clean(data.get("source") or "appfolio_communication_inbox")
    source_message_id = _clean(data.get("source_message_id"))
    if source_message_id:
        identity = f"{source}|{source_message_id}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()
    identity = "|".join(
        _clean(value)
        for value in (
            source,
            data.get("source_thread_id"),
            data.get("occurred_at"),
            data.get("message_text") or data.get("preview"),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def store_communication(data: dict, db_path: Optional[str] = None) -> str:
    """Store a company communication without requiring a WO relationship."""
    initialize_event_memory(db_path)
    now = _now()
    communication_id = data.get("communication_id") or _communication_id(data)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO communications (
                communication_id, source, source_thread_id, source_message_id,
                contact_name, contact_role, property_address, unit,
                occurred_at, direction, message_text, preview, summary, topic,
                related_wo, related_tenant, related_vendor, internal_user_id,
                first_seen_at, last_updated_at, processing_status,
                metadata_json, conversation_identity_key
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(communication_id) DO UPDATE SET
                message_text = COALESCE(NULLIF(excluded.message_text, ''), communications.message_text),
                related_wo = COALESCE(NULLIF(excluded.related_wo, ''), communications.related_wo),
                last_updated_at = excluded.last_updated_at,
                processing_status = excluded.processing_status,
                metadata_json = excluded.metadata_json,
                conversation_identity_key = COALESCE(
                    NULLIF(excluded.conversation_identity_key, ''),
                    communications.conversation_identity_key
                )
            """,
            (
                communication_id,
                _clean(data.get("source") or "appfolio_communication_inbox"),
                _clean(data.get("source_thread_id")),
                _clean(data.get("source_message_id")),
                _clean(data.get("contact_name")),
                _clean(data.get("contact_role")),
                _clean(data.get("property_address")),
                _clean(data.get("unit")),
                _clean(data.get("occurred_at")) or now,
                _clean(data.get("direction") or "inbound"),
                _clean(data.get("message_text")),
                _clean(data.get("preview")),
                _clean(data.get("summary")),
                _clean(data.get("topic")),
                _clean(data.get("related_wo")),
                _clean(data.get("related_tenant")),
                _clean(data.get("related_vendor")),
                _clean(data.get("internal_user_id")),
                now, now,
                _clean(data.get("processing_status") or "stored"),
                json.dumps(data.get("metadata") or {}, sort_keys=True, default=str),
                _clean(data.get("conversation_identity_key")),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return communication_id


def resolve_and_link_conversation_identity(
    *,
    conversation_identity_key: str,
    legacy_thread_id: str,
    db_path: Optional[str] = None,
) -> str:
    """
    Resolve the canonical durable conversation identity for one row read.

    Idempotent: rereading the same conversation always yields the same
    canonical identity, and calling this repeatedly never duplicates or
    overwrites an existing link - it only refreshes last_seen_at. Preserves
    continuity across a one-time native-id transition (a conversation that
    previously had no AppFolio-native thread id getting one later) by
    preferring a previously-recorded link for this legacy_thread_id over a
    freshly computed identity key, so historical messages are never
    orphaned into a second logical conversation.
    """
    initialize_event_memory(db_path)
    legacy_thread_id = _clean(legacy_thread_id)
    conversation_identity_key = _clean(conversation_identity_key)
    now = _now()
    conn = _connect(db_path)
    try:
        existing = None
        if legacy_thread_id:
            existing = conn.execute(
                "SELECT conversation_identity_key FROM "
                "conversation_identity_links WHERE legacy_thread_id = ?",
                (legacy_thread_id,),
            ).fetchone()
        canonical = (
            str(existing[0]) if existing and existing[0]
            else conversation_identity_key
        )
        if legacy_thread_id and canonical:
            conn.execute(
                """
                INSERT INTO conversation_identity_links (
                    legacy_thread_id, conversation_identity_key,
                    first_linked_at, last_seen_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(legacy_thread_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (legacy_thread_id, canonical, now, now),
            )
            conn.commit()
    finally:
        conn.close()
    return canonical or conversation_identity_key


def backfill_conversation_identity_for_legacy_thread(
    *,
    conversation_identity_key: str,
    legacy_thread_id: str,
    db_path: Optional[str] = None,
) -> int:
    """
    Attach the canonical identity to prior messages stored only under the
    legacy thread id, without touching source_thread_id or any other field.
    Only ever fills an empty conversation_identity_key - never overwrites
    an already-set value. Returns the number of rows updated.
    """
    legacy_thread_id = _clean(legacy_thread_id)
    conversation_identity_key = _clean(conversation_identity_key)
    if not legacy_thread_id or not conversation_identity_key:
        return 0
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE communications SET conversation_identity_key = ? "
            "WHERE source_thread_id = ? AND "
            "(conversation_identity_key IS NULL OR conversation_identity_key = '')",
            (conversation_identity_key, legacy_thread_id),
        )
        updated = cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    return updated


def link_conversation_identities(
    *,
    from_identity_key: str,
    to_identity_key: str,
    db_path: Optional[str] = None,
) -> int:
    """
    Manually merge one conversation identity's history into another.

    Intended for the rare case where an AppFolio-native thread id becomes
    available for a conversation previously tracked only under its
    synthetic fallback identity, after a human (or a targeted diagnostic)
    has confirmed they are the same real conversation. Deliberately manual
    - safely detecting that automatically would require fuzzy content
    matching, which risks merging two genuinely different conversations.
    Non-destructive: existing rows keep their original source_thread_id
    verbatim; this only redirects conversation_identity_links and
    communications.conversation_identity_key so both old and new rows
    resolve to to_identity_key going forward. Returns the number of
    communications rows updated.
    """
    initialize_event_memory(db_path)
    from_identity_key = _clean(from_identity_key)
    to_identity_key = _clean(to_identity_key)
    if not from_identity_key or not to_identity_key:
        raise ValueError("both identity keys are required")
    if from_identity_key == to_identity_key:
        return 0
    now = _now()
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE conversation_identity_links SET "
            "conversation_identity_key = ?, last_seen_at = ? "
            "WHERE conversation_identity_key = ?",
            (to_identity_key, now, from_identity_key),
        )
        cursor = conn.execute(
            "UPDATE communications SET conversation_identity_key = ? "
            "WHERE conversation_identity_key = ?",
            (to_identity_key, from_identity_key),
        )
        updated = cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    return updated


def find_related_work_order(
    *, message_text: str, contact_name: str = "", contact_role: str = "",
    property_address: str = "", unit: str = "",
    db_path: Optional[str] = None,
) -> Optional[dict]:
    """Associate explicit WO numbers or exactly one contextual active match."""
    explicit = re.search(
        r"\b(?:WO|work\s+order)\b(?:[^0-9]{0,20})(\d+-\d+)\b",
        message_text or "",
        re.I,
    )
    conn = _connect(db_path)
    try:
        available_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if explicit:
            wo_num = explicit.group(1)
            if "wo_triage" in available_tables:
                row = conn.execute(
                    "SELECT * FROM wo_triage WHERE wo_num = ? LIMIT 1",
                    (wo_num,),
                ).fetchone()
                if row:
                    return dict(row)
            if "wo_scanner_state" in available_tables:
                row = conn.execute(
                    "SELECT wo_num, wo_url, tenant, address, "
                    "'' AS vendor_name, appfolio_status AS status "
                    "FROM wo_scanner_state WHERE wo_num = ? LIMIT 1",
                    (wo_num,),
                ).fetchone()
                if row:
                    return dict(row)
            return None

        name = _clean(contact_name)
        if not name:
            return None
        role = _clean(contact_role).lower()
        if role not in {"tenant", "resident", "vendor"}:
            return None
        if "wo_triage" not in available_tables:
            return None
        field = "vendor_name" if role == "vendor" else "tenant"
        status_placeholders = ", ".join("?" for _ in ACTIVE_WO_TRIAGE_STATUSES)
        rows = conn.execute(
            f"""
            SELECT * FROM wo_triage WHERE {field} LIKE ?
            AND status IN ({status_placeholders})
            ORDER BY updated_at DESC
            """,
            (f"%{name.split()[-1]}%", *ACTIVE_WO_TRIAGE_STATUSES),
        ).fetchall()
        candidates = [dict(row) for row in rows]
        prop = _clean(property_address).lower()
        unit_value = _clean(unit).lower()
        if prop:
            candidates = [row for row in candidates if prop in _clean(row["address"]).lower()]
        if unit_value:
            candidates = [row for row in candidates if unit_value in _clean(row["address"]).lower()]
        return candidates[0] if len(candidates) == 1 else None
    finally:
        conn.close()


def record_verified_appfolio_change(
    *, operation: str, target_type: str, target_id: str, new_state: dict,
    previous_state: Optional[dict] = None, internal_user_id: str = "",
    verification_result: str = "verified", details: Optional[dict] = None,
    db_path: Optional[str] = None,
) -> int:
    """Immediately reflect a verified AppFolio write into durable memory."""
    if _clean(verification_result).lower() != "verified":
        raise ValueError("unverified AppFolio change cannot update memory")
    if not all(_clean(value) for value in (operation, target_type, target_id)):
        raise ValueError("verified AppFolio change target is incomplete")
    if not isinstance(new_state, dict) or not new_state:
        raise ValueError("verified AppFolio change state is missing")
    initialize_event_memory(db_path)
    now = _now()
    conn = _connect(db_path)
    try:
        if target_type == "work_order":
            appfolio_status = _clean(
                new_state.get("appfolio_status") or new_state.get("status")
            )
            workflow_status = _clean(
                new_state.get("workflow_status") or new_state.get("status")
            )
            vendor_name = _clean(new_state.get("vendor_name"))
            scheduled_for = _clean(new_state.get("scheduled_for"))
            conn.execute(
                "UPDATE wo_scanner_state SET appfolio_status = "
                "COALESCE(NULLIF(?, ''), appfolio_status), last_seen_at = ? "
                "WHERE wo_num = ?",
                (appfolio_status, now, target_id),
            )
            conn.execute(
                """
                UPDATE wo_triage SET
                    status = COALESCE(NULLIF(?, ''), status),
                    vendor_name = COALESCE(NULLIF(?, ''), vendor_name),
                    scheduled_for = COALESCE(NULLIF(?, ''), scheduled_for),
                    updated_at = ? WHERE wo_num = ?
                """,
                (workflow_status, vendor_name, scheduled_for, now, target_id),
            )
        cursor = conn.execute(
            """
            INSERT INTO appfolio_action_audit (
                operation, target_type, target_id, internal_user_id, source,
                previous_state_json, new_state_json, performed_at, verified_at,
                verification_result, details_json
            ) VALUES (?, ?, ?, ?, 'rachel_appfolio_write', ?, ?, ?, ?, ?, ?)
            """,
            (
                operation, target_type, target_id, internal_user_id,
                json.dumps(previous_state or {}, sort_keys=True, default=str),
                json.dumps(new_state or {}, sort_keys=True, default=str),
                now, now, verification_result,
                json.dumps(details or {}, sort_keys=True, default=str),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()
