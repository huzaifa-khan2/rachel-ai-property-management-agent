"""
Rachel - Full Inbox Manager v2
Reads rachel@clientco.example and handles ALL incoming emails.

Email types:
  1. AppFolio work order notifications → triage (coming soon)
  2. Manager/Owner emails → smart handling (simple replies directly, PM actions via DB)
  3. Tenant replies → reply directly to tenant + notify Manager
  4. Spam/junk → delete
  5. Unknown/important → acknowledge sender + forward to Manager

Run with:
  python3 rachel_inbox_monitor.py         (check once)
  python3 rachel_inbox_monitor.py --watch (check every 5 minutes)
"""

import os, re, imaplib, email, sqlite3, json, time, sys, subprocess
import smtplib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, date, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from anthropic import Anthropic
from rachel_appfolio_browser import get_appfolio_browser_manager
from rachel_event_memory import (
    get_internal_user_context,
    internal_user_has_access,
    resolve_internal_user,
    store_communication,
)
from rachel_safety import email_allowed, sms_allowed
from rachel_appfolio_write_intents import (
    claim_outbound_intent,
    complete_outbound_intent,
    prepare_outbound_intent,
    record_outbound_intent_failure,
)

def get_secret(env_name, service):
    value = os.getenv(env_name, "")
    if value:
        return value
    try:
        return subprocess.check_output(
            [
                "security",
                "find-generic-password",
                "-a",
                "rachel",
                "-s",
                service,
                "-w",
            ],
            text=True,
        ).strip()
    except Exception:
        return ""

DB_PATH          = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")
MANAGER_EMAIL     = os.getenv("MANAGER_EMAIL", "manager@clientco.example")
RACHEL_EMAIL     = os.getenv("RACHEL_FROM_EMAIL", "rachel@clientco.example")
RACHEL_IMAP_USER = os.getenv("RACHEL_EMAIL_USER", "rachel@clientco.example")
RACHEL_IMAP_PASS = get_secret("RACHEL_EMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD")
OWNER_EMAIL       = os.getenv("OWNER_EMAIL", "owner@clientco.example")

OWNER_QA_MODE = os.getenv(
    "RACHEL_OWNER_QA_MODE",
    "false",
).strip().lower() in ("1", "true", "yes", "on")

APPFOLIO_SENDER_DOMAINS = {"appfolio.com", "appfolio.us"}
MANAGER_SENDERS   = [MANAGER_EMAIL, OWNER_EMAIL]

# SMS via Google Voice email-to-SMS gateway
# Rachel sends texts by emailing [number]@txt.voice.google.com
MANAGER_CELL      = "18055046160"
OWNER_CELL        = "18472742377"
GOOGLE_VOICE_SMS = "txt.voice.google.com"

ANTHROPIC_KEY = get_secret(
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
)
client = Anthropic(api_key=ANTHROPIC_KEY)

# Import work order triage engine
try:
    from rachel_wo_triage import (
        evaluate_triage_eligibility,
        parse_work_order_email,
        triage_work_order,
        triage_work_order_details,
        _truncate_at_word_boundary,
    )
    WO_TRIAGE_AVAILABLE = True
    print("[Monitor] Work order triage engine loaded")
except ImportError:
    WO_TRIAGE_AVAILABLE = False
    print("[Monitor] rachel_wo_triage.py not found — WO triage disabled")

# Import AppFolio inbox monitor for tenant text replies
try:
    from rachel_appfolio_inbox import (
        check_appfolio_inbox,
        process_pending_rachel_owned_response_drafts,
        process_pending_tenant_triage_replies,
        process_rachel_owned_appfolio_threads,
    )
    AF_INBOX_AVAILABLE = True
    print("[Monitor] AppFolio inbox monitor loaded")
except ImportError:
    AF_INBOX_AVAILABLE = False
    print("[Monitor] rachel_appfolio_inbox.py not found — text reply monitor disabled")


# Import the 24/7 AppFolio Work Order scanner. It reuses the same
# logged-in browser page created by this inbox-monitor process.
try:
    from rachel_work_order_scanner import (
        init_scanner_state_table,
        reconcile_logged_in_page,
    )
    WO_SCANNER_AVAILABLE = True
    print("[Monitor] Work order scanner loaded")
except ImportError as exc:
    WO_SCANNER_AVAILABLE = False
    print(f"[Monitor] Work order scanner disabled: {exc}")

# ── Email sending ──────────────────────────────────────────────────────────

_CURRENT_EMAIL_EVENT_KEY = ContextVar(
    "rachel_current_email_event_key",
    default="",
)


@contextmanager
def _outbound_delivery_event(event_key: str):
    """Bind SMTP sends to one durable inbound event for this call stack."""
    token = _CURRENT_EMAIL_EVENT_KEY.set(str(event_key or "").strip())
    try:
        yield
    finally:
        _CURRENT_EMAIL_EVENT_KEY.reset(token)


def send_email(
    to: str,
    subject: str,
    body: str,
    also_owner: bool = False,
    delivery_key: str = "",
):
    """
    Send email from Rachel.
    also_owner=True adds Owner as second TO (for Manager alerts during QA phase).
    """
    requested_recipients = [to]

    if also_owner and to.lower() != OWNER_EMAIL.lower():
        requested_recipients.append(OWNER_EMAIL)

    allowed_recipients = []

    for recipient in requested_recipients:
        allowed, reason = email_allowed(recipient)

        if allowed:
            if recipient.lower() not in {
                item.lower() for item in allowed_recipients
            }:
                allowed_recipients.append(recipient)
        else:
            print(
                f"[Safety] Email blocked to {recipient}: {reason}"
            )

    if not allowed_recipients:
        print(f"[Safety] Email delivery fully suppressed: {subject[:80]}")
        return False

    if not RACHEL_IMAP_USER or not RACHEL_IMAP_PASS:
        print(
            f"[Email] Rachel Gmail credentials are unavailable\\n"
            f"Allowed recipients: {allowed_recipients}\\n"
            f"Subject: {subject}"
        )
        return False

    durable_target = (
        str(delivery_key or "").strip()
        or _CURRENT_EMAIL_EVENT_KEY.get()
    )
    if not durable_target:
        print(
            "[Email] Delivery blocked because no durable source-event key "
            "was supplied."
        )
        return False

    intent = prepare_outbound_intent(
        operation="smtp_email",
        target_id=durable_target,
        recipient=",".join(allowed_recipients),
        message=f"{subject}\n{body}",
        db_path=DB_PATH,
    )
    if intent["status"] == "sent_verified":
        print("[Email] Duplicate verified SMTP delivery suppressed")
        return True
    if intent["status"] != "prepared":
        print(
            "[Email] Prior SMTP attempt is unverified; automatic retry "
            "is blocked."
        )
        return False

    try:
        # External emails (e.g. AppFolio's own auto-notifications) can carry
        # a raw CR/LF inside their Subject header. Forwarding that text
        # verbatim into an outgoing header trips Python's SMTP header-
        # injection guard and raises, and since that happens before the
        # message is ever sent, the source email is never marked processed
        # and gets retried — and re-crashes — on every future poll forever.
        subject = re.sub(r"\s+", " ", subject).strip()

        msg = EmailMessage()
        msg["From"] = f"Rachel — ClientCo <{RACHEL_EMAIL}>"
        msg["To"] = ", ".join(allowed_recipients)
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=30,
        ) as server:
            server.login(RACHEL_IMAP_USER, RACHEL_IMAP_PASS)
            claim_outbound_intent(intent["intent_id"], db_path=DB_PATH)
            refused = server.send_message(
                msg,
                from_addr=RACHEL_EMAIL,
                to_addrs=allowed_recipients,
            )

        if refused:
            print(f"[Email] Gmail SMTP refused recipients: {refused}")
            record_outbound_intent_failure(
                intent["intent_id"],
                reason="smtp_recipient_refused",
                db_path=DB_PATH,
            )
            return False

        complete_outbound_intent(
            intent["intent_id"],
            verification_receipt=(
                f"smtp_accepted_{intent['intent_id'][:16]}"
            ),
            db_path=DB_PATH,
        )

        recipient_note = ", ".join(allowed_recipients)
        print(f"[Email] ✅ To {recipient_note}: {subject[:50]}")

        management_recipients = [
            recipient
            for recipient in allowed_recipients
            if recipient.lower() in {
                MANAGER_EMAIL.lower(),
                OWNER_EMAIL.lower(),
            }
        ]

        outbound_wo_match = re.search(
            r"(?:WO\\s*#?|work\\s*order\\s*#?)([\\d-]+)",
            f"{subject} {body}",
            re.IGNORECASE,
        )

        try:
            for recipient in management_recipients:
                record_management_email_audit(
                    direction="outbound",
                    sender=RACHEL_EMAIL,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    related_wo=(
                        outbound_wo_match.group(1)
                        if outbound_wo_match
                        else ""
                    ),
                    classification="rachel_management_response",
                )
        except Exception as audit_error:
            print(
                "[Email] Delivery verified, but local management audit "
                f"failed: {type(audit_error).__name__}"
            )

        return True
    except Exception as e:
        try:
            record_outbound_intent_failure(
                intent["intent_id"],
                reason="smtp_transport_failure",
                db_path=DB_PATH,
            )
        except Exception:
            pass
        print(f"[Email] Gmail SMTP failed: {e}")
        return False


# ── SMS via Google Voice ───────────────────────────────────────────────────

def send_sms(
    message: str,
    urgent: bool = False,
    delivery_key: str = "",
):
    """
    Send SMS to both Manager and Owner via Gmail SMTP -> Google Voice email-to-SMS.
    Uses Rachel's Google Workspace mailbox through Gmail SMTP.
    Every text is also emailed.
    urgent=True adds 🚨 prefix.
    """
    allowed, reason = sms_allowed()
    if not allowed:
        print(f"[Safety] SMS blocked: {reason}")
        return False

    import smtplib
    from email.mime.text import MIMEText

    prefix = "🚨 " if urgent else ""
    sms_body = f"{prefix}{message}"

    gmail_user = get_secret("RACHEL_GMAIL_USER", "RACHEL_GMAIL_USER")
    gmail_pass = get_secret("RACHEL_GMAIL_PASSWORD", "RACHEL_GMAIL_PASSWORD")

    if not gmail_pass:
        print("[SMS] No Gmail password — cannot send SMS")
        return False

    durable_target = (
        str(delivery_key or "").strip()
        or _CURRENT_EMAIL_EVENT_KEY.get()
    )
    if not durable_target:
        print(
            "[SMS] Delivery blocked because no durable source-event key "
            "was supplied."
        )
        return False

    # Send to both Manager and Owner via Google Voice SMS gateway
    sms_recipients = [
        f"{MANAGER_CELL}@{GOOGLE_VOICE_SMS}",
        f"{OWNER_CELL}@{GOOGLE_VOICE_SMS}",
    ]

    sms_intent = prepare_outbound_intent(
        operation="google_voice_sms",
        target_id=durable_target,
        recipient=",".join(sms_recipients),
        message=sms_body,
        db_path=DB_PATH,
    )
    sms_delivered = sms_intent["status"] == "sent_verified"
    if sms_delivered:
        print("[SMS] Duplicate verified gateway delivery suppressed")
    elif sms_intent["status"] != "prepared":
        print(
            "[SMS] Prior gateway attempt is unverified; automatic retry "
            "is blocked."
        )
        return False
    else:
        server = None
        try:
            server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
            server.login(gmail_user, gmail_pass)
            claim_outbound_intent(
                sms_intent["intent_id"],
                db_path=DB_PATH,
            )

            refused_recipients = {}
            for recipient in sms_recipients:
                msg = MIMEText(sms_body)
                msg["From"] = gmail_user
                msg["To"] = recipient
                msg["Subject"] = ""  # SMS gateway ignores subject
                refused = server.sendmail(
                    gmail_user, recipient, msg.as_string()
                ) or {}
                if refused:
                    refused_recipients.update(refused)

            if refused_recipients:
                print("[SMS] Gateway refused one or more recipients")
                record_outbound_intent_failure(
                    sms_intent["intent_id"],
                    reason="sms_gateway_recipient_refused",
                    db_path=DB_PATH,
                )
            else:
                complete_outbound_intent(
                    sms_intent["intent_id"],
                    verification_receipt=(
                        "sms_gateway_accepted_"
                        f"{sms_intent['intent_id'][:16]}"
                    ),
                    db_path=DB_PATH,
                )
                sms_delivered = True
                print("[SMS] ✅ Gateway accepted both management texts")
        except Exception as e:
            try:
                record_outbound_intent_failure(
                    sms_intent["intent_id"],
                    reason="sms_gateway_transport_failure",
                    db_path=DB_PATH,
                )
            except Exception:
                pass
            print(f"[SMS] Failed: {e}")
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

    # Also email both through Rachel's Google Workspace mailbox
    email_subject = f"{'🚨 URGENT — ' if urgent else ''}Rachel Update"
    email_delivered = bool(send_email(
        MANAGER_EMAIL,
        email_subject,
        f"{sms_body}\n\n— Rachel\nAssistant Property Manager\n"
        "ClientCo Property Management",
        also_owner=True,
        delivery_key=f"{durable_target}:management-email-copy",
    ))

    if sms_delivered and email_delivered:
        print(f"[SMS] Sent to Manager + Owner: {sms_body[:60]}")
        return True
    print(
        "[SMS] Management notification was not fully verified "
        f"sms_delivered={sms_delivered} email_delivered={email_delivered}"
    )
    return False


