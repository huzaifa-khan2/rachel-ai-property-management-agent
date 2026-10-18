"""
Rachel - Vendor Lifecycle Module
Everything that happens AFTER a vendor is assigned to a work order:

  1. Vendor follow-up nudges — no estimate in 3 days / no schedule in 2 days
     -> nudge vendor via AppFolio email; escalate to Manager after 2nd nudge
  2. Estimate approval flow — vendor replies with estimate -> Rachel extracts
     amount -> texts Manager approve/reject/get-another -> relays decision
     (POLICY: Manager approves ALL work in writing, regardless of amount)
  3. Scheduling automation — vendor replies with date/time -> Rachel updates
     the WO Schedule (Actions -> Schedule modal) in AppFolio
  4. Post-work satisfaction check — Manager texts "1540 done" -> Rachel asks
     the tenant if everything was resolved; unhappy reply alerts Manager

Called from rachel_inbox_monitor.py (email triggers) and main.py (daily job).
"""

import os, re, json, time, random, sqlite3
import hashlib
from datetime import datetime, timedelta
from anthropic import Anthropic
from rachel_appfolio_browser import get_appfolio_browser_manager
from rachel_appfolio_ui import (
    AppFolioReadAdapter,
    WorkOrderDetailPage,
    combined_selector,
    sanitized_failure_reason,
    unique_visible,
)
from rachel_safety import appfolio_write_allowed
from rachel_maintenance_scheduling import (
    AWAITING_TENANT_AVAILABILITY,
    AWAITING_TENANT_CONFIRMATION,
    AWAITING_VENDOR_WINDOW,
    APPFOLIO_SCHEDULE_WRITE_UNVERIFIED,
    READY_TO_SCHEDULE,
    SCHEDULED_COMPLETE,
    SCHEDULED_PENDING_CONFIRMATIONS,
    TENANT_AVAILABILITY_RECEIVED,
    ScheduleWorkflowError,
    build_tenant_window_confirmation_request,
    build_vendor_window_request,
    claim_appfolio_schedule_attempt,
    capture_tenant_availability,
    get_schedule,
    initialize_schedule,
    normalize_two_hour_window,
    record_appfolio_schedule_verified,
    record_final_confirmation_delivery,
    record_tenant_counter_availability,
    record_tenant_window_confirmation,
    record_vendor_availability_delivery,
    record_vendor_two_hour_window,
    require_future_two_hour_window,
    verify_appfolio_schedule_attempt_claim,
)

DB_PATH = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")
client = Anthropic()

# Reuse triage helpers (SMS, AppFolio login, messaging)
from rachel_wo_triage import (
    send_sms_via_gmail,
    login_appfolio,
    human_delay,
    _send_appfolio_message,
    send_vendor_appfolio_message,
    CLOSING_MESSAGE,
    TEST_MODE,
    _truncate_at_word_boundary,
)
   

ESTIMATE_NUDGE_DAYS = 3   # no estimate after N days -> nudge
SCHEDULE_NUDGE_DAYS = 2   # no scheduling confirmation after N days -> nudge
MAX_NUDGES = 2            # after this, escalate to Manager


# ── DB helpers ──────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_lifecycle_columns():
    """Add lifecycle tracking columns to wo_triage if missing."""
    conn = get_db()
    for col, typ in [
    ("vendor_name", "TEXT"),
    ("nudge_count", "INTEGER DEFAULT 0"),
    ("last_nudge_at", "TEXT"),
    ("estimate_amount", "REAL"),
    ("scheduled_for", "TEXT"),

    # Scheduling coordination
    ("resident_availability", "TEXT"),
    ("proposed_date", "TEXT"),
    ("proposed_time", "TEXT"),
    ("proposed_duration", "TEXT"),
    ("proposed_by", "TEXT"),
    ("waiting_for", "TEXT"),
    ("proposed_options", "TEXT"),
]:
        try:
            conn.execute(f"ALTER TABLE wo_triage ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _update_wo(wo_num: str, **kwargs):
    conn = get_db()
    for col, val in kwargs.items():
        try:
            conn.execute(
                f"UPDATE wo_triage SET {col} = ?, updated_at = ? "
                f"WHERE wo_num = ?",
                (val, datetime.now().isoformat(), wo_num))
        except Exception as e:
            print(f"[Lifecycle] DB update error {col}: {e}")
    conn.commit()
    conn.close()


def _get_wo(wo_num: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM wo_triage WHERE wo_num = ? OR wo_num LIKE ?",
        (wo_num, f"{wo_num}-%")).fetchone()
    conn.close()
    return dict(row) if row else None


# ── 1. Vendor follow-up nudges (run daily from main.py) ────────────────────

def check_vendor_followups():
    """
    Daily job. For every WO with status 'vendor_assigned':
      - past ESTIMATE_NUDGE_DAYS with no estimate -> nudge vendor
      - past SCHEDULE_NUDGE_DAYS with no schedule -> nudge vendor
      - past MAX_NUDGES nudges -> escalate to Manager, stop nudging
    """
    _ensure_lifecycle_columns()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM wo_triage WHERE status = 'vendor_assigned'").fetchall()
    conn.close()

    now = datetime.now()
    for row in rows:
        wo_num = row["wo_num"]
        try:
            updated = datetime.fromisoformat(row["updated_at"])
        except Exception:
            continue
        days_idle = (now - updated).days
        nudges = row["nudge_count"] or 0
        vendor = row["vendor_name"] or "the vendor"

        needs_nudge = (
            (row["estimate_amount"] is None and days_idle >= ESTIMATE_NUDGE_DAYS)
            or (row["scheduled_for"] is None and days_idle >= SCHEDULE_NUDGE_DAYS)
        )
        if not needs_nudge:
            continue

        if nudges >= MAX_NUDGES:
            escalated = send_sms_via_gmail(
                f"WO #{wo_num} — {vendor} has not responded after "
                f"{nudges} follow-ups ({days_idle} days idle).\n"
                f"Tenant: {row['tenant']} | {row['address']}\n"
                f"Recommend calling them or choosing a different vendor.",
                urgent=False)
            if escalated:
                _update_wo(wo_num, status="vendor_stalled")
                print(f"[Followup] WO#{wo_num} escalated to management")
            else:
                print(
                    f"[Followup] WO#{wo_num} escalation delivery failed; "
                    "state retained for retry"
                )
            continue

        missing = ("your written estimate"
                   if row["estimate_amount"] is None
                   else "one specific two-hour arrival window for office coordination")
        nudge_msg = (
            f"Hello,\n\n"
            f"Following up on Work Order #{wo_num} at {row['address']}. "
            f"We have not yet received {missing}. "
            f"Please reply to this email at your earliest convenience so we "
            f"can keep the resident informed.\n\n"
            f"Reminder: no work shall commence until we have received a "
            f"written estimate and you have received written approval from "
            f"our office.\n\n"
            f"Thank you,\nRachel\nAssistant Property Manager\n"
            f"ClientCo Property Management\n555-010-0000")

        ok = send_vendor_appfolio_message(
            row["wo_url"], nudge_msg,
            wo_num=wo_num, tenant=row["tenant"], vendor_name=vendor,
        )
        if ok:
            _update_wo(
                wo_num, nudge_count=nudges + 1,
                last_nudge_at=now.isoformat(),
            )
        else:
            send_sms_via_gmail(
                f"WO #{wo_num} — vendor follow-up could not be verified "
                f"for {vendor}. Manual contact is required.", urgent=False,
            )
        print(f"[Followup] WO#{wo_num} nudge {nudges+1} "
              f"{'sent' if ok else 'FAILED'}")


# ── 2. Estimate approval flow ───────────────────────────────────────────────

def handle_vendor_estimate(email_data: dict, wo_num: str):
    """
    Vendor email contains an estimate. Extract the amount, save it,
    and ask Manager for written approval.
    """
    _ensure_lifecycle_columns()
    body = email_data["body"]
    wo = _get_wo(wo_num)
    if not wo:
        print(f"[Estimate] No triage record for WO#{wo_num}")
        return False

    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=200,
        system=("Extract the estimate details from this vendor email. "
                "Reply ONLY with JSON: "
                '{"amount": 185.00, "scope": "one line describing the work"} '
                "Use null for amount if no dollar figure is present."),
        messages=[{"role": "user", "content": body[:2000]}])
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "")
    try:
        est = json.loads(raw)
    except json.JSONDecodeError:
        est = {"amount": None, "scope": _truncate_at_word_boundary(body, 100)}

    amount = est.get("amount")
    scope = est.get("scope", "")
    _update_wo(wo["wo_num"], estimate_amount=amount)

    amt_str = f"${amount:,.2f}" if amount else "amount unclear — see email"
    notified = send_sms_via_gmail(
        f"ESTIMATE — WO #{wo_num}\n"
        f"{wo['vendor_name'] or 'Vendor'}: {amt_str}\n"
        f"Scope: {scope}\n"
        f"Tenant: {wo['tenant']} | {wo['address']}\n\n"
        f'Reply: "approve {wo_num}" | "reject {wo_num}" | '
        f'"another {wo_num}"',
        urgent=False)
    if notified:
        _update_wo(wo["wo_num"], status="estimate_pending_approval")
        print(f"[Estimate] WO#{wo_num} management approval requested")
        return True
    print(f"[Estimate] WO#{wo_num} management notification failed")
    return False


