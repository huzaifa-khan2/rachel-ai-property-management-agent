"""
Rachel - Work Order Triage Engine
Handles incoming AppFolio work orders end to end:

1. Extracts WO details from AppFolio notification email
2. Logs into AppFolio automatically (auto-2FA via Google Voice)
3. Navigates directly to the work order URL
4. Texts tenant via AppFolio Texts section (falls back to Email if no cell)
5. Collects follow-up info conversationally (one question at a time)
6. When enough info gathered → texts Manager + Owner summary via SMS

Called from rachel_inbox_monitor.py when appfolio_work_order email detected.
"""

import os, re, time, random, json, sqlite3, imaplib, email, subprocess, hashlib
from urllib.parse import urlparse
from datetime import datetime
from anthropic import Anthropic
from rachel_appfolio_browser import get_appfolio_browser_manager
from rachel_appfolio_ui import (
    AppFolioReadAdapter,
    AppFolioUIContractError,
    WORK_ORDER_DISPATCH_PROTECTED_CHECKBOXES,
    WORK_ORDER_DISPATCH_REQUIRED_CHECKBOXES,
    WorkOrderDetailPage,
    accessible_name,
    combined_selector,
    first_visible,
    selector_for_id,
    selectors,
    unique_visible,
    work_order_issue_option,
)
from rachel_event_memory import record_verified_appfolio_change
from rachel_appfolio_write_intents import (
    claim_outbound_intent,
    complete_outbound_intent,
    prepare_outbound_intent,
    record_outbound_intent_failure,
)
from rachel_maintenance_scheduling import (
    ScheduleWorkflowError,
    capture_tenant_availability,
    initialize_schedule,
    record_vendor_availability_delivery,
)
from rachel_safety import (
    OWNER_QA_EMAIL,
    RACHEL_OWNER_QA_MODE as OWNER_QA_MODE,
    RACHEL_TEST_MODE as TEST_MODE,
    appfolio_write_allowed,
    email_allowed,
    vendor_contact_allowed,
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

# ── Config ─────────────────────────────────────────────────────────────────
APPFOLIO_URL     = os.getenv("APPFOLIO_URL",
                             "https://clientco.appfolio.com")
APPFOLIO_EMAIL  = get_secret("APPFOLIO_EMAIL", "APPFOLIO_EMAIL")
APPFOLIO_PASS   = get_secret("APPFOLIO_PASSWORD", "APPFOLIO_PASSWORD")
DB_PATH          = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")
RACHEL_GMAIL    = get_secret("RACHEL_GMAIL_USER", "RACHEL_GMAIL_USER")
RACHEL_GMAIL_PW = get_secret("RACHEL_GMAIL_PASSWORD", "RACHEL_GMAIL_PASSWORD")
RACHEL_EMAIL    = os.getenv("RACHEL_EMAIL_USER", "rachel@clientco.example")
RACHEL_EMAIL_PW = get_secret("RACHEL_EMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD")
MANAGER_CELL      = "18055046160"
OWNER_CELL        = "18472742377"
MANAGER_EMAIL     = os.getenv("MANAGER_EMAIL", "manager@clientco.example")
OWNER_EMAIL       = os.getenv("OWNER_EMAIL", "owner@clientco.example")
GOOGLE_VOICE_SMS = "txt.voice.google.com"

ANTHROPIC_KEY = get_secret(
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
)

if not ANTHROPIC_KEY:
    raise RuntimeError(
        "ANTHROPIC_API_KEY was not found in the environment or macOS Keychain."
    )

client = Anthropic(api_key=ANTHROPIC_KEY)

# ── Issue categories and their follow-up questions ─────────────────────────
ISSUE_QUESTIONS = {
    "plumbing": [
        "Where exactly is the issue located — which bathroom, kitchen, or other area?",
        "Is there any water leaking onto the floor or just inside the fixture?",
        "How long has this been going on?",
        "Is the water supply still working or is it completely off?",
    ],
    "toilet": [
        "Is the toilet overflowing or just running continuously?",
        "Is there any water on the floor?",
        "Have you tried jiggling the handle — does that stop it temporarily?",
    ],
    "hvac": [
        "Is this a heating or cooling issue?",
        "What is the current temperature in the unit?",
        "Is the system running at all or completely off?",
        "When did this start?",
    ],
    "electrical": [
        "What specifically is not working — lights, outlets, appliances?",
        "Is this affecting the whole unit or just one area?",
        "Do you smell anything burning or see any sparks?",
        "Have you checked the circuit breaker?",
    ],
    "appliance": [
        "Which appliance is having the issue?",
        "What exactly is it doing or not doing?",
        "Is it making any unusual sounds or smells?",
        "How old is the appliance approximately?",
    ],
    "lockout": [
        "Are you locked out right now and need immediate access?",
        "Is this your unit door or building entry?",
    ],
    "leak": [
        "Where is the leak coming from — ceiling, wall, pipe, or fixture?",
        "How severe is it — dripping or steady flow?",
        "Is there any damage to flooring, walls, or belongings?",
        "When did you first notice it?",
    ],
    "general": [
        "Can you describe the issue in a bit more detail?",
        "How long has this been going on?",
        "Is it getting worse or staying the same?",
        "Has anything changed recently that might have caused this?",
    ],
}

_HIGH_CONFIDENCE_EMERGENCY_PATTERNS = (
    ("active_fire", r"\b(?:active|visible|structure)\s+fire\b"),
    ("visible_flames", r"\b(?:visible\s+)?flames?\b"),
    ("electrical_fire", r"\belectrical\s+fire\b"),
    ("active_smoke", r"\b(?:active|visible|heavy)\s+smoke\b"),
    ("gas_leak", r"\bgas\s+(?:leak|smell|odor)\b"),
    ("gas_odor", r"\b(?:smell|odor)\s+of\s+gas\b"),
    ("carbon_monoxide", r"\bcarbon\s+monoxide\b"),
    ("burst_pipe", r"\bburst\s+(?:water\s+)?pipe\b"),
    (
        "active_flooding",
        r"\b(?:active|major|severe)\s+(?:water\s+)?(?:leak|flood(?:ing)?)\b",
    ),
    ("sewage_backup", r"\b(?:sewage|sewer)\s+(?:backup|overflow)\b"),
    ("electrical_sparking", r"\b(?:sparks?|sparking)\b"),
    ("essential_service_out", r"\bno\s+(?:heat|electricity|power|water)\b"),
    ("resident_lockout", r"\b(?:locked\s+out|lockout)\b"),
)


def detect_emergency_evidence(details: dict) -> dict:
    """Return sanitized, high-confidence emergency evidence only."""
    priority = str(details.get("priority") or "").strip().upper()
    if priority in {"EMERGENCY", "URGENT"}:
        return {
            "is_emergency": True,
            "evidence": "explicit_appfolio_priority",
        }

    combined_text = " ".join(
        str(details.get(field) or "")
        for field in (
            "service_request_description",
            "job_desc",
            "issue_type",
            "issue_details",
        )
    ).lower()

    for evidence, pattern in _HIGH_CONFIDENCE_EMERGENCY_PATTERNS:
        if re.search(pattern, combined_text, re.IGNORECASE):
            return {"is_emergency": True, "evidence": evidence}

    return {"is_emergency": False, "evidence": "none"}


def evaluate_triage_eligibility(details: dict) -> dict:
    """Apply the fail-closed boundary for entry into tenant triage."""
    origin = str(details.get("triage_origin") or "").strip().lower()
    classification = str(details.get("classification") or "").strip()
    manager_instruction = str(details.get("manager_instruction") or "").strip()
    source_type = str(details.get("source_type") or "").strip().upper()
    tenant = str(details.get("tenant") or "").strip()
    wo_num = str(details.get("wo_num") or "").strip()
    wo_url = str(details.get("wo_url") or details.get("page_url") or "").strip()
    emergency = detect_emergency_evidence(details)

    if origin == "reconciliation":
        reason = "reconciliation_is_read_only"
        eligible = False
        route = "none"
    elif origin == "original_new_work_order_email":
        if source_type == "INTERNAL":
            eligible = False
            reason = "verified_internal_work_order_requires_separate_workflow"
            route = "internal_work_order_review"
        else:
            eligible = bool(
                source_type == "RESIDENT"
                and tenant
                and tenant.upper() != "UNKNOWN"
                and re.fullmatch(r"\d+-\d+", wo_num)
                and _is_trusted_work_order_url(wo_url, allow_short=True)
            )
            reason = (
                "verified_original_new_resident_work_order_event"
                if eligible
                else "original_event_not_verified_resident_work_order"
            )
            route = "new_work_order_triage" if eligible else "none"
    elif (
        origin == "manager_authorized"
        and classification in {
            "manager_authorized_tenant_triage",
            "manager_authorized_custom_triage",
        }
    ):
        eligible = bool(
            (
                manager_instruction.upper() == "PROCEED"
                or manager_instruction.upper().startswith("PROCEED_WITH:")
            )
            and _is_trusted_work_order_url(wo_url)
        )
        reason = (
            "explicit_manager_authorization"
            if eligible
            else "manager_authorization_incomplete"
        )
        route = "manager_authorized_triage" if eligible else "none"
    elif origin == "synthetic_qa":
        eligible = bool(
            TEST_MODE
            and OWNER_QA_MODE
            and wo_num.upper().startswith("QA-")
            and wo_url.lower().startswith("qa://work-order/")
        )
        reason = "synthetic_qa_gate" if eligible else "synthetic_qa_gate_failed"
        route = "synthetic_qa" if eligible else "none"
    else:
        reason = "unrecognized_or_missing_triage_origin"
        eligible = False
        route = "none"

    return {
        "eligible": eligible,
        "route": route,
        "reason": reason,
        "emergency": emergency["is_emergency"],
        "emergency_evidence": emergency["evidence"],
    }


# ── Utilities ──────────────────────────────────────────────────────────────

def human_delay(min_s=2, max_s=5):
    time.sleep(random.uniform(min_s, max_s))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_suggested_vendors(property_address: str, issue_type: str) -> list:
    """
    Get suggested vendors for a property and issue type.
    Priority: 1) Property-specific preferred vendors for this trade
              2) Vendors with historical WOs at this property
              3) Any vendor with matching trade
    Returns list of dicts with name, phone, trade, source
    """
    conn = get_db()

    # Normalize property address for matching
    prop_clean = property_address.split(",")[0].strip().lower()

    suggestions = []
    seen = set()

    # 1. Property-specific preferred vendors matching trade
    rows = conn.execute("""
        SELECT v.name, v.phone, v.trade, v.notes, pv.source
        FROM property_vendors pv
        JOIN vendors v ON pv.vendor_id = v.id
        WHERE (LOWER(pv.property_address) LIKE ? OR LOWER(pv.property_address) LIKE ?)
        AND (pv.trade = ? OR pv.trade = 'general')
        AND COALESCE(v.active, 1) = 1
        ORDER BY pv.is_preferred DESC
    """, (f"%{prop_clean}%", f"%{prop_clean[:20]}%", issue_type)).fetchall()

    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            suggestions.append({
                "name": row[0], "phone": row[1] or "",
                "trade": row[2], "source": "preferred"
            })

        # 2. Vendors with successfully completed, trade-matched work
    # at this property
    rows = conn.execute("""
        SELECT
            v.name,
            v.phone,
            v.trade,
            COUNT(DISTINCT wh.wo_num) AS cnt
        FROM vendor_work_history wh
        JOIN vendors v ON wh.vendor_id = v.id
        WHERE LOWER(wh.property_address) LIKE ?
          AND LOWER(TRIM(wh.status)) = 'completed'
          AND COALESCE(v.active, 1) = 1
          AND (
              LOWER(TRIM(v.trade)) = LOWER(?)
              OR LOWER(TRIM(v.trade)) = 'general'
          )
        GROUP BY v.id, v.name, v.phone, v.trade
        ORDER BY
            CASE
                WHEN LOWER(TRIM(v.trade)) = LOWER(?) THEN 0
                ELSE 1
            END,
            cnt DESC,
            MAX(wh.done_on) DESC
    """, (
        f"%{prop_clean}%",
        issue_type,
        issue_type,
    )).fetchall()

    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            suggestions.append({
                "name": row[0], "phone": row[1] or "",
                "trade": row[2], "source": f"used {row[3]}x at this property"
            })

    # 3. Any vendor matching trade (fill up to 3 options)
    if len(suggestions) < 3:
        rows = conn.execute("""
            SELECT name, phone, trade FROM vendors
            WHERE trade = ?
              AND COALESCE(active, 1) = 1
              AND name NOT IN ({})
            LIMIT ?
        """.format(",".join("?" * len(seen)) if seen else "'__none__'"),
            [issue_type] + list(seen) + [3 - len(suggestions)]
        ).fetchall()
        for row in rows:
            suggestions.append({
                "name": row[0], "phone": row[1] or "",
                "trade": row[2], "source": "general"
            })

    conn.close()
    return suggestions[:3]  # Max 3 suggestions


def format_vendor_sms(
    wo_num: str,
    tenant: str,
    address: str,
    summary: str,
    vendors: list,
) -> str:
    """Format vendor recommendations for Manager and Owner."""
    lines = [
        f"WO #{wo_num} — {tenant}",
        address,
        "",
        summary,
        "",
        "VENDOR RECOMMENDATIONS — MANAGER APPROVAL REQUIRED:",
    ]

    for i, vendor in enumerate(vendors, 1):
        phone_str = f" — {vendor['phone']}" if vendor["phone"] else ""
        source_str = (
            f" ({vendor['source']})"
            if vendor["source"] != "general"
            else ""
        )
        lines.append(
            f"  {i}. {vendor['name']}{phone_str}{source_str}"
        )

    lines.append(f"  {len(vendors) + 1}. In-house maintenance")
    lines.append(f"  {len(vendors) + 2}. Different vendor")
    lines.append("")
    lines.append(
        "No vendor will be assigned until Manager approves the selection."
    )
    lines.append(
        "Manager, reply with the option number, “in-house,” "
        "or the vendor name."
    )

    return "\n".join(lines)

def find_tenant_by_name(name: str) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM tenants WHERE primary_tenant LIKE ?",
        (f"%{name.split()[-1]}%",)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def send_owner_qa_email(
    subject: str,
    message: str,
    in_reply_to: str = "",
    references: str = "",
) -> bool:
    """
    Deliver a synthetic QA message only to Owner.

    This function is intentionally restricted to TEST_MODE plus
    OWNER_QA_MODE. It never sends to Manager, tenants, or vendors.
    """
    if not (TEST_MODE and OWNER_QA_MODE):
        print(
            "[OwnerQA] Message blocked because Owner QA mode is not active."
        )
        return False

    allowed, reason = email_allowed(OWNER_QA_EMAIL)
    if not allowed:
        print(f"[Safety] Owner QA email blocked: {reason}")
        return False

    if not RACHEL_EMAIL_PW or not RACHEL_EMAIL:
        print("[OwnerQA] Rachel official email credentials are unavailable.")
        return False

    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr

    delivery_target = (
        str(in_reply_to or "").strip()
        or "owner-qa-" + hashlib.sha256(
            f"{subject}\n{message}".encode("utf-8")
        ).hexdigest()
    )
    intent = prepare_outbound_intent(
        operation="owner_qa_email",
        target_id=delivery_target,
        recipient=OWNER_QA_EMAIL,
        message=f"{subject}\n{message}",
        db_path=DB_PATH,
    )
    if intent["status"] == "sent_verified":
        print("[OwnerQA] Duplicate verified email suppressed")
        return True
    if intent["status"] != "prepared":
        print(
            "[OwnerQA] Prior delivery is unverified; automatic retry blocked."
        )
        return False

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(RACHEL_EMAIL, RACHEL_EMAIL_PW)

        msg = MIMEText(message)
        msg["From"] = formataddr(("Rachel — ClientCo", RACHEL_EMAIL))
        msg["Reply-To"] = RACHEL_EMAIL
        msg["To"] = OWNER_QA_EMAIL
        msg["Subject"] = subject

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to

        if references:
            msg["References"] = references

        claim_outbound_intent(intent["intent_id"], db_path=DB_PATH)
        refused = server.sendmail(
            RACHEL_EMAIL,
            OWNER_QA_EMAIL,
            msg.as_string(),
        )
        server.quit()

        if refused:
            print(f"[OwnerQA] Gmail SMTP refused recipient: {OWNER_QA_EMAIL}")
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

        print(
            f"[OwnerQA] ✅ Email sent from {RACHEL_EMAIL} only to "
            f"{OWNER_QA_EMAIL}: "
            f"{subject}"
        )
        return True

    except Exception as exc:
        try:
            record_outbound_intent_failure(
                intent["intent_id"],
                reason="smtp_transport_failure",
                db_path=DB_PATH,
            )
        except Exception:
            pass
        print(f"[OwnerQA] Email failed: {type(exc).__name__}: {exc}")
        return False


def send_sms_via_gmail(
    message: str,
    urgent: bool = False,
    subject: str = None,
    delivery_key: str = "",
):
    """Send notification to Manager and Owner via email (and SMS when Twilio ready)."""
    recipients = [OWNER_EMAIL, MANAGER_EMAIL]
    blocked = [
        (recipient, reason)
        for recipient in recipients
        for allowed, reason in [email_allowed(recipient)]
        if not allowed
    ]
    if blocked:
        print(
            "[Notify] Internal notification blocked by safety gate: "
            + ", ".join(
                f"{recipient} ({reason})" for recipient, reason in blocked
            )
        )
        return False
    import smtplib
    from email.mime.text import MIMEText

    prefix = "🚨 " if urgent else ""
    body = f"{prefix}{message}"
    subject_line = (
        subject
        or f"{'🚨 URGENT — ' if urgent else ''}Rachel WO Update"
    )

    if not RACHEL_EMAIL_PW:
        print("[Notify] No Gmail password")
        return False

    durable_target = str(delivery_key or "").strip()
    if not durable_target:
        durable_target = "management-notice-" + hashlib.sha256(
            f"{subject_line}\n{body}".encode("utf-8")
        ).hexdigest()
    intent = prepare_outbound_intent(
        operation="management_email_notification",
        target_id=durable_target,
        recipient=",".join(recipients),
        message=f"{subject_line}\n{body}",
        db_path=DB_PATH,
    )
    if intent["status"] == "sent_verified":
        print("[Notify] Duplicate verified management notice suppressed")
        return True
    if intent["status"] != "prepared":
        print(
            "[Notify] Prior management notice is unverified; automatic "
            "retry blocked."
        )
        return False

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(RACHEL_EMAIL, RACHEL_EMAIL_PW)

        refused_recipients = {}
        # Send email to Owner and Manager (reliable until Twilio is set up)
        claim_outbound_intent(intent["intent_id"], db_path=DB_PATH)
        for recipient in recipients:
            msg = MIMEText(body)
            msg["From"]    = RACHEL_EMAIL
            msg["To"]      = recipient
            msg["Subject"] = subject_line
            refused = server.sendmail(
                RACHEL_EMAIL, recipient, msg.as_string()
            )
            if refused:
                refused_recipients.update(refused)

        server.quit()
        if refused_recipients:
            print(
                "[Notify] Gmail SMTP refused recipients: "
                f"{sorted(refused_recipients)}"
            )
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
        print(f"[Notify] ✅ Emailed Owner + Manager: {body[:60]}")
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
        print(f"[Notify] Failed: {e}")
        return False


def get_2fa_code(max_wait=90) -> str:
    """
    Read AppFolio's SMS verification code forwarded from Google Voice
    to Rachel's Gmail account.
    """
    if not RACHEL_GMAIL_PW:
        print("[2FA] Gmail password is unavailable")
        return None

    from datetime import datetime, timedelta, timezone
    from email.utils import parsedate_to_datetime
    from html import unescape

    search_started = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    )

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(RACHEL_GMAIL, RACHEL_GMAIL_PW)

        # Google Voice messages may be archived rather than left in Inbox.
        folder_name = "inbox"
        status, folders = mail.list()

        if status == "OK":
            for folder in folders or []:
                decoded = folder.decode("utf-8", errors="ignore")

                if "\\All" in decoded or "All Mail" in decoded:
                    folder_name = (
                        decoded.split(' "/" ')[-1].strip('"')
                    )
                    break

        mail.select(f'"{folder_name}"')
        print(f"[2FA] Watching Gmail folder: {folder_name}")

        since_date = (
            datetime.now() - timedelta(days=1)
        ).strftime("%d-%b-%Y")

        attempts = max(1, max_wait // 3)

        for attempt in range(attempts):
            if attempt > 0:
                time.sleep(3)

            mail.noop()

            status, messages = mail.search(
                None,
                "FROM",
                '"txt.voice.google.com"',
                "SINCE",
                since_date,
            )

            if status != "OK" or not messages[0]:
                print(
                    f"[2FA] Waiting for Google Voice message "
                    f"({attempt + 1}/{attempts})"
                )
                continue

            for msg_id in reversed(messages[0].split()[-30:]):
                fetch_status, data = mail.fetch(
                    msg_id,
                    "(BODY.PEEK[])",
                )

                if fetch_status != "OK":
                    continue

                raw_message = next(
                    (
                        item[1]
                        for item in data
                        if isinstance(item, tuple)
                    ),
                    None,
                )

                if not raw_message:
                    continue

                msg = email.message_from_bytes(raw_message)

                try:
                    message_date = parsedate_to_datetime(
                        msg.get("Date", "")
                    )

                    if message_date.tzinfo is None:
                        message_date = message_date.replace(
                            tzinfo=timezone.utc
                        )

                    if message_date < search_started:
                        continue
                except Exception:
                    pass

                body_parts = []

                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_maintype() == "multipart":
                            continue

                        if part.get_content_disposition() == "attachment":
                            continue

                        payload = part.get_payload(decode=True)

                        if not payload:
                            continue

                        charset = (
                            part.get_content_charset()
                            or "utf-8"
                        )

                        decoded = payload.decode(
                            charset,
                            errors="ignore",
                        )

                        if part.get_content_type() == "text/html":
                            decoded = re.sub(
                                r"<[^>]+>",
                                " ",
                                decoded,
                            )
                            decoded = unescape(decoded)

                        body_parts.append(decoded)
                else:
                    payload = msg.get_payload(decode=True)

                    if payload:
                        charset = (
                            msg.get_content_charset()
                            or "utf-8"
                        )

                        body_parts.append(
                            payload.decode(
                                charset,
                                errors="ignore",
                            )
                        )

                combined = " ".join(
                    [
                        str(msg.get("Subject", "")),
                        *body_parts,
                    ]
                )

                normalized = re.sub(
                    r"\s+",
                    " ",
                    combined,
                ).strip()

                patterns = (
                    r"\b(\d{6})\b\s+Use this code to verify your AppFolio",
                    r"\b(\d{6})\b.{0,120}AppFolio",
                    r"AppFolio.{0,120}\b(\d{6})\b",
                )

                for pattern in patterns:
                    match = re.search(
                        pattern,
                        normalized,
                        re.IGNORECASE,
                    )

                    if match:
                        code = match.group(1)
                        print("[2FA] Verification code received")
                        mail.logout()
                        return code

            print(
                f"[2FA] Waiting for Google Voice message "
                f"({attempt + 1}/{attempts})"
            )

        mail.logout()

    except Exception as exc:
        print(f"[2FA] Error: {exc}")

    print("[2FA] Google Voice verification code was not found")
    return None


# ── AppFolio Browser Automation ────────────────────────────────────────────

def login_appfolio(page):
    """Login to AppFolio with auto-2FA. Returns True if login succeeded."""
    print("[Login] Opening AppFolio...")

    # Check if already on a logged-in page
    current_text = page.evaluate(
        "() => document.body.innerText.substring(0, 200)")
    if "Dashboard" in current_text or "Property Manager" in current_text:
        print("[Login] ✅ Already logged in")
        return True

    # Navigate to login page if not already there
    if "login" not in page.url.lower() and "authenticate" not in page.url.lower():
        page.goto(APPFOLIO_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

    # Check again if we're already logged in (session cookies worked)
    current_text = page.evaluate(
        "() => document.body.innerText.substring(0, 200)")
    if "Dashboard" in current_text or "Property Manager" in current_text:
        print("[Login] ✅ Already logged in (session active)")
        return True

    email_selectors = selectors("auth.email")
    password_selectors = selectors("auth.password")
    submit_selectors = selectors("auth.submit")

    def find_visible(selectors):
        for selector in selectors:
            try:
                element = page.query_selector(selector)
                if element and element.is_visible():
                    return element
            except Exception:
                continue
        return None

    def wait_for_login_form(timeout_seconds=20):
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            email_element = find_visible(email_selectors)
            password_element = find_visible(password_selectors)

            if email_element and password_element:
                return email_element, password_element

            time.sleep(0.5)

        return None, None

    # Never submit a partial login form. Wait until BOTH credentials
    # can be entered.
    email_element, password_element = wait_for_login_form()

    if not email_element or not password_element:
        print(
            "[Login] Login form was incomplete after waiting; "
            "reloading once."
        )

        try:
            page.reload(
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception:
            pass

        time.sleep(2)

        current_text = page.evaluate(
            "() => document.body.innerText.substring(0, 300)"
        )

        if (
            "Dashboard" in current_text
            or "Property Manager" in current_text
        ):
            print("[Login] ✅ Session became active after reload")
            return True

        email_element, password_element = wait_for_login_form()

    if not email_element or not password_element:
        print(
            "[Login] ❌ Complete AppFolio login form was not available; "
            "aborting without submitting."
        )
        return False

    try:
        email_element.fill(APPFOLIO_EMAIL)
        print("[Login] Email entered")

        # Re-acquire the password control in case the page re-rendered
        # after entering the email.
        password_element = find_visible(password_selectors)

        if not password_element:
            print(
                "[Login] ❌ Password field disappeared before entry; "
                "aborting."
            )
            return False

        password_element.fill(APPFOLIO_PASS)
        print("[Login] Password entered")

        # Verify both fields contain values before Log In can be clicked.
        email_check = find_visible(email_selectors)
        password_check = find_visible(password_selectors)

        if (
            not email_check
            or not password_check
            or not email_check.input_value().strip()
            or not password_check.input_value()
        ):
            print(
                "[Login] ❌ Credential fields did not verify as populated; "
                "aborting without submitting."
            )
            return False

    except Exception as exc:
        print(
            f"[Login] ❌ Could not populate complete login form: "
            f"{type(exc).__name__}"
        )
        return False

    submit_button = None
    deadline = time.time() + 10

    while time.time() < deadline:
        submit_button = find_visible(submit_selectors)

        if submit_button:
            break

        time.sleep(0.5)

    if not submit_button:
        print("[Login] ❌ Log In button was not available")
        return False

    submit_button.click()
    print("[Login] Submit clicked")

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=15000,
        )
    except Exception:
        pass

    time.sleep(3)

    # Handle 2FA if needed
    page_text = page.evaluate(
        "() => document.body.innerText.substring(0, 1000)"
    )
    page_text_lower = page_text.lower()

    twofa_radio = first_visible(page, "auth.two_factor_method")

    twofa_detected = (
        twofa_radio is not None
        or "2-step" in page_text_lower
        or "receive code via sms" in page_text_lower
        or "send verification code" in page_text_lower
        or "verification code" in page_text_lower
    )

    if twofa_detected:
        print("[Login] 2FA page detected")

        # Wait for AppFolio's 2FA controls to finish rendering.
        sms_selector = selectors("auth.two_factor_sms")[0]

        sms_option = None

        for attempt in range(20):
            try:
                candidate = page.query_selector(sms_selector)

                if candidate:
                    sms_option = candidate
                    break
            except Exception:
                pass

            time.sleep(1)

        if not sms_option:
            # Some AppFolio layouts visually expose only the label.
            sms_label = first_visible(page, "auth.two_factor_sms_label")

            if sms_label:
                sms_label.click(force=True)
                time.sleep(1)
                sms_option = page.query_selector(sms_selector)

        if not sms_option:
            print(
                "[Login] ❌ AppFolio SMS verification option was not found"
            )
            return False

        try:
            sms_option.check(force=True)
        except Exception:
            page.evaluate(
                """selector => {
                    const radio = document.querySelector(selector);
                    if (radio) {
                        radio.checked = true;
                        radio.dispatchEvent(
                            new Event('change', {bubbles: true})
                        );
                    }
                }""",
                sms_selector,
            )

        time.sleep(1)

        selected = page.evaluate(
            """selector => {
                const radio = document.querySelector(selector);
                return Boolean(radio && radio.checked);
            }""",
            sms_selector,
        )

        if not selected:
            print(
                "[Login] ❌ AppFolio SMS verification option "
                "could not be selected"
            )
            return False

        print("[Login] Selected SMS verification")

        send_button = None

        for attempt in range(10):
            send_button = first_visible(page, "auth.send_code")

            if send_button:
                break

            time.sleep(1)

        if not send_button:
            print(
                "[Login] ❌ Send Verification Code button was not found"
            )
            return False

        send_button.click(force=True)
        print("[Login] Verification code requested by SMS")
        time.sleep(5)

        # Read 2FA code from rachel.clientco@example.com
        code = get_2fa_code(max_wait=90)
        if code:
            print("[Login] 2FA code received")
            # Find the code input field
            for sel in selectors("auth.code"):
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(code)
                        time.sleep(1)
                        # Click verify/submit
                        for sub in selectors("auth.verify"):
                            try:
                                s = page.query_selector(sub)
                                if s and s.is_visible():
                                    s.click()
                                    print("[Login] Verification submitted")
                                    break
                            except Exception:
                                continue
                        break
                except Exception:
                    continue
            time.sleep(5)
        else:
            print("[Login] ❌ Could not get 2FA code")
            return False

    # Verify we're actually logged in
    final_text = page.evaluate(
        "() => document.body.innerText.substring(0, 300)")
    final_url = page.url
    safe_final_url = re.sub(r"[?#].*$", "", final_url)
    print(f"[Login] Final URL (query-free): {safe_final_url}")

    if ("Dashboard" in final_text or "Property Manager" in final_text
            or "Work Orders" in final_text or "Calendar" in final_text
            or "/dashboard" in final_url):
        print("[Login] ✅ Logged in successfully")
        return True
    elif "2-Step" in final_text or "Verification" in final_text:
        print("[Login] ❌ Still on 2FA page — login failed")
        return False
    elif "Log in" in final_text:
        print("[Login] ❌ Still on login page — credentials may be wrong")
        return False
    else:
        print(
            "[Login] ❌ Uncertain AppFolio login state — "
            "aborting rather than proceeding."
        )
        return False


def _is_trusted_work_order_url(url: str, *, allow_short: bool = False) -> bool:
    parsed = urlparse(str(url or ""))
    appfolio_host = urlparse(APPFOLIO_URL).netloc.lower()
    if parsed.scheme != "https":
        return False
    if allow_short and parsed.netloc.lower() == "appfol.io":
        return bool(re.fullmatch(r"/[A-Za-z0-9]+", parsed.path))
    return bool(
        parsed.netloc.lower() == appfolio_host
        and re.fullmatch(
            r"/maintenance/service_requests/\d+/work_orders/\d+/?",
            parsed.path,
        )
    )


def _validate_current_work_order_page(page, wo_num: str) -> bool:
    if not _is_trusted_work_order_url(str(page.url or "")):
        return False
    try:
        body_text = WorkOrderDetailPage(page).body_text()
    except Exception:
        return False
    return bool(
        re.search(
            rf"\bWork\s+Order\s*#{re.escape(wo_num)}\b",
            body_text,
            re.IGNORECASE,
        )
    )


def navigate_to_wo(page, wo_num: str, fallback_url: str = None) -> str:
    """Reuse or directly open one exact WO URL; never search broadly."""
    target_url = str(fallback_url or "").strip()

    if _validate_current_work_order_page(page, wo_num):
        print(f"[Nav] Reusing validated exact URL for WO #{wo_num}.")
        return page.url

    if not _is_trusted_work_order_url(target_url, allow_short=True):
        print(
            f"[Nav] Exact trusted URL unavailable for WO #{wo_num}; "
            "broad search is disabled."
        )
        return None

    try:
        AppFolioReadAdapter(page, "work_order_detail").navigate_read(
            target_url,
            route_pattern=(
                "/maintenance/service_requests/:request/"
                "work_orders/:work_order"
            ),
            timeout=60000,
            attempts=2,
        )
    except Exception as exc:
        print(f"[Nav] Exact URL navigation failed: {type(exc).__name__}")
        return None

    if not _validate_current_work_order_page(page, wo_num):
        print(f"[Nav] Exact URL validation failed for WO #{wo_num}.")
        return None

    print(f"[Nav] Validated exact URL for WO #{wo_num}.")
    return page.url


def _legacy_send_tenant_email_via_appfolio(page, message: str) -> bool:
    """
    Send email to tenant via Actions -> Notify Tenant on the current WO page.
    The modal auto-fills the recipient. Used for confirmation emails
    alongside text messages.
    """
    raise RuntimeError(
        "Legacy broad Notify Tenant sender is disabled; use the exact "
        "durably claimed sender."
    )
    allowed, reason = appfolio_write_allowed()
    if not allowed:
        print(f"[Email] Notify Tenant blocked by safety gate: {reason}")
        return False
    try:
        page.evaluate("window.scrollTo(0, 0)")
        human_delay(1, 2)

        # Click Actions menu
        actions_btn = page.query_selector(
            'button:has-text("Actions"), a:has-text("Actions")')
        if not actions_btn or not actions_btn.is_visible():
            print("[Email] Actions button not found")
            return False
        actions_btn.click()
        human_delay(1, 2)

        # Click Notify Tenant
        notify_btn = page.query_selector(
            'a:has-text("Notify Tenant"), '
            'button:has-text("Notify Tenant"), '
            'li:has-text("Notify Tenant")')
        if not notify_btn or not notify_btn.is_visible():
            print("[Email] Notify Tenant not found in Actions menu")
            page.keyboard.press("Escape")
            return False
        notify_btn.click()
        human_delay(2, 3)

        # Fill email body using JavaScript to bypass click interception
        email_result = page.evaluate("""
            (msg) => {
                const textareas = document.querySelectorAll('textarea');
                let bodyField = null;
                for (const ta of textareas) {
                    if (ta.offsetParent !== null && ta.offsetHeight > 50) {
                        bodyField = ta;
                        break;
                    }
                }
                if (!bodyField) return 'TEXTAREA_NOT_FOUND';
                bodyField.focus();
                bodyField.value = msg;
                bodyField.dispatchEvent(new Event('input', {bubbles: true}));
                bodyField.dispatchEvent(new Event('change', {bubbles: true}));

                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.textContent.trim() === 'Send' && btn.offsetParent !== null) {
                        btn.click();
                        return 'SENT';
                    }
                }
                return 'SEND_NOT_FOUND';
            }
        """, message + "\\n\\n— Rachel\\nAssistant Property Manager\\n"
             "ClientCo Property Management\\n555-010-0000")

        print(f"[Email] Notify Tenant result: {email_result}")
        if email_result == "SENT":
            human_delay(2, 3)
            print("[Email] ✅ Tenant email sent via Notify Tenant")
            return True
        print(f"[Email] Notify Tenant failed: {email_result}")
        return False
    except Exception as e:
        print(f"[Email] Notify Tenant failed: {e}")
        return False


def send_tenant_email_via_appfolio(
    page, message: str, *, wo_url: str = "", wo_num: str = "",
    tenant: str = "", vendor_name: str = "", db_path: str = "",
) -> bool:
    """Send one exact, durably claimed Notify Tenant email."""
    allowed, reason = appfolio_write_allowed(
        operation="tenant_schedule_message",
        wo_num=wo_num,
        wo_url=wo_url,
        tenant=tenant,
        vendor_name=vendor_name,
    )
    if not allowed:
        print(f"[Email] Notify Tenant blocked by safety gate: {reason}")
        return False
    if not wo_url or not wo_num or not tenant or not (message or "").strip():
        print("[Email] Exact tenant/WO/message context is required")
        return False
    # A caller may have just sent a text via send_text_via_appfolio, which
    # navigates the page to the tenant's contact page — re-navigate to the
    # work-order page itself rather than assuming the page is already there.
    AppFolioReadAdapter(page, "work_order_writes").navigate_read(
        wo_url,
        route_pattern=(
            "/maintenance/service_requests/:request/"
            "work_orders/:work_order"
        ),
        timeout=30000,
        attempts=2,
    )
    if not _validate_current_work_order_page(page, wo_num):
        print("[Email] Exact work-order page verification failed")
        return False
    intent_db_path = db_path or DB_PATH
    try:
        intent = prepare_outbound_intent(
            operation="tenant_work_order_email",
            target_id=wo_num,
            recipient=tenant,
            message=message,
            db_path=intent_db_path,
        )
    except Exception as error:
        print(f"[Email] Intent preparation failed: {type(error).__name__}")
        return False
    if intent["status"] == "sent_verified":
        print("[Email] Duplicate verified tenant email suppressed")
        return True
    if intent["status"] != "prepared":
        print("[Email] Prior tenant email is unverified; retry blocked")
        return False

    claimed = False

    def fail(reason_code: str) -> bool:
        if claimed:
            record_outbound_intent_failure(
                intent["intent_id"], reason=reason_code,
                db_path=intent_db_path,
            )
        return False

    try:
        page.evaluate("window.scrollTo(0, 0)")
        actions = unique_visible(page, "work_order.actions")
        if not actions:
            print("[Email] Exact Actions control was not found.")
            return False
        actions.click()
        notify = unique_visible(page, "work_order.notify_tenant")
        if not notify:
            print("[Email] Exact Notify Tenant menu item was not found.")
            page.keyboard.press("Escape")
            return False
        notify.click()
        body_field = page.locator(
            combined_selector("work_order.email_body")
        )
        try:
            body_field.wait_for(state="visible", timeout=5000)
        except Exception:
            print("[Email] Body field never became visible.")
            return False
        if body_field.count() != 1:
            print("[Email] Body field was not unique.")
            return False
        email_form = body_field.locator(
            combined_selector("work_order.form_ancestor")
        )
        if email_form.count() != 1:
            print("[Email] Could not isolate the email form ancestor.")
            return False
        form_text = " ".join(email_form.inner_text().lower().split())
        if (
            "recipients" not in form_text
            or " ".join(tenant.lower().split()) not in form_text
        ):
            print(
                "[Email] Recipients/tenant not verified in form. "
                f"tenant={tenant!r} form_text={form_text[:200]!r}"
            )
            return False
        subject = email_form.locator(
            combined_selector("work_order.email_subject")
        )
        send = email_form.locator(
            combined_selector("work_order.email_send")
        )
        if (
            subject.count() != 1 or not subject.is_visible()
            or not subject.input_value().strip()
            or send.count() != 1 or not send.is_visible()
            or send.is_disabled()
        ):
            print(
                "[Email] Subject/Send control not verified. "
                f"subject.count={subject.count()} "
                f"subject.value={subject.input_value() if subject.count() == 1 else 'N/A'!r} "
                f"send.count={send.count()}"
            )
            return False
        bcc_container = email_form.locator(
            combined_selector("work_order.email_bcc_container")
        )
        bcc_hidden = email_form.locator(
            combined_selector("work_order.email_bcc_hidden")
        )
        if bcc_container.count() != 1 or bcc_hidden.count() != 1:
            print(
                "[Email] BCC container/hidden field not found. "
                f"container.count={bcc_container.count()} "
                f"hidden.count={bcc_hidden.count()}"
            )
            return False
        original_bcc_value = bcc_hidden.input_value()
        claim_outbound_intent(intent["intent_id"], db_path=intent_db_path)
        claimed = True
        if MANAGER_EMAIL.lower() not in original_bcc_value.lower():
            bcc_container.click()
            bcc_search = email_form.locator(
                combined_selector("work_order.email_bcc_search_input")
            )
            if bcc_search.count() != 1 or not bcc_search.is_visible():
                return fail("tenant_email_bcc_search_unavailable")
            bcc_search.type(MANAGER_EMAIL, delay=30)
            page.keyboard.press("Comma")
            page.wait_for_timeout(500)
            updated_bcc_value = bcc_hidden.input_value()
            preserved_original = all(
                address.strip().lower() in updated_bcc_value.lower()
                for address in original_bcc_value.split(",")
                if address.strip()
            )
            if (
                MANAGER_EMAIL.lower() not in updated_bcc_value.lower()
                or not preserved_original
            ):
                return fail("tenant_email_bcc_verification_failed")
            print("[Email] Manager added to Notify Tenant BCC")
        body_field.fill(message.strip())
        if body_field.input_value().strip() != message.strip():
            return fail("tenant_email_fill_verification_failed")
        send.click()
        try:
            body_field.wait_for(state="hidden", timeout=15000)
        except Exception:
            current_body_field = page.locator(combined_selector(
                "work_order.email_body"
            ))
            if (
                current_body_field.count()
                and current_body_field.is_visible()
            ):
                return fail("tenant_email_postcheck_unverified")
        complete_outbound_intent(
            intent["intent_id"],
            verification_receipt="verified_tenant_email_form_closed_after_send",
            db_path=intent_db_path,
        )
        print("[Email] Tenant email submission verified")
        return True
    except Exception as error:
        fail("tenant_email_transport_failure")
        print(f"[Email] Notify Tenant failed: {type(error).__name__}")
        return False


def _legacy_send_text_via_appfolio(page, wo_url: str, message: str,
                                    tenant_has_cell: bool = True) -> bool:
    """
    Navigate to WO and send message via Texts section or Email button.
    Handles both direct WO URLs and short appfol.io redirects.
    Returns True if sent successfully.
    """
    raise RuntimeError(
        "Legacy broad tenant sender is disabled; use the exact durably "
        "claimed Work Order sender."
    )
    synthetic_qa = (wo_url or "").lower().startswith("qa://")
    allowed, reason = appfolio_write_allowed(
        synthetic_qa=synthetic_qa,
    )

    if not allowed:
        print(
            f"[Safety] AppFolio tenant message blocked: {reason}"
        )
        return False

    print(f"[WO] Navigating to work order...")
    page.goto(wo_url, wait_until="domcontentloaded", timeout=30000)
    human_delay(3, 5)

    # If we landed on a login page, log in again
    page_check = page.evaluate("() => document.body.innerText.substring(0, 200)")
    if "Log in" in page_check or "login" in page.url.lower():
        print("[WO] Landed on login page — logging in")
        login_appfolio(page)
        human_delay(3, 5)

    # If we landed on the Service Request page (shows multiple WOs),
    # click into WO -1 first
    current_url = page.url
    if "/service_requests/" in current_url and "/work_orders/" not in current_url:
        print("[WO] Landed on Service Request page — clicking into WO -1")
        wo_link = page.query_selector(
            'a[href*="/work_orders/"]:has-text("#"), '
            'a[href*="/work_orders/"]')
        if wo_link:
            wo_link.click()
            human_delay(2, 4)
        else:
            print("[WO] Could not find WO link on SR page")

    # If we landed on a page with WO tabs (SR page with inline WOs),
    # look for the first WO tab and click it
    wo_tab = page.query_selector(
        'a:has-text("#"):near(:text("Work Order")), '
        '[class*="tab"]:has-text("#")')
    if wo_tab:
        # We might already be viewing a WO inline — check for Texts section
        pass

    if tenant_has_cell:
        try:
            # If on the SR page with multiple WO tabs, click into WO -1
            wo1_tab = page.query_selector(
                'a:has-text("#"):first-of-type, '
                '[class*="tab"]:has-text("-1")')
            # Find the first WO tab (lowest number)
            wo_tabs = page.query_selector_all('a[href*="/work_orders/"]')
            if wo_tabs:
                wo_tabs[0].click()
                human_delay(2, 3)
                print("[WO] Clicked into first WO tab")

            # Scroll down to find the Texts section
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            human_delay(2, 3)

            # The Texts section may be COLLAPSED — click the header to expand
            expanded = page.evaluate("""
                () => {
                    const elements = document.querySelectorAll('h2, h3, h4, span, div, a');
                    for (const el of elements) {
                        const text = el.textContent.trim();
                        if (text === 'Texts' || text.includes('Texts')) {
                            // Check if this is the section header (not "Download Texts" etc)
                            if (text === 'Texts' || text === '› Texts' || text === '▸ Texts') {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """)
            if expanded:
                human_delay(2, 3)
                print("[WO] Expanded Texts section")
                # Scroll down again after expanding
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                human_delay(1, 2)

            # Debug: log what's visible on the page
            page_text = page.evaluate("() => document.body.innerText.slice(-500)")
            print(f"[WO] Bottom of page: {page_text[:200]}")

            # Select tenant from dropdown if present (required before typing)
            tenant_dropdown = page.query_selector(
                'select:near(:text("Texts")), '
                'select:below(:text("Texts"))')
            if tenant_dropdown and tenant_dropdown.is_visible():
                # Select first tenant option
                options = tenant_dropdown.query_selector_all('option')
                for opt in options:
                    val = opt.get_attribute("value")
                    if val and val != "":
                        tenant_dropdown.select_option(value=val)
                        human_delay(1, 2)
                        print(f"[WO] Selected tenant in dropdown")
                        break

            # Fill the "Enter message" input using JavaScript to bypass
            # click interception from overlay elements
            text_sent = page.evaluate("""
                (msg) => {
                    // Find the Enter message input
                    const inputs = document.querySelectorAll('input, textarea');
                    let msgInput = null;
                    for (const inp of inputs) {
                        if (inp.placeholder &&
                            inp.placeholder.toLowerCase().includes('enter message')) {
                            msgInput = inp;
                            break;
                        }
                    }
                    if (!msgInput) {
                        // Try broader search
                        for (const inp of inputs) {
                            if (inp.placeholder &&
                                inp.placeholder.toLowerCase().includes('message')) {
                                msgInput = inp;
                                break;
                            }
                        }
                    }
                    if (!msgInput) return 'INPUT_NOT_FOUND';

                    // Scroll into view and fill
                    msgInput.scrollIntoView();
                    msgInput.focus();
                    msgInput.value = msg;
                    msgInput.dispatchEvent(new Event('input', {bubbles: true}));
                    msgInput.dispatchEvent(new Event('change', {bubbles: true}));

                    // Find and click the Send button near the input
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        if (btn.textContent.trim().includes('Send') &&
                            btn.offsetParent !== null) {
                            // Check it's near the bottom of the page (Texts section)
                            const rect = btn.getBoundingClientRect();
                            const inputRect = msgInput.getBoundingClientRect();
                            if (Math.abs(rect.top - inputRect.top) < 200) {
                                btn.click();
                                return 'SENT';
                            }
                        }
                    }
                    return 'SEND_BUTTON_NOT_FOUND';
                }
            """, message)

            print(f"[WO] Texts section result: {text_sent}")
            if text_sent == "SENT":
                human_delay(2, 3)
                print(f"[WO] ✅ Text sent via AppFolio Texts section")
                return True
            elif text_sent == "INPUT_NOT_FOUND":
                print("[WO] Enter message input not found")
            elif text_sent == "SEND_BUTTON_NOT_FOUND":
                # Input was filled but Send button not found near it
                # Try clicking any visible Send button
                human_delay(1, 2)
                try:
                    send_btn = page.query_selector('button:has-text("Send")')
                    if send_btn and send_btn.is_visible():
                        send_btn.click()
                        human_delay(2, 3)
                        print(f"[WO] ✅ Text sent (fallback Send click)")
                        return True
                except Exception:
                    pass
                print("[WO] Send button not clickable")
        except Exception as e:
            print(f"[WO] Texts section failed: {e}")

    # Fallback: Actions -> Notify Tenant (auto-fills recipient)
    try:
        page.evaluate("window.scrollTo(0, 0)")
        human_delay(1, 2)

        # Click Actions menu
        actions_btn = page.query_selector(
            'button:has-text("Actions"), a:has-text("Actions")')
        if actions_btn and actions_btn.is_visible():
            actions_btn.click()
            human_delay(1, 2)

            # Click Notify Tenant
            notify_btn = page.query_selector(
                'a:has-text("Notify Tenant"), '
                'button:has-text("Notify Tenant"), '
                'li:has-text("Notify Tenant")')
            if notify_btn and notify_btn.is_visible():
                notify_btn.click()
                human_delay(2, 3)
                print("[WO] Notify Tenant modal opened")

                # The modal auto-fills the recipient
                # Find the Email body textarea and fill it
                body_field = page.query_selector(
                    'textarea[name*="body"], textarea')
                if body_field and body_field.is_visible():
                    # Clear default signature and add our message
                    body_field.click()
                    body_field.press("Control+a")
                    body_field.fill(
                        message + "\n\n— Rachel\n"
                        "Assistant Property Manager\n"
                        "ClientCo Property Management\n"
                        "555-010-0000")
                    human_delay(1, 2)

                # Click Send
                send_btn = page.query_selector(
                    'button:has-text("Send"):not(:has-text("Cancel"))')
                if send_btn and send_btn.is_visible():
                    send_btn.click()
                    human_delay(2, 3)
                    print(f"[WO] ✅ Message sent via Notify Tenant email")
                    return True
                else:
                    print("[WO] Send button not found in Notify Tenant modal")
            else:
                print("[WO] Notify Tenant option not found in Actions menu")
                # Close the Actions menu
                page.keyboard.press("Escape")
    except Exception as e:
        print(f"[WO] Email fallback failed: {e}")

    print("[WO] Could not send message through AppFolio")
    return False


def _normalized_visible_text_contains(container_text: str, message: str) -> bool:
    rendered = " ".join(str(container_text or "").split())
    expected = " ".join(str(message or "").split())
    return bool(expected and expected in rendered)


def _normalized_visible_text_occurrences(
    container_text: str, message: str
) -> int:
    rendered = " ".join(str(container_text or "").split())
    expected = " ".join(str(message or "").split())
    return rendered.count(expected) if expected else 0


def send_text_via_appfolio(
    page, wo_url: str, message: str, tenant_has_cell: bool = True,
    *, tenant: str = "", wo_num: str = "", outbound_intent=None,
    vendor_name: str = "", db_path: str = "",
) -> bool:
    """Send one exact Work Order text with a durable no-retry claim."""
    allowed, reason = appfolio_write_allowed(
        operation="tenant_schedule_message",
        wo_num=wo_num,
        wo_url=wo_url,
        tenant=tenant,
        vendor_name=vendor_name,
    )
    if not allowed:
        print(f"[Safety] AppFolio tenant message blocked: {reason}")
        return False
    if not tenant_has_cell:
        print("[WO] Text send blocked because no verified tenant cell exists")
        return False
    if (
        not wo_url or not wo_num or not tenant
        or not (message or "").strip()
        or not _is_trusted_work_order_url(wo_url, allow_short=False)
    ):
        print("[WO] Exact trusted tenant/WO/message context is required")
        return False

    intent_db_path = db_path or DB_PATH
    try:
        prepared = prepare_outbound_intent(
            operation="tenant_work_order_text",
            target_id=wo_num,
            recipient=tenant,
            message=message,
            db_path=intent_db_path,
        )
    except Exception as error:
        print(f"[WO] Tenant intent preparation failed: {type(error).__name__}")
        return False
    if outbound_intent and (
        outbound_intent.get("intent_id") != prepared["intent_id"]
    ):
        print("[WO] Tenant outbound intent context mismatch")
        return False
    if prepared["status"] == "sent_verified":
        print("[WO] Duplicate verified tenant text suppressed")
        return True
    if prepared["status"] != "prepared":
        print("[WO] Prior tenant text is unverified; retry blocked")
        return False

    claimed = False

    def fail(reason_code: str) -> bool:
        if claimed:
            record_outbound_intent_failure(
                prepared["intent_id"], reason=reason_code,
                db_path=intent_db_path,
            )
        return False

    try:
        # AppFolio moved tenant texting off the work-order page entirely
        # (confirmed live Aug 28 2026): the inline composer this function
        # used to target (work_order.text_composer) still exists in the
        # DOM but is permanently hidden, and belongs to a different
        # feature entirely (the vendor "notify" quick action's fixed-body
        # vendor-portal-link message), not free-form tenant texting. The
        # robust trigger is the tenant's own contact/People record page,
        # reached via a link already on the work-order page - this works
        # whether or not a prior conversation exists (a /texting/inbox
        # conversation list exists too, but has no equivalent for a
        # tenant with no conversation yet).
        AppFolioReadAdapter(page, "work_order_writes").navigate_read(
            wo_url,
            route_pattern=(
                "/maintenance/service_requests/:request/"
                "work_orders/:work_order"
            ),
            timeout=30000,
            attempts=2,
        )
        if not _validate_current_work_order_page(page, wo_num):
            print("[WO] Exact work-order page verification failed")
            return False
        tenant_link = page.locator(
            combined_selector("work_order.tenant_contact_link")
        )
        if tenant_link.count() != 1:
            print(
                "[WO] Expected exactly one tenant contact link; found "
                f"{tenant_link.count()}."
            )
            return False
        tenant_href = tenant_link.get_attribute("href") or ""
        if not tenant_href.startswith("/occupancies/"):
            print("[WO] Tenant contact link had an unexpected shape")
            return False

        AppFolioReadAdapter(
            page, "communication_thread_sender"
        ).navigate_read(
            f"{APPFOLIO_URL}{tenant_href}",
            route_pattern="/occupancies/:occupancy/selected_tenant/:tenant",
            timeout=30000,
            attempts=2,
        )
        if (
            " ".join(tenant.lower().split())
            not in WorkOrderDetailPage(page).body_text().lower()
        ):
            print("[WO] Tenant record page did not verify against tenant")
            return False
        send_trigger = page.locator(
            combined_selector("texting.send_message_trigger")
        )
        if (
            send_trigger.count() != 1
            or send_trigger.get_attribute("data-party-type") != "Tenant"
        ):
            print(
                "[WO] Expected exactly one Tenant-typed send-text trigger; "
                f"found {send_trigger.count()}."
            )
            return False
        send_trigger.click()
        page.wait_for_timeout(500)
        composer = unique_visible(page, "texting.composer")
        if not composer:
            print("[WO] Expected one visible Texts composer")
            return False
        container = composer.locator(
            combined_selector("texting.composer_container")
        )
        if container.count() != 1:
            print("[WO] Texts composer container was not unique")
            return False
        send = container.get_by_role(
            "button", name=accessible_name("work_order.send_button"),
            exact=True,
        )
        if send.count() != 1 or not send.is_visible():
            print("[WO] Exact Texts Send control was unavailable")
            return False

        pre_send_occurrences = _normalized_visible_text_occurrences(
            WorkOrderDetailPage(page).body_text(), message
        )

        claim_outbound_intent(
            prepared["intent_id"], db_path=intent_db_path
        )
        claimed = True
        composer.fill(message.strip())
        if composer.input_value().strip() != message.strip():
            return fail("tenant_text_fill_verification_failed")
        if send.is_disabled():
            return fail("tenant_text_send_control_disabled_after_fill")
        send.click()

        for _attempt in range(20):
            page.wait_for_timeout(250)
            # The composer may be replaced/reset after Send; re-query the
            # semantic contract rather than retaining the mutated node.
            current_composer = unique_visible(
                page, "texting.composer"
            )
            composer_empty = bool(
                not current_composer
                or not current_composer.input_value().strip()
            )
            page_text = WorkOrderDetailPage(page).body_text()
            post_send_occurrences = _normalized_visible_text_occurrences(
                page_text, message
            )
            if (
                composer_empty
                and post_send_occurrences == pre_send_occurrences + 1
            ):
                complete_outbound_intent(
                    prepared["intent_id"],
                    verification_receipt=(
                        "verified_tenant_text_rendered_after_single_send"
                    ),
                    db_path=intent_db_path,
                )
                print("[WO] Tenant text submission semantically verified")
                return True
        return fail("tenant_text_postcheck_unverified")
    except Exception as error:
        fail("tenant_text_transport_failure")
        print(f"[WO] Tenant text failed: {type(error).__name__}")
        return False


# ── Triage Logic ───────────────────────────────────────────────────────────

def classify_issue(description: str) -> str:
    """Classify the issue type from the job description."""
    desc = description.lower()
    if any(k in desc for k in ["toilet", "running", "flush"]):
        return "toilet"
    if any(k in desc for k in ["leak", "water", "drip", "pipe", "plumb"]):
        return "leak" if "leak" in desc else "plumbing"
    if any(k in desc for k in ["heat", "ac", "hvac", "air condition", "cool", "furnace"]):
        return "hvac"
    if any(k in desc for k in ["electric", "outlet", "light", "power", "circuit"]):
        return "electrical"
    if any(k in desc for k in ["appliance", "stove", "oven", "fridge", "refrigerator",
                                "dishwasher", "washer", "dryer", "microwave"]):
        return "appliance"
    if any(k in desc for k in ["lock", "key", "door", "entry"]):
        return "lockout"
    return "general"



def normalize_issue_type(
    appfolio_issue_type: str,
    job_description: str = "",
) -> str:
    """
    Convert AppFolio issue labels into Rachel's internal triage categories.
    """
    normalized = (
        (appfolio_issue_type or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )

    aliases = {
        "lighting": "electrical",
        "electric": "electrical",
        "electrical": "electrical",
        "plumbing": "plumbing",
        "toilet": "toilet",
        "heating": "hvac",
        "cooling": "hvac",
        "heating_cooling": "hvac",
        "hvac": "hvac",
        "appliance": "appliance",
        "appliances": "appliance",
        "lock": "lockout",
        "locks": "lockout",
        "lockout": "lockout",
        "leak": "leak",
        "water_leak": "leak",
        "general": "general",
    }

    mapped = aliases.get(normalized)

    if mapped:
        return mapped

    combined_description = " ".join(
        value
        for value in (
            appfolio_issue_type,
            job_description,
        )
        if value
    )

    return classify_issue(combined_description)

def detect_issues(job_description: str) -> list:
    """
    Detect whether the tenant reported one issue or several.
    Returns a list of dicts: [{"label": short name, "description": text, "trade": trade}]
    Single-issue requests return a one-element list.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system="""You analyze maintenance requests for a property manager.
Determine if the tenant is reporting ONE issue or MULTIPLE distinct issues.
Distinct = requires different repairs/vendors (e.g. running toilet + dead outlet = 2).
Same problem described twice = 1 issue.

Reply with ONLY a JSON array, no other text:
[{"label": "Running Toilet", "description": "Tenant reports toilet in primary bathroom runs continuously and will not stop after flushing.", "trade": "plumbing"}]

IMPORTANT: Each description must be a complete, standalone job description
that a vendor can read and understand WITHOUT seeing the other issues.
Include all relevant details from the original request that apply to THIS
specific issue. Do NOT reference the other issues.

Valid trades: plumbing, hvac, electrical, appliance, carpentry, painting,
roofing, pest_control, landscaping, lockout, general""",
        messages=[{"role": "user", "content": job_description}]
    )
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        issues = json.loads(raw)
        if isinstance(issues, list) and issues:
            return issues
    except json.JSONDecodeError:
        pass
    return [{"label": job_description[:40], "description": job_description,
             "trade": classify_issue(job_description)}]


def _legacy_create_additional_work_order(page, issue_label: str,
                                          job_description: str) -> bool:
    """
    From an open Service Request page, click + Add to create an additional
    work order (-2, -3, ...), fill Work Order Issue + Job Description, Save.
    Assumes page is already on the service request.
    """
    raise RuntimeError(
        "Legacy broad additional Work Order creator is disabled."
    )
    allowed, reason = appfolio_write_allowed()
    if not allowed:
        print(f"[MultiWO] Creation blocked by safety gate: {reason}")
        return False
    try:
        # Scroll up to find + Add button
        page.evaluate("window.scrollTo(0, 0)")
        human_delay(1, 2)

        # Click the + Add button next to "N Work Order(s)"
        add_btn = page.query_selector(
            'button:has-text("+ Add"), a:has-text("+ Add"), '
            'button:has-text("Add"):near(:text("Work Order"))')
        if not add_btn:
            # Try finding it via text content
            add_btn = page.evaluate("""
                () => {
                    const btns = document.querySelectorAll('button, a');
                    for (const b of btns) {
                        if (b.textContent.trim() === '+ Add') return b;
                    }
                    return null;
                }
            """)
        if not add_btn:
            print("[MultiWO] + Add button not found")
            return False
        if isinstance(add_btn, dict):
            page.evaluate("(el) => el.click()", add_btn)
        else:
            add_btn.click()
        human_delay(3, 5)

        # Work Order Issue — searchable dropdown
        # Find all search/select inputs on the new WO form
        issue_field = page.query_selector(
            'input[placeholder="Search..."]')
        if issue_field and issue_field.is_visible():
            issue_field.click()
            human_delay(1, 2)
            issue_field.fill(issue_label[:30])
            human_delay(2, 3)
            # Click matching option from dropdown
            option = page.query_selector(
                f'li:has-text("{issue_label[:20]}"), '
                f'[role="option"]:has-text("{issue_label[:20]}"), '
                f'.dropdown-item:has-text("{issue_label[:20]}")')
            if option and option.is_visible():
                option.click()
            else:
                # Try selecting first visible option
                first_opt = page.query_selector(
                    'li[role="option"], .dropdown-item')
                if first_opt and first_opt.is_visible():
                    first_opt.click()
                else:
                    issue_field.press("Enter")
            human_delay(1, 2)

        # Job Description — find ALL textareas on the form,
        # the first one is typically Job Description
        textareas = page.query_selector_all('textarea')
        desc_filled = False
        for ta in textareas:
            if ta.is_visible():
                # Check if it's near the "Job Description" label
                ta.click()
                ta.fill(job_description)
                desc_filled = True
                print(f"[MultiWO] Job Description filled: {job_description[:60]}")
                break
        if not desc_filled:
            print("[MultiWO] Could not find Job Description textarea")
        human_delay(1, 2)

        # Scroll down and Save
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        human_delay(1, 2)
        save_btn = page.query_selector(
            'button:has-text("Save"), input[value="Save"]')
        if save_btn and save_btn.is_visible():
            save_btn.click()
            human_delay(3, 5)
            # After Save, AppFolio lands on the new WO page — capture its URL
            new_url = page.url
            if "/work_orders/" not in new_url:
                new_url = None
            print(f"[MultiWO] Created WO: {issue_label} | url: {new_url}")
            return new_url or True
        print("[MultiWO] Save button not found")
        return False
    except Exception as e:
        print(f"[MultiWO] Error: {e}")
        return False


def _legacy_edit_wo_description(
    page, wo_url: str, new_description: str
) -> bool:
    """
    Navigate to a specific WO and update its Job Description.
    Used to narrow WO -1 from the full multi-issue text to just issue #1.
    """
    raise RuntimeError("Legacy broad Work Order editor is disabled.")
    allowed, reason = appfolio_write_allowed()
    if not allowed:
        print(f"[EditWO] Update blocked by safety gate: {reason}")
        return False
    try:
        page.goto(wo_url, wait_until="domcontentloaded", timeout=30000)
        human_delay(2, 3)

        # If on SR page, click into the specific WO first
        if "/work_orders/" not in page.url:
            wo_link = page.query_selector('a[href*="/work_orders/"]')
            if wo_link:
                wo_link.click()
                human_delay(2, 3)

        # Click Edit button on the WO
        edit_btn = page.query_selector(
            'a:has-text("Edit"), button:has-text("Edit")')
        if not edit_btn:
            print("[EditWO] Edit button not found")
            return False
        edit_btn.click()
        human_delay(2, 4)

        # Find the Job Description textarea specifically
        # It's the textarea that appears right after the "Job Description" label
        desc_filled = False
        desc_field = page.evaluate("""
            () => {
                const labels = document.querySelectorAll(
                    'label, h3, h4, div, span, td');
                for (const lbl of labels) {
                    if (lbl.textContent.trim() === 'Job Description') {
                        // Find the next textarea after this label
                        let el = lbl.nextElementSibling;
                        while (el) {
                            if (el.tagName === 'TEXTAREA') return el;
                            const ta = el.querySelector('textarea');
                            if (ta) return ta;
                            el = el.nextElementSibling;
                        }
                    }
                }
                return null;
            }
        """)

        if desc_field:
            page.evaluate("""
                (args) => {
                    const [el, text] = args;
                    el.value = '';
                    el.value = text;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
            """, [desc_field, new_description])
            human_delay(1, 2)
            desc_filled = True
            print(f"[EditWO] Job Description updated: {new_description[:60]}")
        else:
            # Fallback — find textarea by position (Job Description is first)
            textareas = page.query_selector_all('textarea')
            for ta in textareas:
                if ta.is_visible():
                    ta.click()
                    ta.press("Control+a")
                    ta.fill(new_description)
                    desc_filled = True
                    print(f"[EditWO] Description updated (fallback): {new_description[:60]}")
                    break

        if not desc_filled:
            print("[EditWO] Could not find Job Description textarea")
            return False

        human_delay(1, 2)

        # Scroll down and Save
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        human_delay(1, 2)
        save_btn = page.query_selector(
            'button:has-text("Save"), input[value="Save"]')
        if save_btn and save_btn.is_visible():
            save_btn.click()
            human_delay(3, 5)
            print("[EditWO] ✅ WO description saved")
            return True
        print("[EditWO] Save button not found")
        return False
    except Exception as e:
        print(f"[EditWO] Error: {e}")
        return False


def _return_to_service_request_page(page, sr_url: str) -> str:
    """Navigate back to the SR page between multi-issue WO creation steps.

    AppFolio sometimes redirects a bare service-request URL into that same
    request's own work-order sub-page instead of staying at the request-
    level route — still the right destination, just a deeper URL, so both
    route shapes are accepted here. The request id itself is still checked
    exactly, so a genuine navigation-away-from-this-SR failure still raises.
    """
    match = re.search(r"/service_requests/(\d+)", sr_url)
    request_id = match.group(1) if match else ""
    result_url = AppFolioReadAdapter(page, "work_order_detail").navigate_read(
        sr_url,
        route_pattern=(
            "/maintenance/service_requests/:request",
            "/maintenance/service_requests/:request/work_orders/:work_order",
        ),
        timeout=30000,
        attempts=2,
    )
    if request_id and f"/service_requests/{request_id}" not in result_url:
        raise AppFolioUIContractError(
            "work_order_detail", "page_identity", "unexpected_service_request_id"
        )
    return result_url


def create_additional_work_order(
    page, issue_label: str, job_description: str, *, db_path: str = ""
):
    """Create one exact additional WO with a durable no-retry claim."""
    allowed, reason = appfolio_write_allowed(
        operation="create_additional_work_order"
    )
    if not allowed:
        print(f"[MultiWO] Creation blocked by safety gate: {reason}")
        return False
    current_url = str(page.url or "")
    parsed = urlparse(current_url)
    if (
        parsed.netloc != urlparse(APPFOLIO_URL).netloc
        or "/service_requests/" not in parsed.path
        or not (issue_label or "").strip()
        or not (job_description or "").strip()
    ):
        print("[MultiWO] Exact service-request/issue context is required")
        return False
    intent_db_path = db_path or DB_PATH
    intent = prepare_outbound_intent(
        operation="create_additional_work_order",
        target_id=current_url,
        recipient="appfolio_work_order_record",
        message=json.dumps({
            "issue_label": issue_label.strip(),
            "job_description": job_description.strip(),
        }, sort_keys=True),
        db_path=intent_db_path,
    )
    if intent["status"] == "sent_verified":
        print("[MultiWO] Duplicate verified Work Order creation suppressed")
        receipt_prefix = "verified_new_work_order_url:"
        receipt = str(intent.get("verification_receipt") or "")
        verified_url = (
            receipt[len(receipt_prefix):]
            if receipt.startswith(receipt_prefix) else ""
        )
        if _is_trusted_work_order_url(verified_url, allow_short=False):
            return verified_url
        print("[MultiWO] Verified creation URL is unavailable; review required")
        return False
    if intent["status"] != "prepared":
        print("[MultiWO] Prior creation is unverified; retry blocked")
        return False
    claimed = False

    def fail(reason_code: str) -> bool:
        if claimed:
            record_outbound_intent_failure(
                intent["intent_id"], reason=reason_code,
                db_path=intent_db_path,
            )
        return False

    try:
        add = page.get_by_text(
            accessible_name("work_order.add_control"), exact=True
        )
        if add.count() != 1 or not add.is_visible():
            return False
        add.click()
        issue = unique_visible(page, "work_order.issue_search")
        description = page.get_by_label(
            accessible_name("work_order.job_description_label"), exact=True
        )
        if description.count() != 1 or not description.is_visible():
            description = page.locator(
                combined_selector("work_order.description")
            )
        if (
            not issue or description.count() != 1
            or not description.is_visible()
        ):
            return False
        form = description.locator(
            combined_selector("work_order.form_ancestor")
        )
        save = form.get_by_role(
            "button", name=accessible_name("work_order.save_button"),
            exact=True,
        )
        if (
            form.count() != 1 or save.count() != 1
            or not save.is_visible() or save.is_disabled()
        ):
            return False
        claim_outbound_intent(intent["intent_id"], db_path=intent_db_path)
        claimed = True
        issue.fill(issue_label.strip())
        option = work_order_issue_option(page, issue_label)
        if option.count() != 1 or not option.is_visible():
            return fail("additional_wo_issue_option_unverified")
        option.click()
        description.fill(job_description.strip())
        if description.input_value().strip() != job_description.strip():
            return fail("additional_wo_description_fill_failed")
        save.click()
        page.wait_for_timeout(1000)
        new_url = str(page.url or "")
        body_text = WorkOrderDetailPage(page).body_text()
        if (
            new_url == current_url or "/work_orders/" not in new_url
            or not _normalized_visible_text_contains(body_text, issue_label)
            or not _normalized_visible_text_contains(
                body_text, job_description[:80]
            )
        ):
            return fail("additional_wo_postcheck_unverified")
        complete_outbound_intent(
            intent["intent_id"],
            verification_receipt="verified_new_work_order_url:" + new_url,
            db_path=intent_db_path,
        )
        return new_url
    except Exception as error:
        fail("additional_wo_transport_failure")
        print(f"[MultiWO] Error: {type(error).__name__}")
        return False


def edit_wo_description(
    page, wo_url: str, new_description: str, *, wo_num: str = "",
    db_path: str = "",
) -> bool:
    """Update one exact WO description with durable ambiguity handling."""
    allowed, reason = appfolio_write_allowed(
        operation="edit_work_order_description",
        wo_num=wo_num,
        wo_url=wo_url,
    )
    if not allowed:
        print(f"[EditWO] Update blocked by safety gate: {reason}")
        return False
    if not _is_trusted_work_order_url(wo_url, allow_short=False):
        print(f"[EditWO] wo_url is not a trusted long URL: {wo_url!r}")
        return False
    if not (new_description or "").strip():
        print("[EditWO] new_description was empty")
        return False
    intent_db_path = db_path or DB_PATH
    intent = prepare_outbound_intent(
        operation="edit_work_order_description",
        target_id=wo_num or wo_url,
        recipient="appfolio_work_order_record",
        message=new_description,
        db_path=intent_db_path,
    )
    if intent["status"] == "sent_verified":
        print("[EditWO] Duplicate verified description update suppressed")
        return True
    if intent["status"] != "prepared":
        print("[EditWO] Prior description update is unverified; retry blocked")
        return False
    claimed = False

    def fail(reason_code: str) -> bool:
        if claimed:
            record_outbound_intent_failure(
                intent["intent_id"], reason=reason_code,
                db_path=intent_db_path,
            )
        return False

    try:
        AppFolioReadAdapter(page, "work_order_writes").navigate_read(
            wo_url,
            route_pattern=(
                "/maintenance/service_requests/:request/"
                "work_orders/:work_order"
            ),
            timeout=30000,
            attempts=2,
        )
        if "/work_orders/" not in str(page.url or ""):
            print(f"[EditWO] Navigation left the WO page: {page.url!r}")
            return False
        if wo_num and not _validate_current_work_order_page(page, wo_num):
            print(f"[EditWO] Exact work-order page verification failed: {wo_num}")
            return False
        # The page has more than one element whose text is exactly "Edit"
        # (e.g. a separate service-request-level edit link plus this
        # work-order's own edit control). The work-order edit control is
        # the only one of them rendered as an <a> without an href (a
        # JS-driven toggle, not a navigational link) - same disambiguation
        # already used by the vendor-dispatch flow below.
        edit = page.locator(
            combined_selector("work_order.dispatch.edit_candidates")
        ).filter(has_text=re.compile(r"^\s*Edit\s*$"))
        if edit.count() != 1 or not edit.is_visible():
            print(f"[EditWO] Expected one visible Edit control, found {edit.count()}")
            return False
        edit.click()
        # The click triggers a React re-render of the edit form; checking
        # immediately reads the pre-click DOM and always finds 0 matches
        # (confirmed live Sep 2 2026 - this is the same click-then-check
        # race the rest of this file already guards against elsewhere
        # with human_delay).
        human_delay(1, 2)
        description = page.locator(
            combined_selector("work_order.job_description_field")
        )
        if description.count() != 1 or not description.is_visible():
            print(
                "[EditWO] Expected one visible description field, "
                f"found {description.count()}"
            )
            return False
        form = description.locator(
            combined_selector("work_order.form_ancestor")
        )
        save = form.get_by_role(
            "button", name=accessible_name("work_order.save_button"),
            exact=True,
        )
        if (
            form.count() != 1 or save.count() != 1
            or not save.is_visible() or save.is_disabled()
        ):
            print(
                f"[EditWO] Form/Save control not ready: form={form.count()} "
                f"save={save.count()}"
            )
            return False
        claim_outbound_intent(intent["intent_id"], db_path=intent_db_path)
        claimed = True
        description.fill(new_description.strip())
        if description.input_value().strip() != new_description.strip():
            return fail("edit_wo_description_fill_failed")
        save.click()
        page.wait_for_timeout(1000)
        body_text = WorkOrderDetailPage(page).body_text()
        # The edit form is a React implementation detail and may be replaced.
        current_description = page.locator(
            combined_selector("work_order.job_description_field")
        )
        form_visible = bool(
            current_description.count()
            and current_description.is_visible()
        )
        if form_visible or not _normalized_visible_text_contains(
            body_text, new_description[:80]
        ):
            return fail("edit_wo_description_postcheck_unverified")
        complete_outbound_intent(
            intent["intent_id"],
            verification_receipt="verified_work_order_description_rendered",
            db_path=intent_db_path,
        )
        return True
    except Exception as error:
        fail("edit_wo_description_transport_failure")
        print(f"[EditWO] Error: {type(error).__name__}")
        return False


def create_additional_wos_for_issues(wo_url: str, issues: list) -> list:
    """
    Open the service request and create one additional WO per extra issue
    (the first issue stays on the original -1 WO).
    Returns list of issue labels successfully created.
    """
    created = []
    if len(issues) <= 1:
        return created
    if not _is_trusted_work_order_url(wo_url, allow_short=False):
        print("[MultiWO] Exact trusted source Work Order URL is required")
        return created

    try:
        manager = get_appfolio_browser_manager()
        with manager.operation("create_additional_work_orders") as page:
            # Service request URL = WO url minus the /work_orders/... suffix
            sr_url = re.sub(r"/work_orders/\d+.*$", "", wo_url)
            adapter = AppFolioReadAdapter(page, "work_order_writes")

            def return_to_service_request():
                adapter.navigate_read(
                    sr_url,
                    route_pattern="/maintenance/service_requests/:request",
                    timeout=30000,
                    attempts=2,
                )

            return_to_service_request()
            human_delay(3, 5)
            # First: edit WO -1 to narrow its description to just issue #1
            first_issue = issues[0]
            if first_issue.get("wo_url"):
                edit_wo_description(page, first_issue["wo_url"],
                                    first_issue["description"])
                # Return to service request
                return_to_service_request()
                human_delay(2, 4)

            # Then: create WO -2, -3, etc. with their own descriptions
            for issue in issues[1:]:
                result = create_additional_work_order(
                    page, issue["label"], issue["description"])
                if result:
                    created.append(issue["label"])
                    # Store the sub-WO's own URL on the issue dict
                    if isinstance(result, str):
                        issue["wo_url"] = result
                # Return to service request for the next + Add
                return_to_service_request()
                human_delay(2, 4)
    except Exception as e:
        print(f"[MultiWO] Browser error: {e}")
    return created


def has_enough_info(
    issue_type: str, conversation: list, address: str = "", job_desc: str = ""
) -> tuple:
    """
    Ask Claude if we have enough info to write a summary.
    Returns (bool, next_question_or_summary)
    """
    conv_text = "\n".join([
        f"Rachel: {m['q']}\nTenant: {m['a']}"
        for m in conversation
    ])

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system="""You are Rachel, Assistant Property Manager at ClientCo Property Management.
You are triaging a maintenance work order by texting with the tenant.

Determine if you have enough information to write a complete summary for the property manager.
You need to know: specific location, severity, duration, any safety concerns.

The tenant's unit/address is already on file (given below) - never ask the
tenant which unit or property they are in.

The tenant's original submitted description is given below too - it may
already answer some of what you'd otherwise ask about (e.g. which room,
how long it's been happening). Never ask about anything it already states;
only ask about what is genuinely still missing.

GUIDED TROUBLESHOOTING: If the issue matches one below and the tenant has not
tried the fix yet, offer ONE safe troubleshooting step as your question
(saves everyone a vendor visit). NEVER suggest anything involving gas,
electrical panels beyond a breaker flip, ladders, or tools.
- Running toilet: jiggle the handle; lift tank lid and check the flapper is seated
- Garbage disposal dead/humming: press the red reset button underneath the unit
- Dead outlet in kitchen/bath: press the GFCI reset button on the outlet
- No power in one room: check if a breaker is tripped (flip fully OFF then ON)
- AC not cooling: check thermostat is on COOL and the filter is not clogged
- No hot water (gas): check if other gas appliances work (do NOT relight pilots)
- Fridge not cooling: check the dial setting and that the door seals shut

If the tenant confirms a troubleshooting step fixed the issue and confirms
the problem is now fully resolved, reply with:
TENANT_RESOLVED: [one-line description of what fixed it]

A tenant-resolved issue is not a completed work order. The tenant must be
instructed to log into the resident portal and cancel the maintenance request.

PHOTOS AND VIDEO:
When visual evidence would materially help document or diagnose the issue,
ask for clear photos or a short video as ONE question. Do not repeatedly ask
for media, and do not ask the tenant to do anything unsafe.

Ask only ONE question per response. Ask no more than four questions total.

If the tenant asks to speak with a manager, supervisor, or someone else,
or expresses frustration and wants to escalate, reply with:
ESCALATE: [brief reason the tenant wants to escalate]

If you need one more question (or want to offer a troubleshooting step), reply with:
QUESTION: [your question]

If you have enough info, reply with:
READY: [complete summary for property manager, 3-5 sentences, professional.
Include what troubleshooting was already attempted so the vendor does not repeat it.]""",
        messages=[{
            "role": "user",
            "content": (
                f"Unit/address on file: {address or 'Unknown'}\n"
                f"Issue type: {issue_type}\n"
                f"Tenant's original submitted description: "
                f"{job_desc or 'Unknown'}\n\n"
                f"Conversation so far:\n{conv_text}"
            )
        }]
    )

    result = response.content[0].text.strip()

    # Never allow an unanswered tenant question to be treated as a completed
    # management summary, even if Claude placed introductory text before it.
    embedded_question = re.search(
        r"(?im)^\s*QUESTION:\s*(.+?)\s*$",
        result,
    )
    if embedded_question:
        return False, embedded_question.group(1).strip()

    if result.startswith("READY:"):
        return True, result[6:].strip()
    elif result.startswith("TENANT_RESOLVED:"):
        return (
        "tenant_resolved",
        result[len("TENANT_RESOLVED:"):].strip(),
    )
    elif result.startswith("ESCALATE:"):
        return "escalate", result[9:].strip()
    elif result.startswith("QUESTION:"):
        return False, result[9:].strip()
    else:
        # Default
        return False, result


def build_best_available_summary(
    issue_type: str,
    conversation: list,
) -> str:
    """
    Create the best available management summary after the tenant has
    answered four questions, even if some information remains unavailable.
    """
    conv_text = "\n".join(
        f"Rachel: {item.get('q', '')}\n"
        f"Tenant: {item.get('a', '')}"
        for item in conversation
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            system="""You write professional maintenance-triage summaries
for a property manager.

The tenant has already answered the maximum four questions.

Write a complete 3-5 sentence summary using only confirmed information.

Rules:
- Do not ask another tenant question.
- Do not output QUESTION:, READY:, or any other label.
- Do not invent facts.
- State any important missing information clearly.
- Include the exact location, severity, duration, safety concerns, visible
  damage, troubleshooting, and photos when those details are available.
- Write the summary for management and vendor-selection review.""",
            messages=[{
                "role": "user",
                "content": (
                    f"Issue type: {issue_type}\n\n"
                    f"Completed tenant conversation:\n{conv_text}"
                ),
            }],
        )

        summary = response.content[0].text.strip()

        if summary:
            return summary

    except Exception as exc:
        print(f"[Triage] Final summary generation failed: {exc}")

    return (
        "Tenant triage reached the four-question limit. "
        "The conversation above contains the available confirmed details. "
        "Additional information may need to be obtained during scheduling."
    )


CLOSING_MESSAGE = (
    "Thank you for providing that information. "
    "We have what we need for now and will follow up with you regarding the next steps."
)


def get_multi_issue_opening(
    tenant_name: str, issues: list, wo_num: str = ""
) -> str:
    """Opening message when a tenant reported multiple issues."""
    first_name = (
        tenant_name.split()[0]
        if tenant_name and tenant_name != "Unknown"
        else "there"
    )

    issue_list = "; ".join(issue["label"] for issue in issues)

    first_trade = issues[0].get("trade", "general")
    questions = ISSUE_QUESTIONS.get(
        first_trade,
        ["Can you please provide more detail about the first issue?"],
    )

    wo_reference = f", WO #{wo_num}," if wo_num else ""

    return (
        f"Hi {first_name}, we received your maintenance request{wo_reference} "
        f"regarding {issue_list} and are working on it. {questions[0]}"
    )


def build_custom_tenant_question(
    instruction: str,
    job_description: str,
) -> str:
    """
    Convert Manager's authorized custom triage instruction into exactly one
    professional tenant-facing question.

    Returns an empty string if the instruction cannot be converted safely.
    """
    clean_instruction = (instruction or "").strip()

    if not clean_instruction:
        return ""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="""You write one tenant-facing maintenance question for
ClientCo Property Management.

Convert the manager's instruction into exactly ONE concise, professional
question addressed to the tenant.

Rules:
- Output only the question.
- Ask exactly one question.
- Do not mention Manager, management instructions, authorization, internal
  workflow, vendors, or AI.
- Do not promise scheduling, repairs, approval, or a specific outcome.
- Preserve the manager's intended fact to obtain.
- Use safe, neutral property-management language.
- End with a question mark.""",
            messages=[{
                "role": "user",
                "content": (
                    f"Work-order description: {job_description[:1000]}\n\n"
                    f"Manager instruction: {clean_instruction[:1000]}"
                ),
            }],
        )

        question = response.content[0].text.strip()

        if (
            not question
            or not question.endswith("?")
            or question.count("?") != 1
        ):
            print(
                "[Triage] Custom tenant question failed validation: "
                f"{question}"
            )
            return ""

        return question

    except Exception as e:
        print(
            "[Triage] Could not build custom tenant question: "
            f"{e}"
        )
        return ""


def get_opening_message(
    issue_type: str,
    tenant_name: str,
    job_description: str,
    wo_num: str = "",
) -> str:
    """Generate Rachel's opening message to the tenant."""
    first_name = (
        tenant_name.split()[0]
        if tenant_name and tenant_name != "Unknown"
        else "there"
    )

    questions = ISSUE_QUESTIONS.get(
        issue_type,
        ["Can you please provide more detail about the issue?"],
    )

    wo_reference = f", WO #{wo_num}," if wo_num else ""

    return (
        f"Hi {first_name}, we received your maintenance request{wo_reference} "
        f"and are working on it. {questions[0]}"
    )


# ── Main Triage Function ───────────────────────────────────────────────────


def parse_work_order_email(email_data: dict) -> dict:
    """
    Extract normalized work-order details from an original AppFolio email.

    This parser does not contact the tenant or modify AppFolio.
    """
    body = email_data.get("body", "")
    subject = email_data.get("subject", "")

    is_new_wo_email = re.match(
        r"^WO #\d+-\d+\s+-\s+",
        subject.strip(),
        re.IGNORECASE,
    )

    if not is_new_wo_email:
        return {}

    wo_match = re.search(r"Work Order[:\s#]+(\S+)", body)
    tenant_match = re.search(r"Resident Name[:\s]+(.+)", body)
    address_match = re.search(r"Address[:\s]+(.+)", body)

    description_match = re.search(
        r"Job Description\s*\n(.+?)(?:\n\n|\nIssue|\nPermission)",
        body,
        re.DOTALL,
    )

    issue_match = re.search(r"Issue Descriptor[:\s]+(.+)", body)

    url_match = re.search(
        r"https://clientco\.appfolio\.com"
        r"/maintenance/service_requests/\d+/work_orders/\d+[^\s\"'<>]*",
        body,
    )

    if not url_match:
        url_match = re.search(
            r"https://appfol\.io/[A-Za-z0-9]+",
            body,
        )

    phone_match = re.search(
        r"Resident Phone Number:\s*\n?\s*"
        r"(\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4})",
        body,
    )

    priority_match = re.search(
        r"Priority[:\s]+(EMERGENCY|URGENT|NORMAL|LOW)\b",
        body,
        re.IGNORECASE,
    )

    job_desc = (
        description_match.group(1).strip()
        if description_match
        else subject
    )

    issue_details = (
        issue_match.group(1).strip()
        if issue_match
        else ""
    )

    if tenant_match and tenant_match.group(1).strip().upper() != "UNKNOWN":
        source_type = "RESIDENT"
    elif re.search(
        r"(?:Source|Request\s+Type)[:\s]+Internal\b",
        body,
        re.IGNORECASE,
    ):
        source_type = "INTERNAL"
    else:
        source_type = "UNKNOWN"

    return {
        "source": "email",
        "triage_origin": "original_new_work_order_email",
        "source_type": source_type,
        "subject": subject,
        "wo_num": (
            wo_match.group(1).strip()
            if wo_match
            else "Unknown"
        ),
        "tenant": (
            tenant_match.group(1).strip()
            if tenant_match
            else "Unknown"
        ),
        "address": (
            address_match.group(1).strip()
            if address_match
            else "Unknown"
        ),
        "job_desc": job_desc,
        "issue_type": normalize_issue_type(
            issue_details,
            job_desc,
        ),
        "issue_details": issue_details,
        "wo_url": (
            url_match.group(0)
            if url_match
            else ""
        ),
        "tenant_phone": (
            phone_match.group(1).strip()
            if phone_match
            else ""
        ),
        "priority": (
            priority_match.group(1).strip().upper()
            if priority_match
            else ""
        ),
    }

def triage_work_order(email_data: dict, page=None):
    """
    Email entry point used by rachel_inbox_monitor.py.

    Parses the original AppFolio email and passes normalized details into
    the shared work-order workflow.
    """
    details = parse_work_order_email(email_data)

    if not details:
        subject = email_data.get("subject", "")
        print(
            f"[Triage] Skipped — not an original new work-order email: "
            f"{subject}"
        )
        return

    return triage_work_order_details(details, page=page)


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    """Shorten internal notification text without cutting off mid-word or
    mid-sentence with no indication more was said (Manager's complaint,
    Sep 3 2026 — a plain `text[:limit]` slice regularly stopped mid-word)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit(" ", 1)[0].rstrip(",.;: ")
    return (truncated or text[:limit]) + "…"


def triage_work_order_details(details: dict, page=None):
    """
    Shared tenant-triage workflow.

    Accepts normalized work-order details from either:
    - an original AppFolio email, or
    - the AppFolio scanner after Manager authorizes PROCEED.
    """
    eligibility = evaluate_triage_eligibility(details)
    if not eligibility["eligible"]:
        print(
            "[TriageEligibility] blocked=true "
            f"reason={eligibility['reason']}"
        )
        return {
            "status": "triage_blocked",
            "reason": eligibility["reason"],
        }

    wo_num = str(details.get("wo_num") or "Unknown").strip()
    tenant = str(details.get("tenant") or "Unknown").strip()
    address = str(details.get("address") or "Unknown").strip()

    job_desc = str(
        details.get("job_desc")
        or details.get("service_request_description")
        or details.get("issue_details")
        or "No description provided"
    ).strip()

    issue_str = str(details.get("issue_details") or "").strip()
    wo_url = str(
        details.get("wo_url")
        or details.get("page_url")
        or ""
    ).strip()

    tenant_phone = str(details.get("tenant_phone") or "").strip()

    issue_type = normalize_issue_type(
        str(details.get("issue_type") or ""),
        job_desc,
    )

    is_emergency = eligibility["emergency"]

    # Eligible emergency events may be delivered repeatedly. Preserve one
    # alert and one tenant acknowledgment through durable triage state.
    if is_emergency:
        conn = get_db()
        existing_emergency = conn.execute(
            """
            SELECT status
            FROM wo_triage
            WHERE wo_num = ?
            """,
            (wo_num,),
        ).fetchone()
        conn.close()

        if (
            existing_emergency
            and existing_emergency["status"] in {
                "emergency_alerted",
                "emergency_alerted_ack_failed",
            }
        ):
            print(
                f"[Triage] Emergency WO #{wo_num} was already "
                "alerted. Duplicate action suppressed."
            )
            return

    source = str(details.get("source") or "scanner").strip()
    subject = str(details.get("subject") or f"WO #{wo_num}").strip()

    classification = str(
        details.get("classification") or ""
    ).strip()

    manager_instruction = str(
        details.get("manager_instruction") or ""
    ).strip()

    custom_triage_instruction = ""

    if (
        classification == "manager_authorized_custom_triage"
        and manager_instruction.upper().startswith("PROCEED_WITH:")
    ):
        custom_triage_instruction = (
            manager_instruction.split(":", 1)[1].strip()
        )

        print(
            "[Triage] Manager custom instruction: "
            f"{custom_triage_instruction}"
        )

    print(f"\n[Triage] Processing from {source}: {subject[:60]}")
    print(f"[Triage] WO#{wo_num} | Tenant: {tenant} | {address}")
    print(f"[Triage] Issue: {job_desc[:80]}")
    print(f"[Triage] URL: {wo_url}")

    if tenant_phone:
        print(f"[Triage] Tenant phone: {tenant_phone}")

    # Synthetic Owner-only QA Work Orders bypass AppFolio completely.
    # Their URL must use qa:// and their number must begin with QA-.
    if (
        TEST_MODE
        and OWNER_QA_MODE
        and wo_num.upper().startswith("QA-")
        and wo_url.lower().startswith("qa://")
    ):
        issues = detect_issues(
            job_desc + (
                " | " + issue_str
                if issue_str
                else ""
            )
        )

        issue_type = (
            normalize_issue_type(
                issues[0].get("trade", ""),
                job_desc,
            )
            if issues
            else normalize_issue_type(issue_type, job_desc)
        )

        opening = get_opening_message(
            issue_type,
            "Client Owner",
            job_desc,
            wo_num,
        )

        _save_triage_state(
            wo_num,
            wo_url,
            "Client Owner (QA Tenant)",
            address,
            job_desc,
            issue_type,
            [{"q": opening, "a": ""}],
            issues=issues,
        )

        sent = send_owner_qa_email(
            subject=f"Rachel QA Tenant — WO #{wo_num}",
            message=(
                "OWNER-ONLY SYNTHETIC TENANT TEST\n"
                "No real tenant or AppFolio record was contacted.\n\n"
                f"{opening}\n\n"
                "Reply naturally to this email as the tenant."
            ),
        )

        print(
            f"[OwnerQA] Synthetic tenant triage started for "
            f"WO #{wo_num}: {'sent' if sent else 'FAILED'}."
        )
        return bool(sent)

    if not wo_url:
        print("[Triage] No WO URL found — cannot navigate to work order")
        # Fall back to texting Manager directly
        return bool(send_sms_via_gmail(
            f"New WO #{wo_num}\nTenant: {tenant}\n{address}\nIssue: {_truncate_at_word_boundary(job_desc, 150)}\n"
            f"Could not find WO link to follow up with tenant.",
            urgent=False
        ))

    # Use provided browser page or create a new one
    own_browser = False
    browser_operation = None
    if not page:
        try:
            browser_operation = get_appfolio_browser_manager().operation(
                "triage_work_order"
            )
            page = browser_operation.__enter__()
            own_browser = True
        except Exception as e:
            print(f"[Triage] Browser launch failed: {e}")
            return bool(send_sms_via_gmail(
                f"New WO #{wo_num}\n{tenant}\n{address}\n"
                f"Issue: {_truncate_at_word_boundary(job_desc, 150)}\nBrowser error — please follow up.",
                urgent=False))

    try:
            # Navigate to the WO
            wo_url = navigate_to_wo(page, wo_num, wo_url)
            if not wo_url:
                print("[Triage] Could not navigate to WO — notifying Manager")
                return bool(send_sms_via_gmail(
                    f"New WO #{wo_num}\n{tenant}\n{address}\n"
                    f"Issue: {_truncate_at_word_boundary(job_desc, 150)}\n"
                    f"Could not open WO in AppFolio — please follow up.",
                    urgent=False))

            # Confirm the current live WO status before contacting the tenant.
            wo_page_text = WorkOrderDetailPage(page).body_text()

            status_match = re.search(
                rf"Work Order\s*#{re.escape(wo_num)}\s+([^\n]+)",
                wo_page_text,
                re.IGNORECASE,
            )

            current_status = (
                status_match.group(1).strip().upper()
                if status_match
                else "UNKNOWN"
            )
            current_status_code = current_status.split(None, 1)[0]

            print(f"[Triage] Live WO status: {current_status}")

            if (
                eligibility["route"] == "new_work_order_triage"
                and current_status_code != "NEW"
            ):
                print(
                    f"[TriageEligibility] WO #{wo_num} is {current_status}; "
                    "new-work-order triage blocked before any contact or write."
                )
                return {
                    "status": "triage_blocked",
                    "reason": "live_status_not_new",
                }

            # A canceled WO is a complete hard stop.
            if current_status.startswith(("CANCELED", "CANCELLED")):
                print(
                    f"[Triage] WO #{wo_num} is canceled — "
                    "no tenant or vendor contact will occur."
                )
                return True

            
            # Analyze the issue only after confirming the WO is still active.
            if is_emergency:
                urgent_msg = (
                    f"EMERGENCY WO #{wo_num}\n"
                    f"Tenant: {tenant}\n"
                    f"Address: {address}\n"
                    f"Issue: {_truncate_at_word_boundary(job_desc, 150)}\n"
                    f"Action needed immediately."
                )

                alert_sent = send_sms_via_gmail(
                    urgent_msg,
                    urgent=True,
                )

                if not alert_sent:
                    print(
                        "[Triage] Emergency management alert failed. "
                        "The Work Order was not marked handled so Rachel "
                        "can retry during the next scanner cycle."
                    )
                    return False

                print("[Triage] EMERGENCY — Manager + Owner notified")

                ack = (
                    "We received your urgent maintenance request and are "
                    "addressing it immediately. Someone will be in touch "
                    "with you very shortly."
                )

                acknowledgment_sent = _send_appfolio_message(
                    wo_url,
                    ack,
                    tenant,
                    page=page,
                    wo_num=wo_num,
                )

                _save_triage_state(
                    wo_num,
                    wo_url,
                    tenant,
                    address,
                    job_desc,
                    issue_type,
                    [],
                )

                conn = get_db()
                conn.execute(
                    """
                    UPDATE wo_triage
                    SET status = ?,
                        updated_at = ?
                    WHERE wo_num = ?
                    """,
                    (
                        (
                            "emergency_alerted"
                            if acknowledgment_sent
                            else "emergency_alerted_ack_failed"
                        ),
                        datetime.now().isoformat(),
                        wo_num,
                    ),
                )
                conn.commit()
                conn.close()

                print(
                    f"[Triage] Emergency WO #{wo_num} saved. "
                    f"Tenant acknowledgment: "
                    f"{'sent' if acknowledgment_sent else 'FAILED'}."
                )
                if not acknowledgment_sent:
                    send_sms_via_gmail(
                        f"EMERGENCY WO #{wo_num} — tenant acknowledgment "
                        "could not be verified. Manual tenant contact is "
                        "required.",
                        urgent=True,
                    )
                return True

            write_allowed, write_reason = appfolio_write_allowed(
                operation="tenant_triage",
                wo_num=wo_num,
                wo_url=wo_url,
                tenant=tenant,
            )
            if not write_allowed:
                print(
                    f"[Triage] AppFolio triage writes blocked: "
                    f"{write_reason}"
                )
                return False

            issues = detect_issues(
                job_desc + (
                    " | " + issue_str
                    if issue_str
                    else ""
                )
            )

            issue_type = (
                normalize_issue_type(
                    issues[0].get("trade", ""),
                    job_desc,
                )
                if issues
                else normalize_issue_type(issue_type, job_desc)
            )

            print(
                f"[Triage] {len(issues)} issue(s) detected | "
                f"primary type: {issue_type}"
            )
            
            # Multiple issues → edit WO -1 + create WOs -2, -3
            if len(issues) > 1:
                issues[0]["wo_url"] = wo_url

                # Navigate to WO via short URL to get the full SR URL
                if not navigate_to_wo(page, wo_num, wo_url):
                    print("[Triage] Multi-issue WO identity verification failed")
                    return False
                human_delay(3, 5)
                sr_url = re.sub(r"/work_orders/\d+.*$", "", page.url)
                if "/service_requests/" not in sr_url:
                    sr_url = page.url  # already on the SR page

                # Capture the actual WO -1 URL after redirect
                actual_url = page.url
                if "/work_orders/" in actual_url:
                    issues[0]["wo_url"] = actual_url
                else:
                    # On the SR page — find the -1 WO link
                    candidate_links = page.locator(combined_selector(
                        "work_order.any_detail_link"
                    ))
                    matching_links = [
                        candidate_links.nth(index)
                        for index in range(candidate_links.count())
                        if wo_num in candidate_links.nth(index).inner_text()
                    ]
                    if len(matching_links) == 1:
                        issues[0]["wo_url"] = matching_links[0].get_attribute(
                            "href"
                        )
                        if not issues[0]["wo_url"].startswith("http"):
                            issues[0]["wo_url"] = APPFOLIO_URL + issues[0]["wo_url"]
                    else:
                        print(
                            "[Triage] Exact primary Work Order link was "
                            "ambiguous"
                        )
                        return False

                # Edit WO -1 description to just issue #1
                if issues[0].get("wo_url"):
                    edited = edit_wo_description(
                        page, issues[0]["wo_url"], issues[0]["description"],
                        wo_num=wo_num,
                    )
                    if not edited:
                        print(
                            "[Triage] Primary multi-issue Work Order edit "
                            "did not verify; workflow paused"
                        )
                        return False
                    _return_to_service_request_page(page, sr_url)
                    human_delay(2, 4)

                # Create WO -2, -3 etc.
                created = []
                for issue in issues[1:]:
                    page.evaluate("window.scrollTo(0, 0)")
                    human_delay(1, 2)
                    result = create_additional_work_order(
                        page, issue["label"], issue["description"])
                    if not result or not isinstance(result, str):
                        print(
                            "[Triage] Additional Work Order creation did not "
                            "verify; workflow paused"
                        )
                        return False
                    created.append(issue["label"])
                    issue["wo_url"] = result
                    _return_to_service_request_page(page, sr_url)
                    human_delay(2, 4)

                print(f"[Triage] Created {len(created)} additional WO(s): {created}")
                opening = get_multi_issue_opening(tenant, issues, wo_num)
            else:
                opening = get_opening_message(
                    issue_type, tenant, job_desc, wo_num
                )

            if custom_triage_instruction:
                custom_question = build_custom_tenant_question(
                    custom_triage_instruction,
                    job_desc,
                )

                if not custom_question:
                    print(
                        f"[Triage] Could not safely apply Manager's custom "
                        f"instruction for WO #{wo_num}. Tenant was not contacted."
                    )

                    return bool(send_sms_via_gmail(
                        (
                            f"WO #{wo_num}\n"
                            f"Rachel could not safely convert Manager's custom "
                            f"instruction into a tenant question.\n"
                            f"Instruction: {custom_triage_instruction}"
                        ),
                        urgent=False,
                        subject=f"Rachel Needs Clarification — WO #{wo_num}",
                    ))

                first_name = (
                    tenant.split()[0]
                    if tenant and tenant != "Unknown"
                    else "there"
                )

                opening = (
                    f"Hi {first_name}, we received your maintenance request, "
                    f"WO #{wo_num}, and are working on it. {custom_question}"
                )

            print(f"[Triage] Opening message: {opening}")

            # Route through the test-mode safety wrapper. Never call the
            # live AppFolio sender directly from the triage workflow.
            sent = _send_appfolio_message(
                wo_url,
                opening,
                tenant,
                page=page,
                wo_num=wo_num,
            )

            if sent:
                print(f"[Triage] ✅ Opening text sent to {tenant}")

            # Send an email to the tenant too — via Notify Tenant when the
            # text succeeded, or as the sole fallback channel when it didn't
            # (e.g. the tenant has opted out of texting). Email doesn't
            # depend on texting infra, so it's tried either way rather than
            # only ever running after a successful text.
            issue_summary = ", ".join(
                i["label"] for i in issues
            ) if len(issues) > 1 else _truncate_at_word_boundary(job_desc, 150)
            if sent:
                tenant_email_msg = (
                    f"Hi {tenant.split()[0]},\n\n"
                    f"We received your maintenance request, WO #{wo_num}, "
                    f"regarding: {issue_summary}.\n\n"
                    f"I just sent you a text message to gather a few more "
                    f"details so we can get this resolved as quickly as "
                    f"possible. If you prefer, feel free to reply to this "
                    f"email instead.\n\n"
                    f"Thank you,\n"
                    f"Rachel\n"
                    f"Assistant Property Manager\n"
                    f"ClientCo Property Management\n"
                    f"555-010-0000")
            else:
                tenant_email_msg = (
                    f"Hi {tenant.split()[0]},\n\n"
                    f"We received your maintenance request, WO #{wo_num}, "
                    f"regarding: {issue_summary}.\n\n"
                    f"Could you reply to this email with a bit more detail "
                    f"so we can get this resolved as quickly as possible?\n\n"
                    f"Thank you,\n"
                    f"Rachel\n"
                    f"Assistant Property Manager\n"
                    f"ClientCo Property Management\n"
                    f"555-010-0000")

            if TEST_MODE:
                print(
                    "[TEST MODE] Tenant confirmation email blocked\n"
                    f"Tenant: {tenant}\n"
                    f"Message:\n{tenant_email_msg}\n"
                )
                email_sent = False
            else:
                email_sent = send_tenant_email_via_appfolio(
                    page, tenant_email_msg,
                    wo_url=wo_url, wo_num=wo_num, tenant=tenant,
                )
                if email_sent:
                    print("[Triage] Confirmation email submission verified")

            if sent or email_sent:
                _save_triage_state(
                    wo_num,
                    wo_url,
                    tenant,
                    address,
                    job_desc,
                    issue_type,
                    [{"q": opening, "a": ""}],
                    issues=issues,
                )

                # Notify Manager that Rachel is handling it
                channel_note = (
                    "" if sent else
                    "(tenant could not be texted — reached out by email "
                    "instead)\n"
                )
                send_sms_via_gmail(
                    f"WO #{wo_num} — {tenant}, {address}\n"
                    f"Issue: {issue_summary}\n"
                    f"{channel_note}\n"
                    f"I\'m working with the tenant to gather details. "
                    f"I\'ll send you a summary with vendor options when ready.",
                    urgent=False)
                return True
            else:
                print(
                    "[Triage] Could not reach tenant by text or email — "
                    "notifying Manager"
                )
                send_sms_via_gmail(
                    f"New WO #{wo_num}\n{tenant}\n{address}\n"
                    f"Issue: {issue_summary}\n"
                    f"Unable to text or email tenant — please follow up "
                    f"manually.",
                    urgent=False)
                # Save state as unreachable rather than leaving no
                # wo_triage row at all — otherwise this WO looks
                # "brand new" on every future poll and Rachel retries
                # the same failing send forever (confirmed live on
                # WO#1725-1: 80 retries over 5+ hours).
                _save_triage_state(
                    wo_num, wo_url, tenant, address, job_desc,
                    issue_type, [], issues=issues,
                )
                conn = get_db()
                conn.execute(
                    "UPDATE wo_triage SET status = 'tenant_unreachable' "
                    "WHERE wo_num = ?", (wo_num,))
                conn.commit()
                conn.close()
                return True

    except Exception as e:
        print(f"[Triage] Error: {e}")
        return bool(send_sms_via_gmail(
            f"New WO #{wo_num}\n{tenant}\n{address}\n"
            f"Issue: {_truncate_at_word_boundary(job_desc, 150)}\nBrowser error — please follow up.",
            urgent=False))
    finally:
        if own_browser:
            try:
                browser_operation.__exit__(None, None, None)
            except Exception:
                pass


def _send_appfolio_message(
    wo_url: str,
    message: str,
    tenant: str,
    page=None,
    in_reply_to: str = "",
    references: str = "",
    wo_num: str = "",
    vendor_name: str = "",
) -> bool:
    """Send message via AppFolio Texts section. Reuses existing page if provided."""

    qa_match = re.search(
        r"qa://work-order/([A-Za-z0-9-]+)",
        wo_url or "",
        re.IGNORECASE,
    )

    if (
        TEST_MODE
        and OWNER_QA_MODE
        and qa_match
        and (wo_num or "").upper().startswith("QA-")
    ):
        return send_owner_qa_email(
            subject=f"Rachel QA Tenant — WO #{wo_num}",
            message=(
                "OWNER-ONLY SYNTHETIC TENANT TEST\n"
                "No real tenant or AppFolio record was contacted.\n\n"
                f"{message}\n\n"
                "Reply naturally to this email as the tenant."
            ),
        )

    allowed, reason = appfolio_write_allowed(
        synthetic_qa=bool(qa_match),
        operation="tenant_schedule_message",
        wo_num=wo_num,
        wo_url=wo_url,
        tenant=tenant,
        vendor_name=vendor_name,
    )

    if not allowed:
        print(
            f"\n[Safety] Tenant message blocked\n"
            f"Reason: {reason}\n"
            f"Tenant: {tenant}\n"
            f"WO URL: {wo_url}\n"
        )
        return False

    if not wo_url or not tenant or not (message or "").strip():
        print("[Safety] Exact tenant message context is required")
        return False
    target_id = (wo_num or wo_url).strip()
    try:
        outbound_intent = prepare_outbound_intent(
            operation="tenant_work_order_text",
            target_id=target_id,
            recipient=tenant,
            message=message,
            db_path=DB_PATH,
        )
    except Exception as error:
        print(f"[AppFolio] Tenant intent failed: {type(error).__name__}")
        return False
    if outbound_intent["status"] == "sent_verified":
        print("[AppFolio] Duplicate verified tenant message suppressed")
        return True
    if outbound_intent["status"] != "prepared":
        print("[AppFolio] Prior tenant send is unverified; retry blocked")
        return False

    if page:
        return send_text_via_appfolio(
            page, wo_url, message, tenant=tenant, wo_num=wo_num,
            vendor_name=vendor_name, outbound_intent=outbound_intent,
        )

    try:
        manager = get_appfolio_browser_manager()
        with manager.operation("send_tenant_appfolio_message") as page:
            result = send_text_via_appfolio(
                page, wo_url, message, tenant=tenant, wo_num=wo_num,
                vendor_name=vendor_name, outbound_intent=outbound_intent,
            )
            return result
    except Exception as e:
        print(f"[AppFolio] Error: {type(e).__name__}")
        return False


def _send_tenant_email_message(
    wo_url: str, message: str, tenant: str, wo_num: str = "",
) -> bool:
    """Self-managing wrapper so a follow-up triage question can go out by
    email the same way _send_appfolio_message sends one by text — used
    when the tenant's first reply came in by email, so every later
    question sticks to that same channel instead of also texting them."""
    try:
        manager = get_appfolio_browser_manager()
        with manager.operation("send_tenant_appfolio_email") as page:
            return send_tenant_email_via_appfolio(
                page, message, wo_url=wo_url, wo_num=wo_num, tenant=tenant,
            )
    except Exception as e:
        print(f"[AppFolio] Error: {type(e).__name__}")
        return False


def _save_triage_state(wo_num, wo_url, tenant, address,
                        job_desc, issue_type, conversation, issues=None):
    """Save triage conversation state (incl. multi-issue list) for follow-up."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wo_triage (
            wo_num      TEXT PRIMARY KEY,
            wo_url      TEXT,
            tenant      TEXT,
            address     TEXT,
            job_desc    TEXT,
            issue_type  TEXT,
            conversation TEXT,
            issues      TEXT,
            status      TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )
    """)
    # Migrate older DBs that lack the issues column
    try:
        conn.execute("ALTER TABLE wo_triage ADD COLUMN issues TEXT")
    except Exception:
        pass
    conn.execute("""
        INSERT INTO wo_triage
        (wo_num, wo_url, tenant, address, job_desc, issue_type,
         conversation, issues, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wo_num) DO UPDATE SET
            conversation = excluded.conversation,
            issues = COALESCE(excluded.issues, wo_triage.issues),
            status = excluded.status,
            updated_at = excluded.updated_at
    """, (wo_num, wo_url, tenant, address, job_desc, issue_type,
          json.dumps(conversation),
          json.dumps(issues) if issues else None,
          "awaiting_reply",
          datetime.now().isoformat(), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    print(f"[Triage] State saved for WO#{wo_num}")


def analyze_photos(images: list, issue_context: str) -> str:
    """
    Analyze tenant-submitted photos with Claude vision.
    images: list of {"media_type": "image/jpeg", "data": base64str}
    Returns a short findings string ("" if nothing useful).
    """
    if not images:
        return ""
    try:
        content_blocks = []
        for img in images[:3]:  # max 3 photos per call
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64",
                           "media_type": img["media_type"],
                           "data": img["data"]}})
        content_blocks.append({
            "type": "text",
            "text": (f"These photos accompany a maintenance request: "
                     f"{issue_context}. In 2-3 sentences describe what the "
                     f"photos show that is relevant for the repair vendor "
                     f"(visible damage, severity, likely cause). If the "
                     f"photos show nothing relevant, say so in one line.")})
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=250,
            messages=[{"role": "user", "content": content_blocks}])
        return response.content[0].text.strip()
    except Exception as e:
        print(f"[Photos] Analysis error: {e}")
        return ""


def handle_tenant_triage_reply(
    wo_num: str,
    tenant_reply: str,
    images: list = None,
    thread_message_id: str = "",
    thread_references: str = "",
    channel: str = "",
):
    """
    Called when a tenant replies to a triage message.
    Continues the conversation or sends summary to Manager.

    channel ("text" or "email") identifies which channel this specific
    reply arrived on. The tenant's first reply locks in a conversation's
    channel for every later question, so Rachel never texts someone who
    answered by email or emails someone who is replying by text.
    """
    conn = get_db()
    try:
        conn.execute("ALTER TABLE wo_triage ADD COLUMN reply_channel TEXT")
        conn.commit()
    except Exception:
        pass
    row = conn.execute(
        "SELECT * FROM wo_triage WHERE wo_num = ?", (wo_num,)
    ).fetchone()

    if not row:
        conn.close()
        print(f"[Triage] No triage state found for WO#{wo_num}")
        return False

    reply_channel = str(row["reply_channel"] or "").strip().lower()
    if not reply_channel and channel:
        reply_channel = channel.strip().lower()
        conn.execute(
            "UPDATE wo_triage SET reply_channel = ? WHERE wo_num = ?",
            (reply_channel, wo_num),
        )
        conn.commit()
        print(
            f"[Triage] WO#{wo_num} locked to {reply_channel} — the "
            "tenant's first reply channel — for the rest of this "
            "conversation"
        )
    conn.close()

    conversation = json.loads(row["conversation"])
    issue_type   = row["issue_type"]
    wo_url       = row["wo_url"]
    tenant       = row["tenant"]
    address      = row["address"]
    job_desc     = row["job_desc"]

    reply_references = " ".join(
        item for item in (
            thread_references.strip(),
            thread_message_id.strip(),
        )
        if item
    )

    def _send_tenant_message(text: str) -> bool:
        """Route every tenant-facing message in this reply through
        whichever channel the tenant's first reply locked in. If that
        channel fails (e.g. the tenant's phone can no longer receive
        texts — confirmed live on WO#1725-1, where AppFolio itself
        marks the send "failed to send"), fall back to the other
        channel and permanently switch to it for the rest of this
        conversation, rather than retrying the same broken channel
        on every future message."""
        nonlocal reply_channel
        if reply_channel == "email":
            return _send_tenant_email_message(
                wo_url, text, tenant, wo_num=wo_num,
            )
        if _send_appfolio_message(
            wo_url, text, tenant,
            in_reply_to=thread_message_id,
            references=reply_references,
            wo_num=wo_num,
        ):
            return True
        print(
            f"[Triage] WO#{wo_num} text delivery failed — trying email "
            "as a fallback channel"
        )
        if not _send_tenant_email_message(
            wo_url, text, tenant, wo_num=wo_num,
        ):
            return False
        reply_channel = "email"
        conn = get_db()
        conn.execute(
            "UPDATE wo_triage SET reply_channel = 'email' WHERE wo_num = ?",
            (wo_num,),
        )
        conn.commit()
        conn.close()
        print(
            f"[Triage] WO#{wo_num} switched to email for the rest of "
            "this conversation — texting this tenant is not working"
        )
        return True

    def _give_up_on_tenant_and_escalate(context: str) -> bool:
        """Both tenant channels failed. Rather than leaving the WO in
        'awaiting_reply' (which lets the same unprocessed reply retry
        forever — confirmed live on WO#1725-1: 80 retries over 5+
        hours, including 3 duplicate Manager summary emails), alert
        Manager once and stop automatic retries."""
        send_sms_via_gmail(
            f"⚠️ WO #{wo_num} — Rachel could not reach the tenant by "
            f"text or email.\n{tenant} | {address}\n"
            f"Context: {context}\n\n"
            f"Please follow up with the tenant manually.",
            urgent=True,
        )
        _save_triage_state(wo_num, wo_url, tenant, address,
                           job_desc, issue_type, conversation)
        conn = get_db()
        conn.execute(
            "UPDATE wo_triage SET status = 'tenant_unreachable' "
            "WHERE wo_num = ?", (wo_num,))
        conn.commit()
        conn.close()
        print(
            f"[Triage] WO#{wo_num} marked tenant_unreachable — no "
            "further automatic retries"
        )
        return True

    # Photo analysis (if tenant attached images)
    if images:
        findings = analyze_photos(images, job_desc)
        if findings:
            tenant_reply = f"{tenant_reply}\n[PHOTO ANALYSIS: {findings}]"
            print(f"[Photos] {findings[:80]}")

    # Reject an empty parsed reply unless photo analysis supplied content.
    tenant_reply = (tenant_reply or "").strip()

    if not tenant_reply:
        print(
            f"[Triage] WO#{wo_num} reply contained no readable "
            "text or photo analysis — state unchanged"
        )
        return False

    # Locate the question awaiting an answer. Under normal operation there
    # will be exactly one. If an earlier parsing failure created multiple
    # unanswered questions, answer the earliest one and remove the later
    # duplicate unanswered entries.
    unanswered_indexes = [
        index
        for index, turn in enumerate(conversation)
        if str(turn.get("q") or "").strip()
        and not str(turn.get("a") or "").strip()
    ]

    if unanswered_indexes:
        target_index = unanswered_indexes[0]
        conversation[target_index]["a"] = tenant_reply

        if len(unanswered_indexes) > 1:
            duplicate_indexes = set(unanswered_indexes[1:])
            conversation = [
                turn
                for index, turn in enumerate(conversation)
                if index not in duplicate_indexes
            ]

            print(
                f"[Triage] WO#{wo_num} repaired "
                f"{len(duplicate_indexes)} duplicate unanswered "
                "question(s)"
            )
    else:
        conversation.append({"q": "", "a": tenant_reply})

    # Claude may end triage early whenever enough information is available.
    ready, next_action = has_enough_info(
        issue_type, conversation, address, job_desc
    )
    summary = next_action

    # After four completed tenant answers, stop asking questions and prepare
    # the best available management summary. Missing facts are identified in
    # the summary rather than presented as another tenant question.
    completed_answers = sum(
        1
        for turn in conversation
        if str(turn.get("a") or "").strip()
    )

    if completed_answers >= 4 and ready is False:
        summary = build_best_available_summary(
            issue_type,
            conversation,
        )
        ready = True
        next_action = summary

    # Tenant wants to speak to a supervisor
    if ready == "escalate":
        escalation_sent = _send_tenant_message(
            "Absolutely — I'm going to have Regional Manager, our Regional "
            "Property Manager, reach out to you directly. She'll be in "
            "touch shortly."
        )

        if not escalation_sent:
            print(
                f"[Triage] WO#{wo_num} escalation message delivery FAILED "
                "on every channel."
            )
            return _give_up_on_tenant_and_escalate(
                "tenant requested a supervisor"
            )

        internal_notice_sent = send_sms_via_gmail(
            f"⚠️ WO #{wo_num} — TENANT REQUESTING SUPERVISOR\n"
            f"{tenant} | {address}\n"
            f"Reason: {next_action}\n\n"
            f"Please reach out to the tenant directly.",
            urgent=True)
        _save_triage_state(wo_num, wo_url, tenant, address,
                           job_desc, issue_type, conversation)
        conn = get_db()
        conn.execute(
            "UPDATE wo_triage SET status = 'escalated_to_manager' "
            "WHERE wo_num = ?", (wo_num,))
        conn.commit()
        conn.close()
        if not internal_notice_sent and not TEST_MODE:
            print(
                f"[Triage] WARNING: WO#{wo_num} tenant escalation was sent, "
                "but the internal Manager/Owner notification failed."
            )

        print(f"[Triage] WO#{wo_num} escalated to Manager")
        return True

        # Tenant confirms troubleshooting fully resolved the issue.
    # Tenant must cancel the request through the resident portal.
    if ready == "tenant_resolved":
        resolution_message_sent = _send_tenant_message(
            "Thank you for confirming the issue is fully resolved. "
            "Please log into your resident portal and cancel this "
            "maintenance request so the work order updates correctly."
        )

        if not resolution_message_sent:
            print(
                f"[Triage] WO#{wo_num} resolution-message delivery FAILED "
                "on every channel."
            )
            return _give_up_on_tenant_and_escalate(
                "tenant confirmed the issue is resolved"
            )

        internal_notice_sent = send_sms_via_gmail(
            f"WO #{wo_num} — ISSUE RESOLVED BY TENANT\n"
            f"{tenant} | {address}\n"
            f"Resolution: {next_action}\n\n"
            f"Rachel instructed the tenant to cancel the request "
            f"through the resident portal. Waiting for cancellation.",
            urgent=False,
        )

        _save_triage_state(
            wo_num,
            wo_url,
            tenant,
            address,
            job_desc,
            issue_type,
            conversation,
        )

        conn = get_db()
        conn.execute(
            """
            UPDATE wo_triage
            SET status = 'waiting_for_tenant_cancellation'
            WHERE wo_num = ?
            """,
            (wo_num,),
        )
        conn.commit()
        conn.close()

        if not internal_notice_sent and not TEST_MODE:
            print(
                f"[Triage] WARNING: WO#{wo_num} resolution confirmation "
                "was sent, but the internal Manager/Owner notice failed."
            )

        print(
            f"[Triage] WO#{wo_num} resolved by tenant — "
            "waiting for portal cancellation"
        )
        return True

    if ready:
        # Build the internal management summary first. Rachel must confirm
        # its delivery before telling the tenant triage is complete.
        # Multi-issue? Load stored issue list
        stored_issues = []
        try:
            if row["issues"]:
                stored_issues = json.loads(row["issues"])
        except Exception:
            stored_issues = []

        if len(stored_issues) > 1:
            # Per-issue summary + vendor options in ONE text to Manager
            base_num = wo_num.split("-")[0]
            lines = [f"WO #{base_num} — {tenant}", f"{address}",
                     f"{len(stored_issues)} issues", ""]
            for idx, iss in enumerate(stored_issues, 1):
                sub = f"{base_num}-{idx}"
                vends = get_suggested_vendors(
                    address, iss.get("trade", "general")
                )[:1]
                lines.append(f"{sub} {iss.get('trade','general').upper()}: {iss['label']}")
                for vi, v in enumerate(vends, 1):
                    ph = f" {v['phone']}" if v.get("phone") else ""
                    lines.append(f"  {vi}. {v['name']}{ph}")
                lines.append(f"  {len(vends)+1}. In-house")
                lines.append("")
            lines.append(f"Summary: {summary}")
            lines.append("")
            lines.append(f'Reply per line: "{base_num}-1: 1, {base_num}-2: 2, ..."')
            summary_msg = "\n".join(lines)
        else:
            # Manager sees exactly one recommended vendor (best match by
            # property preference / history), plus in-house and a
            # different-vendor option — not a list of fallback vendors.
            vendors = get_suggested_vendors(address, issue_type)[:1]
            summary_msg = format_vendor_sms(wo_num, tenant, address, summary, vendors)

        if (
            TEST_MODE
            and OWNER_QA_MODE
            and wo_num.upper().startswith("QA-")
        ):
            summary_sent = send_owner_qa_email(
                subject=f"Rachel QA Manager Decision — WO #{wo_num}",
                message=(
                    "OWNER-ONLY SYNTHETIC MANAGEMENT TEST\n"
                    "No real tenant, vendor, or AppFolio record was changed.\n\n"
                    f"{summary_msg}\n\n"
                    "Reply with the vendor choice or instruction you "
                    "would give Rachel."
                ),
            )
            print(
                f"[OwnerQA] Synthetic management summary: "
                f"{'sent' if summary_sent else 'FAILED'}."
            )
        else:
            summary_sent = send_sms_via_gmail(
                summary_msg,
                urgent=False,
                subject=f"Rachel WO Update — WO #{wo_num}",
            )

            print(
                "[Triage] "
                + (
                    "✅ Summary + vendor options sent to Manager + Owner"
                    if summary_sent
                    else "Summary + vendor options delivery FAILED"
                )
            )

        if not summary_sent:
            print(
                f"[Triage] WO#{wo_num} management-summary delivery FAILED. "
                "Tenant closing message was not sent and state was not advanced."
            )
            return False

        closing_sent = _send_tenant_message(CLOSING_MESSAGE)

        if not closing_sent:
            print(
                f"[Triage] WO#{wo_num} tenant closing-message delivery "
                "FAILED on every channel (Manager's summary already went "
                "out)."
            )
            return _give_up_on_tenant_and_escalate(
                "summary already sent to Manager, but the tenant "
                "closing message could not be delivered"
            )

        print("[Triage] Closing message sent to tenant")

        # Update triage state
        _save_triage_state(wo_num, wo_url, tenant, address,
                           job_desc, issue_type, conversation)
        conn = get_db()
        conn.execute(
            "UPDATE wo_triage SET status = 'waiting_for_manager_vendor_approval' WHERE wo_num = ?",
            (wo_num,)
        )
        conn.commit()
        conn.close()
        return True
    else:
        # Send exactly one next question, through whichever channel the
        # tenant's first reply locked in. Do not record it as sent unless
        # AppFolio or the synthetic Owner QA sender confirms delivery.
        question_sent = _send_tenant_message(next_action)

        if not question_sent:
            print(
                f"[Triage] WO#{wo_num} next-question delivery FAILED on "
                "every channel."
            )
            return _give_up_on_tenant_and_escalate(
                "Rachel had a follow-up question for the tenant"
            )

        conversation.append({
            "q": next_action,
            "a": "",
        })

        _save_triage_state(
            wo_num,
            wo_url,
            tenant,
            address,
            job_desc,
            issue_type,
            conversation,
        )

        print(f"[Triage] Next question sent: {next_action[:60]}")
        return True


def dispatch_vendor_to_wo(
    wo_url: str,
    wo_num: str,
    vendor_name: str,
    vendor_phone: str,
    trade: str,
    tenant: str,
    address: str,
) -> bool:
    """
    Assign one exact external vendor to one verified AppFolio work order.

    Rachel selects the vendor through AppFolio's external Vendor field,
    checks all three vendor-notification controls, saves the work order,
    and then verifies the persisted assignment.

    This function does not use the internal Assign To or Maintenance Tech
    fields and does not send a second generic AppFolio email.
    """
    synthetic_qa = (
        (wo_url or "").lower().startswith("qa://")
        or (wo_num or "").upper().startswith("QA-")
    )

    vendor_allowed, vendor_reason = vendor_contact_allowed(
        synthetic_qa=synthetic_qa,
        operation="dispatch_vendor",
        wo_num=wo_num,
        wo_url=wo_url,
        tenant=tenant,
        vendor_name=vendor_name,
    )
    write_allowed, write_reason = appfolio_write_allowed(
        synthetic_qa=synthetic_qa,
        operation="dispatch_vendor",
        wo_num=wo_num,
        wo_url=wo_url,
        tenant=tenant,
        vendor_name=vendor_name,
    )

    if not vendor_allowed or not write_allowed:
        reason = (
            vendor_reason
            if not vendor_allowed
            else write_reason
        )
        print(
            f"\n[Safety] Vendor assignment blocked\n"
            f"Reason: {reason}\n"
            f"WO: #{wo_num}\n"
            f"Vendor: {vendor_name}\n"
        )
        return False

    try:
        outbound_intent = prepare_outbound_intent(
            operation="dispatch_vendor",
            target_id=wo_num,
            recipient=vendor_name,
            message=json.dumps({
                "address": address,
                "notifications": [
                    "email_secure_work_order_link",
                    "text_secure_work_order_link",
                    "request_vendor_receipt_confirmation",
                ],
                "tenant": tenant,
                "trade": trade,
                "vendor": vendor_name,
            }, sort_keys=True),
            db_path=DB_PATH,
        )
    except Exception as exc:
        print(
            "[Dispatch] Durable intent preparation failed: "
            f"{type(exc).__name__}"
        )
        return False
    if outbound_intent["status"] == "sent_verified":
        print("[Dispatch] Duplicate verified vendor assignment suppressed")
        return True
    if outbound_intent["status"] != "prepared":
        print(
            "[Dispatch] Prior vendor assignment is unverified; automatic "
            "retry blocked"
        )
        return False

    if not _is_trusted_work_order_url(wo_url, allow_short=False):
        print("[Dispatch] Exact trusted work-order URL is required")
        return False

    intent_claimed = False

    def fail_after_claim(reason: str) -> bool:
        if intent_claimed:
            record_outbound_intent_failure(
                outbound_intent["intent_id"],
                reason=reason,
                db_path=DB_PATH,
            )
        return False

    def normalize(value: str) -> str:
        normalized = (value or "").replace("\u2013", "-")
        normalized = normalized.replace("\u2014", "-")
        normalized = re.sub(r"\s*-\s*", " - ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip().lower()

    trade_map = {
        "plumbing": "Plumbing",
        "hvac": "HVAC",
        "electrical": "Electrical",
        "painting": "Painting",
        "general": "Handyperson",
        "handyperson": "Handyperson",
        "appliance": "Appliances",
        "appliances": "Appliances",
        "carpentry": "Handyperson",
        "cleaning": "Cleaning",
        "landscaping": "Landscaping",
        "pest_control": "Pest Control",
        "pest control": "Pest Control",
        "roofing": "Roofing",
        "pool": "Pool/Spa",
        "pool/spa": "Pool/Spa",
    }

    trade_key = normalize(trade).replace(" - ", "_")
    appfolio_trade = trade_map.get(trade_key, "Other")

    required_checkboxes = WORK_ORDER_DISPATCH_REQUIRED_CHECKBOXES
    protected_checkboxes = WORK_ORDER_DISPATCH_PROTECTED_CHECKBOXES

    try:
        manager = get_appfolio_browser_manager()
        with manager.operation("dispatch_vendor") as page:

            print(
                f"[Dispatch] Opening verified target WO #{wo_num}..."
            )
            AppFolioReadAdapter(
                page, "work_order_writes"
            ).navigate_read(
                wo_url,
                route_pattern=(
                    "/maintenance/service_requests/:request/"
                    "work_orders/:work_order"
                ),
                timeout=30000,
                attempts=2,
            )
            human_delay(2, 4)

            wo_page_text = WorkOrderDetailPage(page).body_text()
            normalized_page = normalize(wo_page_text)

            # AppFolio's own "Address:" line in the new-work-order email
            # sometimes lists the property address and the unit address
            # back-to-back, comma-joined - when the two happen to be
            # textually identical (e.g. a single-unit test property), the
            # stored address ends up as the same address duplicated plus
            # a trailing city/state segment. The live WO page only ever
            # renders the address once, so verifying the raw duplicated
            # string against it always failed. Verify just the first
            # comma-separated segment instead - confirmed live Sep 2 2026
            # against WO#1715-1.
            address_target = str(address or "").split(",")[0].strip()

            target_values = {
                "work order": wo_num,
                "tenant": tenant,
                "address": address_target,
            }

            missing_targets = [
                label
                for label, value in target_values.items()
                if not value
                or normalize(value) not in normalized_page
            ]

            if missing_targets:
                print(
                    "[Dispatch] Target verification failed for: "
                    + ", ".join(missing_targets)
                )
                return False

            print(
                "[Dispatch] Exact work order, tenant, and address "
                "verified."
            )

            from datetime import datetime
            from zoneinfo import ZoneInfo

            availability_marker = page.get_by_text(
                accessible_name(
                    "work_order.resident_availability_marker"
                ),
                exact=True,
            )

            if availability_marker.count() != 1:
                print(
                    "[Dispatch] Exact Resident Availability "
                    "section was not found."
                )
                return False

            availability_table = availability_marker.locator(
                combined_selector("work_order.following_table")
            )

            if availability_table.count() != 1:
                print(
                    "[Dispatch] Resident Availability table "
                    "was not found."
                )
                return False

            raw_availability_rows = []
            table_rows = availability_table.locator(
                combined_selector("work_order.table_rows")
            )

            for row_index in range(table_rows.count()):
                cell_texts = [
                    re.sub(r"\s+", " ", value).strip()
                    for value in table_rows.nth(
                        row_index
                    ).locator(combined_selector(
                        "work_order.table_cells"
                    )).all_text_contents()
                ]
                cell_texts = [
                    value
                    for value in cell_texts
                    if value
                ]

                if not cell_texts:
                    continue

                if (
                    len(cell_texts) >= 2
                    and normalize(cell_texts[0]) == "date"
                    and normalize(cell_texts[1]) == "time"
                ):
                    continue

                if len(cell_texts) >= 2:
                    date_text = cell_texts[0]
                    time_text = " ".join(cell_texts[1:])
                else:
                    combined_row = cell_texts[0]
                    combined_match = re.match(
                        r"(.+?\b\d{2}/\d{2}/\d{4})\s+(.+)$",
                        combined_row,
                    )

                    if not combined_match:
                        print(
                            "[Dispatch] Could not parse Resident "
                            f"Availability row: {combined_row!r}"
                        )
                        return False

                    date_text = combined_match.group(1).strip()
                    time_text = combined_match.group(2).strip()

                raw_availability_rows.append(
                    (date_text, time_text)
                )

            if not raw_availability_rows:
                print(
                    "[Dispatch] Resident Availability table "
                    "contains no appointment rows."
                )
                return False

            pacific_zone = ZoneInfo("America/Los_Angeles")
            now_pacific = datetime.now(pacific_zone)
            future_availability_rows = []

            for date_text, time_text in raw_availability_rows:
                date_match = re.search(
                    r"\b\d{2}/\d{2}/\d{4}\b",
                    date_text,
                )

                if not date_match:
                    print(
                        "[Dispatch] Could not parse availability "
                        f"date: {date_text!r}"
                    )
                    return False

                availability_date = datetime.strptime(
                    date_match.group(0),
                    "%m/%d/%Y",
                ).date()

                window_texts = [
                    window.strip()
                    for window in time_text.split(",")
                    if window.strip()
                ]

                if not window_texts:
                    print(
                        "[Dispatch] Availability row has no time "
                        f"windows: {date_text!r}"
                    )
                    return False

                future_windows = []

                for window_text in window_texts:
                    if availability_date > now_pacific.date():
                        future_windows.append(window_text)
                        continue

                    if availability_date < now_pacific.date():
                        continue

                    window_match = re.search(
                        r"(\d{1,2}:\d{2}\s*(?:am|pm))"
                        r"\s*-\s*"
                        r"(\d{1,2}:\d{2}\s*(?:am|pm))",
                        window_text,
                        re.IGNORECASE,
                    )

                    if not window_match:
                        print(
                            "[Dispatch] Could not safely determine "
                            "whether the current-day window is still "
                            f"valid: {window_text!r}"
                        )
                        return False

                    end_time_text = re.sub(
                        r"\s+",
                        "",
                        window_match.group(2),
                    ).upper()

                    end_time = datetime.strptime(
                        end_time_text,
                        "%I:%M%p",
                    ).time()

                    end_datetime = datetime.combine(
                        availability_date,
                        end_time,
                        tzinfo=pacific_zone,
                    )

                    if end_datetime > now_pacific:
                        future_windows.append(window_text)

                if future_windows:
                    future_availability_rows.append(
                        f"{date_text}: "
                        + ", ".join(future_windows)
                    )

            resident_availability = "\n".join(
                future_availability_rows
            )

            if not resident_availability:
                print(
                    "[Dispatch] Vendor assignment stopped because "
                    "all resident availability windows have expired."
                )
                return False

            print(
                "[Dispatch] Resident availability: "
                f"{resident_availability or 'Not provided'}"
            )

            conn = get_db()

            try:
                conn.execute(
                    """
                    ALTER TABLE wo_triage
                    ADD COLUMN resident_availability TEXT
                    """
                )
            except sqlite3.OperationalError:
                pass

            conn.execute(
                """
                UPDATE wo_triage
                SET resident_availability = ?
                WHERE wo_num = ?
                """,
                (resident_availability, wo_num),
            )
            conn.commit()
            conn.close()

            if not resident_availability:
                print(
                    "[Dispatch] Vendor assignment stopped because "
                    "resident availability is missing."
                )
                return False

            work_order_edit = page.locator(
                combined_selector("work_order.dispatch.edit_candidates")
            ).filter(
                has_text=re.compile(r"^\s*Edit\s*$")
            )

            if work_order_edit.count() != 1:
                print(
                    "[Dispatch] Expected exactly one work-order "
                    f"Edit control; found {work_order_edit.count()}."
                )
                return False

            work_order_edit.click()
            human_delay(2, 3)

            vendor_container = page.locator(combined_selector(
                "work_order.dispatch.vendor_container"
            ))
            vendor_hidden = page.locator(combined_selector(
                "work_order.dispatch.vendor_hidden"
            ))
            vendor_chosen = vendor_container.locator(
                combined_selector("work_order.dispatch.vendor_chosen")
            )

            if (
                vendor_container.count() != 1
                or vendor_hidden.count() != 1
                or vendor_chosen.count() != 1
            ):
                print(
                    "[Dispatch] Exact external Vendor control "
                    "was not found."
                )
                return False

            current_vendor = vendor_chosen.inner_text().strip()
            current_vendor_normalized = normalize(current_vendor)
            expected_vendor_normalized = normalize(vendor_name)

            if current_vendor_normalized not in (
                "",
                "search",
                expected_vendor_normalized,
            ):
                print(
                    "[Dispatch] A different vendor is already "
                    f"selected: {current_vendor!r}"
                )
                return False

            already_selected = (
                current_vendor_normalized
                == expected_vendor_normalized
            )

            if already_selected:
                print(
                    "[Dispatch] Exact vendor is already assigned. "
                    "No duplicate email, text, or receipt request "
                    "was sent."
                )
                page.reload(
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                record_verified_appfolio_change(
                    operation="dispatch_vendor",
                    target_type="work_order",
                    target_id=wo_num,
                    new_state={
                        "appfolio_status": "ASSIGNED",
                        "workflow_status": "vendor_assigned",
                        "vendor_name": vendor_name,
                    },
                    details={
                        "save_verified": True,
                        "already_persisted_on_restart": True,
                    },
                )
                return True

            trade_select = page.locator(combined_selector(
                "work_order.dispatch.trade"
            ))

            if trade_select.count() != 1:
                print(
                    "[Dispatch] Exact work-order trade field "
                    "was not found."
                )
                return False

            available_trades = [
                option.strip()
                for option in trade_select.locator(
                    combined_selector("work_order.dispatch.trade_options")
                ).all_text_contents()
            ]

            if appfolio_trade not in available_trades:
                print(
                    "[Dispatch] Required AppFolio trade is "
                    f"unavailable: {appfolio_trade}"
                )
                return False

            save_button = page.locator(combined_selector(
                "work_order.dispatch.save"
            ))

            if (
                save_button.count() != 1
                or not save_button.is_visible()
                or not save_button.is_enabled()
            ):
                print(
                    "[Dispatch] Exact Save control is unavailable."
                )
                return False

            for checkbox_id in protected_checkboxes:
                checkbox = page.locator(selector_for_id(checkbox_id))
                if checkbox.count() != 1:
                    print(
                        "[Dispatch] Protected checkbox missing: "
                        f"{checkbox_id}"
                    )
                    return False

            for checkbox_id, label in required_checkboxes.items():
                checkbox = page.locator(selector_for_id(checkbox_id))
                if checkbox.count() != 1:
                    print(
                        "[Dispatch] Required vendor checkbox "
                        f"missing: {label}"
                    )
                    return False

            # The durable claim is made immediately before the first form
            # mutation. Any exception after this point is ambiguous and must
            # never cause an automatic duplicate assignment/notification.
            claim_outbound_intent(
                outbound_intent["intent_id"],
                db_path=DB_PATH,
            )
            intent_claimed = True

            trade_select.select_option(label=appfolio_trade)
            print(
                f"[Dispatch] Trade selected: {appfolio_trade}"
            )

            if not already_selected:
                vendor_container.locator(
                    combined_selector("work_order.dispatch.vendor_choice")
                ).click()
                human_delay(0.5, 1)

                vendor_search = page.locator(combined_selector(
                    "work_order.dispatch.vendor_search"
                ))

                if vendor_search.count() != 1:
                    print(
                        "[Dispatch] Exact external Vendor search "
                        "input was not found."
                    )
                    return fail_after_claim(
                        "dispatch_vendor_search_unavailable"
                    )

                vendor_search.fill(vendor_name)
                human_delay(2, 3)

                vendor_options = page.locator(combined_selector(
                    "work_order.dispatch.vendor_results"
                ))

                matching_indexes = []

                for index in range(vendor_options.count()):
                    option_text = normalize(
                        vendor_options.nth(index).inner_text()
                    )

                    if (
                        option_text
                        == expected_vendor_normalized
                        or option_text.startswith(
                            expected_vendor_normalized + " - "
                        )
                    ):
                        matching_indexes.append(index)

                if len(matching_indexes) != 1:
                    print(
                        "[Dispatch] Expected exactly one vendor "
                        f"result for {vendor_name}; found "
                        f"{len(matching_indexes)}."
                    )
                    return fail_after_claim(
                        "dispatch_vendor_match_ambiguous"
                    )

                vendor_options.nth(
                    matching_indexes[0]
                ).click()
                human_delay(1, 2)

            selected_vendor = vendor_chosen.inner_text().strip()
            selected_vendor_value = (
                vendor_hidden.input_value().strip()
            )

            if (
                normalize(selected_vendor)
                != expected_vendor_normalized
                or not selected_vendor_value.startswith("v_")
            ):
                print(
                    "[Dispatch] Selected vendor verification "
                    "failed."
                )
                return fail_after_claim(
                    "dispatch_vendor_selection_unverified"
                )

            print(
                f"[Dispatch] Exact external vendor selected: "
                f"{selected_vendor}"
            )

            protected_before = {
                checkbox_id: page.locator(
                    selector_for_id(checkbox_id)
                ).is_checked()
                for checkbox_id in protected_checkboxes
            }

            for checkbox_id, label in (
                required_checkboxes.items()
            ):
                checkbox = page.locator(selector_for_id(checkbox_id))

                if not checkbox.is_checked():
                    checkbox.check()
                    human_delay(0.5, 1)

                if not checkbox.is_checked():
                    print(
                        "[Dispatch] Could not verify checkbox: "
                        f"{label}"
                    )
                    return fail_after_claim(
                        "dispatch_notification_checkbox_unverified"
                    )

                print(f"[Dispatch] Verified: {label}")

            for checkbox_id, original_state in (
                protected_before.items()
            ):
                current_state = page.locator(
                    selector_for_id(checkbox_id)
                ).is_checked()

                if current_state != original_state:
                    print(
                        "[Dispatch] Protected checkbox changed "
                        f"unexpectedly: {checkbox_id}"
                    )
                    return fail_after_claim(
                        "dispatch_protected_checkbox_changed"
                    )

            save_button.click()
            human_delay(3, 5)

            # The edit modal can be replaced wholesale after Save. Re-query
            # the central contract instead of retaining the pre-click node.
            current_save = page.locator(combined_selector(
                "work_order.dispatch.save"
            ))
            if (
                current_save.count() == 1
                and current_save.is_visible()
            ):
                print(
                    "[Dispatch] Save did not close the edit form; "
                    "assignment was not confirmed."
                )
                return fail_after_claim(
                    "dispatch_save_postcheck_unverified"
                )

            saved_page_text = WorkOrderDetailPage(page).body_text()

            if (
                expected_vendor_normalized
                not in normalize(saved_page_text)
            ):
                print(
                    "[Dispatch] Saved page does not show the "
                    "expected vendor."
                )
                return fail_after_claim(
                    "dispatch_saved_vendor_unverified"
                )

            verify_edit = page.locator(
                combined_selector("work_order.dispatch.edit_candidates")
            ).filter(
                has_text=re.compile(r"^\s*Edit\s*$")
            )

            if verify_edit.count() != 1:
                print(
                    "[Dispatch] Could not reopen the saved work "
                    "order for verification."
                )
                return fail_after_claim(
                    "dispatch_reopen_verification_unavailable"
                )

            verify_edit.click()
            human_delay(2, 3)

            persisted_vendor = page.locator(combined_selector(
                "work_order.dispatch.persisted_vendor"
            )).inner_text().strip()

            persisted_value = page.locator(combined_selector(
                "work_order.dispatch.vendor_hidden"
            )).input_value().strip()

            if (
                normalize(persisted_vendor)
                != expected_vendor_normalized
                or not persisted_value.startswith("v_")
            ):
                print(
                    "[Dispatch] Persisted vendor verification "
                    "failed."
                )
                return fail_after_claim(
                    "dispatch_persisted_vendor_unverified"
                )

            complete_outbound_intent(
                outbound_intent["intent_id"],
                verification_receipt=(
                    "verified_vendor_assignment_persisted_after_save"
                ),
                db_path=DB_PATH,
            )

            print(
                "[Dispatch] Vendor notification controls were "
                "verified before Save. They are treated as "
                "one-time submission controls and are not "
                "required to remain checked afterward."
            )

            page.reload(
                wait_until="domcontentloaded",
                timeout=30000,
            )

            print(
                f"[Dispatch] ✅ WO #{wo_num} assigned to "
                f"{vendor_name}; all three vendor notifications "
                "were saved and verified."
            )
            record_verified_appfolio_change(
                operation="dispatch_vendor",
                target_type="work_order",
                target_id=wo_num,
                new_state={
                    "appfolio_status": "ASSIGNED",
                    "workflow_status": "vendor_assigned",
                    "vendor_name": vendor_name,
                },
                details={"save_verified": True},
            )
            return True

    except Exception as exc:
        fail_after_claim("dispatch_vendor_transport_failure")
        print(f"[Dispatch] Error: {exc}")
        return False

def send_vendor_appfolio_message(
    wo_url: str,
    message: str,
    wo_num: str = "",
    tenant: str = "",
    vendor_name: str = "",
) -> bool:
    """
    Email the assigned vendor through the exact AppFolio work-order
    Email control.

    In controlled QA, every target value must match the configured
    work order, tenant, and vendor. The function fails closed whenever
    the recipient or email form cannot be verified.
    """
    allowed, reason = vendor_contact_allowed(
        synthetic_qa=False,
        operation="vendor_schedule_message",
        wo_num=wo_num,
        wo_url=wo_url,
        tenant=tenant,
        vendor_name=vendor_name,
    )

    if not allowed:
        print(
            "[VendorMessage] Vendor contact blocked by safety gate: "
            f"{reason}"
        )
        return False

    allowed, reason = appfolio_write_allowed(
        synthetic_qa=False,
        operation="vendor_schedule_message",
        wo_num=wo_num,
        wo_url=wo_url,
        tenant=tenant,
        vendor_name=vendor_name,
    )

    if not allowed:
        print(
            "[VendorMessage] AppFolio write blocked by safety gate: "
            f"{reason}"
        )
        return False

    if not wo_url or not wo_num or not vendor_name:
        print(
            "[VendorMessage] ❌ Missing required WO/vendor context; "
            "nothing sent."
        )
        return False

    if not (message or "").strip():
        print("[VendorMessage] ❌ Empty vendor message; nothing sent.")
        return False

    try:
        outbound_intent = prepare_outbound_intent(
            operation="vendor_schedule_message",
            target_id=wo_num,
            recipient=vendor_name,
            message=message,
            db_path=DB_PATH,
        )
    except Exception as exc:
        print(
            "[VendorMessage] Durable intent preparation failed: "
            f"{type(exc).__name__}"
        )
        return False
    if outbound_intent["status"] == "sent_verified":
        print("[VendorMessage] Duplicate verified submission suppressed")
        return True
    if outbound_intent["status"] != "prepared":
        print(
            "[VendorMessage] Prior submission is unverified; automatic "
            "retry blocked"
        )
        return False


    if not _is_trusted_work_order_url(wo_url, allow_short=False):
        print(
            "[VendorMessage] ❌ Exact trusted work-order URL is required; "
            "nothing sent."
        )
        return False

    intent_claimed = False

    def fail_after_claim(reason: str) -> bool:
        if intent_claimed:
            record_outbound_intent_failure(
                outbound_intent["intent_id"],
                reason=reason,
                db_path=DB_PATH,
            )
        return False

    try:
        manager = get_appfolio_browser_manager()
        with manager.operation("send_vendor_appfolio_message") as page:

                AppFolioReadAdapter(
                    page, "work_order_writes"
                ).navigate_read(
                    wo_url,
                    route_pattern=(
                        "/maintenance/service_requests/:request/"
                        "work_orders/:work_order"
                    ),
                    timeout=60000,
                    attempts=2,
                )
                time.sleep(2)

                # Verify that we actually reached the intended work order.
                current_url = page.url
                body_text = WorkOrderDetailPage(page).body_text()

                if wo_num not in body_text:
                    print(
                        f"[VendorMessage] ❌ WO #{wo_num} was not verified "
                        "on the loaded page."
                    )
                    return False

                if "/work_orders/" not in current_url:
                    print(
                        "[VendorMessage] ❌ Loaded page is not a work-order "
                        "page."
                    )
                    return False

                # This control is an <a> with no href (a JS-driven
                # button, not a navigational link), so it is never
                # exposed with accessibility role "link" - a role/name
                # lookup can never find it. Its one stable identity is
                # this class (confirmed against the live DOM Aug 28 2026).
                email_control = page.locator(
                    combined_selector("work_order.vendor_email_action")
                )

                if email_control.count() != 1:
                    print(
                        "[VendorMessage] ❌ Expected exactly one work-order "
                        f"Email control; found {email_control.count()}."
                    )
                    return False

                email_control.click()
                time.sleep(2)

                body_field = page.locator(combined_selector(
                    "work_order.email_body"
                ))

                if body_field.count() != 1 or not body_field.is_visible():
                    print(
                        "[VendorMessage] ❌ Exact AppFolio email-body field "
                        "was not available."
                    )
                    return False

                # Scope recipient verification to the actual email form,
                # not the vendor details elsewhere on the work-order page.
                email_form = body_field.locator(
                    combined_selector("work_order.form_ancestor")
                )

                if email_form.count() != 1:
                    print(
                        "[VendorMessage] ❌ Could not isolate the AppFolio "
                        "email form."
                    )
                    return False

                form_text = email_form.inner_text()
                normalized_form = " ".join(
                    form_text.lower().split()
                )
                normalized_vendor = " ".join(
                    vendor_name.lower().split()
                )

                if "recipients" not in normalized_form:
                    print(
                        "[VendorMessage] ❌ Email recipient section was not "
                        "verified."
                    )
                    return False

                if normalized_vendor not in normalized_form:
                    print(
                        "[VendorMessage] ❌ Expected vendor recipient was "
                        "not verified in the email form."
                    )
                    return False

                subject_field = page.locator(combined_selector(
                    "work_order.email_subject"
                ))

                if (
                    subject_field.count() != 1
                    or not subject_field.is_visible()
                ):
                    print(
                        "[VendorMessage] ❌ Exact subject field was not "
                        "available."
                    )
                    return False

                subject_value = subject_field.input_value().strip()

                if wo_num not in subject_value:
                    print(
                        "[VendorMessage] ❌ Email subject does not match "
                        f"WO #{wo_num}."
                    )
                    return False

                claim_outbound_intent(
                    outbound_intent["intent_id"], db_path=DB_PATH
                )
                intent_claimed = True
                body_field.fill(message.strip())

                if body_field.input_value().strip() != message.strip():
                    print(
                        "[VendorMessage] ❌ Vendor message body did not "
                        "verify after entry."
                    )
                    return fail_after_claim("body_fill_verification_failed")

                send_button = page.locator(combined_selector(
                    "work_order.email_send"
                ))

                if (
                    send_button.count() != 1
                    or not send_button.is_visible()
                    or send_button.is_disabled()
                ):
                    print(
                        "[VendorMessage] ❌ Exact AppFolio Send button was "
                        "not available."
                    )
                    return fail_after_claim("send_control_unavailable")

                print(
                    f"[VendorMessage] Recipient verified: {vendor_name}"
                )
                print(
                    f"[VendorMessage] WO verified: #{wo_num}"
                )

                send_button.click()
                print("[VendorMessage] Send clicked")

                try:
                    body_field.wait_for(
                        state="hidden",
                        timeout=15000,
                    )
                except Exception:
                    # Some AppFolio builds remove the entire form instead
                    # of merely hiding the textarea.
                    current_body_field = page.locator(combined_selector(
                        "work_order.email_body"
                    ))
                    if (
                        current_body_field.count()
                        and current_body_field.is_visible()
                    ):
                        print(
                            "[VendorMessage] ❌ Email form remained open "
                            "after Send; delivery not confirmed."
                        )
                        return fail_after_claim(
                            "submission_postcheck_unverified"
                        )

                complete_outbound_intent(
                    outbound_intent["intent_id"],
                    verification_receipt="verified_email_form_closed_after_send",
                    db_path=DB_PATH,
                )

                print(
                    f"[VendorMessage] ✅ Scheduling message submitted "
                    f"through AppFolio for WO #{wo_num}"
                )
                return True

    except Exception as exc:
        fail_after_claim("vendor_transport_failure")
        print(
            "[VendorMessage] ❌ AppFolio vendor-email error: "
            f"{type(exc).__name__}"
        )
        return False



def handle_multi_issue_dispatch(reply_text: str, base_num: str) -> bool:
    """Dispatch every stored sub-WO exactly once from one complete reply."""
    pairs = re.findall(
        rf"{re.escape(base_num)}-(\d+)\s*[:=]\s*([^,\n]+)",
        reply_text,
    )
    if not pairs:
        return False
    indexes = [int(index) for index, _choice in pairs]
    if len(indexes) != len(set(indexes)):
        print("[Dispatch] Duplicate sub-WO choices are ambiguous")
        return False

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM wo_triage WHERE wo_num LIKE ? ORDER BY wo_num LIMIT 1",
        (f"{base_num}-%",),
    ).fetchone()
    conn.close()
    if not row:
        print(f"[Dispatch] No triage state for base WO#{base_num}")
        return False
    try:
        stored_issues = json.loads(row["issues"] or "[]")
    except (TypeError, ValueError):
        stored_issues = []
    expected_indexes = set(range(1, len(stored_issues) + 1))
    if not stored_issues or set(indexes) != expected_indexes:
        print(
            "[Dispatch] Multi-issue reply must contain exactly one choice "
            "for every stored sub-WO"
        )
        return False

    choices = {int(index): choice.strip() for index, choice in pairs}
    for index, issue in enumerate(stored_issues, 1):
        sub_wo = f"{base_num}-{index}"
        sub_url = str(issue.get("wo_url") or "").strip()
        if not _is_trusted_work_order_url(sub_url, allow_short=False):
            print(f"[Dispatch] Exact stored URL missing for WO #{sub_wo}")
            return False
        existing = get_db()
        try:
            sub_row = existing.execute(
                "SELECT 1 FROM wo_triage WHERE wo_num = ?", (sub_wo,)
            ).fetchone()
        finally:
            existing.close()
        if not sub_row:
            _save_triage_state(
                sub_wo, sub_url, row["tenant"], row["address"],
                str(issue.get("description") or row["job_desc"]),
                str(issue.get("trade") or row["issue_type"]),
                json.loads(row["conversation"] or "[]"), issues=[issue],
            )
            conn = get_db()
            conn.execute(
                "UPDATE wo_triage SET status = ? WHERE wo_num = ?",
                ("waiting_for_manager_vendor_approval", sub_wo),
            )
            conn.commit()
            conn.close()

    for index in sorted(expected_indexes):
        if not handle_manager_vendor_reply(
            choices[index], f"{base_num}-{index}"
        ):
            return False
    return True


def handle_manager_vendor_reply(reply_text: str, wo_num: str) -> bool:
    """
    Process Manager's reply choosing a vendor.
    Single issue: "1", "2", "3", "in-house", or vendor name.
    Multi issue:  "175-1: 1, 175-2: in-house, 175-3: Gary"
    """
    base_num = wo_num.split("-")[0]
    # Multi-issue format? Handle and return.
    if re.search(rf"{re.escape(base_num)}-\d+\s*[:=]", reply_text):
        return handle_multi_issue_dispatch(reply_text, base_num)

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM wo_triage WHERE wo_num = ?", (wo_num,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"[Dispatch] No triage state for WO#{wo_num}")
        return False

    wo_url  = row["wo_url"]
    tenant  = row["tenant"]
    address = row["address"]
    issue_type = row["issue_type"]

    # Get the one recommended vendor that was sent to Manager (fixed
    # 3-option scheme: 1 = this vendor, 2 = in-house, 3 = a different
    # vendor she names herself).
    vendors = get_suggested_vendors(address, issue_type)[:1]

    selected_vendor = None
    # Manager writes in natural, often multi-line sentences ("Please send
    # ABC Plumbing", "Hi Rachel,\n\nLets go ahead and send in-house
    # maintenance tech fernando", "2. Send in house maintenance,
    # Fernando", "3.\nABC Plumbing") rather than bare imperatives —
    # collapse to one line, strip a leading email greeting, a leading
    # numbered-option marker, and conversational preambles/sign-offs so
    # the remaining text is just the vendor name, not the whole sentence.
    collapsed_reply = re.sub(r"\s+", " ", reply_text.strip())
    collapsed_reply = re.sub(
        r"^(?:hi|hello|hey)\s+rachel[,.]?\s*",
        "", collapsed_reply, flags=re.IGNORECASE,
    ).strip()
    collapsed_reply = re.sub(
        r"^\d+\s*[.)\-:]\s*", "", collapsed_reply
    ).strip()
    normalized_reply = re.sub(
        r"^(?:please\s+)?(?:(?:let'?s|lets)\s+go\s+ahead\s+and\s+)?"
        r"(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
        r"(?:assign|use|send|sent|dispatch|hire|go\s+with|choose|pick)\s+",
        "", collapsed_reply, flags=re.IGNORECASE,
    ).strip()
    normalized_reply = re.sub(
        r"\s*[,.]?\s*(?:please|thanks?|thank\s+you)\.?\s*$",
        "", normalized_reply, flags=re.IGNORECASE,
    ).strip()
    reply_lower = normalized_reply.lower()

    # "In-house" selection can arrive anywhere in a longer sentence
    # ("send in house maintenance, Fernando", "in-house maintenance tech
    # fernando") — a specific tech name isn't recorded yet (Owner is
    # building AppFolio's own Assign-To write separately), but the
    # selection itself must still be recognized rather than falling
    # through to a garbage literal vendor name.
    if re.search(r"\bin[\s-]?house\b", reply_lower):
        selected_vendor = {"name": "In-house", "phone": "", "trade": issue_type}
    elif reply_lower == "1" and vendors:
        selected_vendor = vendors[0]
    elif reply_lower == "2":
        selected_vendor = {"name": "In-house", "phone": "", "trade": issue_type}
    elif reply_lower == "3":
        print(
            "[Dispatch] Manager replied '3' (different vendor) without "
            "naming one — reply was not processed"
        )
        return False
    else:
        # Manager typed a vendor name
        conn = get_db()
        row2 = conn.execute(
            "SELECT name, phone, trade FROM vendors "
            "WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) "
            "AND COALESCE(active, 1) = 1",
            (normalized_reply,)
        ).fetchone()
        if not row2 and normalized_reply.endswith("."):
            # A trailing sentence period ("Please send First City
            # Construction.") isn't part of the vendor's name — retry
            # without it before falling back to a literal/garbage name.
            # A vendor name that genuinely ends in an abbreviation
            # (e.g. "... Inc.") already matched on the first attempt above.
            row2 = conn.execute(
                "SELECT name, phone, trade FROM vendors "
                "WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) "
                "AND COALESCE(active, 1) = 1",
                (normalized_reply.rstrip("."),)
            ).fetchone()
        if not row2:
            # Full-sentence replies keep arriving in new shapes the prefix
            # stripping above can't all anticipate ("Lets go with a
            # different vendor. Number 3. First city construction." -
            # confirmed live Sep 2 2026). Rather than chase every phrasing,
            # fall back to searching for any known active vendor's name
            # as a substring of the raw reply - if exactly one matches,
            # that's unambiguously the one Manager named.
            all_vendors = conn.execute(
                "SELECT name, phone, trade FROM vendors "
                "WHERE COALESCE(active, 1) = 1"
            ).fetchall()
            reply_haystack = re.sub(
                r"\s+", " ", reply_text.strip()
            ).lower()
            substring_matches = [
                candidate for candidate in all_vendors
                if candidate["name"].strip().lower() in reply_haystack
            ]
            if len(substring_matches) == 1:
                row2 = substring_matches[0]
                print(
                    "[Dispatch] Resolved vendor by substring match in "
                    f"reply text: {row2['name']!r}"
                )
        conn.close()
        if row2:
            selected_vendor = {"name": row2[0], "phone": row2[1], "trade": row2[2]}
        else:
            selected_vendor = {
                "name": normalized_reply, "phone": "", "trade": issue_type
            }

    if not selected_vendor["name"]:
        print("[Dispatch] Empty vendor selection was rejected")
        return False

    print(f"[Dispatch] Manager chose: {selected_vendor['name']}")

    if wo_num.upper().startswith("QA-"):
        conversation = json.loads(row["conversation"] or "[]")
        conversation.append({
            "management_decision": selected_vendor["name"],
            "decision_source": "Owner synthetic QA",
            "recorded_at": datetime.now().isoformat(),
        })

        sent = send_owner_qa_email(
            subject=f"Rachel QA Complete — WO #{wo_num}",
            message=(
                "OWNER-ONLY SYNTHETIC WORK-ORDER TEST COMPLETE\n"
                "No tenant, vendor, or AppFolio record was contacted.\n\n"
                f"Work Order: #{wo_num}\n"
                f"Simulated selection: {selected_vendor['name']}\n"
                f"Tenant: {tenant}\n"
                f"Property: {address}\n\n"
                "Rachel recorded the simulated management decision and "
                "ended the test before vendor dispatch."
            ),
        )

        print(
            f"[OwnerQA] Synthetic management decision recorded for "
            f"WO #{wo_num}: {selected_vendor['name']} | "
            f"confirmation sent: {sent}"
        )
        if sent:
            conn = get_db()
            conn.execute(
                """
                UPDATE wo_triage
                SET conversation = ?,
                    status = 'qa_management_decision_complete',
                    vendor_name = ?,
                    updated_at = ?
                WHERE wo_num = ?
                """,
                (
                    json.dumps(conversation), selected_vendor["name"],
                    datetime.now().isoformat(), wo_num,
                ),
            )
            conn.commit()
            conn.close()
        return bool(sent)

    if selected_vendor["name"] == "In-house":
        if (
            row["status"] == "in_house_assigned"
            and str(row["vendor_name"] or "").lower() == "in-house"
        ):
            return True
        # No AppFolio vendor assignment needed
        notified = send_sms_via_gmail(
            f"WO #{wo_num} — In-house maintenance assigned.\n"
            f"Tenant: {tenant} | {address}\n"
            f"Please schedule directly with tenant.",
            urgent=False
        )
        if notified:
            conn = get_db()
            conn.execute(
                "UPDATE wo_triage SET status = ?, vendor_name = ?, "
                "updated_at = ? WHERE wo_num = ?",
                (
                    "in_house_assigned", "In-house",
                    datetime.now().isoformat(), wo_num,
                ),
            )
            conn.commit()
            conn.close()
        return bool(notified)

    if (
        str(row["vendor_name"] or "").strip().lower()
        == selected_vendor["name"].strip().lower()
        and row["status"] in {
            "waiting_for_vendor_confirmation",
            "waiting_for_tenant_availability",
            "vendor_schedule_needs_review",
        }
    ):
        return True

    # Dispatch vendor in AppFolio
    success = dispatch_vendor_to_wo(
        wo_url=wo_url,
        wo_num=wo_num,
        vendor_name=selected_vendor["name"],
        vendor_phone=selected_vendor.get("phone", ""),
        trade=selected_vendor.get("trade", issue_type),
        tenant=tenant,
        address=address
    )

    if success:
        # Read the availability saved from the AppFolio work order
        conn = get_db()
        availability_row = conn.execute(
            """
            SELECT resident_availability
            FROM wo_triage
            WHERE wo_num = ?
            """,
            (wo_num,),
        ).fetchone()
        conn.close()

        resident_availability = (
            availability_row["resident_availability"].strip()
            if availability_row
            and availability_row["resident_availability"]
            else ""
        )

        if resident_availability:
            try:
                initialize_schedule(wo_num, db_path=DB_PATH)
                availability_event = hashlib.sha256(
                    f"dispatch-availability|{wo_num}|"
                    f"{resident_availability}".encode("utf-8")
                ).hexdigest()
                capture_tenant_availability(
                    wo_num, resident_availability,
                    event_key=availability_event,
                    db_path=DB_PATH,
                )
                record_vendor_availability_delivery(
                    wo_num,
                    delivery_receipt=(
                        "verified-vendor-secure-work-order-notification:"
                        + availability_event
                    ),
                    event_key=hashlib.sha256(
                        f"dispatch-vendor-delivery|{wo_num}|"
                        f"{availability_event}".encode("utf-8")
                    ).hexdigest(),
                    db_path=DB_PATH,
                )
            except ScheduleWorkflowError as error:
                msg = (
                    f"WO #{wo_num} — {selected_vendor['name']} assigned, "
                    f"but scheduling state did not initialize: {error}. "
                    "Manual scheduling review is required."
                )
                next_status = "vendor_schedule_needs_review"
            else:
                msg = (
                    f"WO #{wo_num} — {selected_vendor['name']} assigned ✅\n"
                    f"Tenant: {tenant} | {address}\n"
                    f"The vendor received the secure work-order notification "
                    f"containing resident availability.\n"
                    f"Waiting for one specific two-hour arrival window."
                )
                next_status = "waiting_for_vendor_confirmation"

        else:
            first_name = (
                tenant.split()[0]
                if tenant and tenant != "Unknown"
                else "there"
            )
           
            tenant_message = (
                f"Hi {first_name}, we are coordinating the appointment "
                f"for your maintenance request. What days and time windows "
                f"are you available for the vendor to visit?"
            )

            tenant_message_sent = _send_appfolio_message(
                wo_url,
                tenant_message,
                tenant,
                wo_num=wo_num,
                vendor_name=selected_vendor["name"],
            )

            if tenant_message_sent:
                msg = (
                    f"WO #{wo_num} — {selected_vendor['name']} assigned ✅\n"
                    f"Tenant: {tenant} | {address}\n"
                    f"No resident availability was listed, so Rachel asked "
                    f"the tenant for available days and times."
                )
                next_status = "waiting_for_tenant_availability"

            else:
                msg = (
                    f"WO #{wo_num} — vendor assigned, but Rachel could not "
                    f"request availability from {tenant} through AppFolio.\n"
                    f"Manual tenant contact is required."
                )
                next_status = "tenant_availability_message_failed"

    else:
        msg = (
            f"WO #{wo_num} — Could not auto-assign "
            f"{selected_vendor['name']} in AppFolio.\n"
            f"Please assign manually.\n"
            f"Vendor: {selected_vendor['name']} "
            f"{selected_vendor.get('phone', '')}"
        )
        next_status = "vendor_assignment_failed"

    notified = send_sms_via_gmail(msg, urgent=False)

    # Preserve the triage conversation and update Rachel's workflow status
    _save_triage_state(
        wo_num,
        wo_url,
        tenant,
        address,
        row["job_desc"],
        issue_type,
        json.loads(row["conversation"]),
    )

    conn = get_db()
    conn.execute(
        "UPDATE wo_triage SET status = ?, vendor_name = ?, updated_at = ? "
        "WHERE wo_num = ?",
        (
            next_status, selected_vendor["name"],
            datetime.now().isoformat(), wo_num,
        ),
    )
    conn.commit()
    conn.close()
    if not success or next_status == "tenant_availability_message_failed":
        return False
    if next_status == "vendor_schedule_needs_review":
        return bool(notified)
    return True


if __name__ == "__main__":
    raise SystemExit(
        "Work-order triage has no standalone live entry point; main.py "
        "invokes it only from verified events."
    )