# ── Database ───────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def find_tenant(name: str):
    if not name:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM tenants WHERE primary_tenant = ?", (name,)
    ).fetchone()
    if not row:
        last = name.strip().split()[-1]
        row = conn.execute(
            "SELECT * FROM tenants WHERE primary_tenant LIKE ?",
            (f"%{last}%",)
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_tenant(occupancy_id: str, **kwargs):
    conn = get_db()
    for col, val in kwargs.items():
        try:
            conn.execute(
                f"UPDATE tenants SET {col} = ? WHERE occupancy_id = ?",
                (val, occupancy_id)
            )
        except Exception as e:
            print(f"[DB] Error: {e}")
    conn.commit()
    conn.close()


def mark_insurance_resolved(occupancy_id: str):
    conn = get_db()
    conn.execute(
        "UPDATE insurance_policies SET status = 'RESOLVED' WHERE occupancy_id = ?",
        (occupancy_id,)
    )
    conn.commit()
    conn.close()


def search_rachel_database(question: str, internal_user_id: str = "") -> str:
    """
    Search Rachel's local operational database for information relevant to
    a management question.

    This function is read-only. It does not modify AppFolio or Rachel's DB.
    """
    clean_question = (question or "").strip()

    if not clean_question:
        return "No question was provided."

    stop_words = {
        "about", "after", "again", "been", "being", "could", "does",
        "going", "have", "from", "into", "just", "know", "like", "more",
        "please", "should", "that", "their", "there", "these", "they",
        "this", "what", "when", "where", "which", "with", "work",
        "would", "your",
    }

    keywords = []

    for word in re.findall(r"[A-Za-z0-9'-]+", clean_question):
        normalized = word.strip().lower()

        if (
            len(normalized) >= 3
            and normalized not in stop_words
            and normalized not in keywords
        ):
            keywords.append(normalized)

    wo_match = re.search(
        r"(?:WO\s*#?|work\s*order\s*#?)([\d-]+)",
        clean_question,
        re.IGNORECASE,
    )

    exact_wo = wo_match.group(1) if wo_match else ""
    question_lower = clean_question.lower()

    recent_work_order_request = (
        any(
            phrase in question_lower
            for phrase in (
                "recent work orders",
                "latest work orders",
                "newest work orders",
                "most recent work orders",
                "current work orders",
            )
        )
        or (
            "work order" in question_lower
            and any(
                word in question_lower
                for word in ("recent", "latest", "newest", "current")
            )
        )
    )

    conn = get_db()
    sections = []
    internal_user = (
        get_internal_user_context(internal_user_id, DB_PATH)
        if internal_user_id
        else None
    )

    def visible_communications(rows):
        if not internal_user_id:
            return []
        visible = []
        for row in rows:
            record = dict(row)
            property_key = record.get("property_address")
            has_portfolio_access = internal_user_has_access(
                internal_user_id,
                permission="property_management",
                property_key=property_key or "*",
                db_path=DB_PATH,
            )
            if property_key and has_portfolio_access:
                visible.append(record)
            elif (
                not property_key
                and record.get("internal_user_id") == internal_user_id
            ):
                visible.append(record)
            elif not property_key and has_portfolio_access:
                visible.append(record)
        return visible

    def visible_operational_rows(rows):
        if not internal_user_id:
            return []
        visible = []
        for row in rows:
            record = dict(row)
            property_key = (
                record.get("address")
                or record.get("property_address")
                or "*"
            )
            if internal_user_has_access(
                internal_user_id,
                permission="property_management",
                property_key=property_key,
                db_path=DB_PATH,
            ):
                visible.append(record)
        return visible

    def visible_management_email_rows(rows):
        """Limit legacy email history to the user or an in-scope WO."""
        if not internal_user:
            return []
        user_email = str(internal_user.get("email") or "").lower()
        visible = []
        for row in rows:
            record = dict(row)
            related_wo = str(record.get("related_wo") or "").strip()
            if related_wo:
                wo_row = conn.execute(
                    "SELECT address FROM wo_scanner_state WHERE wo_num = ? "
                    "UNION ALL SELECT address FROM wo_triage WHERE wo_num = ? "
                    "LIMIT 1",
                    (related_wo, related_wo),
                ).fetchone()
                if wo_row and internal_user_has_access(
                    internal_user_id,
                    permission="property_management",
                    property_key=wo_row["address"],
                    db_path=DB_PATH,
                ):
                    visible.append(record)
                continue
            participants = {
                str(record.get("sender") or "").lower(),
                str(record.get("recipient") or "").lower(),
            }
            if user_email and user_email in participants:
                visible.append(record)
                continue
            if internal_user_has_access(
                internal_user_id,
                permission="property_management",
                property_key="*",
                db_path=DB_PATH,
            ):
                visible.append(record)
        return visible

    try:
        if recent_work_order_request:
            recent_rows = conn.execute(
                """
                SELECT
                    wo_num,
                    tenant,
                    address,
                    job_desc,
                    issue_type,
                    appfolio_status,
                    classification,
                    last_seen_at
                FROM wo_scanner_state
                ORDER BY last_seen_at DESC
                LIMIT 15
                """
            ).fetchall()
            visible_recent_rows = visible_operational_rows(recent_rows)

            return (
                "MOST RECENT WORK ORDERS:\n"
                + json.dumps(
                    visible_recent_rows,
                    default=str,
                    indent=2,
                )
                if visible_recent_rows
                else "No work orders were found in Rachel's database."
            )

        if exact_wo:
            scanner_row = conn.execute(
                """
                SELECT *
                FROM wo_scanner_state
                WHERE wo_num = ?
                """,
                (exact_wo,),
            ).fetchone()

            triage_row = conn.execute(
                """
                SELECT *
                FROM wo_triage
                WHERE wo_num = ?
                """,
                (exact_wo,),
            ).fetchone()

            if scanner_row and visible_operational_rows([scanner_row]):
                sections.append(
                    "SCANNER WORK ORDER RECORD:\n"
                    + json.dumps(
                        dict(scanner_row),
                        default=str,
                        indent=2,
                    )
                )

            if triage_row and visible_operational_rows([triage_row]):
                sections.append(
                    "TRIAGE WORK ORDER RECORD:\n"
                    + json.dumps(
                        dict(triage_row),
                        default=str,
                        indent=2,
                    )
                )

            communication_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'communications'"
            ).fetchone()
            if communication_table_exists:
                communication_rows = conn.execute(
                    """
                    SELECT * FROM communications
                    WHERE related_wo = ?
                    ORDER BY occurred_at DESC LIMIT 50
                    """,
                    (exact_wo,),
                ).fetchall()
                visible_rows = visible_communications(communication_rows)
                if visible_rows:
                    sections.append(
                        "COMMUNICATION HISTORY:\n"
                        + json.dumps(visible_rows, default=str, indent=2)
                    )

            audit_table_exists = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'management_email_audit'
                """
            ).fetchone()

            if audit_table_exists:
                audit_rows = conn.execute(
                    """
                    SELECT
                        direction,
                        sender,
                        recipient,
                        subject,
                        body,
                        related_wo,
                        classification,
                        recorded_at
                    FROM management_email_audit
                    WHERE related_wo = ?
                    ORDER BY recorded_at DESC
                    LIMIT 30
                    """,
                    (exact_wo,),
                ).fetchall()

                audit_rows = visible_management_email_rows(audit_rows)
                if audit_rows:
                    sections.append(
                        "MANAGEMENT EMAIL HISTORY:\n"
                        + json.dumps(
                            [dict(row) for row in audit_rows],
                            default=str,
                            indent=2,
                        )
                    )

            return (
                "\n\n".join(sections)
                if sections
                else (
                    f"No stored records were found for "
                    f"Work Order #{exact_wo}."
                )
            )

        search_terms = keywords[:8]

        if search_terms:
            like_values = [f"%{term}%" for term in search_terms]

            # Render may already have an older tenants table that predates
            # newer columns such as all_tenants. Search only columns that exist.
            tenant_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(tenants)").fetchall()
            }
            searchable_tenant_columns = [
                column
                for column in (
                    "primary_tenant",
                    "all_tenants",
                    "property_address",
                    "unit",
                    "notes",
                )
                if column in tenant_columns
            ]

            tenant_conditions = []
            tenant_params = []

            for value in like_values:
                column_conditions = [
                    f"LOWER(COALESCE({column}, '')) LIKE ?"
                    for column in searchable_tenant_columns
                ]
                if column_conditions:
                    tenant_conditions.append(
                        "(" + " OR ".join(column_conditions) + ")"
                    )
                    tenant_params.extend(
                        [value] * len(searchable_tenant_columns)
                    )

            tenant_order = "rowid DESC"
            for candidate in ("last_scraped", "updated_at", "created_at"):
                if candidate in tenant_columns:
                    tenant_order = f"{candidate} DESC"
                    break

            tenant_rows = conn.execute(
                f"""
                SELECT *
                FROM tenants
                WHERE {" OR ".join(tenant_conditions)}
                ORDER BY {tenant_order}
                LIMIT 10
                """,
                tenant_params,
            ).fetchall()

            tenant_rows = visible_operational_rows(tenant_rows)
            if tenant_rows:
                sections.append(
                    "TENANT AND LEASE RECORDS:\n"
                    + json.dumps(
                        [dict(row) for row in tenant_rows],
                        default=str,
                        indent=2,
                    )
                )

            scanner_conditions = []
            scanner_params = []

            for value in like_values:
                scanner_conditions.append(
                    """
                    (
                        LOWER(wo_num) LIKE ?
                        OR LOWER(tenant) LIKE ?
                        OR LOWER(address) LIKE ?
                        OR LOWER(job_desc) LIKE ?
                        OR LOWER(issue_type) LIKE ?
                        OR LOWER(issue_details) LIKE ?
                        OR LOWER(appfolio_status) LIKE ?
                        OR LOWER(last_actions_log) LIKE ?
                    )
                    """
                )
                scanner_params.extend([value] * 8)

            scanner_rows = conn.execute(
                f"""
                SELECT *
                FROM wo_scanner_state
                WHERE {" OR ".join(scanner_conditions)}
                ORDER BY last_seen_at DESC
                LIMIT 15
                """,
                scanner_params,
            ).fetchall()
            scanner_rows = visible_operational_rows(scanner_rows)

            if scanner_rows:
                sections.append(
                    "WORK ORDER SCANNER RECORDS:\n"
                    + json.dumps(
                        [dict(row) for row in scanner_rows],
                        default=str,
                        indent=2,
                    )
                )

            triage_conditions = []
            triage_params = []

            for value in like_values:
                triage_conditions.append(
                    """
                    (
                        LOWER(wo_num) LIKE ?
                        OR LOWER(tenant) LIKE ?
                        OR LOWER(address) LIKE ?
                        OR LOWER(job_desc) LIKE ?
                        OR LOWER(issue_type) LIKE ?
                        OR LOWER(status) LIKE ?
                        OR LOWER(conversation) LIKE ?
                        OR LOWER(vendor_name) LIKE ?
                    )
                    """
                )
                triage_params.extend([value] * 8)

            triage_rows = conn.execute(
                f"""
                SELECT *
                FROM wo_triage
                WHERE {" OR ".join(triage_conditions)}
                ORDER BY updated_at DESC
                LIMIT 15
                """,
                triage_params,
            ).fetchall()
            triage_rows = visible_operational_rows(triage_rows)

            if triage_rows:
                sections.append(
                    "WORK ORDER TRIAGE RECORDS:\n"
                    + json.dumps(
                        [dict(row) for row in triage_rows],
                        default=str,
                        indent=2,
                    )
                )

            communication_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'communications'"
            ).fetchone()
            if communication_table_exists:
                communication_conditions = []
                communication_params = []
                for value in like_values:
                    communication_conditions.append(
                        """
                        (
                            LOWER(contact_name) LIKE ? OR LOWER(contact_role) LIKE ?
                            OR LOWER(property_address) LIKE ? OR LOWER(unit) LIKE ?
                            OR LOWER(message_text) LIKE ? OR LOWER(preview) LIKE ?
                            OR LOWER(summary) LIKE ? OR LOWER(topic) LIKE ?
                            OR LOWER(related_wo) LIKE ? OR LOWER(related_tenant) LIKE ?
                            OR LOWER(related_vendor) LIKE ?
                        )
                        """
                    )
                    communication_params.extend([value] * 11)
                communication_rows = conn.execute(
                    f"""
                    SELECT * FROM communications
                    WHERE {" OR ".join(communication_conditions)}
                    ORDER BY occurred_at DESC LIMIT 50
                    """,
                    communication_params,
                ).fetchall()
                visible_rows = visible_communications(communication_rows)
                if visible_rows:
                    sections.insert(
                        0,
                        "DURABLE COMMUNICATION MEMORY:\n"
                        + json.dumps(visible_rows, default=str, indent=2),
                    )

            audit_table_exists = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'management_email_audit'
                """
            ).fetchone()

            if audit_table_exists:
                audit_conditions = []
                audit_params = []

                for value in like_values:
                    audit_conditions.append(
                        """
                        (
                            LOWER(subject) LIKE ?
                            OR LOWER(body) LIKE ?
                            OR LOWER(sender) LIKE ?
                            OR LOWER(recipient) LIKE ?
                            OR LOWER(related_wo) LIKE ?
                            OR LOWER(classification) LIKE ?
                        )
                        """
                    )
                    audit_params.extend([value] * 6)

                if exact_wo:
                    audit_conditions.append(
                        "related_wo = ?"
                    )
                    audit_params.append(exact_wo)

                if audit_conditions:
                    audit_rows = conn.execute(
                        f"""
                        SELECT
                            direction,
                            sender,
                            recipient,
                            subject,
                            body,
                            related_wo,
                            classification,
                            recorded_at
                        FROM management_email_audit
                        WHERE {" OR ".join(audit_conditions)}
                        ORDER BY recorded_at DESC
                        LIMIT 20
                        """,
                        audit_params,
                    ).fetchall()

                    audit_rows = visible_management_email_rows(audit_rows)
                    if audit_rows:
                        sections.append(
                            "MANAGEMENT EMAIL HISTORY:\n"
                            + json.dumps(
                                [dict(row) for row in audit_rows],
                                default=str,
                                indent=2,
                            )
                        )

        if not sections:
            recent_rows = conn.execute(
                """
                SELECT
                    wo_num,
                    tenant,
                    address,
                    job_desc,
                    appfolio_status,
                    classification,
                    last_seen_at
                FROM wo_scanner_state
                ORDER BY last_seen_at DESC
                LIMIT 10
                """
            ).fetchall()
            recent_rows = visible_operational_rows(recent_rows)

            if recent_rows:
                sections.append(
                    "RECENT WORK ORDERS FOR GENERAL CONTEXT:\n"
                    + json.dumps(
                        [dict(row) for row in recent_rows],
                        default=str,
                        indent=2,
                    )
                )

    finally:
        conn.close()

    return (
        "\n\n".join(sections)
        if sections
        else "No relevant records were found in Rachel's local database."
    )