def handle_manager_estimate_decision(decision: str, wo_num: str) -> bool:
    """
    Manager replied approve/reject/another for a pending estimate.
    Relay the decision to the vendor via AppFolio.
    """
    wo = _get_wo(wo_num)
    if not wo:
        print(f"[Estimate] No record for WO#{wo_num}")
        return False
    d = decision.strip().lower()
    amt = wo.get("estimate_amount")
    amt_str = f"${amt:,.2f}" if amt else "your estimate"

    if d.startswith("approve"):
        msg = (f"Hello,\n\nYour estimate of {amt_str} for Work Order "
               f"#{wo_num} has been APPROVED in writing by our office. "
               f"You may proceed with the approved scope after scheduling "
               f"through our office. Do not finalize directly with the "
               f"resident. Please reply with one specific two-hour arrival "
               f"window within the resident's provided availability. Update "
               f"the work order when complete.\n\nThank you,\nRachel\n"
               f"Assistant Property Manager\nClientCo Property Management")
        new_status = "work_approved"
        manager_note = f"WO #{wo_num} — approval sent to vendor ✅"
    elif d.startswith("reject"):
        msg = (f"Hello,\n\nWe are unable to approve the estimate of "
               f"{amt_str} for Work Order #{wo_num} at this time. "
               f"Please do not proceed with any work. We will follow up "
               f"with next steps shortly.\n\nThank you,\nRachel\n"
               f"Assistant Property Manager\nClientCo Property Management")
        new_status = "estimate_rejected"
        manager_note = f"WO #{wo_num} — rejection sent to vendor"
    else:  # another
        msg = (f"Hello,\n\nThank you for your estimate for Work Order "
               f"#{wo_num}. We are gathering additional estimates before "
               f"proceeding. Please do not begin any work. We will be in "
               f"touch.\n\nThank you,\nRachel\n"
               f"Assistant Property Manager\nClientCo Property Management")
        new_status = "getting_other_estimates"
        # Suggest alternates to Manager
        try:
            from rachel_wo_triage import get_suggested_vendors
            alts = get_suggested_vendors(wo["address"], wo["issue_type"])
            alt_lines = "\n".join(
                f"  {i}. {v['name']} {v.get('phone','')}"
                for i, v in enumerate(alts, 1)
                if v["name"] != (wo.get("vendor_name") or ""))
        except Exception:
            alt_lines = "  (no alternates in database)"
        manager_note = (f"WO #{wo_num} — vendor told to hold. Alternates:\n"
                       f"{alt_lines}\nReply: \"{wo_num}: [number or name]\"")

    ok = send_vendor_appfolio_message(
        wo["wo_url"], msg,
        wo_num=wo["wo_num"], tenant=wo.get("tenant", ""),
        vendor_name=wo.get("vendor_name", ""),
    )
    _update_wo(
        wo["wo_num"],
        status=(new_status if ok else "estimate_decision_delivery_failed"),
    )
    send_sms_via_gmail(
        manager_note if ok else
        f"WO #{wo_num} — could not send decision via AppFolio, "
        f"please contact vendor directly.", urgent=False)
    print(f"[Estimate] WO#{wo_num} -> {new_status}")
    return bool(ok)


# ── 3. Scheduling automation ────────────────────────────────────────────────

def _schedule_event_key(kind: str, wo_num: str, payload: str) -> str:
    return hashlib.sha256(
        f"{kind}|{wo_num}|{payload}".encode("utf-8")
    ).hexdigest()


def _verified_submission_receipt(kind: str, wo_num: str, payload: str) -> str:
    return "verified-appfolio-submission:" + _schedule_event_key(
        kind, wo_num, payload
    )


def _ensure_vendor_window_state(wo: dict, vendor_event_key: str) -> dict:
    """Bridge legacy WO state into the durable scheduling state machine."""
    state = initialize_schedule(wo["wo_num"], db_path=DB_PATH)
    if state["state"] == AWAITING_TENANT_AVAILABILITY:
        availability = str(wo.get("resident_availability") or "").strip()
        if not availability:
            raise ScheduleWorkflowError(
                "vendor_window_received_before_tenant_availability"
            )
        state = capture_tenant_availability(
            wo["wo_num"], availability,
            event_key=_schedule_event_key(
                "legacy-tenant-availability", wo["wo_num"], availability
            ),
            db_path=DB_PATH,
        )
    if state["state"] == TENANT_AVAILABILITY_RECEIVED:
        # A vendor reply containing a proposed window is durable evidence that
        # the vendor obtained enough resident availability to coordinate.
        state = record_vendor_availability_delivery(
            wo["wo_num"],
            delivery_receipt=f"vendor-reply-evidence:{vendor_event_key}",
            event_key=_schedule_event_key(
                "vendor-reply-proves-availability-delivery",
                wo["wo_num"], vendor_event_key,
            ),
            db_path=DB_PATH,
        )
    return state

