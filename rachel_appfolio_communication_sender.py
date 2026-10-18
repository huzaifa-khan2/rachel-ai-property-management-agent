"""Fail-closed AppFolio Communication send intent and execution boundary.

This module never discovers a recipient and never opens a thread. It accepts
only the exact list row and semantically verified thread produced by the
read-only pipeline. Live execution is separately disabled by Rachel's safety
configuration and uses a durable ``sending_unverified`` state so a crash can
never cause an automatic duplicate retry.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone

from rachel_communication_workflow import (
    AUTHORIZED_DRAFT_REASONS,
    CommunicationWorkflowError,
    _verified_thread_context_from_connection,
    load_verified_thread_context,
)
from rachel_event_memory import (
    inbox_selector_key,
    initialize_event_memory,
)
from rachel_appfolio_ui import CommunicationThreadPage
from rachel_safety import appfolio_communication_send_allowed


class CommunicationSendError(RuntimeError):
    """Sanitized, fail-closed outbound boundary error."""


class AppFolioExactCommunicationTransport:
    """Exact known-DOM composer transport used only after intent claiming."""

    def __init__(self, page, selected_row: dict):
        self.page = page
        self.selected_row = dict(selected_row)

    def _semantic_frame(self):
        from rachel_appfolio_inbox import (
            _INBOX_THREAD_READ_SCRIPT,
            _discover_inbox_frames,
            _verify_thread_identity,
        )

        matches = []
        for frame in _discover_inbox_frames(self.page):
            try:
                snapshot = frame.evaluate(_INBOX_THREAD_READ_SCRIPT) or {}
                _verify_thread_identity(self.selected_row, snapshot)
            except Exception:
                continue
            if (
                int(snapshot.get("thread_root_count", 0) or 0) == 1
                and int(snapshot.get("history_region_count", 0) or 0) == 1
                and int(snapshot.get("composer_region_count", 0) or 0) == 1
                and int(snapshot.get("composer_textarea_count", 0) or 0) == 1
                and int(snapshot.get("send_control_count", 0) or 0) == 1
                and int(snapshot.get("send_via_sms_label_count", 0) or 0) == 1
                and snapshot.get("message_structure_valid")
            ):
                matches.append((frame, snapshot))
        if len(matches) != 1:
            raise CommunicationSendError("exact_semantic_thread_frame_ambiguous")
        return matches[0]

    @staticmethod
    def _exact_composer_locators(frame):
        controls = CommunicationThreadPage(frame).exact_composer_controls()
        if not controls:
            raise CommunicationSendError("exact_composer_target_ambiguous")
        textarea, send, _root = controls
        return textarea, send

    def send_exact(self, message: str, *, expected: dict) -> dict:
        """Fill once, click once, then semantically verify one appended send."""
        from rachel_appfolio_inbox import _normalize_thread_messages

        frame, before = self._semantic_frame()
        if int(before.get("message_count", 0) or 0) != int(
            expected.get("messages_before", -1)
        ):
            raise CommunicationSendError("transport_history_count_changed")
        normalized_before = _normalize_thread_messages(
            _clean(self.selected_row.get("thread_id")),
            list(before.get("messages") or []),
        )
        if [
            item["message_fingerprint"] for item in normalized_before
        ] != list(expected.get("message_fingerprints") or []):
            raise CommunicationSendError(
                "transport_history_fingerprints_changed"
            )
        if _recipient_identity_digest(self.selected_row) != expected.get(
            "recipient_identity_digest"
        ):
            raise CommunicationSendError("transport_recipient_changed")
        textarea, send = self._exact_composer_locators(frame)
        if _clean(textarea.input_value()):
            raise CommunicationSendError("transport_composer_not_empty")
        textarea.fill(message)
        if _clean(textarea.input_value()) != _clean(message):
            raise CommunicationSendError("transport_composer_fill_mismatch")
        if send.is_disabled():
            raise CommunicationSendError("transport_send_control_disabled")
        send.click()

        last_reason = "outbound_not_mounted"
        for attempt in range(20):
            try:
                _post_frame, after = self._semantic_frame()
                normalized = _normalize_thread_messages(
                    _clean(self.selected_row.get("thread_id")),
                    list(after.get("messages") or []),
                )
                if len(normalized) != int(expected["messages_before"]) + 1:
                    last_reason = "message_count_not_incremented"
                elif [
                    item["message_fingerprint"] for item in normalized[:-1]
                ] != list(expected.get("message_fingerprints") or []):
                    last_reason = "history_prefix_changed"
                elif normalized[-1].get("direction") != "outbound":
                    last_reason = "appended_message_not_outbound"
                elif _digest(_clean(normalized[-1].get("message_text"))) != (
                    expected.get("message_digest")
                ):
                    last_reason = "appended_message_digest_mismatch"
                else:
                    return {
                        "exact_thread_preserved": True,
                        "recipient_identity_digest": expected[
                            "recipient_identity_digest"
                        ],
                        "outbound_message_digest": expected["message_digest"],
                        "messages_after": len(normalized),
                        "sent_message_fingerprint": normalized[-1][
                            "message_fingerprint"
                        ],
                    }
            except CommunicationSendError:
                raise
            except Exception:
                last_reason = "semantic_postsend_read_failed"
            if attempt < 19:
                self.page.wait_for_timeout(250)
        raise CommunicationSendError(f"send_postcheck_{last_reason}")


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _recipient_identity_digest(row: dict) -> str:
    identity = "|".join(
        _clean(row.get(key)).lower()
        for key in (
            "contact_name", "contact_role", "property", "unit"
        )
    )
    if not _clean(row.get("contact_name")) or not _clean(
        row.get("contact_role")
    ):
        raise CommunicationSendError("recipient_identity_incomplete")
    return _digest(identity)


def _load_draft_and_intent(
    intent_id: str, *, db_path: str
) -> tuple[dict, dict]:
    initialize_event_memory(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        intent = conn.execute(
            "SELECT * FROM communication_send_intents WHERE intent_id = ?",
            (_clean(intent_id),),
        ).fetchone()
        draft = None
        if intent:
            draft = conn.execute(
                "SELECT * FROM communication_drafts WHERE draft_id = ?",
                (intent["draft_id"],),
            ).fetchone()
    finally:
        conn.close()
    if not intent or not draft:
        raise CommunicationSendError("send_intent_or_draft_missing")
    return dict(intent), dict(draft)


def prepare_send_intent(
    draft_id: str,
    *,
    selected_row: dict,
    authorization_evidence_id: str,
    db_path: str,
) -> dict:
    """Persist one recipient-bound, idempotent intent without touching UI."""
    initialize_event_memory(db_path)
    evidence_id = _clean(authorization_evidence_id)
    if not evidence_id:
        raise CommunicationSendError("authorization_evidence_missing")
    thread_id = _clean(selected_row.get("thread_id"))
    if not thread_id:
        raise CommunicationSendError("selected_thread_id_missing")
    computed_selector = inbox_selector_key(selected_row)
    supplied_selector = _clean(selected_row.get("selector_key")).lower()
    if supplied_selector and supplied_selector != computed_selector:
        raise CommunicationSendError("selected_selector_mismatch")
    recipient_digest = _recipient_identity_digest(selected_row)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        draft_row = conn.execute(
            "SELECT * FROM communication_drafts WHERE draft_id = ?",
            (_clean(draft_id),),
        ).fetchone()
    finally:
        conn.close()
    if not draft_row or draft_row["status"] != "draft_only":
        raise CommunicationSendError("eligible_draft_missing")
    draft = dict(draft_row)
    if draft["source_thread_id"] != thread_id:
        raise CommunicationSendError("draft_thread_mismatch")
    if draft["authorization_reason"] not in AUTHORIZED_DRAFT_REASONS:
        raise CommunicationSendError("draft_authorization_invalid")

    try:
        context = load_verified_thread_context(thread_id, db_path=db_path)
    except CommunicationWorkflowError as error:
        raise CommunicationSendError(f"context_{error}") from error
    if (
        draft["latest_communication_id"]
        != context["latest_communication_id"]
        or draft["context_digest"] != context["context_digest"]
        or context["latest_direction"] != "inbound"
    ):
        raise CommunicationSendError("draft_context_is_stale")

    message_digest = _digest(_clean(draft["draft_text"]))
    idempotency_key = _digest("|".join((
        thread_id,
        computed_selector,
        recipient_digest,
        context["latest_message_fingerprint"],
        context["context_digest"],
        message_digest,
    )))
    intent_id = _digest(f"communication-send-intent|{idempotency_key}")
    now = _now()
    values = (
        intent_id, idempotency_key, draft["draft_id"], thread_id,
        computed_selector, recipient_digest,
        context["latest_message_fingerprint"], context["context_digest"],
        message_digest, draft["authorization_reason"], evidence_id,
        "prepared", now, now,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT * FROM communication_send_intents "
            "WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO communication_send_intents (
                    intent_id, idempotency_key, draft_id, source_thread_id,
                    target_selector_key, recipient_identity_digest,
                    reply_to_message_fingerprint, context_digest,
                    message_digest, authorization_reason,
                    authorization_evidence_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
            created = True
        else:
            created = False
            immutable = {
                "draft_id": draft["draft_id"],
                "source_thread_id": thread_id,
                "target_selector_key": computed_selector,
                "recipient_identity_digest": recipient_digest,
                "reply_to_message_fingerprint": context[
                    "latest_message_fingerprint"
                ],
                "context_digest": context["context_digest"],
                "message_digest": message_digest,
                "authorization_evidence_id": evidence_id,
            }
            if any(existing[key] != value for key, value in immutable.items()):
                raise CommunicationSendError("send_intent_idempotency_conflict")
    finally:
        conn.close()
    return {
        "intent_id": intent_id,
        "created": created,
        "status": "prepared" if created else str(existing["status"]),
        "composer_actions_attempted": 0,
        "send_actions_attempted": 0,
        "appfolio_writes_attempted": 0,
    }


def verify_send_preconditions(
    intent_id: str,
    *,
    selected_row: dict,
    thread_snapshot: dict,
    db_path: str,
) -> dict:
    """Reverify exact recipient, history, and composer immediately pre-send."""
    intent, draft = _load_draft_and_intent(intent_id, db_path=db_path)
    if intent["status"] != "prepared":
        raise CommunicationSendError(
            f"send_intent_not_prepared_{intent['status']}"
        )
    if _clean(selected_row.get("thread_id")) != intent["source_thread_id"]:
        raise CommunicationSendError("selected_thread_changed")
    if inbox_selector_key(selected_row) != intent["target_selector_key"]:
        raise CommunicationSendError("selected_event_changed")
    if _recipient_identity_digest(selected_row) != intent[
        "recipient_identity_digest"
    ]:
        raise CommunicationSendError("recipient_identity_changed")

    exact_counts = {
        "thread_root_count": 1,
        "header_region_count": 1,
        "history_region_count": 1,
        "composer_region_count": 1,
        "composer_textarea_count": 1,
        "send_control_count": 1,
        "send_via_sms_label_count": 1,
    }
    for key, expected in exact_counts.items():
        if int(thread_snapshot.get(key, 0) or 0) != expected:
            raise CommunicationSendError(f"semantic_{key}_invalid")
    if not thread_snapshot.get("message_structure_valid"):
        raise CommunicationSendError("semantic_message_structure_invalid")
    if int(thread_snapshot.get("composer_value_length", -1)) != 0:
        raise CommunicationSendError("composer_not_empty")
    if _clean(thread_snapshot.get("contact_name")).lower() != _clean(
        selected_row.get("contact_name")
    ).lower():
        raise CommunicationSendError("opened_contact_changed")
    if _clean(thread_snapshot.get("contact_role")).lower() != _clean(
        selected_row.get("contact_role")
    ).lower():
        raise CommunicationSendError("opened_role_changed")
    context_text = _clean(thread_snapshot.get("detail_context")).lower()
    for key in ("property", "unit"):
        expected = _clean(selected_row.get(key)).lower()
        if expected and expected not in context_text:
            raise CommunicationSendError(f"opened_{key}_changed")

    context = load_verified_thread_context(
        intent["source_thread_id"], db_path=db_path
    )
    if (
        context["context_digest"] != intent["context_digest"]
        or context["latest_message_fingerprint"]
        != intent["reply_to_message_fingerprint"]
        or draft["context_digest"] != intent["context_digest"]
        or _digest(_clean(draft["draft_text"])) != intent["message_digest"]
    ):
        raise CommunicationSendError("durable_context_changed")
    mounted_fingerprints = list(
        thread_snapshot.get("normalized_message_fingerprints") or []
    )
    durable_fingerprints = [
        message["message_fingerprint"] for message in context["messages"]
    ]
    if mounted_fingerprints != durable_fingerprints:
        raise CommunicationSendError("mounted_history_changed")
    return {
        "intent_id": intent["intent_id"],
        "message_text": draft["draft_text"],
        "message_digest": intent["message_digest"],
        "recipient_identity_digest": intent["recipient_identity_digest"],
        "messages_verified": len(durable_fingerprints),
        "message_fingerprints": durable_fingerprints,
        "composer_actions_attempted": 0,
        "send_actions_attempted": 0,
        "appfolio_writes_attempted": 0,
    }


def dry_run_send_intent(
    intent_id: str,
    *,
    selected_row: dict,
    thread_snapshot: dict,
    db_path: str,
) -> dict:
    """Exercise every semantic send precondition without UI mutation."""
    verified = verify_send_preconditions(
        intent_id,
        selected_row=selected_row,
        thread_snapshot=thread_snapshot,
        db_path=db_path,
    )
    return {
        key: value for key, value in verified.items()
        if key != "message_text"
    } | {"status": "dry_run_verified"}


def execute_send_intent(
    intent_id: str,
    *,
    selected_row: dict,
    thread_snapshot: dict,
    transport,
    db_path: str,
) -> dict:
    """Execute one preverified send through an injected exact transport.

    The durable transition to ``sending_unverified`` happens before the first
    composer mutation. Any exception leaves that state in place for manual
    reconciliation; the intent is never automatically retried.
    """
    current_intent, _current_draft = _load_draft_and_intent(
        intent_id, db_path=db_path
    )
    allowed, reason = appfolio_communication_send_allowed(
        inbox_selector_key(selected_row),
        current_intent["authorization_evidence_id"],
    )
    if not allowed:
        raise CommunicationSendError(f"live_send_blocked_{reason}")
    verified = verify_send_preconditions(
        intent_id,
        selected_row=selected_row,
        thread_snapshot=thread_snapshot,
        db_path=db_path,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked_intent = conn.execute(
            "SELECT * FROM communication_send_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if not locked_intent or locked_intent["status"] != "prepared":
            raise CommunicationSendError("send_intent_claim_failed")
        try:
            locked_context = _verified_thread_context_from_connection(
                conn, locked_intent["source_thread_id"]
            )
        except CommunicationWorkflowError as error:
            raise CommunicationSendError(
                f"claim_context_{error}"
            ) from error
        if (
            locked_context["context_digest"]
            != locked_intent["context_digest"]
            or locked_context["latest_message_fingerprint"]
            != locked_intent["reply_to_message_fingerprint"]
        ):
            raise CommunicationSendError(
                "durable_context_changed_before_claim"
            )
        changed = conn.execute(
            "UPDATE communication_send_intents SET "
            "status = 'sending_unverified', attempt_count = attempt_count + 1, "
            "updated_at = ? WHERE intent_id = ? AND status = 'prepared'",
            (_now(), intent_id),
        ).rowcount
        if changed != 1:
            raise CommunicationSendError("send_intent_claim_failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        postcheck = transport.send_exact(
            verified["message_text"],
            expected={
                "thread_id": _clean(selected_row.get("thread_id")),
                "recipient_identity_digest": verified[
                    "recipient_identity_digest"
                ],
                "message_digest": verified["message_digest"],
                "messages_before": verified["messages_verified"],
                "message_fingerprints": verified["message_fingerprints"],
            },
        )
        if not isinstance(postcheck, dict):
            raise CommunicationSendError("send_postcheck_missing")
        if not postcheck.get("exact_thread_preserved"):
            raise CommunicationSendError("send_postcheck_thread_changed")
        if postcheck.get("recipient_identity_digest") != verified[
            "recipient_identity_digest"
        ]:
            raise CommunicationSendError("send_postcheck_recipient_changed")
        if postcheck.get("outbound_message_digest") != verified[
            "message_digest"
        ]:
            raise CommunicationSendError("send_postcheck_message_mismatch")
        if int(postcheck.get("messages_after", 0) or 0) != (
            verified["messages_verified"] + 1
        ):
            raise CommunicationSendError("send_postcheck_count_mismatch")
        sent_fingerprint = _clean(
            postcheck.get("sent_message_fingerprint")
        )
        if not sent_fingerprint:
            raise CommunicationSendError("send_postcheck_fingerprint_missing")
    except Exception as error:
        failure = (
            str(error)
            if isinstance(error, CommunicationSendError)
            else f"transport_{type(error).__name__}"
        )
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE communication_send_intents SET failure_reason = ?, "
                "updated_at = ? WHERE intent_id = ?",
                (failure, _now(), intent_id),
            )
            conn.commit()
        finally:
            conn.close()
        if isinstance(error, CommunicationSendError):
            raise
        raise CommunicationSendError(failure) from error

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE communication_send_intents SET status = 'sent_verified', "
            "sent_message_fingerprint = ?, failure_reason = '', "
            "updated_at = ? WHERE intent_id = ? "
            "AND status = 'sending_unverified'",
            (sent_fingerprint, _now(), intent_id),
        ).rowcount
        if changed != 1:
            raise CommunicationSendError(
                "send_completion_state_invalid"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "intent_id": intent_id,
        "status": "sent_verified",
        "composer_actions_attempted": 1,
        "send_actions_attempted": 1,
        "appfolio_writes_attempted": 1,
    }