# ── IMAP ───────────────────────────────────────────────────────────────────

def connect_imap():
    if not RACHEL_IMAP_PASS:
        print("[Inbox] No Rachel email password — check RACHEL_EMAIL_PASSWORD")
        return None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(RACHEL_IMAP_USER, RACHEL_IMAP_PASS)
        return mail
    except Exception as e:
        print(f"[Inbox] IMAP connect failed: {e}")
        return None


def _decode_email_part(part) -> str:
    """
    Decode a MIME email part using its declared charset.

    Gmail, Outlook, Apple Mail, and AppFolio-generated messages may use
    UTF-8, Windows-1252, ISO-8859-1, or another declared encoding.
    """
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None

    if payload is None:
        raw_payload = part.get_payload()

        if isinstance(raw_payload, str):
            return raw_payload

        return ""

    charset_candidates = [
        part.get_content_charset(),
        "utf-8",
        "windows-1252",
        "iso-8859-1",
    ]

    attempted = set()

    for charset in charset_candidates:
        if not charset:
            continue

        normalized = charset.lower()

        if normalized in attempted:
            continue

        attempted.add(normalized)

        try:
            return payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue

    return payload.decode("utf-8", errors="replace")


def _html_email_to_text(html_body: str) -> str:
    """
    Convert an HTML email into readable text while excluding quoted history.

    This supports HTML generated by Gmail, Apple Mail, Outlook, Yahoo,
    AppFolio, and other standards-compliant email clients.
    """
    from html import unescape
    from html.parser import HTMLParser

    class EmailHTMLParser(HTMLParser):
        BLOCK_TAGS = {
            "address",
            "article",
            "aside",
            "body",
            "div",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "p",
            "section",
            "table",
            "td",
            "th",
            "tr",
        }

        VOID_TAGS = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }

        QUOTE_MARKERS = {
            "appendonsend",
            "divrplyfwdmsg",
            "gmail_attr",
            "gmail_quote",
            "gmail_extra",
            "moz-cite-prefix",
            "protonmail_quote",
            "replyforward",
            "yahoo_quoted",
        }

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.output = []
            self.suppressed_depth = 0

        def _append_line_break(self):
            if not self.output or self.output[-1] != "\n":
                self.output.append("\n")

        def _is_quoted_container(self, tag, attrs):
            if tag.lower() == "blockquote":
                return True

            attr_values = []

            for name, value in attrs:
                if name.lower() in {"id", "class", "name"} and value:
                    attr_values.append(str(value).lower())

            marker_text = " ".join(attr_values)

            return any(
                marker in marker_text
                for marker in self.QUOTE_MARKERS
            )

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()

            if self.suppressed_depth:
                if tag not in self.VOID_TAGS:
                    self.suppressed_depth += 1
                return

            if tag in {"head", "script", "style", "title"}:
                self.suppressed_depth = 1
                return

            if self._is_quoted_container(tag, attrs):
                self.suppressed_depth = 1
                return

            if tag == "br":
                self._append_line_break()
            elif tag in self.BLOCK_TAGS:
                self._append_line_break()

        def handle_startendtag(self, tag, attrs):
            if self.suppressed_depth:
                return

            if tag.lower() in {"br", "hr"}:
                self._append_line_break()

        def handle_endtag(self, tag):
            tag = tag.lower()

            if self.suppressed_depth:
                self.suppressed_depth -= 1
                return

            if tag in self.BLOCK_TAGS:
                self._append_line_break()

        def handle_data(self, data):
            if not self.suppressed_depth and data:
                self.output.append(data)

        def get_text(self):
            return "".join(self.output)

    parser = EmailHTMLParser()

    try:
        parser.feed(html_body or "")
        parser.close()
        value = parser.get_text()
    except Exception:
        # Last-resort conversion for severely malformed HTML.
        value = re.sub(
            r"(?is)<(?:script|style|head)\b.*?>.*?</(?:script|style|head)>",
            "",
            html_body or "",
        )
        value = re.sub(
            r"(?is)<blockquote\b.*?>.*?</blockquote>",
            "",
            value,
        )
        value = re.sub(r"(?i)<br\s*/?>", "\n", value)
        value = re.sub(
            r"(?i)</(?:div|p|li|tr|h[1-6])\s*>",
            "\n",
            value,
        )
        value = re.sub(r"(?is)<[^>]+>", "", value)

    value = unescape(value)
    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")
    value = value.replace("\ufeff", "")

    return value


def _remove_quoted_email_history(body: str) -> str:
    """Remove earlier messages included below the sender's current reply."""
    quote_patterns = (
        # Gmail and Apple Mail: On [date], [person] wrote:
        r"(?im)^\s*On .{1,500}\bwrote:\s*$",

        # Common forwarded-message separators.
        r"(?im)^\s*-{2,}\s*(?:Original|Forwarded) Message\s*-{2,}\s*$",
        r"(?im)^\s*Begin forwarded message:\s*$",
        r"(?im)^\s*Forwarded message\s*$",

        # Outlook header block.
        (
            r"(?ims)^\s*From:\s+.+?\n"
            r"\s*(?:Sent|Date):\s+.+?\n"
            r"\s*To:\s+.+?\n"
            r"(?:\s*Cc:\s+.+?\n)?"
            r"\s*Subject:\s+.+?$"
        ),

        # Common AppFolio or automated email separators.
        r"(?im)^\s*_{5,}\s*$",
    )

    earliest_match = None

    for pattern in quote_patterns:
        match = re.search(pattern, body)

        if match and (
            earliest_match is None
            or match.start() < earliest_match.start()
        ):
            earliest_match = match

    if earliest_match:
        body = body[:earliest_match.start()]

    # A remaining line beginning with ">" usually starts quoted plain text.
    cleaned_lines = []

    for line in body.splitlines():
        if re.match(r"^\s*>", line):
            break

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def _remove_email_signature(body: str) -> str:
    """
    Remove a clearly identifiable email signature without deleting normal
    tenant statements that happen to contain words such as "thanks."
    """
    lines = body.splitlines()

    for index, line in enumerate(lines):
        stripped = line.strip()

        # Standard RFC signature delimiter.
        if stripped in {"--", "-- "}:
            return "\n".join(lines[:index]).strip()

        # Mobile-client signature.
        if re.fullmatch(
            r"(?i)sent from my (?:iphone|ipad|android|mobile device)",
            stripped,
        ):
            return "\n".join(lines[:index]).strip()

        # Sign-off followed by recognizable contact information.
        if not re.fullmatch(
            r"(?i)(thank you|thanks|best|best regards|regards|"
            r"sincerely|kind regards|warm regards),?",
            stripped,
        ):
            continue

        signature_preview = "\n".join(lines[index + 1:index + 12])

        has_contact_information = re.search(
            r"(?i)("
            r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
            r"https?://|"
            r"www\.|"
            r"\b(?:tel|phone|mobile|cell|fax|direct|ext)\b|"
            r"\b\d{3}[-().\s]+\d{3}[-.\s]+\d{4}\b|"
            r"\bDRE\s*#?"
            r")",
            signature_preview,
        )

        if has_contact_information:
            return "\n".join(lines[:index]).strip()

    return body.strip()


def _clean_email_reply(body: str) -> str:
    """Normalize an extracted email body into the sender's current reply."""
    body = body or ""

    body = body.replace("\r\n", "\n")
    body = body.replace("\r", "\n")
    body = body.replace("\x00", "")
    body = body.replace("\u200b", "")
    body = body.replace("\ufeff", "")

    body = _remove_quoted_email_history(body)
    body = _remove_email_signature(body)

    # Remove trailing whitespace without destroying paragraph boundaries.
    lines = [line.rstrip() for line in body.splitlines()]
    body = "\n".join(lines)

    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n[ \t]+", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)

    return body.strip()


def get_body(msg) -> str:
    """
    Extract the sender's current reply from common email formats.

    Parsing order:
      1. Prefer a readable text/plain MIME part.
      2. Fall back to text/html when plain text is absent or blank.
      3. Ignore attachments and inline image data.
      4. Remove quoted history and recognizable signatures.
    """
    plain_candidates = []
    html_candidates = []

    parts = msg.walk() if msg.is_multipart() else [msg]

    for part in parts:
        if part.is_multipart():
            continue

        content_type = part.get_content_type().lower()
        disposition = (
            part.get_content_disposition()
            or ""
        ).lower()
        filename = part.get_filename()

        # Never treat an attached document, image, or forwarded file as the
        # tenant's written reply.
        if disposition == "attachment":
            continue

        if filename and content_type not in {
            "text/plain",
            "text/html",
        }:
            continue

        if content_type not in {
            "text/plain",
            "text/html",
        }:
            continue

        decoded = _decode_email_part(part)

        if not decoded or not decoded.strip():
            continue

        if content_type == "text/plain":
            plain_candidates.append(decoded)
        else:
            html_candidates.append(decoded)

    # Prefer plain text, but only when it produces an actual readable reply.
    for candidate in plain_candidates:
        cleaned = _clean_email_reply(candidate)

        if cleaned:
            return cleaned

    # Gmail, Apple Mail, Outlook, or AppFolio may provide HTML only.
    for candidate in html_candidates:
        converted = _html_email_to_text(candidate)
        cleaned = _clean_email_reply(converted)

        if cleaned:
            return cleaned

    return ""


def decode_subject(msg) -> str:
    raw = msg.get("Subject", "")
    parts = decode_header(raw)
    subject = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            subject += part.decode(enc or "utf-8", errors="ignore")
        else:
            subject += str(part)
    # msg.get("Subject") returns RFC 5322 folded headers verbatim — a long
    # subject line legitimately contains a raw "\r\n " continuation, which
    # Python's classic email.message.Message API never unfolds on read.
    # Left as-is, that embedded CR/LF later trips smtplib's header-injection
    # guard the moment this subject is reused in any outgoing email, and
    # since that raise happens before the source email is ever marked
    # processed, it gets retried — and re-crashes — on every future poll.
    return re.sub(r"\s+", " ", subject).strip()


def get_sender_email(sender_raw: str) -> str:
    m = re.search(r"<(.+?)>", sender_raw)
    return m.group(1).lower() if m else sender_raw.strip().lower()


def _email_dedupe_id(email_data: dict) -> str:
    """
    Return a permanent identifier for an incoming email.

    IMAP sequence numbers can change or be reused as mailbox contents
    change, so the RFC Message-ID header is used whenever available.
    """
    message_id = (
        email_data.get("message_id_header")
        or ""
    ).strip()

    if message_id:
        return f"message-id:{message_id}"

    fallback = email_data.get("msg_id", b"")

    if isinstance(fallback, bytes):
        fallback = fallback.decode(errors="replace")

    return f"imap-sequence:{fallback}"