def handle_vendor_schedule_reply(email_data: dict, wo_num: str):
    """
    Process appointment options proposed by the vendor.

    Do not schedule the work order yet. Store the vendor's proposal,
    send it to the tenant, and wait for the tenant's response.
    """
    _ensure_lifecycle_columns()

    body = email_data.get("body", "")
    wo = _get_wo(wo_num)

    if not wo:
        print(f"[Schedule] No work-order record found for WO#{wo_num}")
        return False

    body = str(body or "").strip()
    vendor_event_key = _schedule_event_key(
        "vendor-schedule-reply", wo["wo_num"], body
    )
    try:
        _ensure_vendor_window_state(wo, vendor_event_key)
    except ScheduleWorkflowError as error:
        alerted = send_sms_via_gmail(
            f"WO #{wo_num} — vendor scheduling reply could not advance: "
            f"{error}.", urgent=False,
        )
        if alerted:
            _update_wo(
                wo["wo_num"], waiting_for="manual_schedule_review",
                status="vendor_schedule_needs_review",
            )
        return bool(alerted)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=250,
        system=(
            "Extract one specific two-hour arrival window proposed and agreed "
            "by the repair vendor. "
            "Today is "
            + datetime.now().strftime("%A, %B %d, %Y")
            + ". Reply ONLY with valid JSON: "
            '{"date": "MM/DD/YYYY", "start": "9:00 AM", '
            '"end": "11:00 AM"}. '
            "A valid proposal has one unambiguous date, start, and end and "
            "lasts exactly two hours. Return null values for an exact time, "
            "one-hour window, broad/variable window, multiple options, or "
            "missing details. Never invent or expand a window."
        ),
        messages=[
            {
                "role": "user",
                "content": body[:2000],
            }
        ],
    )

    raw = (
        response.content[0]
        .text.strip()
        .replace("```json", "")
        .replace("```", "")
    )

    try:
        payload = json.loads(raw)
        date_value = str(payload.get("date") or "").strip()
        start_value = str(payload.get("start") or "").strip()
        end_value = str(payload.get("end") or "").strip()
        window = normalize_two_hour_window(
            date_value=date_value,
            start_value=start_value,
            end_value=end_value,
        )
    except (json.JSONDecodeError, AttributeError, ScheduleWorkflowError):
        alerted = send_sms_via_gmail(
            f"WO #{wo_num} — the vendor replied about scheduling, but "
            f"Rachel could not verify one specific two-hour arrival window.\n\n"
            f"Vendor message:\n{body[:300]}",
            urgent=False,
        )
        if alerted:
            _update_wo(
                wo["wo_num"],
                proposed_by="vendor",
                waiting_for="manager",
                status="vendor_schedule_needs_review",
            )
        return bool(alerted)

    try:
        schedule_state = record_vendor_two_hour_window(
            wo["wo_num"],
            date_value=window["date"],
            start_value=window["start"],
            end_value=window["end"],
            event_key=vendor_event_key,
            db_path=DB_PATH,
        )
    except ScheduleWorkflowError as error:
        alerted = send_sms_via_gmail(
            f"WO #{wo_num} — vendor window did not match scheduling state: "
            f"{error}.", urgent=False,
        )
        if alerted:
            _update_wo(
                wo["wo_num"], waiting_for="manual_schedule_review",
                status="vendor_schedule_needs_review",
            )
        return bool(alerted)
    if schedule_state.get("idempotent_replay"):
        print(f"[Schedule] WO#{wo_num} duplicate vendor window suppressed")

    first_name = (
        wo["tenant"].split()[0]
        if wo.get("tenant") and wo["tenant"] != "Unknown"
        else "there"
    )

    tenant_message = (
        f"Hi {first_name}, "
        + build_tenant_window_confirmation_request(
            wo["wo_num"], schedule_state
        )
    )

    message_sent = _send_appfolio_message(
        wo["wo_url"],
        tenant_message,
        wo["tenant"],
        wo_num=wo["wo_num"],
        vendor_name=wo.get("vendor_name", ""),
    )

    if message_sent:
        _update_wo(
            wo["wo_num"],
            proposed_date=schedule_state["window_date"],
            proposed_time=(
                f"{schedule_state['window_start']}-"
                f"{schedule_state['window_end']}"
            ),
            proposed_duration="2 Hours",
            proposed_options=json.dumps([{
                "date": schedule_state["window_date"],
                "start": schedule_state["window_start"],
                "end": schedule_state["window_end"],
                "window_fingerprint": schedule_state["window_fingerprint"],
            }]),
            proposed_by="vendor",
            waiting_for="tenant",
            status="waiting_for_tenant_confirmation",
        )

        send_sms_via_gmail(
            f"WO #{wo_num} — vendor two-hour arrival window sent to "
            f"{wo['tenant']} for confirmation.\n"
            f"Rachel is waiting for the tenant's response.",
            urgent=False,
        )
        return True
    else:
        _update_wo(
            wo["wo_num"],
            proposed_date=schedule_state["window_date"],
            proposed_time=(
                f"{schedule_state['window_start']}-"
                f"{schedule_state['window_end']}"
            ),
            proposed_duration="2 Hours",
            proposed_by="vendor",
            waiting_for="manual_tenant_contact",
            status="tenant_schedule_message_failed",
        )

        alerted = send_sms_via_gmail(
            f"WO #{wo_num} — Rachel received the vendor's appointment "
            f"options but could not send them to the tenant through "
            f"AppFolio. Manual tenant contact is required.",
            urgent=False,
        )
        return bool(alerted)


def _schedule_window_visible_in_text(text: str, window: dict) -> bool:
    """Match the exact date/window across common AppFolio render formats."""
    normalized = " ".join(str(text or "").lower().split())
    date_value = datetime.strptime(window["date"], "%Y-%m-%d")
    date_variants = {
        window["date"].lower(),
        date_value.strftime("%m/%d/%Y").lower(),
        date_value.strftime("%m/%d/%y").lower(),
        date_value.strftime("%B %d, %Y").lower(),
        date_value.strftime("%b %d, %Y").lower(),
        date_value.strftime("%b %-d, %Y").lower(),
        date_value.strftime("%B %-d, %Y").lower(),
    }
    start = datetime.strptime(window["start"], "%H:%M")
    end = datetime.strptime(window["end"], "%H:%M")
    start_variants = {
        window["start"].lower(),
        start.strftime("%I:%M %p").lstrip("0").lower(),
    }
    end_variants = {
        window["end"].lower(),
        end.strftime("%I:%M %p").lstrip("0").lower(),
    }
    date_present = any(value in normalized for value in date_variants)
    start_present = any(value in normalized for value in start_variants)
    end_or_duration_present = (
        any(value in normalized for value in end_variants)
        or "2 hours" in normalized
        or "120 minutes" in normalized
    )
    return date_present and start_present and end_or_duration_present


