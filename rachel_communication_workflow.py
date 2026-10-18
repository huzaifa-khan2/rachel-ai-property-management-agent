"""Portfolio-aware ownership, routing, and draft-only communication workflow."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from rachel_event_memory import (
    DB_PATH,
    initialize_event_memory,
    internal_user_has_access,
    resolve_internal_user_identity,
)


RACHEL_OWNER_ALIASES = {
    "rachel",
    "rachel asst",
    "rachel assistant",
    "rachel@clientco.example",
}

AUTHORIZED_DRAFT_REASONS = {
    "thread_owned_by_rachel",
    "thread_started_by_rachel",
    "explicitly_delegated_to_rachel",
    "workflow_authorized_takeover",
}


class CommunicationWorkflowError(RuntimeError):
    """Fail-closed semantic workflow error with a sanitized reason."""


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _property_context(row: dict) -> tuple[str, str]:
    property_key = _clean(
        row.get("property") or row.get("property_address")
    )
    property_group = _clean(row.get("property_group"))
    if not property_key:
        detail = _clean(row.get("list_detail"))
        role = _clean(row.get("contact_role")).lower()
        if detail and detail.lower() not in {
            role, "tenant", "resident", "vendor", "owner"
        }:
            property_key = detail
    return property_key, property_group


def _configured_portfolio_assignment(
    property_key: str, *, db_path: Optional[str]
) -> Optional[dict]:
    if not property_key:
        return None
    effective_path = db_path or DB_PATH
    if effective_path == ":memory:":
        # A one-shot in-memory connection cannot share initialization state;
        # callers using it are deterministic tests with no durable mappings.
        return None
    initialize_event_memory(effective_path)
    conn = sqlite3.connect(effective_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM property_portfolio_assignments "
            "WHERE LOWER(property_key) = LOWER(?) LIMIT 1",
            (property_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def classify_communication_ownership(
    row: dict, *, db_path: Optional[str] = None
) -> dict:
    """Classify ownership without opening a thread or changing AppFolio."""
    assigned = _clean(row.get("assigned_user"))
    assigned_normalized = assigned.lower()
    property_key, property_group = _property_context(row)
    configured = _configured_portfolio_assignment(
        property_key, db_path=db_path
    )
    if configured and not property_group:
        property_group = _clean(configured.get("property_group"))

    if assigned_normalized in RACHEL_OWNER_ALIASES:
        return {
            "classification": "rachel_owned",
            "owner_internal_user_id": "rachel-asst",
            "portfolio_access_verified": True,
            "property_context_available": bool(
                property_key or property_group
            ),
            "routing_action": "eligible_for_rachel_reader",
            "automatic_open_allowed": True,
            "reason": "assigned_to_rachel",
        }

    assigned_user = resolve_internal_user_identity(
        assigned, db_path=db_path
    ) if assigned else None
    if assigned_user:
        permission = "property_management"
        has_access = internal_user_has_access(
            assigned_user["user_id"],
            permission=permission,
            property_key=property_key,
            property_group=property_group,
            db_path=db_path,
        )
        wildcard = "*" in {
            _clean(value).lower()
            for value in assigned_user.get("portfolio_scope", [])
        }
        if configured:
            configured_manager = _clean(
                configured.get("manager_user_id")
            )
            if (
                configured_manager
                and configured_manager != assigned_user["user_id"]
            ):
                return {
                    "classification": "internal_owner_scope_mismatch",
                    "owner_internal_user_id": assigned_user["user_id"],
                    "portfolio_access_verified": False,
                    "property_context_available": True,
                    "routing_action": "manual_ownership_review",
                    "automatic_open_allowed": False,
                    "reason": "configured_portfolio_owner_mismatch",
                }
        if not has_access and not wildcard:
            return {
                "classification": (
                    "internal_owner_scope_mismatch"
                    if property_key or property_group
                    else "internal_owner_scope_unverified"
                ),
                "owner_internal_user_id": assigned_user["user_id"],
                "portfolio_access_verified": False,
                "property_context_available": bool(
                    property_key or property_group
                ),
                "routing_action": "manual_ownership_review",
                "automatic_open_allowed": False,
                "reason": "portfolio_scope_not_verified",
            }
        return {
            "classification": (
                "pm_owned"
                if assigned_user.get("role") == "property_manager"
                else "internal_user_owned"
            ),
            "owner_internal_user_id": assigned_user["user_id"],
            "portfolio_access_verified": bool(has_access or wildcard),
            "property_context_available": bool(
                property_key or property_group
            ),
            "routing_action": "route_to_internal_owner",
            "automatic_open_allowed": False,
            "reason": "assigned_internal_owner_verified",
        }

    if not assigned and configured and _clean(
        configured.get("manager_user_id")
    ):
        return {
            "classification": "portfolio_manager_unassigned",
            "owner_internal_user_id": _clean(
                configured.get("manager_user_id")
            ),
            "portfolio_access_verified": True,
            "property_context_available": True,
            "routing_action": "route_to_configured_portfolio_owner",
            "automatic_open_allowed": False,
            "reason": "configured_portfolio_owner_available",
        }

    return {
        "classification": "ownership_unknown" if not assigned else "human_owned",
        "owner_internal_user_id": "",
        "portfolio_access_verified": False,
        "property_context_available": bool(property_key or property_group),
        "routing_action": "manual_ownership_review",
        "automatic_open_allowed": False,
        "reason": "no_verified_internal_owner",
    }


def _verified_thread_context_from_connection(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    conversation_identity_key: str = "",
) -> dict:
    """
    Load and validate one thread using the caller's transaction.

    thread_id is the legacy identifier and remains the primary lookup key
    for backward compatibility. When conversation_identity_key is also
    supplied (the durable logical-conversation linkage - see
    rachel_event_memory.conversation_identity_key), the communications and
    appfolio_thread_selector_state lookups additionally match on it, so a
    conversation whose legacy id changed (e.g. a native AppFolio thread id
    becoming available where only a synthetic fallback existed before)
    still resolves its complete history as one conversation rather than
    being split across two.
    """
    normalized_thread_id = _clean(thread_id)
    normalized_identity_key = _clean(conversation_identity_key)
    if not normalized_thread_id and not normalized_identity_key:
        raise CommunicationWorkflowError("thread_id_missing")
    if normalized_identity_key:
        rows = conn.execute(
            "SELECT * FROM communications WHERE source = ? AND "
            "(source_thread_id = ? OR conversation_identity_key = ?) "
            "ORDER BY first_seen_at, communication_id",
            (
                "appfolio_communication_inbox_thread",
                normalized_thread_id, normalized_identity_key,
            ),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM communications WHERE source = ? AND "
            "source_thread_id = ? ORDER BY first_seen_at, communication_id",
            ("appfolio_communication_inbox_thread", normalized_thread_id),
        ).fetchall()
    latest_state = conn.execute(
        "SELECT processing_status FROM appfolio_communication_state "
        "WHERE thread_id = ? ORDER BY last_seen_at DESC, rowid DESC "
        "LIMIT 1",
        (normalized_thread_id,),
    ).fetchone()
    if normalized_identity_key:
        selector_state = conn.execute(
            "SELECT processing_status FROM appfolio_thread_selector_state "
            "WHERE thread_id = ? OR conversation_identity_key = ? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (normalized_thread_id, normalized_identity_key),
        ).fetchone()
    else:
        selector_state = conn.execute(
            "SELECT processing_status FROM appfolio_thread_selector_state "
            "WHERE thread_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (normalized_thread_id,),
        ).fetchone()
    durable_read_verified = (
        selector_state["processing_status"] == "thread_read_verified"
        if selector_state
        else bool(
            latest_state
            and latest_state["processing_status"] == "thread_read_verified"
        )
    )
    if (
        not rows
        or not durable_read_verified
    ):
        raise CommunicationWorkflowError("verified_full_thread_unavailable")

    messages = []
    for row in rows:
        item = dict(row)
        try:
            metadata = json.loads(item.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        fingerprint = _clean(metadata.get("message_fingerprint"))
        if not fingerprint:
            raise CommunicationWorkflowError("message_fingerprint_missing")
        if "rendered_index" not in metadata:
            raise CommunicationWorkflowError("message_order_missing")
        try:
            rendered_index = int(metadata["rendered_index"])
        except (TypeError, ValueError):
            raise CommunicationWorkflowError("message_order_invalid")
        if rendered_index < 0:
            raise CommunicationWorkflowError("message_order_invalid")
        messages.append({
            "communication_id": item["communication_id"],
            "message_fingerprint": fingerprint,
            "direction": _clean(item.get("direction")).lower(),
            "message_text": _clean(item.get("message_text")),
            "timestamp": _clean(item.get("occurred_at")),
            "rendered_index": rendered_index,
            "related_wo": _clean(item.get("related_wo")),
        })
    messages.sort(
        key=lambda item: (
            item["rendered_index"], item["communication_id"]
        )
    )
    rendered_indexes = [item["rendered_index"] for item in messages]
    if rendered_indexes != list(range(len(messages))):
        raise CommunicationWorkflowError("message_order_ambiguous")
    if any(
        item["direction"] not in {"inbound", "outbound"}
        or not item["message_text"]
        for item in messages
    ):
        raise CommunicationWorkflowError("verified_message_invalid")
    fingerprints = [item["message_fingerprint"] for item in messages]
    if len(fingerprints) != len(set(fingerprints)):
        raise CommunicationWorkflowError("duplicate_message_fingerprint")
    context_digest = hashlib.sha256(
        json.dumps(
            [
                {
                    "fingerprint": item["message_fingerprint"],
                    "direction": item["direction"],
                    "text": item["message_text"],
                }
                for item in messages
            ],
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "thread_id": normalized_thread_id,
        "conversation_identity_key": normalized_identity_key,
        "messages": messages,
        "message_count": len(messages),
        "latest_communication_id": messages[-1]["communication_id"],
        "latest_message_fingerprint": messages[-1]["message_fingerprint"],
        "latest_direction": messages[-1]["direction"],
        "related_work_orders": sorted({
            item["related_wo"] for item in messages if item["related_wo"]
        }),
        "context_digest": context_digest,
        "complete_verified_history": True,
    }


def load_verified_thread_context(
    thread_id: str, *, db_path: str, conversation_identity_key: str = ""
) -> dict:
    """Load a complete, verified, ordered thread from durable memory."""
    initialize_event_memory(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _verified_thread_context_from_connection(
            conn, thread_id,
            conversation_identity_key=conversation_identity_key,
        )
    finally:
        conn.close()


def create_response_draft_plan(
    thread_id: str,
    *,
    ownership: dict,
    authorization_reason: str,
    db_path: str,
    conversation_identity_key: str = "",
) -> dict:
    """Create a deterministic draft-only plan; never touch a composer."""
    classification = _clean(
        ownership.get("classification")
    ).lower()
    authorization = _clean(authorization_reason)
    if authorization not in AUTHORIZED_DRAFT_REASONS:
        raise CommunicationWorkflowError("draft_authority_not_verified")
    if (
        authorization == "thread_owned_by_rachel"
        and classification != "rachel_owned"
    ):
        raise CommunicationWorkflowError("draft_authority_owner_mismatch")
    context = load_verified_thread_context(
        thread_id, db_path=db_path,
        conversation_identity_key=conversation_identity_key,
    )
    if context["latest_direction"] != "inbound":
        raise CommunicationWorkflowError("no_unanswered_inbound_message")
    idempotency_key = hashlib.sha256(
        "|".join((
            context["thread_id"],
            context["latest_message_fingerprint"],
            context["context_digest"],
            classification,
            authorization,
        )).encode("utf-8")
    ).hexdigest()
    return {
        **context,
        "ownership_classification": classification,
        "authorization_reason": authorization,
        "idempotency_key": idempotency_key,
        "draft_only": True,
        "composer_actions_attempted": 0,
        "send_actions_attempted": 0,
    }


def build_response_prompt(plan: dict) -> str:
    """Build a complete-history prompt for an external draft generator."""
    if not plan.get("complete_verified_history") or not plan.get("draft_only"):
        raise CommunicationWorkflowError("draft_plan_incomplete")
    history = "\n".join(
        f"{item['direction'].upper()}: {item['message_text']}"
        for item in plan.get("messages", [])
    )
    return (
        "Draft a concise property-management response using the complete "
        "verified conversation below. Do not invent facts, commitments, "
        "appointments, approvals, or work-order state. Return draft text only.\n\n"
        f"VERIFIED CONVERSATION:\n{history}"
    )


def generate_response_draft(plan: dict, *, generator) -> str:
    """Generate draft text through an injected model adapter, never a UI."""
    prompt = build_response_prompt(plan)
    generated = generator(prompt)
    text = _clean(generated)
    if not text:
        raise CommunicationWorkflowError("draft_generation_empty")
    return text


def store_response_draft(
    plan: dict, draft_text: str, *, db_path: str
) -> dict:
    """Persist one idempotent draft; sending remains a separate gated phase."""
    if not plan.get("draft_only"):
        raise CommunicationWorkflowError("draft_only_guard_missing")
    current = load_verified_thread_context(
        _clean(plan.get("thread_id")), db_path=db_path,
        conversation_identity_key=_clean(
            plan.get("conversation_identity_key")
        ),
    )
    if (
        current["context_digest"] != _clean(plan.get("context_digest"))
        or current["latest_message_fingerprint"]
        != _clean(plan.get("latest_message_fingerprint"))
    ):
        raise CommunicationWorkflowError("draft_context_changed_before_store")
    text = _clean(draft_text)
    if not text:
        raise CommunicationWorkflowError("draft_text_missing")
    initialize_event_memory(db_path)
    idempotency_key = _clean(plan.get("idempotency_key"))
    if not idempotency_key:
        raise CommunicationWorkflowError("draft_idempotency_key_missing")
    draft_id = hashlib.sha256(
        f"communication-draft|{idempotency_key}".encode("utf-8")
    ).hexdigest()
    now = _now()
    conn = sqlite3.connect(db_path)
    try:
        existing = conn.execute(
            "SELECT draft_text FROM communication_drafts "
            "WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        existed = bool(existing)
        if existing and _clean(existing[0]) != text:
            raise CommunicationWorkflowError("draft_idempotency_conflict")
        conn.execute(
            """
            INSERT INTO communication_drafts (
                draft_id, idempotency_key, source_thread_id,
                latest_communication_id, context_digest,
                ownership_classification, authorization_reason,
                draft_text, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft_only', ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (
                draft_id, idempotency_key, plan["thread_id"],
                plan["latest_communication_id"], plan["context_digest"],
                plan["ownership_classification"],
                plan["authorization_reason"], text, now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "draft_id": draft_id,
        "idempotency_key": idempotency_key,
        "created": not existed,
        "status": "draft_only",
        "composer_actions_attempted": 0,
        "send_actions_attempted": 0,
    }