def _get_processed_ids():
    """Get set of email message IDs we've already processed."""
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_emails (
        msg_uid TEXT PRIMARY KEY, processed_at TEXT)""")
    conn.commit()
    rows = conn.execute("SELECT msg_uid FROM processed_emails").fetchall()
    conn.close()
    return set(r[0] for r in rows)


def _mark_processed(msg_uid: str):
    """Mark an email as processed so we don't handle it twice."""
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_emails (
        msg_uid TEXT PRIMARY KEY, processed_at TEXT)""")
    conn.execute("INSERT OR IGNORE INTO processed_emails VALUES (?, ?)",
                 (msg_uid, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def _ensure_runtime_state_table(conn) -> None:
    """Create persistent timestamps for recurring Rachel jobs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rachel_runtime_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT,
            updated_at TEXT
        )
        """
    )


def _work_order_reconciliation_due() -> bool:
    """
    Return True when no successful Work Order scan has run during the
    configured interval. The default is twice daily.
    """
    try:
        interval_minutes = max(
            1,
            int(os.getenv("RACHEL_WO_RECONCILIATION_MINUTES", "720")),
        )
    except ValueError:
        interval_minutes = 720

    conn = get_db()
    try:
        _ensure_runtime_state_table(conn)
        conn.commit()

        row = conn.execute(
            """
            SELECT state_value
            FROM rachel_runtime_state
            WHERE state_key IN (
                'last_successful_wo_reconciliation',
                'last_successful_work_order_scan'
            )
            ORDER BY CASE state_key
                WHEN 'last_successful_wo_reconciliation' THEN 0 ELSE 1 END
            LIMIT 1
            """
        ).fetchone()
        retry_row = conn.execute(
            """
            SELECT state_value
            FROM rachel_runtime_state
            WHERE state_key = 'wo_reconciliation_retry_not_before'
            """
        ).fetchone()
    finally:
        conn.close()

    if retry_row and retry_row["state_value"]:
        try:
            retry_not_before = datetime.fromisoformat(retry_row["state_value"])
            if retry_not_before.tzinfo is None:
                retry_not_before = retry_not_before.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < retry_not_before:
                print(
                    "[ReconciliationSchedule] Failure backoff active until "
                    f"{retry_not_before.isoformat()}."
                )
                return False
        except (TypeError, ValueError):
            pass

    if not row or not row["state_value"]:
        print(
            f"[ReconciliationSchedule] No prior successful reconciliation found; "
            f"reconciliation is due now. Interval={interval_minutes} minute(s)."
        )
        return True

    try:
        last_scan = datetime.fromisoformat(row["state_value"])
        if last_scan.tzinfo is None:
            last_scan = last_scan.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        print(
            "[ReconciliationSchedule] Saved timestamp is invalid; "
            "reconciliation is due now."
        )
        return True

    elapsed = datetime.now(timezone.utc) - last_scan
    due = elapsed >= timedelta(minutes=interval_minutes)

    print(
        f"[ReconciliationSchedule] Last successful reconciliation: "
        f"{last_scan.isoformat()} | "
        f"Elapsed: {elapsed.total_seconds() / 60:.1f} minute(s) | "
        f"Interval: {interval_minutes} minute(s) | Due: {due}"
    )

    return due


def _mark_work_order_reconciliation_completed() -> None:
    """Persist the completion time only after a successful scan."""
    completed_at = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    try:
        _ensure_runtime_state_table(conn)
        conn.execute(
            """
            INSERT INTO rachel_runtime_state (
                state_key,
                state_value,
                updated_at
            )
            VALUES (
                'last_successful_wo_reconciliation',
                ?,
                ?
            )
            ON CONFLICT(state_key) DO UPDATE SET
                state_value = excluded.state_value,
                updated_at = excluded.updated_at
            """,
            (completed_at, completed_at),
        )
        conn.execute(
            "DELETE FROM rachel_runtime_state "
            "WHERE state_key = 'wo_reconciliation_retry_not_before'"
        )
        conn.commit()
    finally:
        conn.close()

    print(
        "[Reconciliation] Successful timestamp saved. "
        "Next reconciliation is due in approximately 12 hours."
    )


def _mark_work_order_reconciliation_failed() -> None:
    """Persist scan retry backoff so Gmail polling cannot cause a retry storm."""
    try:
        backoff_minutes = max(
            5,
            int(os.getenv("RACHEL_WO_RECONCILIATION_BACKOFF_MINUTES", "30")),
        )
    except ValueError:
        backoff_minutes = 30
    now = datetime.now(timezone.utc)
    retry_at = now + timedelta(minutes=backoff_minutes)
    conn = get_db()
    try:
        _ensure_runtime_state_table(conn)
        conn.execute(
            """
            INSERT INTO rachel_runtime_state (state_key, state_value, updated_at)
            VALUES ('wo_reconciliation_retry_not_before', ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                state_value = excluded.state_value,
                updated_at = excluded.updated_at
            """,
            (retry_at.isoformat(), now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"[ReconciliationSchedule] Failure backoff until {retry_at.isoformat()}.")


def _appfolio_inbox_poll_due() -> bool:
    """Keep AppFolio inbox polling independent from one-minute Gmail checks."""
    try:
        interval_minutes = max(
            5,
            int(os.getenv("RACHEL_APPFOLIO_INBOX_POLL_MINUTES", "10")),
        )
    except ValueError:
        interval_minutes = 10
    conn = get_db()
    try:
        _ensure_runtime_state_table(conn)
        conn.commit()
        row = conn.execute(
            "SELECT state_value FROM rachel_runtime_state "
            "WHERE state_key = 'last_successful_appfolio_inbox_poll'"
        ).fetchone()
        retry_row = conn.execute(
            "SELECT state_value FROM rachel_runtime_state "
            "WHERE state_key = 'appfolio_inbox_retry_not_before'"
        ).fetchone()
    finally:
        conn.close()
    if retry_row and retry_row["state_value"]:
        try:
            retry_not_before = datetime.fromisoformat(retry_row["state_value"])
            if retry_not_before.tzinfo is None:
                retry_not_before = retry_not_before.replace(
                    tzinfo=timezone.utc
                )
            if datetime.now(timezone.utc) < retry_not_before:
                return False
        except (TypeError, ValueError):
            pass
    if not row or not row["state_value"]:
        return True
    try:
        last_poll = datetime.fromisoformat(row["state_value"])
        if last_poll.tzinfo is None:
            last_poll = last_poll.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - last_poll >= timedelta(
        minutes=interval_minutes
    )


def _mark_appfolio_inbox_poll_completed() -> None:
    completed_at = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        _ensure_runtime_state_table(conn)
        conn.execute(
            """
            INSERT INTO rachel_runtime_state (state_key, state_value, updated_at)
            VALUES ('last_successful_appfolio_inbox_poll', ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                state_value = excluded.state_value,
                updated_at = excluded.updated_at
            """,
            (completed_at, completed_at),
        )
        conn.execute(
            "DELETE FROM rachel_runtime_state "
            "WHERE state_key = 'appfolio_inbox_retry_not_before'"
        )
        conn.commit()
    finally:
        conn.close()


def _mark_appfolio_inbox_poll_failed() -> None:
    """Persist failure backoff so the one-minute loop cannot retry-storm."""
    try:
        backoff_minutes = max(
            5,
            int(os.getenv("RACHEL_APPFOLIO_INBOX_BACKOFF_MINUTES", "15")),
        )
    except ValueError:
        backoff_minutes = 15
    now = datetime.now(timezone.utc)
    retry_at = now + timedelta(minutes=backoff_minutes)
    conn = get_db()
    try:
        _ensure_runtime_state_table(conn)
        conn.execute(
            """
            INSERT INTO rachel_runtime_state (state_key, state_value, updated_at)
            VALUES ('appfolio_inbox_retry_not_before', ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                state_value = excluded.state_value,
                updated_at = excluded.updated_at
            """,
            (retry_at.isoformat(), now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def record_management_email_audit(
    direction: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    message_uid: str = "",
    related_wo: str = "",
    classification: str = "",
):
    """
    Permanently record an email between Rachel and management.

    direction must be either:
    - inbound
    - outbound
    """
    normalized_direction = (direction or "").strip().lower()

    if normalized_direction not in {"inbound", "outbound"}:
        raise ValueError(
            "Management email direction must be inbound or outbound."
        )

    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS management_email_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_uid TEXT,
            direction TEXT NOT NULL,
            sender TEXT,
            recipient TEXT,
            subject TEXT,
            body TEXT,
            related_wo TEXT,
            classification TEXT,
            recorded_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        INSERT INTO management_email_audit (
            message_uid,
            direction,
            sender,
            recipient,
            subject,
            body,
            related_wo,
            classification,
            recorded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_uid or "",
            normalized_direction,
            sender or "",
            recipient or "",
            subject or "",
            body or "",
            related_wo or "",
            classification or "",
            datetime.now().isoformat(),
        ),
    )

    conn.commit()
    conn.close()

    management_address = (
        sender if normalized_direction == "inbound" else recipient
    )
    management_user = resolve_internal_user(management_address, DB_PATH)
    internal_user_id = (management_user or {}).get("user_id", "")
    store_communication(
        {
            "source": "gmail",
            "source_message_id": message_uid,
            "contact_name": sender if normalized_direction == "inbound" else recipient,
            "contact_role": "internal_user",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "direction": normalized_direction,
            "message_text": body,
            "preview": subject,
            "topic": classification,
            "related_wo": related_wo,
            "internal_user_id": internal_user_id,
            "processing_status": "stored",
            "metadata": {"subject": subject},
        },
        DB_PATH,
    )


def fetch_unread_emails(mail) -> list:
    mail.select("inbox")
    # Search every unread message, not a calendar lookback. A date-limited
    # query silently loses messages after multi-day runtime downtime. Durable
    # Message-ID state below suppresses duplicates after restart.
    status, messages = mail.search(None, "UNSEEN")
    print(
        f"[Inbox] IMAP search UNSEEN: status={status}, "
        f"count={len(messages[0].split()) if messages[0] else 0}"
    )
    
    if status != "OK" or not messages[0]:
        print("[Inbox] No emails found")
        return []
    
    emails = []
    for msg_id in messages[0].split():
        # BODY.PEEK[] (not RFC822) so reading the message does not itself
        # set \Seen. A message whose handler fails below must stay UNSEEN
        # so the next UNSEEN-based poll finds it again; mark_read() below
        # is the only thing that should ever set \Seen, and only on success.
        status, data = mail.fetch(msg_id, "(BODY.PEEK[])")
        if status != "OK":
            continue
        msg = email.message_from_bytes(data[0][1])
        sender_raw = msg.get("From", "")

        # AppFolio's mailer relay delivers a copy of Rachel's own just-sent
        # Notify-Tenant email back into this same inbox (confirmed live
        # Sep 2 2026: "From: Rachel Asst <communications@...>, To:
        # <the tenant's real address>" sitting as unseen in Rachel's own
        # mailbox). A genuine tenant/vendor reply is always addressed To
        # Rachel's own address instead. Left unfiltered, these self-echoes
        # get misread as the tenant answering - even as answering with
        # Rachel's own just-sent question text - and Rachel ends up
        # replying to herself in a runaway loop.
        to_header = str(msg.get("To", "") or "").lower()
        if RACHEL_IMAP_USER.lower() not in to_header:
            print(
                "[Inbox] Skipping self-addressed echo (not sent to "
                f"{RACHEL_IMAP_USER}): {sender_raw[:60]}"
            )
            mark_read(mail, msg_id)
            continue

        # Extract image attachments (for photo analysis)
        images = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("image/jpeg", "image/png", "image/webp",
                             "image/gif"):
                    try:
                        import base64 as _b64
                        payload = part.get_payload(decode=True)
                        if payload and len(payload) < 5_000_000:
                            images.append({
                                "media_type": ctype,
                                "data": _b64.b64encode(payload).decode()})
                    except Exception:
                        pass

        emails.append({
            "msg_id": msg_id,
            "sender_raw": sender_raw,
            "sender_email": get_sender_email(sender_raw),
            "subject": decode_subject(msg),
            "body": get_body(msg),
            "message_id_header": msg.get("Message-ID", "").strip(),
            "references_header": msg.get("References", "").strip(),
            "images": images,
        })
    return emails


def mark_read(mail, msg_id):
    mail.store(msg_id, "+FLAGS", "\\Seen")


def delete_email(mail, msg_id):
    mail.store(msg_id, "+FLAGS", "\\Deleted")
    mail.expunge()


# ── Handlers ───────────────────────────────────────────────────────────────

def interpret_manager_review_reply(reply_text: str) -> str:
    """
    Convert Manager's natural-language Work Order reply into one of Rachel's
    internal review instructions:

    - PROCEED
    - HOLD
    - HANDLED
    - OTHER: specific instructions

    Exact commands are handled without an AI call. Natural-language replies
    are interpreted by Claude.
    """
    clean_reply = (reply_text or "").strip()

    if not clean_reply:
        return ""

    first_line = next(
        (
            line.strip()
            for line in clean_reply.splitlines()
            if line.strip()
            and not line.strip().startswith(">")
        ),
        "",
    )

    upper_line = first_line.upper()

    # Fast, deterministic handling for Rachel's standard commands.
    if upper_line.startswith("PROCEED"):
        if upper_line == "PROCEED":
            return "PROCEED"

        # Preserve authorized conditions or additional tenant directions.
        return f"PROCEED_WITH: {first_line}"

    if upper_line.startswith("HOLD"):
        if upper_line == "HOLD":
            return "HOLD"

        return f"OTHER: {first_line}"

    if upper_line.startswith("HANDLED"):
        return "HANDLED"

    if upper_line.startswith("OTHER:"):
        return first_line

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            system="""You interpret management instructions from Manager,
the Regional Property Manager, concerning one specific Work Order.

Return exactly one of these formats:

PROCEED
PROCEED_WITH: [complete concise tenant-triage instruction]
HOLD
HANDLED
OTHER: [complete concise non-triage instruction]
UNSURE

Rules:
- PROCEED only when Manager clearly authorizes Rachel to begin the normal
  tenant-triage workflow without conditions or modifications.
- PROCEED_WITH when Manager authorizes tenant contact but adds a condition,
  modified first question, or specific tenant-triage direction.
- HOLD when Manager wants Rachel to wait or take no action.
- HANDLED when Manager says she or someone else already handled the matter
  and Rachel should take no further action.
- OTHER when Manager gives conditions, modifications, questions, vendor
  directions, timing instructions, or any instruction beyond normal triage.
- Preserve all important details in an OTHER instruction.
- UNSURE when her intent cannot be determined safely.
- Do not explain your answer.""",
            messages=[{
                "role": "user",
                "content": clean_reply[:2000],
            }],
        )

        interpreted = response.content[0].text.strip()
        interpreted_upper = interpreted.upper()

        if interpreted_upper in {"PROCEED", "HOLD", "HANDLED"}:
            return interpreted_upper

        if interpreted_upper.startswith("PROCEED_WITH:"):
            return interpreted

        if interpreted_upper.startswith("OTHER:"):
            return interpreted

        print(
            "[ManagerReview] Natural-language instruction was unclear: "
            f"{interpreted}"
        )
        return ""

    except Exception as e:
        print(
            "[ManagerReview] Instruction interpretation failed: "
            f"{e}"
        )
        return ""


def handle_manager_or_owner(email_data: dict):
    """
    Handle emails from Manager or Owner.
    Rachel first tries to respond directly (simple conversation, hello, etc.)
    If it's a PM action (moving out, renewal, etc.) she updates the DB.
    If she's unsure, she asks for clarification.
    """
    subject      = email_data["subject"]
    body         = email_data["body"]
    sender_email = email_data["sender_email"]

    print(f"[Manager/Owner] From {sender_email}: {subject[:60]}")

    body_stripped = body.strip()

    qa_tenant_match = re.search(
        r"Rachel QA Tenant\s*[—-]\s*WO\s*#?"
        r"([A-Za-z0-9-]+)",
        subject,
        re.IGNORECASE,
    )

    if (
        OWNER_QA_MODE
        and sender_email.strip().lower() == OWNER_EMAIL.lower()
        and qa_tenant_match
        and qa_tenant_match.group(1).upper().startswith("QA-")
    ):
        from rachel_wo_triage import handle_tenant_triage_reply

        qa_wo_num = qa_tenant_match.group(1)

        print(
            f"[OwnerQA] Routing Owner's reply as synthetic tenant "
            f"for WO #{qa_wo_num}."
        )

        return handle_tenant_triage_reply(
            qa_wo_num,
            body_stripped,
            images=email_data.get("images"),
            channel="email",
            thread_message_id=email_data.get(
                "message_id_header",
                "",
            ),
            thread_references=email_data.get(
                "references_header",
                "",
            ),
        )

    inbound_wo_match = re.search(
        r"(?:WO\s*#?|work\s*order\s*#?)([\d-]+)",
        f"{subject} {body_stripped}",
        re.IGNORECASE,
    )

    record_management_email_audit(
        direction="inbound",
        sender=sender_email,
        recipient=RACHEL_EMAIL,
        subject=subject,
        body=body_stripped,
        message_uid=_email_dedupe_id(email_data),
        related_wo=(
            inbound_wo_match.group(1)
            if inbound_wo_match
            else ""
        ),
        classification="management_email_received",
    )

    # Property-level rent-control review replies.
    # These must be processed before Rachel's generic management
    # conversation and work-order routing.
    try:
        from rachel_property_review import (
            apply_review_answers_to_sheet,
            find_review_case_from_email,
            record_property_review_reply,
        )

        property_review_case = find_review_case_from_email(
            subject=subject,
            body=body_stripped,
        )

    except Exception as exc:
        property_review_case = None
        print(
            "[PropertyReview] Could not inspect management email: "
            f"{type(exc).__name__}: {exc}"
        )

    if property_review_case:
        case_id = property_review_case["case_id"]
        property_address = property_review_case[
            "property_address"
        ]

        authorized_property_review_senders = {
            MANAGER_EMAIL.strip().lower(),
            OWNER_EMAIL.strip().lower(),
        }

        if (
            sender_email.strip().lower()
            not in authorized_property_review_senders
        ):
            print(
                f"[PropertyReview] Ignored response for {case_id} "
                "from an unauthorized sender."
            )
            return True

        try:
            review_result = record_property_review_reply(
                client=client,
                case_id=case_id,
                reply_text=body_stripped,
                answered_by=sender_email,
            )

            if not review_result.get("processed"):
                send_email(
                    MANAGER_EMAIL,
                    f"Re: {subject}",
                    (
                        "I found the property-review case, but it is "
                        "no longer awaiting a response.\n\n"
                        f"Review ID: {case_id}\n"
                        f"Property: {property_address}\n"
                        f"Current status: "
                        f"{review_result.get('status', 'Unknown')}\n\n"
                        "— Rachel\n"
                        "Assistant Property Manager\n"
                        "ClientCo Property Management"
                    ),
                    also_owner=True,
                )
                return

            interpretation = review_result.get(
                "interpretation",
                {},
            )

            # Apply every confirmed fact immediately. Partial answers
            # are applied while unresolved items remain open.
            apply_result = apply_review_answers_to_sheet(
                case_id=case_id,
            )

            if not apply_result.get("applied"):
                if apply_result.get("deferred"):
                    print(
                        f"[PropertyReview] Recorded {case_id}; shared-sheet "
                        "apply is durably deferred by its write gate."
                    )
                    return True
                raise RuntimeError(
                    "Manager's answers were recorded, but the property "
                    "sheet was not updated: "
                    + str(
                        apply_result.get(
                            "reason",
                            apply_result.get("status", "Unknown"),
                        )
                    )
                )

            print(
                f"[PropertyReview] Applied "
                f"{len(apply_result.get('applied_fields', {}))} "
                f"confirmed field(s) for {case_id}."
            )

            if apply_result.get("status") in {
                "CLARIFICATION_REQUIRED",
                "CLARIFICATION_REQUIRED_APPLIED",
            }:
                clarification = str(
                    interpretation.get(
                        "clarification_question",
                        "",
                    )
                ).strip()

                send_email(
                    MANAGER_EMAIL,
                    f"Re: {subject}",
                    (
                        "Hi Manager,\n\n"
                        "I recorded the facts I could confirm from "
                        "your response. I need one clarification:\n\n"
                        f"{clarification}\n\n"
                        f"Review ID: {case_id}\n"
                        f"Property: {property_address}\n\n"
                        "Please reply naturally to this email.\n\n"
                        "— Rachel\n"
                        "Assistant Property Manager\n"
                        "ClientCo Property Management"
                    ),
                    also_owner=True,
                )

                print(
                    f"[PropertyReview] Clarification requested for "
                    f"{case_id}: {property_address}"
                )

            else:
                summary = str(
                    interpretation.get("summary", "")
                ).strip()

                confirmation = (
                    "I recorded your property-review answers."
                )

                if summary:
                    confirmation += f"\n\nSummary: {summary}"

                send_email(
                    MANAGER_EMAIL,
                    f"Rachel Confirmation — {case_id}",
                    (
                        f"Hi Manager,\n\n"
                        f"{confirmation}\n\n"
                        f"Property: {property_address}\n"
                        f"Review ID: {case_id}\n\n"
                        "Rachel will now apply the confirmed facts "
                        "to the property record and recalculate the "
                        "remaining rent-control review items.\n\n"
                        "— Rachel\n"
                        "Assistant Property Manager\n"
                        "ClientCo Property Management"
                    ),
                    also_owner=True,
                )

                print(
                    f"[PropertyReview] Manager's answers recorded for "
                    f"{case_id}: {property_address}"
                )

            return

        except Exception as exc:
            print(
                f"[PropertyReview] Reply processing failed for "
                f"{case_id}: {type(exc).__name__}: {exc}"
            )

            send_email(
                MANAGER_EMAIL,
                f"Re: {subject}",
                (
                    "Hi Manager,\n\n"
                    "I received your property-review response, but "
                    "I could not process it safely. No property facts "
                    "were changed.\n\n"
                    f"Review ID: {case_id}\n"
                    f"Property: {property_address}\n"
                    f"Error: {type(exc).__name__}: {exc}\n\n"
                    "Please keep this email thread; Rachel will retry "
                    "after the processing issue is corrected.\n\n"
                    "— Rachel\n"
                    "Assistant Property Manager\n"
                    "ClientCo Property Management"
                ),
                also_owner=True,
            )
            return

    # Scanner management-review decisions.
    # Only Manager may authorize PROCEED, HOLD, HANDLED, or OTHER.
    review_match = re.search(
        r"Rachel Review Needed\s*[—-]\s*WO\s*#?"
        r"([A-Za-z0-9-]+)",
        subject,
        re.IGNORECASE,
    )

    if review_match:
        wo_num = review_match.group(1)

        sender_normalized = sender_email.strip().lower()

        authorized_sender = (
            sender_normalized == MANAGER_EMAIL.lower()
            or (
                OWNER_QA_MODE
                and wo_num.upper().startswith("QA-")
                and sender_normalized == OWNER_EMAIL.lower()
            )
        )

        if not authorized_sender:
            print(
                f"[ManagerReview] Ignored review instruction for "
                f"WO #{wo_num} from unauthorized sender: "
                f"{sender_email}"
            )
            return True

        try:
            from rachel_work_order_scanner import (
                record_manager_review_instruction,
                triage_work_order_details,
            )

            interpreted_reply = interpret_manager_review_reply(
                body_stripped,
            )

            if not interpreted_reply:
                return bool(send_email(
                    sender_email,
                    f"Re: {subject}",
                    (
                        f"Hi Manager,\n\n"
                        f"I could not determine exactly what you want me "
                        f"to do with Work Order #{wo_num}. Please clarify "
                        f"whether I should proceed, hold, mark it handled, "
                        f"or follow another instruction.\n\n"
                        f"— Rachel\n"
                        f"Assistant Property Manager\n"
                        f"ClientCo Property Management"
                    ),
                ))

            print(
                f"[ManagerReview] Interpreted instruction for "
                f"WO #{wo_num}: {interpreted_reply}"
            )

            result = record_manager_review_instruction(
                wo_num,
                interpreted_reply,
            )

            if result:
                print(
                    f"[ManagerReview] Manager's instruction processed "
                    f"for WO #{wo_num}."
                )

                if result.get("classification") in {
                    "manager_authorized_tenant_triage",
                    "manager_authorized_custom_triage",
                }:
                    print(
                        f"[ManagerReview] Starting authorized tenant "
                        f"triage for WO #{wo_num}."
                    )
                    triage_result = triage_work_order_details(
                        {**result, "triage_origin": "manager_authorized"}
                    )
                    if triage_result is False:
                        print(
                            f"[ManagerReview] Authorized triage did not "
                            f"complete for WO #{wo_num}; source retained"
                        )
                        return False

                classification = result.get("classification", "")
                instruction = result.get("manager_instruction", "")

                status_messages = {
                    "manager_authorized_tenant_triage": (
                        "Your instruction was received and tenant triage "
                        "has started."
                    ),
                    "manager_authorized_custom_triage": (
                        "Your instruction was received. Tenant triage has "
                        "started using your customized direction."
                    ),
                    "manager_hold": (
                        "Your instruction was received. Rachel will take "
                        "no further tenant-triage action until you provide "
                        "a new instruction."
                    ),
                    "manager_handled": (
                        "Your instruction was received. The work order has "
                        "been marked as handled by management."
                    ),
                    "manager_other_instruction": (
                        "Your specific instruction was received and recorded."
                    ),
                }

                confirmation_message = status_messages.get(
                    classification,
                    "Your instruction was received and recorded.",
                )

                return bool(send_email(
                    MANAGER_EMAIL,
                    f"Rachel Confirmation — WO #{wo_num}",
                    (
                        f"Hi Manager,\n\n"
                        f"{confirmation_message}\n\n"
                        f"Work Order: #{wo_num}\n"
                        f"Instruction: {instruction}\n\n"
                        f"— Rachel\n"
                        f"Assistant Property Manager\n"
                        f"ClientCo Property Management"
                    ),
                    also_owner=True,
                ))
            else:
                print(
                    f"[ManagerReview] Manager's reply for WO #{wo_num} "
                    "was not recognized. No workflow action was taken."
                )
                return bool(send_email(
                    sender_email,
                    f"Re: {subject}",
                    f"I could not apply the instruction to Work Order "
                    f"#{wo_num} because no matching review record was found. "
                    "No workflow action was taken.\n\n— Rachel",
                ))

        except Exception as e:
            print(
                f"[ManagerReview] Instruction processing error "
                f"for WO #{wo_num}: {e}"
            )
            return False

    # Estimate decisions: "approve 1540" / "reject 1540" / "another 1540"
    est_match = re.match(
        r"^(approve|reject|another)\s+#?([\d-]+)\s*$",
        body_stripped, re.IGNORECASE)
    if est_match and WO_TRIAGE_AVAILABLE:
        try:
            from rachel_vendor_lifecycle import handle_manager_estimate_decision
            return handle_manager_estimate_decision(
                est_match.group(1), est_match.group(2))
        except Exception as e:
            print(f"[Manager/Owner] Estimate decision error: {e}")
            return False

    # Work-done trigger: "1540 done" -> tenant satisfaction check
    done_match = re.match(r"^#?([\d-]+)\s+done\s*$",
                          body_stripped, re.IGNORECASE)
    if done_match and WO_TRIAGE_AVAILABLE:
        try:
            from rachel_vendor_lifecycle import send_satisfaction_check
            return send_satisfaction_check(done_match.group(1))
        except Exception as e:
            print(f"[Manager/Owner] Satisfaction check error: {e}")
            return False

    # Check if this is a vendor dispatch reply (number or vendor name in response to WO summary)
    # Pattern: short reply like "1", "2", "Gary Plumber", "in-house"
    # Must require a digit after "WO" — Rachel's own generic subject line
    # ("Rachel WO Update") otherwise self-matches as a fake WO number
    # ("Update"), which wins over the real "WO #1712-1" in the quoted body
    # since subject is searched before body and re.search takes the first hit.
    wo_match = re.search(r"WO[:\s#]+(\d[\d-]*)", subject + " " + body, re.IGNORECASE)
    vendor_reply_lower = body_stripped.lower()
    vendor_decision_pending = False
    if WO_TRIAGE_AVAILABLE and wo_match:
        candidate_wo = wo_match.group(1).rstrip(".")
        conn = get_db()
        try:
            candidate = conn.execute(
                "SELECT status FROM wo_triage WHERE wo_num = ?",
                (candidate_wo,),
            ).fetchone()
        finally:
            conn.close()
        vendor_decision_pending = bool(
            candidate
            and candidate["status"] in {
                "waiting_for_manager_vendor_approval",
                "vendor_assignment_failed",
                "tenant_availability_message_failed",
            }
        )

    is_question = (
        "?" in body_stripped
        or re.match(
            r"^(what|when|where|why|who|how|is|are|did|does|can|could|"
            r"should|has|have)\b",
            vendor_reply_lower,
        )
        is not None
    )

    is_vendor_reply = (
        WO_TRIAGE_AVAILABLE
        and len(body_stripped) < 100
        and not is_question
        and (
            body_stripped.isdigit()
            or vendor_reply_lower == "in-house"
            or re.search(r"\b\d+-\d+\s*[:=]", body_stripped) is not None
            or re.match(
                r"^(assign|use|send|dispatch)\b",
                vendor_reply_lower,
            )
            is not None
            or vendor_decision_pending
        )
    )

    if is_vendor_reply and wo_match:
        wo_num = wo_match.group(1).rstrip(".")
        print(f"[Manager/Owner] Vendor dispatch reply for WO#{wo_num}: {body_stripped}")
        try:
            from rachel_wo_triage import handle_manager_vendor_reply
            return handle_manager_vendor_reply(body_stripped, wo_num)
        except Exception as e:
            print(f"[Manager/Owner] Vendor dispatch error: {e}")
            return False

    # Search Rachel's operational memory before answering management.
    internal_user = resolve_internal_user(
        email_data.get("sender_email", ""),
        DB_PATH,
    )
    database_context = search_rachel_database(
        f"Subject: {subject}\n\nMessage: {body}",
        internal_user_id=(internal_user or {}).get("user_id", ""),
    )

    internal_user_name = (internal_user or {}).get(
        "display_name", "an authorized ClientCo team member"
    )
    internal_user_role = (internal_user or {}).get(
        "role", "internal user"
    ).replace("_", " ")

    # Ask Claude to answer, research, or identify a supported PM action.
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system=f"""You are Rachel, ClientCo Property Management's company-wide
Assistant Property Manager, currently supporting {internal_user_name}, whose
configured role is {internal_user_role}. ClientCo uses one shared Rachel
intelligence system with per-user portfolio and permission boundaries.

Respond as a capable real assistant.

Choose exactly one path:

1. CONVERSATION
For greetings, tests, or ordinary conversation, reply naturally and briefly.

2. MANAGEMENT QUESTION
Answer property-management questions using only the supplied Rachel database
context. You may summarize and connect relevant records, but never invent
facts. Clearly distinguish:
- confirmed information in Rachel's stored records,
- information that may be outdated,
- information requiring a live AppFolio check.

When timestamps are available, mention the latest relevant stored date or
time. If the records do not answer the question, say that plainly rather
than guessing.

For management questions:
- Answer the question directly.
- Do not claim a vendor, assignment, communication, or action exists unless
  it appears in the supplied records.
- Do not offer to contact tenants, vendors, owners, or other parties unless
  the requested action is already supported by Rachel's implemented tools.
- Do not end with an unnecessary "Would you like me to..." question.
- You may state that a live AppFolio check is required when stored records
  are incomplete or outdated.

3. SUPPORTED TENANT ACTION
For one of these specific actions, reply with JSON only:
{{
  "action": "moving_out|renewed|convert_to_mtm|non_renewal|insurance_resolved|no_action",
  "tenant_name": "full name if mentioned",
  "move_out_date": "YYYY-MM-DD or null",
  "db_note": "brief note",
  "appfolio_steps": ["step 1", "step 2"],
  "reply": "confirmation message"
}}

Do not classify a question as an action merely because it discusses a
tenant, lease, insurance, or work order.

4. CLARIFICATION
When the request is ambiguous or required information is missing, ask one
concise clarification question.

For conversation, management questions, and clarification, return normal
text only. For a supported tenant action, return JSON only.""",
        messages=[{
            "role": "user",
            "content": (
                f"EMAIL SUBJECT:\n{subject}\n\n"
                f"EMAIL MESSAGE:\n{body}\n\n"
                f"RACHEL DATABASE CONTEXT:\n"
                f"{database_context[:14000]}"
            ),
        }],
    )

    result_text = response.content[0].text.strip()
    print(f"[Manager/Owner] Claude response: {result_text[:100]}")

    # Check if it's a PM action (JSON)
    if result_text.startswith("{"):
        try:
            result = json.loads(result_text)
            action = result.get("action", "no_action")
            print(f"[Manager/Owner] PM action: {action}")

            # Find tenant and execute DB action
            tenant_name = result.get("tenant_name")
            tenant = find_tenant(tenant_name) if tenant_name else None

            if tenant:
                occ_id = tenant["occupancy_id"]
                if action == "moving_out":
                    move_date = result.get("move_out_date", "")
                    note = f"MOVING OUT{' on ' + move_date if move_date else ''} — per Manager {date.today()}"
                    update_tenant(occ_id, notes=note, past_lease_end=0)
                elif action == "renewed":
                    update_tenant(occ_id,
                        notes=f"Lease renewed per Manager {date.today()}",
                        past_lease_end=0)
                elif action == "convert_to_mtm":
                    update_tenant(occ_id,
                        lease_type="month-to-month",
                        past_lease_end=0,
                        notes=f"Converted to M2M per Manager {date.today()}")
                elif action == "non_renewal":
                    update_tenant(occ_id,
                        notes=f"Non-renewal in progress per Manager {date.today()}",
                        past_lease_end=0)
                elif action == "insurance_resolved":
                    mark_insurance_resolved(occ_id)
                    update_tenant(occ_id,
                        notes=f"Insurance resolved per Manager {date.today()}")

            reply_text = result.get("reply", "Got it — I have updated my records.")
            steps = result.get("appfolio_steps", [])
            if steps:
                reply_text += "\n\nAppFolio steps needed:\n"
                for i, s in enumerate(steps, 1):
                    reply_text += f"  {i}. {s}\n"
            reply_text += "\n\n— Rachel\nAssistant Property Manager\nClientCo Property Management"
            return bool(send_email(sender_email, f"Re: {subject}", reply_text))
        except json.JSONDecodeError:
            pass

    # Plain text response — send directly
    if not result_text.strip().endswith("ClientCo Property Management"):
        result_text += "\n\n— Rachel\nAssistant Property Manager\nClientCo Property Management"
    return bool(send_email(sender_email, f"Re: {subject}", result_text))


def handle_tenant(email_data: dict):
    """Reply directly to tenant and notify Manager."""
    subject      = email_data["subject"]
    body         = email_data["body"]
    sender_email = email_data["sender_email"]

    print(f"[Tenant] From {sender_email}: {subject[:50]}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="""You are Rachel, Assistant Property Manager at ClientCo Property Management, supporting Regional Manager (Regional Property Manager).
A tenant emailed you. Reply professionally and helpfully.
- Maintenance issue: acknowledge receipt, say you are looking into it
- Question you cannot answer: say you will follow up shortly
- URGENT (flooding, gas, no heat, lockout, fire): tell them to call 555-010-0000 immediately
Keep replies concise. Do not sign off — that will be added automatically.""",
        messages=[{
            "role": "user",
            "content": f"Subject: {subject}\n\nTenant message: {body}"
        }]
    )

    reply = response.content[0].text.strip()
    reply += "\n\n— Rachel\nClientCo Property Management\n555-010-0000"

    # Reply to tenant
    tenant_reply_sent = bool(send_email(
        sender_email, f"Re: {subject}", reply
    ))
    if not tenant_reply_sent:
        return False

    # Notify Manager
    manager_note = (f"Hi Manager,\n\nFYI — a tenant emailed Rachel. "
                   f"I replied. See below for your records.\n\n"
                   f"From: {sender_email}\nSubject: {subject}\n\n"
                   f"Their message:\n{body}\n\n"
                   f"My reply:\n{reply}\n\n— Rachel")
    # The tenant reply is the required delivery. A management-copy failure is
    # reported but must not cause an automatic duplicate reply to the tenant.
    management_copy_sent = bool(send_email(
        MANAGER_EMAIL,
        f"FYI — Tenant Email: {subject}",
        manager_note,
        also_owner=True,
    ))
    if not management_copy_sent:
        print("[Tenant] Tenant reply verified; management FYI copy failed")
    return True


def handle_appfolio_work_order(email_data: dict):
    """
    Smart router for ALL AppFolio emails referencing a WO#.
    Uses the triage database status to decide what this email IS:
      no record                 -> brand new WO -> full triage
      awaiting_reply            -> tenant answered a triage question
      satisfaction_check_sent   -> tenant answered the satisfaction check
      vendor_* / work_approved  -> vendor replied (estimate or scheduling)
      anything else             -> new WO triage (safe default)
    """
    subject = email_data["subject"]
    body    = email_data["body"]
    print(f"[WorkOrder] {subject[:60]}")

    if not WO_TRIAGE_AVAILABLE:
        return bool(send_sms(
            f"New WO: {_truncate_at_word_boundary(subject, 100)}",
            urgent=False))

    wo_match = re.search(r"WO[:\s#]+([\d-]+)", subject + " " + body,
                         re.IGNORECASE)
    wo_num = wo_match.group(1).rstrip("-.") if wo_match else None

    if not wo_num:
        # A reply to any triage question after the very first one quotes
        # that later question, not the opening message - so it never
        # repeats the WO#. These all still carry the stable AppFolio
        # "{address} service request" subject; fall back to matching the
        # most recently active awaiting_reply conversation for that
        # address (confirmed live Sep 2 2026 - the tenant's second+
        # reply on WO#1717-1 had no WO# anywhere in subject or body).
        address_match = re.search(
            r"^(?:re:\s*)?(.+?)\s+service request\s*$",
            subject.strip(), re.IGNORECASE,
        )
        if address_match:
            # Compare with whitespace collapsed - the stored address can
            # carry doubled internal spacing (AppFolio's own "Address:"
            # line sometimes joins the property and unit address with
            # extra whitespace), which a plain SQL LIKE would miss.
            candidate_address = re.sub(
                r"\s+", " ", address_match.group(1).strip()
            ).lower()
            conn = get_db()
            candidates = conn.execute(
                "SELECT wo_num, address, updated_at FROM wo_triage "
                "WHERE status = 'awaiting_reply' "
                "ORDER BY updated_at DESC"
            ).fetchall()
            conn.close()
            for candidate in candidates:
                stored_address = re.sub(
                    r"\s+", " ", str(candidate["address"] or "")
                ).lower()
                if candidate_address in stored_address:
                    wo_num = candidate["wo_num"]
                    print(
                        f"[WorkOrder] Resolved WO#{wo_num} by address "
                        f"match (no WO# in subject/body): "
                        f"{candidate_address!r}"
                    )
                    break

    status = None
    if wo_num:
        conn = get_db()
        conn.execute("""CREATE TABLE IF NOT EXISTS wo_triage (
            wo_num TEXT PRIMARY KEY, wo_url TEXT, tenant TEXT,
            address TEXT, job_desc TEXT, issue_type TEXT,
            conversation TEXT, issues TEXT, status TEXT,
            created_at TEXT, updated_at TEXT, vendor_name TEXT,
            nudge_count INTEGER DEFAULT 0, last_nudge_at TEXT,
            estimate_amount REAL, scheduled_for TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY, primary_tenant TEXT, unit TEXT,
            property TEXT, phone TEXT, email TEXT)""")
        conn.commit()
        row = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = ? "
            "OR wo_num LIKE ?", (wo_num, f"{wo_num}-%")).fetchone()
        conn.close()
        status = row["status"] if row else None
    print(f"[WorkOrder] WO#{wo_num} | db status: {status}")

    try:
        if status is None:
            # The original AppFolio email parser is a pure, local eligibility
            # gate. Run it before acquiring the browser so routine status
            # notifications cannot cause a login or WO detail navigation.
            new_work_order_details = parse_work_order_email(email_data)
            if not new_work_order_details:
                print(
                    "[Triage] Skipped before AppFolio access — not an "
                    f"original new work-order email: {subject}"
                )
                print(
                    "[AppFolioEventMetrics] targeted_wo_detail_opens=0 "
                    "source=gmail_work_order_event reason=non_actionable"
                )
                return True
            eligibility = evaluate_triage_eligibility(new_work_order_details)
            if not eligibility["eligible"]:
                print(
                    "[TriageEligibility] Browser access skipped "
                    f"reason={eligibility['reason']}"
                )
                return True
            manager = get_appfolio_browser_manager()
            with manager.operation("email_new_work_order") as page:
                print(
                    "[AppFolioEventMetrics] targeted_wo_detail_opens=1 "
                    "source=gmail_work_order_event"
                )
                return triage_work_order_details(
                    new_work_order_details, page=page
                )
        elif status == "awaiting_reply":
            from rachel_wo_triage import handle_tenant_triage_reply
            return handle_tenant_triage_reply(
                wo_num,
                body,
                images=email_data.get("images"),
                channel="email",
                thread_message_id=email_data.get(
                    "message_id_header",
                    "",
                ),
                thread_references=email_data.get(
                    "references_header",
                    "",
                ),
            )
        elif status in (
            "satisfaction_check_sent",
            "satisfaction_response_delivery_failed",
            "satisfaction_escalation_delivery_failed",
        ):
            from rachel_vendor_lifecycle import (
                handle_tenant_satisfaction_reply,
            )
            return handle_tenant_satisfaction_reply(wo_num, body)

        elif status in (
            "waiting_for_tenant_availability",
            "waiting_for_tenant_confirmation",
            "schedule_write_blocked_before_claim",
            "appointment_scheduled_confirmation_issue",
            "appointment_scheduled",
        ):
            from rachel_vendor_lifecycle import (
                handle_tenant_schedule_reply,
            )
            return handle_tenant_schedule_reply(wo_num, body)

        elif status in (
            "vendor_assigned",
            "waiting_for_vendor_availability",
            "waiting_for_vendor_confirmation",
            "work_approved",
            "getting_other_estimates",
            "estimate_pending_approval",
            "vendor_stalled",
            "scheduled",
            "tenant_schedule_message_failed",
        ):
            return _route_vendor_reply(email_data, wo_num)
        else:
            return triage_work_order(email_data)
    except Exception as e:
        print(f"[WorkOrder] Router error: {e}")
        send_sms(f"WO #{wo_num or '?'} — error processing an update, "
                 f"please check Rachel's inbox. ({str(e)[:80]})",
                 urgent=False)
        return False


def _route_vendor_reply(email_data: dict, wo_num: str):
    """Vendor replied — figure out if it's an estimate, a schedule, or other."""
    body = email_data["body"]
    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=10,
        messages=[{"role": "user", "content":
                   f"A repair vendor sent this about a work order:\n"
                   f"{body[:800]}\n\nClassify with ONE word:\n"
                   f"ESTIMATE (contains a price/quote)\n"
                   f"SCHEDULE (contains an appointment date/time)\n"
                   f"OTHER"}])
    kind = response.content[0].text.strip().upper()
    print(f"[VendorReply] WO#{wo_num} -> {kind}")

    from rachel_vendor_lifecycle import (handle_vendor_estimate,
                                          handle_vendor_schedule_reply)
    if "ESTIMATE" in kind:
        return handle_vendor_estimate(email_data, wo_num)
    elif "SCHEDULE" in kind:
        return handle_vendor_schedule_reply(email_data, wo_num)
    else:
        return bool(send_sms(
            f"WO #{wo_num} — vendor replied:\n{_truncate_at_word_boundary(body, 200)}",
            urgent=False,
        ))


def _parse_appfolio_texting_notification(body: str):
    """Pull the sender/message/reply-link out of AppFolio's own texting-
    inbox notification email, so Manager gets a direct, human sentence
    instead of AppFolio's raw notification dump (Owner's ask, Sep 3 2026).
    Returns None if the body doesn't match that exact shape — callers must
    keep their raw-body fallback for every other AppFolio alert shape."""
    name_match = re.search(
        r"New Message From (.+?)\s*\((\w+)\)", body,
    )
    message_match = re.search(
        r"Message:\s*(.+?)\s*\n=+", body, re.S,
    )
    link_match = re.search(
        r"Respond to this message:\s*(\S+)", body,
    )
    if not (name_match and message_match and link_match):
        return None
    return {
        "contact_name": name_match.group(1).strip(),
        "contact_role": name_match.group(2).strip(),
        "message_text": re.sub(r"\s+", " ", message_match.group(1)).strip(),
        "reply_link": link_match.group(1).strip(),
    }


def handle_appfolio_other(email_data: dict):
    """Forward only actionable AppFolio notifications to Manager and Owner."""
    subject = email_data["subject"]
    body = email_data["body"]
    combined = f"{subject}\n{body}".lower()

    # Routine vendor progress notes remain in AppFolio and Rachel's email
    # history. Do not duplicate them by email unless action is required.
    if "added a new note on work order" in combined:
        escalation_terms = [
            "urgent",
            "emergency",
            "approval",
            "approve",
            "authorization",
            "estimate",
            "cannot access",
            "can't access",
            "no access",
            "tenant unavailable",
            "reschedule",
            "flood",
            "active leak",
            "gas smell",
            "no heat",
            "no water",
            "water shutoff",
        ]
        if not any(term in combined for term in escalation_terms):
            print(
                "[AppFolio] Routine vendor work-order note received; "
                "internal forwarding suppressed"
            )
            return True

    # Suppress routine AppFolio account-security confirmations.
    # These are expected when Rachel signs in from Render and require no action.
    routine_security_phrases = [
        "logged in from a new device",
        "if this was you, there's nothing you need to do",
        "if this was you, there is nothing you need to do",
        "we noticed you logged in",
    ]
    if any(phrase in combined for phrase in routine_security_phrases):
        print(
            "[AppFolio] Routine new-device security notice received; "
            "internal forwarding suppressed"
        )
        return True

    # Suppress routine vendor payment-preference-updated notifications.
    # Purely informational (a vendor changed how they receive payment via
    # AppFolio's own Vendor Portal) — nothing for Manager to act on. Owner
    # explicitly asked these stop being forwarded (Sep 2 2026).
    if (
        "vendor payment preference updated" in combined
        or "updated their payment preference" in combined
    ):
        print(
            "[AppFolio] Routine vendor payment-preference update received; "
            "internal forwarding suppressed"
        )
        return True

    # Use whole-word matching so 'noticed' does not trigger 'notice'.
    keyword_patterns = [
        r"\bpayment\b",
        r"\blease\b",
        r"\bmove\b",
        r"\bnotice\b",
        r"\bviolation\b",
        r"\binspection\b",
        r"\beviction\b",
        r"\bdelinquent\b",
        r"\burgent\b",
        r"\bemergency\b",
        r"\bapproval\b",
        r"\bauthorization\b",
        r"\bestimate\b",
        r"\bcannot access\b",
        r"\bno access\b",
    ]

    if any(re.search(pattern, combined) for pattern in keyword_patterns):
        texting_notification = _parse_appfolio_texting_notification(body)
        if texting_notification:
            alert_body = (
                f"Hi Manager,\n\n"
                f"{texting_notification['contact_name']} "
                f"({texting_notification['contact_role']}) texted: "
                f"\"{texting_notification['message_text']}\"\n\n"
                f"Reply here: {texting_notification['reply_link']}\n\n"
                f"— Rachel"
            )
        else:
            alert_body = (
                f"Hi Manager,\n\nAppFolio sent this:\n\n{body[:500]}\n\n"
                f"— Rachel"
            )
        return bool(send_email(
            MANAGER_EMAIL,
            f"AppFolio Alert: {subject}",
            alert_body,
            also_owner=True,
        ))
    return True


def handle_management_prospect(email_data: dict):
    """
    Handle property owner/investor asking about GOPM management services.
    Use NEPQ opener questions to engage them — get them talking about their property.
    Notify Owner and Manager.
    """
    subject      = email_data["subject"]
    body         = email_data["body"]
    sender_email = email_data["sender_email"]

    print(f"[Prospect-Mgmt] From {sender_email}: {subject[:50]}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="""You are Rachel, Assistant Property Manager at ClientCo Property Management, supporting Regional Manager (Regional Property Manager).
A property owner or investor has reached out about our management services.

Write a warm, curious, non-salesy reply using NEPQ opener questions.
Goal: get them talking about their property and situation.

Use 2-3 of these types of questions naturally:
- "What prompted you to reach out today?"
- "What's been your biggest challenge managing the property?"
- "What would the ideal property management partnership look like for you?"
- "Tell me more about your property — what type and where is it located?"
- "How long have you owned the property?"

Keep it brief, warm and conversational. DO NOT mention being AI.
DO NOT pitch services yet. Just open the conversation.
Sign as Rachel, ClientCo Property Management, 555-010-0000.""",
        messages=[{
            "role": "user",
            "content": f"Subject: {subject}\n\nMessage: {body}"
        }]
    )

    reply = response.content[0].text.strip()
    prospect_reply_sent = bool(send_email(
        sender_email, f"Re: {subject}", reply
    ))
    if not prospect_reply_sent:
        return False

    # Notify Owner and Manager of new management prospect
    alert = (f"Hi Owner and Manager,\n\n"
             f"New management prospect inquiry received.\n\n"
             f"From: {sender_email}\n"
             f"Subject: {subject}\n\n"
             f"Their message:\n{body}\n\n"
             f"My opening reply:\n{reply}\n\n"
             f"Waiting for their response before following up further.\n\n"
             f"— Rachel")
    owner_alert_sent = bool(send_email(
        OWNER_EMAIL,
        f"🎯 New Management Prospect: {sender_email}",
        alert,
        also_owner=False,
    ))
    # Also send to Manager
    manager_alert_sent = bool(send_email(
        MANAGER_EMAIL,
        f"🎯 New Management Prospect: {sender_email}",
        alert,
        also_owner=False,
    ))
    if not (owner_alert_sent and manager_alert_sent):
        print("[Prospect-Mgmt] Prospect reply verified; internal alert failed")
    return True


def handle_tenant_prospect(email_data: dict):
    """
    Handle someone looking to rent.
    Direct them to the leasing team.
    """
    subject      = email_data["subject"]
    body         = email_data["body"]
    sender_email = email_data["sender_email"]

    print(f"[Prospect-Tenant] From {sender_email}: {subject[:50]}")

    reply = ("Thank you for reaching out to ClientCo Property Management!\n\n"
             "For rental inquiries and available units, please contact our leasing team directly:\n\n"
             "  📧 info@clientco.example\n"
             "  📞 555-010-0000\n"
             "  🌐 www.clientco.example\n\n"
             "We look forward to helping you find your next home!\n\n"
             "— Rachel\nClientCo Property Management")

    prospect_reply_sent = bool(send_email(
        sender_email, f"Re: {subject}", reply
    ))
    if not prospect_reply_sent:
        return False

    # Notify Manager of tenant prospect
    management_copy_sent = bool(send_email(
        MANAGER_EMAIL,
        f"Tenant Prospect Inquiry: {sender_email}",
        f"Hi Manager,\n\nTenant prospect inquiry received. "
        f"Directed them to leasing team.\n\n"
        f"From: {sender_email}\nSubject: {subject}\n\n"
        f"Message:\n{body}\n\n— Rachel",
        also_owner=True,
    ))
    if not management_copy_sent:
        print("[Prospect-Tenant] Prospect reply verified; management FYI failed")
    return True


def handle_vendor_command(email_data: dict) -> bool:
    """
    Handle vendor management commands emailed to Rachel.
    Returns True if email was a vendor command, False otherwise.

    Supported commands (in email body):
      ADD VENDOR
      Property: [address]
      Vendor: [name]
      Phone: [number]
      Trade: [plumbing/hvac/electrical/general/painting/appliance/etc]
      Notes: [optional notes]

      UPDATE VENDOR
      Vendor: [name]
      Phone: [new number]
      Notes: [new notes]

      ADD PROPERTY VENDOR
      Property: [address]
      Vendor: [existing vendor name]
      Trade: [trade]

      REMOVE VENDOR
      Vendor: [name]
    """
    body    = email_data["body"].strip()
    subject = email_data["subject"]
    sender  = email_data["sender_email"]

    # Check if this is a vendor command
    commands = ["ADD VENDOR", "UPDATE VENDOR", "ADD PROPERTY VENDOR", "REMOVE VENDOR"]
    body_upper = body.upper()
    matched_cmd = next((c for c in commands if c in body_upper), None)

    if not matched_cmd:
        return False

    def extract(field):
        m = re.search(rf"{field}:\s*(.+)", body, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    internal_user = resolve_internal_user(sender, DB_PATH)
    requested_property = extract("Property") or "*"
    if (
        not internal_user
        or not internal_user_has_access(
            internal_user["user_id"],
            permission="property_management",
            property_key=requested_property,
            db_path=DB_PATH,
        )
    ):
        print(
            "[VendorCmd] Ignored command from unverified or out-of-scope "
            "sender"
        )
        return False

    print(f"[VendorCmd] Authorized command detected: {matched_cmd}")

    conn = get_db()
    result_msg = ""

    try:
        if matched_cmd == "ADD VENDOR":
            vendor  = extract("Vendor")
            phone   = extract("Phone")
            trade   = extract("Trade").lower() or "general"
            notes   = extract("Notes")
            prop    = extract("Property")

            if not vendor:
                result_msg = "Could not add vendor — Vendor name is required."
            else:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vendors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        phone TEXT, email TEXT, trade TEXT,
                        notes TEXT, active INTEGER DEFAULT 1,
                        created_at TEXT
                    )""")
                conn.execute("""
                    INSERT OR IGNORE INTO vendors
                    (name, phone, trade, notes, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (vendor, phone, trade, notes, 
                      __import__('datetime').datetime.now().isoformat()))
                vid = conn.execute(
                    "SELECT id FROM vendors WHERE name = ?", (vendor,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE vendors SET active = 1 WHERE id = ?",
                    (vid,),
                )

                if prop:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS property_vendors (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            property_address TEXT, vendor_id INTEGER,
                            trade TEXT, is_preferred INTEGER DEFAULT 1,
                            source TEXT,
                            UNIQUE(property_address, vendor_id)
                        )""")
                    conn.execute("""
                        INSERT OR IGNORE INTO property_vendors
                        (property_address, vendor_id, trade, source)
                        VALUES (?, ?, ?, ?)
                    """, (prop, vid, trade, "manual"))

                conn.commit()
                prop_note = f" for {prop}" if prop else ""
                result_msg = (f"✅ Added vendor: {vendor}{prop_note}\n"
                              f"   Phone: {phone or 'not provided'}\n"
                              f"   Trade: {trade}\n"
                              f"   Notes: {notes or 'none'}")

        elif matched_cmd == "UPDATE VENDOR":
            vendor = extract("Vendor")
            phone  = extract("Phone")
            notes  = extract("Notes")

            if not vendor:
                result_msg = "Could not update — Vendor name required."
            else:
                updates = []
                vals = []
                if phone:
                    updates.append("phone = ?")
                    vals.append(phone)
                if notes:
                    updates.append("notes = ?")
                    vals.append(notes)
                if updates:
                    vals.append(vendor)
                    conn.execute(
                        f"UPDATE vendors SET {', '.join(updates)} WHERE name = ?",
                        vals)
                    conn.commit()
                result_msg = f"✅ Updated vendor: {vendor}"

        elif matched_cmd == "ADD PROPERTY VENDOR":
            prop   = extract("Property")
            vendor = extract("Vendor")
            trade  = extract("Trade").lower() or "general"

            if not prop or not vendor:
                result_msg = "Could not add — Property and Vendor are both required."
            else:
                vid = conn.execute(
                    "SELECT id FROM vendors "
                    "WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) "
                    "AND COALESCE(active, 1) = 1",
                    (vendor,)
                ).fetchone()
                if not vid:
                    result_msg = f"Vendor '{vendor}' not found. Add them first with ADD VENDOR."
                else:
                    conn.execute("""
                        INSERT OR IGNORE INTO property_vendors
                        (property_address, vendor_id, trade, source)
                        VALUES (?, ?, ?, ?)
                    """, (prop, vid[0], trade, "manual"))
                    conn.commit()
                    result_msg = f"✅ Linked {vendor} to {prop} ({trade})"

        elif matched_cmd == "REMOVE VENDOR":
            vendor = extract("Vendor")
            if not vendor:
                result_msg = "Could not remove — Vendor name required."
            else:
                changed = conn.execute(
                    "UPDATE vendors SET active = 0 "
                    "WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))",
                    (vendor,),
                ).rowcount
                conn.commit()
                result_msg = (
                    f"✅ Deactivated vendor: {vendor}"
                    if changed == 1
                    else f"Vendor '{vendor}' was not found by exact name."
                )

    except Exception as e:
        result_msg = f"Error processing command: {e}"
        print(f"[VendorCmd] Error: {e}")
    finally:
        conn.close()

    # Reply to sender confirming action
    reply = (f"Hi,\n\n{result_msg}\n\n"
             f"To manage vendors, email rachel@clientco.example with:\n\n"
             f"  ADD VENDOR\n"
             f"  Property: [address]\n"
             f"  Vendor: [name]\n"
             f"  Phone: [number]\n"
             f"  Trade: [plumbing/hvac/electrical/general/etc]\n"
             f"  Notes: [optional]\n\n"
             f"— Rachel\nAssistant Property Manager\nClientCo Property Management")

    confirmed = bool(send_email(
        sender, f"Re: {subject}", reply, also_owner=True
    ))
    print(
        f"[VendorCmd] Processed; confirmation to sender "
        f"{'verified' if confirmed else 'failed'}"
    )
    return confirmed


def handle_spam(email_data: dict, mail):
    print(f"[Spam] Deleting: {email_data['subject'][:50]}")
    delete_email(mail, email_data["msg_id"])
    return True


def handle_forward_to_manager(email_data: dict):
    """Forward unknown email to Manager and acknowledge sender."""
    subject      = email_data["subject"]
    body         = email_data["body"]
    sender_email = email_data["sender_email"]

    print(f"[Forward] {subject[:50]}")

    forwarded = bool(send_email(
        MANAGER_EMAIL,
        f"FWD (Need guidance): {subject}",
        f"Hi Manager,\n\nI received this email and wasn't sure how to respond.\n\n"
        f"From: {sender_email}\nSubject: {subject}\n\nMessage:\n{body}\n\n"
        f"Please reply to let me know how to handle this.\n\n— Rachel",
        also_owner=True,
    ))
    if not forwarded:
        return False

    # Acknowledge sender (unless automated)
    if not any(x in sender_email for x in ["noreply", "no-reply", "mailer", "donotreply"]):
        acknowledged = bool(send_email(
            sender_email,
            f"Re: {subject}",
            f"Thank you for reaching out to ClientCo Property Management.\n\n"
            f"We have received your message and will follow up shortly.\n"
            f"For urgent matters please call 555-010-0000.\n\n"
            f"— Rachel\nClientCo Property Management",
        ))
        if not acknowledged:
            # The management forward already succeeded. Do not retry and send
            # Manager duplicate forwards solely because the acknowledgement did
            # not verify.
            print("[Forward] Management forward verified; sender acknowledgement failed")
    return True


def _is_appfolio_sender(sender_email: str) -> bool:
    """Require an AppFolio-owned email domain, never a substring match."""
    normalized = str(sender_email or "").strip().lower()
    if normalized.count("@") != 1:
        return False
    domain = normalized.rsplit("@", 1)[1].rstrip(".")
    return any(
        domain == allowed or domain.endswith("." + allowed)
        for allowed in APPFOLIO_SENDER_DOMAINS
    )


def classify_email(sender_email: str, subject: str, body: str) -> str:
    """Classify email type using rules + Claude."""
    # Rule-based fast checks
    if _is_appfolio_sender(sender_email):
        # Work order subjects contain "WO #", "WO#" (no space — AppFolio's
        # actual format for vendor-note notifications), "work order", or
        # "maintenance request".
        if re.search(
            r"\bwo\s*#|\bwork order\b|\bmaintenance request\b",
            subject.lower(),
        ):
            return "appfolio_work_order"
        # A tenant replying to Rachel's own Notify-Tenant email comes back
        # through AppFolio's mailer-relay address with a generic "{address}
        # service request" subject (no WO#) — confirmed live Sep 2 2026.
        # The WO# is only findable in the body when this is literally the
        # tenant's first reply (quoting the opening message, which is the
        # only one that mentions it) - a reply to any later follow-up
        # question quotes that question instead, which never repeats the
        # WO#. Route on the stable "... service request" subject shape
        # too; handle_appfolio_work_order falls back to matching by
        # property address when its own WO# regex finds nothing.
        if (
            re.search(r"\bwo\s*#\s*\d+-\d+\b", body.lower())
            or re.search(r"\bservice request\b", subject.lower())
        ):
            return "appfolio_work_order"
        return "appfolio_other"
    if resolve_internal_user(sender_email, DB_PATH):
        return "manager_reply"

    # Claude classification for everything else
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=20,
        messages=[{
            "role": "user",
            "content": f"""Classify this email sent to a property management company.
Reply with ONE word only:

- management_prospect (owner/investor asking about property management services)
- tenant_prospect (person looking to rent a unit or asking about availability)
- tenant_reply (existing tenant with a question or maintenance issue)
- spam (junk, promotional, irrelevant)
- forward_to_manager (important, needs human attention — legal, complaints, vendors)

From: {sender_email}
Subject: {subject}
Body: {body[:300]}"""
        }]
    )
    return response.content[0].text.strip().lower()


# ── Main ───────────────────────────────────────────────────────────────────


def run_once():
    print(f"\n[Rachel Inbox Monitor] {datetime.now().strftime('%B %d, %Y %I:%M %p')}")
    print(f"Checking {RACHEL_IMAP_USER}...")
    print(f"[Debug] Email password loaded: {'YES' if RACHEL_IMAP_PASS else 'NO'}")

    # A property-review reply is durable before the shared sheet is touched.
    # Once its independent production gate is enabled, resume any pending
    # applies even when no new copy of the original email arrives.
    try:
        from rachel_property_review import (
            apply_pending_review_answers_to_sheet,
        )

        pending_applies = apply_pending_review_answers_to_sheet()
        if pending_applies.get("applied") or pending_applies.get("failed"):
            print(
                "[PropertyReviewApply] "
                f"applied={len(pending_applies.get('applied', []))} "
                f"failed={len(pending_applies.get('failed', []))}"
            )
    except Exception as exc:
        print(
            "[PropertyReviewApply] Pending apply check failed: "
            f"{type(exc).__name__}"
        )

    gmail_events_seen = 0
    relevant_gmail_events = 0
    mail = connect_imap()
    if not mail:
        print("[Inbox] IMAP connection failed — skipping email check")
    else:
        emails = fetch_unread_emails(mail)
        already_processed = _get_processed_ids()
        emails = [
            email_data
            for email_data in emails
            if _email_dedupe_id(email_data) not in already_processed
        ]
        print(f"[Inbox] {len(emails)} new email(s) to process")
        gmail_events_seen = len(emails)

        for e in emails:
            print(f"\n[Email] From: {e['sender_email']}")
            print(f"[Email] Subject: {e['subject'][:60]}")

            # Use the permanent RFC Message-ID for deduplication.
            msg_uid = _email_dedupe_id(e)

            # Check for vendor management commands first (before
            # classification). Bind any SMTP delivery to the permanent source
            # Message-ID so a restart cannot send the same response twice.
            with _outbound_delivery_event(msg_uid):
                vendor_command_handled = handle_vendor_command(e)
            if vendor_command_handled:
                _mark_processed(msg_uid)
                mark_read(mail, e["msg_id"])
                continue

            email_type = classify_email(e["sender_email"], e["subject"], e["body"])
            print(f"[Email] Type: {email_type}")
            if email_type in {
                "manager_reply", "appfolio_work_order", "appfolio_other",
                "tenant_reply", "management_prospect", "tenant_prospect",
            }:
                relevant_gmail_events += 1

            email_wo_match = re.search(
                r"\b(?:WO|work\s+order)\s*#?\s*(\d+-\d+)\b",
                f"{e['subject']} {e['body']}",
                re.IGNORECASE,
            )
            sender_user = resolve_internal_user(e["sender_email"], DB_PATH)
            if email_type != "spam":
                store_communication(
                    {
                        "source": "gmail",
                        "source_message_id": msg_uid,
                        "contact_name": e["sender_email"],
                        "contact_role": email_type,
                        "direction": "inbound",
                        "message_text": e["body"],
                        "preview": e["subject"],
                        "topic": email_type,
                        "related_wo": (
                            email_wo_match.group(1) if email_wo_match else ""
                        ),
                        "internal_user_id": (
                            (sender_user or {}).get("user_id", "")
                        ),
                        "processing_status": "stored",
                        "metadata": {"sender": e["sender_email"]},
                    },
                    DB_PATH,
                )

            handler_result = None

            with _outbound_delivery_event(msg_uid):
                if email_type == "manager_reply":
                    handler_result = handle_manager_or_owner(e)
                elif email_type == "appfolio_work_order":
                    handler_result = handle_appfolio_work_order(e)
                elif email_type == "appfolio_other":
                    handler_result = handle_appfolio_other(e)
                elif email_type == "tenant_reply":
                    handler_result = handle_tenant(e)
                elif email_type == "management_prospect":
                    handler_result = handle_management_prospect(e)
                elif email_type == "tenant_prospect":
                    handler_result = handle_tenant_prospect(e)
                elif email_type == "spam":
                    handler_result = handle_spam(e, mail)
                else:
                    handler_result = handle_forward_to_manager(e)

            # Existing handlers that return None retain their current
            # behavior. An explicit False means downstream processing or
            # outbound delivery failed, so leave the email available for
            # a future retry.
            if handler_result is False:
                print(
                    f"[Inbox] Processing failed for {msg_uid}. "
                    "Email was not marked read or processed."
                )
                continue

            _mark_processed(msg_uid)
            mark_read(mail, e["msg_id"])

        mail.logout()
        print(f"\n[Inbox] Done — processed {len(emails)} email(s)")

    print(
        f"[GmailMetrics] checks=1 events_seen={gmail_events_seen} "
        f"relevant_events={relevant_gmail_events}"
    )

    inbox_poll_due = AF_INBOX_AVAILABLE and _appfolio_inbox_poll_due()
    work_order_reconciliation_due = (
        WO_SCANNER_AVAILABLE and _work_order_reconciliation_due()
    )

    # Gmail checks never open AppFolio by themselves. Only acquire the shared
    # session when the independently scheduled inbox poll or scanner is due.
    if inbox_poll_due or work_order_reconciliation_due:
        scan_failure_recorded = False
        inbox_failure_recorded = False
        try:
            manager = get_appfolio_browser_manager()
            with manager.operation(
                "scheduled_appfolio_poll",
                track_feature_health=False,
            ) as page:
                if inbox_poll_due:
                    try:
                        with manager.feature_scope("communication_inbox"):
                            check_appfolio_inbox(page)
                        _mark_appfolio_inbox_poll_completed()
                    except Exception as error:
                        _mark_appfolio_inbox_poll_failed()
                        inbox_failure_recorded = True
                        print(
                            "[AppFolioPoll] Inbox poll failed independently: "
                            f"{type(error).__name__}"
                        )

                    # Deliver Rachel's real continuation for any tenant
                    # AppFolio-text reply check_appfolio_inbox already linked
                    # to an in-progress triage. Its own circuit breaker, so a
                    # failure here never blocks the read-only observer above.
                    try:
                        with manager.feature_scope("communication_inbox_response"):
                            process_pending_tenant_triage_replies()
                    except Exception as error:
                        print(
                            "[AppFolioPoll] Tenant triage reply delivery "
                            f"failed independently: {type(error).__name__}"
                        )

                    # Draft-only auto-reply feature: kept on its own circuit
                    # breaker so a failure here can never throttle how often
                    # check_appfolio_inbox (real tenant/vendor reply routing)
                    # gets to run.
                    try:
                        with manager.feature_scope("communication_inbox_drafts"):
                            process_rachel_owned_appfolio_threads(page)
                            process_pending_rachel_owned_response_drafts()
                    except Exception as error:
                        print(
                            "[AppFolioPoll] Draft-reply pass failed "
                            f"independently: {type(error).__name__}"
                        )

                if work_order_reconciliation_due:
                    print("[Reconciliation] Twice-daily WO list check is due.")
                    scan_started = time.monotonic()
                    try:
                        with manager.feature_scope(
                            "work_order_reconciliation"
                        ):
                            init_scanner_state_table()
                            scan_counts = reconcile_logged_in_page(
                                page,
                                navigation_metrics_provider=(
                                    lambda: manager.metrics
                                ),
                            )
                    except Exception:
                        _mark_work_order_reconciliation_failed()
                        scan_failure_recorded = True
                        raise
                    print(
                        "[Reconciliation] WO list check completed "
                        f"duration_seconds={time.monotonic() - scan_started:.2f} "
                        f"counts={scan_counts}"
                    )
                    _mark_work_order_reconciliation_completed()
        except Exception as e:
            if inbox_poll_due and not inbox_failure_recorded:
                _mark_appfolio_inbox_poll_failed()
            if work_order_reconciliation_due and not scan_failure_recorded:
                _mark_work_order_reconciliation_failed()
            print(f"[AppFolioPoll] Scheduled operation failed: {e}")


def run():
    watch = "--watch" in sys.argv
    if watch:
        raise RuntimeError(
            "Standalone inbox watch mode is disabled; main.py owns the "
            "single recurring Gmail/AppFolio loop."
        )
    run_once()


if __name__ == "__main__":
    run()