def _select_react_dropdown_option(page, control, option_text):
    """Open a react-select combobox and click its exact-text option.

    AppFolio's Start Time / Duration schedule fields are react-select
    comboboxes, not native <select> elements: their input never holds the
    chosen value (it is a search box that stays empty), so a value can only
    be set by opening the dropdown and clicking the matching
    role="menuitem" button (confirmed against the live DOM Aug 28 2026).
    """
    container = control.locator(
        combined_selector("work_order.schedule_control_container")
    )
    if container.count() != 1:
        return False
    container.click()
    human_delay(0.3, 0.6)
    option = page.locator(
        combined_selector("work_order.schedule_option")
    ).filter(has_text=re.compile(rf"^\s*{re.escape(option_text)}\s*$"))
    if option.count() != 1 or not option.is_visible():
        return False
    option.click()
    human_delay(0.2, 0.4)
    return True


def update_wo_schedule(
    wo_url: str, date_str: str, start_time: str, end_time: str,
    duration: str = "2 Hours", *, wo_num: str = "", tenant: str = "",
    vendor_name: str = "", write_attempt_id: str = "",
):
    """Save one previously double-confirmed two-hour arrival window."""
    try:
        normalized_window = normalize_two_hour_window(
            date_value=date_str,
            start_value=start_time,
            end_value=end_time,
        )
        require_future_two_hour_window(normalized_window)
    except ScheduleWorkflowError as error:
        print(f"[Schedule] Invalid two-hour arrival window: {error}")
        return False
    if str(duration or "").strip().lower() != "2 hours":
        print("[Schedule] Duration must be exactly 2 Hours")
        return False
    if TEST_MODE:
        print(
            f"\n[TEST MODE] AppFolio schedule update blocked\n"
            f"WO URL: {wo_url}\n"
            f"Date: {normalized_window['date']}\n"
            f"Arrival window: {normalized_window['start']}-"
            f"{normalized_window['end']}\n"
            f"Duration: {duration}\n"
        )
        return False
    allowed, reason = appfolio_write_allowed(
        operation="schedule_work_order",
        wo_url=wo_url,
        wo_num=wo_num,
        tenant=tenant,
        vendor_name=vendor_name,
    )
    if not allowed:
        print(
            "[Schedule] AppFolio schedule update blocked by "
            f"safety gate: {reason}"
        )
        return False
    try:
        current_state = get_schedule(wo_num, db_path=DB_PATH)
        if not current_state:
            raise ScheduleWorkflowError("appfolio_schedule_state_missing")
        if current_state.get("state") == APPFOLIO_SCHEDULE_WRITE_UNVERIFIED:
            verify_appfolio_schedule_attempt_claim(
                wo_num,
                window_fingerprint=normalized_window["window_fingerprint"],
                attempt_id=write_attempt_id,
                db_path=DB_PATH,
            )
            print(
                "[Schedule] Prior browser write attempt is unverified; "
                "automatic retry blocked"
            )
            return False
        if current_state.get("state") != READY_TO_SCHEDULE:
            raise ScheduleWorkflowError("appfolio_schedule_not_ready")
    except ScheduleWorkflowError as error:
        print(f"[Schedule] Durable write readiness check failed: {error}")
        return False
    try:
        manager = get_appfolio_browser_manager()
        with manager.operation("update_work_order_schedule") as page:
            AppFolioReadAdapter(page, "work_order_writes").navigate_read(
                wo_url,
                route_pattern=(
                    "/maintenance/service_requests/:request/"
                    "work_orders/:work_order"
                ),
                timeout=30000,
                attempts=2,
            )
            human_delay(2, 4)

            detail_page = WorkOrderDetailPage(page)
            body_text = detail_page.body_text()
            if (
                "/work_orders/" not in str(page.url or "")
                or not detail_page.has_semantic_identity(
                    wo_num, body_text=body_text
                )
            ):
                print("[Schedule] Exact work-order page verification failed")
                return False

            # Actions menu -> Schedule
            actions = unique_visible(page, "work_order.actions")
            if not actions:
                print("[Schedule] Expected one visible Actions control")
                return False
            actions.click()
            human_delay(1, 2)
            sched_item = unique_visible(page, "work_order.schedule_action")
            if not sched_item:
                print("[Schedule] Expected one visible Schedule action")
                return False
            sched_item.click()
            human_delay(2, 3)

            # The modal opens on a list of radios (one per tenant-submitted
            # availability block, each up to several hours wide) rather
            # than a direct date/time entry form. The exact 2-hour window
            # fields only exist in the DOM after "Choose different time"
            # is selected - confirmed live Sep 2 2026.
            choose_different_time = unique_visible(
                page, "work_order.schedule_choose_different_time"
            )
            if not choose_different_time:
                print(
                    "[Schedule] Expected one visible "
                    "'Choose different time' option"
                )
                return False
            choose_different_time.click()
            human_delay(1, 2)

            date_field = unique_visible(page, "work_order.schedule_date")
            time_field = unique_visible(page, "work_order.schedule_time")
            dur = unique_visible(page, "work_order.schedule_duration")
            save = unique_visible(page, "work_order.schedule_save")
            if not all((date_field, time_field, dur, save)):
                print("[Schedule] Exact schedule form controls were not unique")
                return False

            ui_date = datetime.strptime(
                normalized_window["date"], "%Y-%m-%d"
            ).strftime("%m/%d/%Y")
            ui_start = datetime.strptime(
                normalized_window["start"], "%H:%M"
            ).strftime("%I:%M %p").lstrip("0")

            # Claim immediately before the first form mutation. Navigation,
            # target verification, and control discovery are read-only; a
            # failure in those stages must remain normally retryable rather
            # than being mislabeled as an ambiguous AppFolio write.
            try:
                claimed = claim_appfolio_schedule_attempt(
                    wo_num,
                    window_fingerprint=normalized_window[
                        "window_fingerprint"
                    ],
                    attempt_id=write_attempt_id,
                    event_key=write_attempt_id,
                    db_path=DB_PATH,
                )
                if claimed.get("idempotent_replay"):
                    print("[Schedule] Duplicate write claim blocked")
                    return False
                claimed_state = verify_appfolio_schedule_attempt_claim(
                    wo_num,
                    window_fingerprint=normalized_window[
                        "window_fingerprint"
                    ],
                    attempt_id=write_attempt_id,
                    db_path=DB_PATH,
                )
            except ScheduleWorkflowError as error:
                print(
                    "[Schedule] Durable write claim verification failed: "
                    f"{error}"
                )
                return False
            if (
                claimed_state.get("window_date")
                != normalized_window["date"]
                or claimed_state.get("window_start")
                != normalized_window["start"]
                or claimed_state.get("window_end")
                != normalized_window["end"]
            ):
                print(
                    "[Schedule] Durable window does not match browser "
                    "write request"
                )
                return False

            date_field.fill(ui_date)
            # The date field's own calendar dropdown opens on fill and
            # overlaps the Start Time control below it, intercepting the
            # click that opens that field (confirmed live Aug 28 2026).
            date_field.press("Escape")
            if date_field.input_value().strip() != ui_date:
                print("[Schedule] Date field verification failed")
                return False

            if not _select_react_dropdown_option(page, time_field, ui_start):
                print("[Schedule] Start-time selection failed")
                return False
            selected_time = time_field.locator(
                combined_selector("work_order.schedule_selected_value")
            ).inner_text().strip()
            if selected_time.upper() != ui_start.upper():
                print("[Schedule] Start-time field verification failed")
                return False

            if not _select_react_dropdown_option(page, dur, "2 Hours"):
                print("[Schedule] Duration selection failed")
                return False
            selected_duration = dur.locator(
                combined_selector("work_order.schedule_selected_value")
            ).inner_text().strip()
            if selected_duration.lower() != "2 hours":
                print("[Schedule] Duration field verification failed")
                return False

            save.click()
            for _attempt in range(12):
                human_delay(0.25, 0.35)
                # React may replace the entire modal after Save. Re-query the
                # semantic contract instead of retaining the pre-click node.
                save_visible = bool(
                    unique_visible(page, "work_order.schedule_save")
                )
                rendered = WorkOrderDetailPage(page).body_text()
                if (
                    not save_visible
                    and _schedule_window_visible_in_text(
                        rendered, normalized_window
                    )
                ):
                    receipt = (
                        "schedule_window_semantically_verified_"
                        + normalized_window["window_fingerprint"]
                    )
                    print("[Schedule] Exact two-hour window verified after Save")
                    return receipt
            print("[Schedule] Save postcondition was not semantically verified")
            return False
    except Exception as e:
        print(
            "[Schedule] Error: "
            f"{sanitized_failure_reason(e)}"
        )
        return False
    

def _legacy_handle_tenant_schedule_reply(wo_num: str, reply: str):
    """
    Process a tenant's scheduling response.

    The tenant may:
    - provide initial availability,
    - accept a vendor appointment,
    - select one of several options, or
    - reject the options and provide different availability.
    """
    raise RuntimeError(
        "Legacy scheduling handler is disabled; use the durable two-hour "
        "arrival-window state machine."
    )
    _ensure_lifecycle_columns()

    wo = _get_wo(wo_num)
    if not wo:
        print(f"[Schedule] No work-order record found for WO#{wo_num}")
        return

    reply = (reply or "").strip()
    current_status = wo.get("status") or ""

    if not reply:
        send_sms_via_gmail(
            f"WO #{wo_num} — tenant scheduling reply was empty.",
            urgent=False,
        )
        return

    # Tenant is providing availability for the first time
    if current_status == "waiting_for_tenant_availability":
        vendor_message = (
            f"Hello,\n\n"
            f"The resident provided the following availability for "
            f"Work Order #{wo_num}:\n\n"
            f"{reply}\n\n"
            f"Please reply with an exact date and arrival time that works "
            f"within this availability. If none of these times work, please "
            f"provide two or three alternative appointment options.\n\n"
            f"Please do not schedule directly with the resident. Our office "
            f"will coordinate and confirm the final appointment.\n\n"
            f"Thank you,\n"
            f"Rachel\n"
            f"Assistant Property Manager\n"
            f"ClientCo Property Management\n"
            f"555-010-0000"
        )

        sent = send_vendor_appfolio_message(
            wo["wo_url"],
            vendor_message,
        )

        if sent:
            _update_wo(
                wo["wo_num"],
                resident_availability=reply,
                proposed_options=None,
                proposed_by="tenant",
                waiting_for="vendor",
                status="waiting_for_vendor_confirmation",
            )

            send_sms_via_gmail(
                f"WO #{wo_num} — tenant availability received and sent "
                f"to the vendor. Waiting for the vendor's response.",
                urgent=False,
            )
        else:
            _update_wo(
                wo["wo_num"],
                resident_availability=reply,
                waiting_for="manual_vendor_contact",
                status="vendor_schedule_message_failed",
            )

            send_sms_via_gmail(
                f"WO #{wo_num} — tenant availability was received, but "
                f"Rachel could not send it to the vendor through AppFolio. "
                f"Manual vendor contact is required.",
                urgent=False,
            )

        return

    # Tenant is responding to appointment options from the vendor
    if current_status != "waiting_for_tenant_confirmation":
        send_sms_via_gmail(
            f"WO #{wo_num} — received a tenant scheduling response while "
            f"Rachel's status was '{current_status}'.\n"
            f"Tenant response: {reply[:250]}",
            urgent=False,
        )
        return

    try:
        proposed_options = json.loads(
            wo.get("proposed_options") or "[]"
        )
    except (json.JSONDecodeError, TypeError):
        proposed_options = []

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=250,
        system=(
            "Classify the tenant's response to proposed maintenance "
            "appointment options. Reply ONLY with valid JSON:\n"
            '{"decision": "CONFIRM", "option_number": 1, '
            '"counter_availability": null}\n'
            "or\n"
            '{"decision": "COUNTER", "option_number": null, '
            '"counter_availability": "tenant availability"}\n'
            "or\n"
            '{"decision": "UNCLEAR", "option_number": null, '
            '"counter_availability": null}\n'
            "CONFIRM means the tenant clearly accepts an option. "
            "COUNTER means the tenant rejects the options or supplies "
            "different days or times. Do not invent an option number."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Proposed options:\n"
                    f"{json.dumps(proposed_options)}\n\n"
                    f"Tenant response:\n{reply}"
                ),
            }
        ],
    )

    raw = (
        response.content[0]
        .text.strip()
        .replace("```json", "")
        .replace("```", "")
    )

    try:
        decision_data = json.loads(raw)
    except json.JSONDecodeError:
        decision_data = {
            "decision": "UNCLEAR",
            "option_number": None,
            "counter_availability": None,
        }

    decision = str(
        decision_data.get("decision") or "UNCLEAR"
    ).upper()

    # Tenant rejected the options and supplied different availability
    if decision == "COUNTER":
        counter_availability = (
            decision_data.get("counter_availability") or reply
        ).strip()

        vendor_message = (
            f"Hello,\n\n"
            f"The resident could not accept the previous appointment "
            f"options for Work Order #{wo_num} and provided the following "
            f"availability:\n\n"
            f"{counter_availability}\n\n"
            f"Please reply with an exact date and arrival time that works. "
            f"If none of these times work, please provide new alternatives."
            f"\n\nThank you,\nRachel\n"
            f"ClientCo Property Management\n555-010-0000"
        )

        sent = send_vendor_appfolio_message(
            wo["wo_url"],
            vendor_message,
        )

        if sent:
            _update_wo(
                wo["wo_num"],
                resident_availability=counter_availability,
                proposed_options=None,
                proposed_date=None,
                proposed_time=None,
                proposed_duration=None,
                proposed_by="tenant",
                waiting_for="vendor",
                status="waiting_for_vendor_confirmation",
            )
        else:
            _update_wo(
                wo["wo_num"],
                waiting_for="manual_vendor_contact",
                status="vendor_schedule_message_failed",
            )

        send_sms_via_gmail(
            f"WO #{wo_num} — tenant proposed different availability. "
            f"{'Sent to vendor.' if sent else 'Vendor message failed.'}",
            urgent=False,
        )
        return

    if decision != "CONFIRM":
        first_name = (
            wo["tenant"].split()[0]
            if wo.get("tenant") and wo["tenant"] != "Unknown"
            else "there"
        )

        _send_appfolio_message(
            wo["wo_url"],
            f"Hi {first_name}, please confirm which appointment option "
            f"works for you, or provide other days and times when you "
            f"are available.",
            wo["tenant"],
            wo_num=wo["wo_num"],
            vendor_name=wo.get("vendor_name", ""),
        )
        return

    option_number = decision_data.get("option_number")

    if option_number is None and len(proposed_options) == 1:
        option_number = 1

    try:
        selected_option = proposed_options[int(option_number) - 1]
    except (TypeError, ValueError, IndexError):
        first_name = (
            wo["tenant"].split()[0]
            if wo.get("tenant") and wo["tenant"] != "Unknown"
            else "there"
        )

        _send_appfolio_message(
            wo["wo_url"],
            f"Hi {first_name}, please tell us which appointment option "
            f"you are confirming.",
            wo["tenant"],
            wo_num=wo["wo_num"],
            vendor_name=wo.get("vendor_name", ""),
        )
        return

    date_str = str(selected_option.get("date") or "").strip()
    start_time = str(selected_option.get("time") or "").strip()
    duration = str(
        selected_option.get("duration") or "1 Hour"
    ).strip()

    # AppFolio needs an exact start time, not a broad time window
    broad_time_words = (
        " after ",
        " before ",
        " between ",
        " anytime",
        "morning",
        "afternoon",
        "evening",
        " to ",
    )

    is_time_window = any(
        word in f" {start_time.lower()} "
        for word in broad_time_words
    )

    if not date_str or not start_time or is_time_window:
        vendor_message = (
            f"Hello,\n\n"
            f"The resident selected {date_str or 'the proposed date'} "
            f"with the time window '{start_time}'. Please confirm the "
            f"exact arrival time so our office can schedule the appointment "
            f"in AppFolio.\n\n"
            f"Thank you,\nRachel\n"
            f"ClientCo Property Management\n555-010-0000"
        )

        sent = send_vendor_appfolio_message(
            wo["wo_url"],
            vendor_message,
        )

        _update_wo(
            wo["wo_num"],
            proposed_date=date_str,
            proposed_time=start_time,
            proposed_duration=duration,
            proposed_by="tenant",
            waiting_for="vendor",
            status=(
                "waiting_for_vendor_confirmation"
                if sent
                else "vendor_schedule_message_failed"
            ),
        )
        return

    schedule_updated = update_wo_schedule(
        wo["wo_url"],
        date_str,
        start_time,
        duration,
    )

    expected_schedule_receipt = (
        "schedule_window_semantically_verified_"
        + ready["window_fingerprint"]
    )
    if schedule_updated != expected_schedule_receipt:
        _update_wo(
            wo["wo_num"],
            waiting_for="manual_schedule_update",
            status="schedule_update_failed",
        )

        send_sms_via_gmail(
            f"WO #{wo_num} — tenant confirmed {date_str} at "
            f"{start_time}, but Rachel could not update the AppFolio "
            f"schedule. Manual update is required.",
            urgent=False,
        )
        return

    vendor_confirmation = (
        f"Hello,\n\n"
        f"The appointment for Work Order #{wo_num} is confirmed for "
        f"{date_str} at {start_time}.\n\n"
        f"Please arrive at the confirmed time. No repair work may begin "
        f"without written authorization from our office.\n\n"
        f"Thank you,\nRachel\n"
        f"Assistant Property Manager\n"
        f"ClientCo Property Management\n"
        f"555-010-0000"
    )

    vendor_confirmed = send_vendor_appfolio_message(
        wo["wo_url"],
        vendor_confirmation,
    )

    first_name = (
        wo["tenant"].split()[0]
        if wo.get("tenant") and wo["tenant"] != "Unknown"
        else "there"
    )

    tenant_confirmed = _send_appfolio_message(
        wo["wo_url"],
        f"Hi {first_name}, your maintenance appointment is confirmed "
        f"for {date_str} at {start_time}.",
        wo["tenant"],
        wo_num=wo["wo_num"],
        vendor_name=wo.get("vendor_name", ""),
    )

    _update_wo(
        wo["wo_num"],
        proposed_date=date_str,
        proposed_time=start_time,
        proposed_duration=duration,
        scheduled_for=f"{date_str} {start_time}",
        proposed_by="confirmed",
        waiting_for=None,
        status=(
            "appointment_scheduled"
            if vendor_confirmed and tenant_confirmed
            else "appointment_scheduled_confirmation_issue"
        ),
    )

    send_sms_via_gmail(
        f"WO #{wo_num} — appointment scheduled for {date_str} at "
        f"{start_time}.\n"
        f"Vendor confirmation: {'sent' if vendor_confirmed else 'FAILED'}\n"
        f"Tenant confirmation: {'sent' if tenant_confirmed else 'FAILED'}",
        urgent=False,
    )

def _deliver_tenant_availability_to_vendor(
    wo: dict, availability: str, *, source_event_key: str
) -> bool:
    """Advance only after the exact vendor submission is verified."""
    state = get_schedule(wo["wo_num"], db_path=DB_PATH)
    if not state:
        state = initialize_schedule(wo["wo_num"], db_path=DB_PATH)
    if state["state"] == AWAITING_TENANT_AVAILABILITY:
        state = capture_tenant_availability(
            wo["wo_num"], availability,
            event_key=source_event_key,
            db_path=DB_PATH,
        )
    if state["state"] != TENANT_AVAILABILITY_RECEIVED:
        return False
    vendor_message = build_vendor_window_request(
        wo["wo_num"], availability
    )
    sent = send_vendor_appfolio_message(
        wo["wo_url"], vendor_message,
        wo_num=wo["wo_num"], tenant=wo.get("tenant", ""),
        vendor_name=wo.get("vendor_name", ""),
    )
    if not sent:
        return False
    receipt = _verified_submission_receipt(
        "tenant-availability-to-vendor", wo["wo_num"], vendor_message
    )
    record_vendor_availability_delivery(
        wo["wo_num"], delivery_receipt=receipt,
        event_key=_schedule_event_key(
            "tenant-availability-delivered", wo["wo_num"], source_event_key
        ),
        db_path=DB_PATH,
    )
    return True


def _deliver_pending_final_confirmations(wo: dict, state: dict) -> bool:
    """Resume only missing post-schedule deliveries from durable receipts."""
    if state.get("state") == SCHEDULED_COMPLETE:
        return True
    if state.get("state") != SCHEDULED_PENDING_CONFIRMATIONS:
        raise ScheduleWorkflowError("final_confirmation_before_schedule")

    window = normalize_two_hour_window(
        date_value=state["window_date"],
        start_value=state["window_start"],
        end_value=state["window_end"],
    )
    wo_num = wo["wo_num"]
    if not state.get("vendor_final_receipt"):
        vendor_message = (
            f"The two-hour arrival window for Work Order #{wo_num} is "
            f"confirmed: {window['display']}. Please arrive within this "
            "window. No repair work may begin without written authorization "
            "from our office."
        )
        vendor_sent = send_vendor_appfolio_message(
            wo["wo_url"], vendor_message,
            wo_num=wo_num, tenant=wo.get("tenant", ""),
            vendor_name=wo.get("vendor_name", ""),
        )
        if vendor_sent:
            state = record_final_confirmation_delivery(
                wo_num, audience="vendor",
                delivery_receipt=_verified_submission_receipt(
                    "vendor-final-window", wo_num, vendor_message
                ),
                event_key=_schedule_event_key(
                    "vendor-final-window", wo_num,
                    state["window_fingerprint"],
                ),
                db_path=DB_PATH,
            )

    if not state.get("tenant_final_receipt"):
        first_name = (
            wo["tenant"].split()[0]
            if wo.get("tenant") and wo["tenant"] != "Unknown" else "there"
        )
        tenant_message = (
            f"Hi {first_name}, your maintenance two-hour arrival window is "
            f"confirmed for {window['display']}."
        )
        tenant_sent = _send_appfolio_message(
            wo["wo_url"], tenant_message, wo["tenant"],
            wo_num=wo_num, vendor_name=wo.get("vendor_name", ""),
        )
        if tenant_sent:
            state = record_final_confirmation_delivery(
                wo_num, audience="tenant",
                delivery_receipt=_verified_submission_receipt(
                    "tenant-final-window", wo_num, tenant_message
                ),
                event_key=_schedule_event_key(
                    "tenant-final-window", wo_num,
                    state["window_fingerprint"],
                ),
                db_path=DB_PATH,
            )

    final_state = get_schedule(wo_num, db_path=DB_PATH) or state
    complete = final_state.get("state") == SCHEDULED_COMPLETE
    _update_wo(
        wo_num,
        proposed_date=final_state["window_date"],
        proposed_time=(
            f"{final_state['window_start']}-{final_state['window_end']}"
        ),
        proposed_duration="2 Hours",
        scheduled_for=(
            f"{final_state['window_date']} "
            f"{final_state['window_start']}-{final_state['window_end']}"
        ),
        proposed_by="confirmed",
        waiting_for=(None if complete else "final_confirmation_delivery"),
        status=(
            "appointment_scheduled"
            if complete else "appointment_scheduled_confirmation_issue"
        ),
    )
    return complete


def handle_tenant_schedule_reply(wo_num: str, reply: str) -> bool:
    """Advance the deterministic two-hour arrival-window workflow."""
    _ensure_lifecycle_columns()
    wo = _get_wo(wo_num)
    if not wo:
        print(f"[Schedule] No work-order record found for WO#{wo_num}")
        return False
    reply = str(reply or "").strip()
    if not reply:
        return bool(send_sms_via_gmail(
            f"WO #{wo_num} — tenant scheduling reply was empty.",
            urgent=False,
        ))

    state = initialize_schedule(wo["wo_num"], db_path=DB_PATH)
    reply_event_key = _schedule_event_key(
        "tenant-schedule-reply", wo["wo_num"], reply
    )

    if state["state"] in {
        SCHEDULED_PENDING_CONFIRMATIONS, SCHEDULED_COMPLETE
    }:
        return _deliver_pending_final_confirmations(wo, state)

    if state["state"] in {
        AWAITING_TENANT_AVAILABILITY, TENANT_AVAILABILITY_RECEIVED
    }:
        try:
            sent = _deliver_tenant_availability_to_vendor(
                wo, reply, source_event_key=reply_event_key
            )
        except ScheduleWorkflowError as error:
            sent = False
            print(f"[Schedule] Availability transition blocked: {error}")
        _update_wo(
            wo["wo_num"], resident_availability=reply,
            proposed_options=None, proposed_by="tenant",
            waiting_for=("vendor" if sent else "manual_vendor_contact"),
            status=(
                "waiting_for_vendor_confirmation"
                if sent else "vendor_schedule_message_failed"
            ),
        )
        send_sms_via_gmail(
            f"WO #{wo_num} — tenant availability received. "
            + (
                "The vendor was asked for one specific two-hour arrival window."
                if sent else
                "Vendor delivery failed; manual vendor contact is required."
            ),
            urgent=False,
        )
        return sent

    if state["state"] == APPFOLIO_SCHEDULE_WRITE_UNVERIFIED:
        return bool(send_sms_via_gmail(
            f"WO #{wo_num} — a prior AppFolio schedule write has ambiguous "
            "completion. Rachel will not retry it automatically; reconcile "
            "the work order before continuing.",
            urgent=True,
        ))

    if state["state"] not in {
        AWAITING_TENANT_CONFIRMATION, READY_TO_SCHEDULE
    }:
        return bool(send_sms_via_gmail(
            f"WO #{wo_num} — tenant scheduling response was blocked from "
            f"state '{state['state']}'.", urgent=False,
        ))

    window = normalize_two_hour_window(
        date_value=state["window_date"],
        start_value=state["window_start"],
        end_value=state["window_end"],
    )
    ready = state
    if state["state"] == AWAITING_TENANT_CONFIRMATION:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=180,
            system=(
                "Classify whether the resident explicitly confirms the exact "
                "two-hour arrival window shown. Reply ONLY with valid JSON: "
                '{"decision":"CONFIRM","counter_availability":null}, '
                '{"decision":"COUNTER","counter_availability":"..."}, or '
                '{"decision":"UNCLEAR","counter_availability":null}. '
                "CONFIRM requires an unambiguous acceptance of this same "
                "window. Do not treat a vague acknowledgement as confirmation "
                "and never invent availability."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Proposed two-hour window: {window['display']}\n\n"
                    f"Resident reply: {reply}"
                ),
            }],
        )
        raw = response.content[0].text.strip().replace(
            "```json", ""
        ).replace("```", "")
        try:
            decision_data = json.loads(raw)
        except json.JSONDecodeError:
            decision_data = {"decision": "UNCLEAR"}
        decision = str(
            decision_data.get("decision") or "UNCLEAR"
        ).upper()

        if decision == "COUNTER":
            counter = str(
                decision_data.get("counter_availability") or reply
            ).strip()
            try:
                record_tenant_counter_availability(
                    wo["wo_num"], counter,
                    event_key=reply_event_key, db_path=DB_PATH,
                )
                sent = _deliver_tenant_availability_to_vendor(
                    wo, counter,
                    source_event_key=_schedule_event_key(
                        "tenant-counter", wo["wo_num"], reply_event_key
                    ),
                )
            except ScheduleWorkflowError as error:
                sent = False
                print(f"[Schedule] Counter transition blocked: {error}")
            _update_wo(
                wo["wo_num"], resident_availability=counter,
                proposed_options=None, proposed_date=None,
                proposed_time=None, proposed_duration=None,
                proposed_by="tenant",
                waiting_for=("vendor" if sent else "manual_vendor_contact"),
                status=(
                    "waiting_for_vendor_confirmation"
                    if sent else "vendor_schedule_message_failed"
                ),
            )
            return sent

        if decision != "CONFIRM":
            first_name = (
                wo["tenant"].split()[0]
                if wo.get("tenant") and wo["tenant"] != "Unknown"
                else "there"
            )
            return bool(_send_appfolio_message(
                wo["wo_url"],
                f"Hi {first_name}, please explicitly confirm whether the "
                f"two-hour arrival window {window['display']} works, or "
                "provide different availability.",
                wo["tenant"],
                wo_num=wo["wo_num"],
                vendor_name=wo.get("vendor_name", ""),
            ))

        try:
            ready = record_tenant_window_confirmation(
                wo["wo_num"],
                confirmed_window_fingerprint=state["window_fingerprint"],
                explicitly_confirmed=True,
                event_key=reply_event_key,
                db_path=DB_PATH,
            )
        except ScheduleWorkflowError as error:
            return bool(send_sms_via_gmail(
                f"WO #{wo_num} — tenant confirmation did not match the "
                f"durable window: {error}.", urgent=False,
            ))

    attempt_id = _schedule_event_key(
        "appfolio-schedule-attempt", wo["wo_num"],
        ready["window_fingerprint"],
    )
    schedule_updated = update_wo_schedule(
        wo["wo_url"], ready["window_date"], ready["window_start"],
        ready["window_end"], "2 Hours",
        wo_num=wo["wo_num"], tenant=wo.get("tenant", ""),
        vendor_name=wo.get("vendor_name", ""),
        write_attempt_id=attempt_id,
    )
    if not schedule_updated:
        post_write_state = get_schedule(wo["wo_num"], db_path=DB_PATH) or {}
        ambiguous_write = (
            post_write_state.get("state")
            == APPFOLIO_SCHEDULE_WRITE_UNVERIFIED
        )
        _update_wo(
            wo["wo_num"],
            waiting_for=(
                "manual_schedule_reconciliation"
                if ambiguous_write else "appfolio_write_enablement"
            ),
            status=(
                "schedule_write_unverified"
                if ambiguous_write else "schedule_write_blocked_before_claim"
            ),
        )
        send_sms_via_gmail(
            (
                f"WO #{wo_num} — AppFolio schedule write did not verify. "
                "Rachel will not retry automatically; manual reconciliation "
                "is required."
                if ambiguous_write else
                f"WO #{wo_num} — the confirmed two-hour window remains "
                "ready, but the AppFolio write was blocked before any "
                "browser mutation."
            ),
            urgent=ambiguous_write,
        )
        return False

    write_receipt = schedule_updated
    scheduled = record_appfolio_schedule_verified(
        wo["wo_num"],
        window_fingerprint=ready["window_fingerprint"],
        appfolio_write_receipt=write_receipt,
        event_key=_schedule_event_key(
            "appfolio-schedule-verified", wo["wo_num"], attempt_id
        ),
        db_path=DB_PATH,
    )

    return _deliver_pending_final_confirmations(wo, scheduled)


# ── 4. Post-work satisfaction check ────────────────────────────────────────

def send_satisfaction_check(wo_num: str) -> bool:
    """Manager texted '1540 done' -> ask the tenant if all is resolved."""
    wo = _get_wo(wo_num)
    if not wo:
        return bool(send_sms_via_gmail(
            f"Could not find WO #{wo_num} in my records to send a "
            f"satisfaction check.", urgent=False))
    msg = (f"Hi {wo['tenant'].split()[0]}, this is Rachel from ClientCo "
           f"Property Management. Our records show the recent maintenance "
           f"work at your unit has been completed. Was everything resolved "
           f"to your satisfaction? If anything still needs attention, "
           f"just reply here and we will follow up right away.")
    ok = _send_appfolio_message(
        wo["wo_url"], msg, wo["tenant"],
        wo_num=wo["wo_num"], vendor_name=wo.get("vendor_name", ""),
    )
    _update_wo(
        wo["wo_num"],
        status=(
            "satisfaction_check_sent"
            if ok else "satisfaction_check_delivery_failed"
        ),
    )
    send_sms_via_gmail(
        f"WO #{wo_num} — satisfaction check "
        f"{'sent to' if ok else 'FAILED for'} {wo['tenant']}", urgent=False)
    return bool(ok)


def handle_tenant_satisfaction_reply(wo_num: str, reply: str) -> bool:
    """Tenant answered the satisfaction check."""
    wo = _get_wo(wo_num)
    if not wo:
        return False
    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=10,
        messages=[{"role": "user", "content":
                   f"Tenant reply to 'was the repair resolved?': "
                   f"\"{reply}\"\nAnswer ONE word: SATISFIED or UNSATISFIED"}])
    verdict = response.content[0].text.strip().upper()

    if "UNSAT" in verdict:
        tenant_acknowledged = _send_appfolio_message(
            wo["wo_url"],
            "Thank you for letting us know — I am escalating this right "
            "away and someone will follow up with you shortly.",
            wo["tenant"], wo_num=wo["wo_num"],
            vendor_name=wo.get("vendor_name", ""))
        if not tenant_acknowledged:
            _update_wo(
                wo["wo_num"], status="satisfaction_response_delivery_failed"
            )
            return False
        management_alerted = send_sms_via_gmail(
            f"⚠️ WO #{wo_num} — tenant NOT satisfied after completion.\n"
            f"{wo['tenant']} | {wo['address']}\n"
            f"Their reply: {_truncate_at_word_boundary(reply, 150)}\n"
            f"Recommend reopening / re-dispatching.", urgent=False)
        _update_wo(
            wo["wo_num"],
            status=(
                "reopened"
                if management_alerted
                else "satisfaction_escalation_delivery_failed"
            ),
        )
        return bool(management_alerted)
    else:
        tenant_acknowledged = _send_appfolio_message(
            wo["wo_url"],
            "Wonderful — glad everything is taken care of! Don't hesitate "
            "to reach out if you need anything else.",
            wo["tenant"], wo_num=wo["wo_num"],
            vendor_name=wo.get("vendor_name", ""))
        _update_wo(
            wo["wo_num"],
            status=(
                "closed_satisfied"
                if tenant_acknowledged
                else "satisfaction_response_delivery_failed"
            ),
        )
        if not tenant_acknowledged:
            return False
        print(f"[Satisfaction] WO#{wo_num} closed satisfied")
        return True


if __name__ == "__main__":
    raise SystemExit(
        "Standalone vendor follow-up execution is disabled; main.py owns "
        "the daily production schedule."
    )
