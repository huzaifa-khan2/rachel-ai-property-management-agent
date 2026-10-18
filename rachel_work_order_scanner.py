"""
Rachel Work Order Scanner

Daily AppFolio discovery layer for:
- NEW work orders
- WAITING work orders
- Work Order pagination

This scanner is read-only until a work order is deliberately passed
to an approved Rachel workflow.
"""

import hashlib
import json
import os
import re
import time
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from rachel_appfolio_browser import (
    get_appfolio_browser_manager,
    get_browser_profile_dir,
)
from rachel_appfolio_ui import (
    AppFolioReadAdapter,
    AppFolioUIContractError,
    WorkOrderDetailPage,
    WorkOrderListPage,
)
from rachel_foundation_health import assert_zero_write_metrics
from rachel_wo_triage import (
    APPFOLIO_URL,
    detect_emergency_evidence,
    get_db,
    login_appfolio,
    send_sms_via_gmail,
)


WO_LIST_URL = (
    f"{APPFOLIO_URL}/maintenance/service_requests/work_orders"
)

HEADLESS = os.getenv(
    "RACHEL_HEADLESS",
    "true" if os.getenv("RENDER") else "false",
).strip().lower() in ("1", "true", "yes", "on")


def is_emergency_candidate(details: dict) -> bool:
    """Identify only high-confidence emergency evidence in full details."""
    return bool(detect_emergency_evidence(details)["is_emergency"])


MATERIAL_DETAIL_FIELDS = (
    "live_status",
    "source_type",
    "tenant",
    "address",
    "service_request_description",
    "job_desc",
    "issue_type",
    "issue_details",
    "permission_to_enter",
    "created_by",
    "priority",
    "resident_availability",
    "scheduling",
    "estimates",
    "actions_log",
)


def _snapshot_fingerprint(payload: dict) -> str:
    """Return a stable SHA-256 fingerprint for material Work Order data."""
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        default=str,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()


def list_record_fingerprint(record: dict) -> str:
    """
    Fingerprint the information visible without opening the Work Order.

    A changed list fingerprint causes Rachel to reopen the detail page.
    """
    return _snapshot_fingerprint(
        record.get("list_fields") or {
            "wo_num": record.get("wo_num", ""),
            "status": record.get("status", ""),
            "wo_url": record.get("wo_url", ""),
        }
    )


def detail_record_fingerprint(details: dict) -> str:
    """Fingerprint every material field extracted from the detail page."""
    return _snapshot_fingerprint(
        {
            field: details.get(field, "")
            for field in MATERIAL_DETAIL_FIELDS
        }
    )


def load_scanner_states() -> dict:
    """Load Rachel's previous Work Order snapshots by Work Order number."""
    conn = get_db()

    try:
        rows = conn.execute(
            """
            SELECT *
            FROM wo_scanner_state
            """
        ).fetchall()
    finally:
        conn.close()

    return {
        str(row["wo_num"]): dict(row)
        for row in rows
    }


def init_scanner_state_table() -> None:
    """
    Create persistent Work Order scanner tracking.

    This prevents Rachel from repeatedly asking Manager about the same
    Work Order every morning.
    """
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wo_scanner_state (
            wo_num TEXT PRIMARY KEY,
            wo_url TEXT,
            appfolio_status TEXT,
            classification TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            manager_asked_at TEXT,
            waiting_for_manager INTEGER DEFAULT 0,
            manager_instruction TEXT,
            instruction_received_at TEXT,
            last_actions_log TEXT
        )
        """
    )

    # Add fields introduced after the original scanner table was created.
    required_columns = {
        "source_type": "TEXT",
        "tenant": "TEXT",
        "address": "TEXT",
        "service_request_description": "TEXT",
        "job_desc": "TEXT",
        "issue_type": "TEXT",
        "issue_details": "TEXT",
        "permission_to_enter": "TEXT",
        "created_by": "TEXT",
        "priority": "TEXT",
        "resident_availability": "TEXT",
        "scheduling": "TEXT",
        "estimates": "TEXT",
        "list_fingerprint": "TEXT",
        "detail_fingerprint": "TEXT",
        "last_detail_scanned_at": "TEXT",
        "list_fields_json": "TEXT",
        "baseline_generation": "TEXT",
    }

    existing_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(wo_scanner_state)"
        ).fetchall()
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE wo_scanner_state "
                f"ADD COLUMN {column_name} {column_type}"
            )

    conn.commit()
    conn.close()

    print("[ScannerDB] wo_scanner_state table ready.")

def extract_list_fields(wo_num: str, wo_url: str, row_lines: list[str]) -> dict:
    """Extract stable, list-visible columns without visiting WO details."""
    normalized_lines = [re.sub(r"\s+", " ", line).strip() for line in row_lines]
    stable_lines = [
        line for line in normalized_lines
        if not re.search(r"\b\d+\s+(?:minute|hour|day)s?\s+ago\b", line, re.I)
    ]
    upper_lines = [line.upper() for line in normalized_lines]

    def value_after(label: str) -> str:
        try:
            index = upper_lines.index(label.upper())
        except ValueError:
            return ""
        return normalized_lines[index + 1] if index + 1 < len(normalized_lines) else ""

    return {
        "wo_num": wo_num,
        "wo_url": wo_url,
        "status": upper_lines[1] if len(upper_lines) > 1 else "UNKNOWN",
        "source_type": next((line for line in upper_lines if line in {"RESIDENT", "INTERNAL"}), ""),
        "priority": next((line for line in upper_lines if line in {"EMERGENCY", "URGENT"}), ""),
        "vendor_assignment": value_after("Vendor"),
        "property": value_after("Property"),
        "unit": value_after("Unit"),
        "visible_columns": stable_lines,
    }


def scan_current_page(page) -> list:
    """Read every Work Order card displayed on the current page."""
    work_orders = []
    work_order_page = WorkOrderListPage(page)
    links = work_order_page.links()

    print(
        f"[Scanner] Work Order links found: "
        f"{links.count()}"
    )

    for index in range(links.count()):
        link = links.nth(index)
        wo_num = link.inner_text().strip()

        if not re.fullmatch(r"\d+-\d+", wo_num):
            continue

        row = work_order_page.row_for_link(link)

        if row.count() == 0:
            print(
                f"[Scanner] Could not locate card for "
                f"WO #{wo_num}"
            )
            continue

        row_lines = [
            line.strip()
            for line in row.inner_text().splitlines()
            if line.strip()
        ]

        wo_url = link.get_attribute("href") or ""

        list_fields = extract_list_fields(wo_num, wo_url, row_lines)
        record = {
            "wo_num": wo_num,
            "status": list_fields["status"],
            "row_lines": row_lines,
            "wo_url": wo_url,
            "list_fields": list_fields,
        }

        work_orders.append(record)

        print(
            f"[Scanner] WO #{record['wo_num']} | "
            f"{record['status']} | "
            f"{' | '.join(row_lines[2:8])}"
        )

    return work_orders



def scan_all_pages(page, metrics: Optional[dict] = None) -> list:
    """Scan every page in AppFolio's open Work Orders list."""
    work_order_page = WorkOrderListPage(page)
    control_texts = work_order_page.pagination_texts()

    page_numbers = sorted({
        int(text.strip())
        for text in control_texts
        if re.fullmatch(r"\d+", text.strip())
    })

    total_pages = max(page_numbers, default=1)

    print(
        f"[Scanner] Pagination detected: "
        f"{page_numbers or [1]}"
    )

    all_work_orders = []
    seen_wo_numbers = set()

    for page_number in range(1, total_pages + 1):
        if page_number > 1:
            first_wo_before = (
                work_order_page.links()
                .first
                .inner_text()
                .strip()
            )

            page_button = work_order_page.page_button(page_number)

            if page_button.count() == 0:
                print(
                    f"[Scanner] Page {page_number} button "
                    "was not found — stopping pagination."
                )
                break

            print(f"\n[Scanner] Opening page {page_number}...")

            page_button.first.scroll_into_view_if_needed()
            page_button.first.click()

            try:
                work_order_page.wait_for_different_first_work_order(
                    first_wo_before, timeout=20000
                )
            except Exception:
                print(
                    f"[Scanner] Page {page_number} did not "
                    "display different Work Orders."
                )
                break

            time.sleep(2)

        print(
            f"\n[Scanner] Scanning page "
            f"{page_number} of {total_pages}"
        )

        page_work_orders = scan_current_page(page)
        if metrics is not None:
            metrics["list_pages_scanned"] = (
                metrics.get("list_pages_scanned", 0) + 1
            )

        for record in page_work_orders:
            if record["wo_num"] in seen_wo_numbers:
                continue

            seen_wo_numbers.add(record["wo_num"])
            all_work_orders.append(record)

    new_work_orders = [
        record
        for record in all_work_orders
        if record["status"] == "NEW"
    ]

    waiting_work_orders = [
        record
        for record in all_work_orders
        if record["status"] == "WAITING"
    ]

    print("\n" + "=" * 60)
    print("[Scanner] ALL-PAGE READ-ONLY SUMMARY")
    print(f"[Scanner] Total Work Orders: {len(all_work_orders)}")
    print(f"[Scanner] NEW: {len(new_work_orders)}")
    print(f"[Scanner] WAITING: {len(waiting_work_orders)}")

    for record in new_work_orders:
        print(f"[Scanner] NEW candidate: WO #{record['wo_num']}")

    for record in waiting_work_orders:
        print(f"[Scanner] WAITING candidate: WO #{record['wo_num']}")

    return all_work_orders



def extract_work_order_core_fields(
    body_text: str,
    wo_num: str,
) -> dict:
    """
    Extract the core Work Order fields needed by Rachel's shared
    tenant-triage workflow.
    """
    lines = [
        line.strip()
        for line in body_text.splitlines()
        if line.strip()
    ]

    def exact_index(label: str, start: int = 0) -> int:
        label_lower = label.lower()

        for index in range(start, len(lines)):
            if lines[index].lower() == label_lower:
                return index

        return -1

    def value_after(label: str, start: int = 0) -> str:
        index = exact_index(label, start)

        if index != -1 and index + 1 < len(lines):
            return lines[index + 1]

        return ""

    service_request_index = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(
                r"Service Request\s*#",
                line,
                re.IGNORECASE,
            )
        ),
        -1,
    )

    source_type = ""

    if service_request_index != -1:
        for line in lines[
            service_request_index + 1:
            service_request_index + 6
        ]:
            if line.upper() in ("RESIDENT", "INTERNAL"):
                source_type = line.upper()
                break

    description_index = exact_index(
        "Description",
        max(service_request_index, 0),
    )

    service_request_description = ""

    if (
        description_index != -1
        and description_index + 1 < len(lines)
    ):
        service_request_description = lines[
            description_index + 1
        ]

    property_index = exact_index("PROPERTY")

    property_unit = (
        lines[property_index + 1]
        if property_index != -1
        and property_index + 1 < len(lines)
        else ""
    )

    resident_index = exact_index(
        "RESIDENT",
        property_index + 1
        if property_index != -1
        else 0,
    )

    tenant = (
        lines[resident_index + 1]
        if resident_index != -1
        and resident_index + 1 < len(lines)
        else ""
    )

    job_description = value_after("Job Description")

    issue_details_start = exact_index("Issue Details")
    vendor_instructions_start = exact_index(
        "Vendor Instructions",
        issue_details_start + 1
        if issue_details_start != -1
        else 0,
    )

    issue_details_lines = []

    if issue_details_start != -1:
        issue_details_end = (
            vendor_instructions_start
            if vendor_instructions_start != -1
            else len(lines)
        )

        issue_details_lines = lines[
            issue_details_start + 1:
            issue_details_end
        ]

    issue_type = ""

    for index, line in enumerate(issue_details_lines):
        if (
            line.lower() == "issue"
            and index + 1 < len(issue_details_lines)
        ):
            issue_type = issue_details_lines[index + 1]
            break

    permission_match = re.search(
        r"Permission to enter:\s*(.+)",
        body_text,
        re.IGNORECASE,
    )

    created_by_match = re.search(
        r"Created by:\s*(.+)",
        body_text,
        re.IGNORECASE,
    )

    priority_match = re.search(
        r"Priority:\s*(.+)",
        body_text,
        re.IGNORECASE,
    )

    return {
        "source_type": source_type,
        "tenant": tenant,
        "address": property_unit,
        "service_request_description": (
            service_request_description
        ),
        "job_desc": job_description,
        "issue_type": issue_type,
        "issue_details": "\n".join(
            issue_details_lines
        ).strip(),
        "permission_to_enter": (
            permission_match.group(1).strip()
            if permission_match
            else ""
        ),
        "created_by": (
            created_by_match.group(1).strip()
            if created_by_match
            else ""
        ),
        "priority": (
            priority_match.group(1).strip()
            if priority_match
            else ""
        ),
    }

def inspect_work_order_detail(page, record: dict) -> dict:
    """
    Open one Work Order and read its current AppFolio details.

    Read-only: this function does not click Actions, send messages,
    assign vendors, schedule work, or modify the Work Order.
    """
    wo_num = record["wo_num"]
    wo_url = record["wo_url"]
    target_url = (
        wo_url
        if str(wo_url).startswith(("http://", "https://"))
        else APPFOLIO_URL + str(wo_url)
    )

    print(f"\n[Detail] Opening WO #{wo_num}...")

    AppFolioReadAdapter(page, "work_order_detail").navigate_read(
        target_url,
        route_pattern=(
            "/maintenance/service_requests/:request/work_orders/:work_order"
        ),
        timeout=60000,
        attempts=2,
    )

    time.sleep(4)

    detail_page = WorkOrderDetailPage(page)
    body_text = detail_page.body_text()
    if not detail_page.has_semantic_identity(wo_num, body_text=body_text):
        raise AppFolioUIContractError(
            "work_order_detail", "page_identity",
            "visible_work_order_number_mismatch",
        )

    status_match = re.search(
        rf"Work Order\s*#{re.escape(wo_num)}\s+([^\n]+)",
        body_text,
        re.IGNORECASE,
    )

    live_status = (
        status_match.group(1).strip().upper()
        if status_match
        else "UNKNOWN"
    )

    def section_text(start_heading: str, end_headings: tuple) -> str:
        start_index = body_text.lower().find(
            start_heading.lower()
        )

        if start_index == -1:
            return ""

        section_start = start_index + len(start_heading)
        section_end = len(body_text)

        for heading in end_headings:
            possible_end = body_text.lower().find(
                heading.lower(),
                section_start,
            )

            if possible_end != -1:
                section_end = min(section_end, possible_end)

        return body_text[
            section_start:section_end
        ].strip()

    actions_log = section_text(
        "Actions Log",
        (
            "Next Follow-up Date",
            "Assignee",
            "Labor",
            "Purchase Orders",
        ),
    )

    resident_availability = section_text(
        "Resident Availability",
        (
            "Actions Log",
            "Next Follow-up Date",
            "Assignee",
        ),
    )

    estimates = section_text(
        "Estimates",
        (
            "Scheduling",
            "Resident Availability",
            "Actions Log",
        ),
    )

    scheduling = section_text(
        "Scheduling",
        (
            "Resident Availability",
            "Actions Log",
            "Next Follow-up Date",
        ),
    )

    core_fields = extract_work_order_core_fields(
        body_text,
        wo_num,
    )

    details = {
        **record,
        **core_fields,
        "live_status": live_status,
        "actions_log": actions_log,
        "resident_availability": resident_availability,
        "estimates": estimates,
        "scheduling": scheduling,
        "page_url": page.url,
    }

    print(
        f"[Detail] WO #{wo_num} live status: "
        f"{live_status}"
    )

    print(
        f"[Detail] Core fields: "
        f"{core_fields.get('source_type') or 'UNKNOWN'} | "
        f"{core_fields.get('tenant') or 'No resident'} | "
        f"{core_fields.get('address') or 'No property'} | "
        f"{core_fields.get('issue_type') or 'No issue type'}"
    )

    print(
        "[Detail] Job description: "
        f"{core_fields.get('job_desc') or 'Not found'}"
    )

    print(
        "[Detail] Resident availability: "
        f"{resident_availability[:400] or 'Not found'}"
    )

    print(
        "[Detail] Scheduling: "
        f"{scheduling[:400] or 'Not found'}"
    )

    print(
        "[Detail] Estimates: "
        f"{estimates[:400] or 'Not found'}"
    )

    print(
        "[Detail] Actions Log:\n"
        f"{actions_log[:1500] or 'Not found'}"
    )

    return details





def reconcile_tracked_work_order_statuses(
    work_orders: list,
) -> int:
    """
    Update previously tracked Work Orders from the current AppFolio list.

    When a Work Order is no longer NEW or WAITING, remove it from the
    initial-triage/review queue while preserving its historical records.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    updated_count = 0

    for record in work_orders:
        wo_num = record.get("wo_num", "")
        status = record.get("status", "UNKNOWN").upper()

        cursor = conn.execute(
            """
            UPDATE wo_scanner_state
            SET wo_url = ?,
                appfolio_status = ?,
                last_seen_at = ?,
                classification = CASE
                    WHEN ? NOT IN ('NEW', 'WAITING')
                    THEN 'not_actionable_for_initial_triage'
                    ELSE classification
                END,
                waiting_for_manager = CASE
                    WHEN ? NOT IN ('NEW', 'WAITING')
                    THEN 0
                    ELSE waiting_for_manager
                END
            WHERE wo_num = ?
            """,
            (
                record.get("wo_url", ""),
                status,
                now,
                status,
                status,
                wo_num,
            ),
        )

        if cursor.rowcount:
            updated_count += 1

    conn.commit()
    conn.close()

    print(
        f"[ScannerDB] Reconciled {updated_count} "
        "previously tracked Work Order(s)."
    )

    return updated_count

def build_manager_review_message(details: dict) -> str:
    """
    Build a concise management-review request for Manager.

    Returns an empty string when the classification does not require
    an immediate management decision.
    """
    classification = details.get("classification", "")
    wo_num = details.get("wo_num", "Unknown")
    status = details.get("live_status", details.get("status", "UNKNOWN"))
    wo_url = details.get("page_url") or details.get("wo_url", "")

    if classification == "new_internal_needs_manager_direction":
        reason = (
            "This Work Order is still marked NEW and was created as an "
            "INTERNAL Work Order. Rachel will not contact a resident or "
            "take further action until you provide direction."
        )

    elif classification == "new_touched_waiting_for_manager_context":
        reason = (
            "This Work Order is still marked NEW, but its Actions Log "
            "shows prior staff activity. Rachel needs to know what has "
            "already occurred before beginning resident triage."
        )

    elif classification == "new_needs_review":
        reason = (
            "This Work Order is marked NEW, but Rachel could not safely "
            "determine whether it should enter normal resident triage."
        )

    else:
        return ""

    row_summary = "\n".join(
        str(line).strip()
        for line in details.get("row_lines", [])[:10]
        if str(line).strip()
    )

    actions_excerpt = details.get("actions_log", "").strip()[:800]

    return (
        f"WORK ORDER REVIEW NEEDED\n\n"
        f"WO #{wo_num}\n"
        f"Current AppFolio status: {status}\n\n"
        f"{reason}\n\n"
        f"WORK ORDER SUMMARY:\n"
        f"{row_summary or 'No summary was extracted.'}\n\n"
        f"RECENT ACTIONS LOG:\n"
        f"{actions_excerpt or 'No Actions Log activity was found.'}\n\n"
        f"Manager, please reply with one of the following:\n"
        f"PROCEED — Rachel should begin the normal tenant workflow.\n"
        f"HOLD — Rachel should wait and take no action.\n"
        f"HANDLED — No further Rachel action is needed.\n"
        f"OTHER: [your instructions]\n\n"
        f"AppFolio: {wo_url}"
    )


def request_manager_review(
    details: dict,
    scanner_state: dict,
) -> dict:
    """
    Contact Manager and Owner once when management direction is required.

    The Work Order is marked as waiting for Manager only after the
    notification function confirms that the email was successfully sent.
    """
    wo_num = details.get("wo_num", "Unknown")

    message = build_manager_review_message(details)

    if not message:
        return scanner_state

    if scanner_state.get("manager_instruction"):
        print(
            f"[ManagerReview] WO #{wo_num} already has "
            "Manager instructions. No duplicate request sent."
        )
        return scanner_state

    if scanner_state.get("waiting_for_manager"):
        print(
            f"[ManagerReview] WO #{wo_num} is already waiting "
            "for Manager. No duplicate request sent."
        )
        return scanner_state

    sent = send_sms_via_gmail(
        message,
        urgent=False,
        subject=f"Rachel Review Needed — WO #{wo_num}",
    )

    if not sent:
        print(
            f"[ManagerReview] WO #{wo_num} notification was "
            "blocked or failed. It was not marked as sent."
        )
        return scanner_state

    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()

    conn.execute(
        """
        UPDATE wo_scanner_state
        SET manager_asked_at = ?,
            waiting_for_manager = 1
        WHERE wo_num = ?
        """,
        (
            now,
            wo_num,
        ),
    )

    conn.commit()

    saved = conn.execute(
        """
        SELECT *
        FROM wo_scanner_state
        WHERE wo_num = ?
        """,
        (wo_num,),
    ).fetchone()

    updated_state = dict(saved) if saved else scanner_state

    conn.close()

    print(
        f"[ManagerReview] WO #{wo_num} review request sent "
        "and recorded."
    )

    return updated_state

def save_scanner_observation(details: dict) -> dict:
    """
    Save the latest full Work Order snapshot without erasing management data.
    """
    now = datetime.now(timezone.utc).isoformat()
    wo_num = details["wo_num"]

    conn = get_db()

    existing = conn.execute(
        """
        SELECT *
        FROM wo_scanner_state
        WHERE wo_num = ?
        """,
        (wo_num,),
    ).fetchone()

    first_seen_at = (
        existing["first_seen_at"]
        if existing and existing["first_seen_at"]
        else now
    )

    conn.execute(
        """
        INSERT INTO wo_scanner_state (
            wo_num,
            wo_url,
            appfolio_status,
            classification,
            first_seen_at,
            last_seen_at,
            last_actions_log,
            source_type,
            tenant,
            address,
            service_request_description,
            job_desc,
            issue_type,
            issue_details,
            permission_to_enter,
            created_by,
            priority,
            resident_availability,
            scheduling,
            estimates,
            list_fingerprint,
            detail_fingerprint,
            last_detail_scanned_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(wo_num) DO UPDATE SET
            wo_url = excluded.wo_url,
            appfolio_status = excluded.appfolio_status,
            classification = excluded.classification,
            last_seen_at = excluded.last_seen_at,
            last_actions_log = excluded.last_actions_log,
            source_type = COALESCE(
                NULLIF(excluded.source_type, ''),
                wo_scanner_state.source_type
            ),
            tenant = COALESCE(
                NULLIF(excluded.tenant, ''),
                wo_scanner_state.tenant
            ),
            address = COALESCE(
                NULLIF(excluded.address, ''),
                wo_scanner_state.address
            ),
            service_request_description = COALESCE(
                NULLIF(excluded.service_request_description, ''),
                wo_scanner_state.service_request_description
            ),
            job_desc = COALESCE(
                NULLIF(excluded.job_desc, ''),
                wo_scanner_state.job_desc
            ),
            issue_type = COALESCE(
                NULLIF(excluded.issue_type, ''),
                wo_scanner_state.issue_type
            ),
            issue_details = COALESCE(
                NULLIF(excluded.issue_details, ''),
                wo_scanner_state.issue_details
            ),
            permission_to_enter = COALESCE(
                NULLIF(excluded.permission_to_enter, ''),
                wo_scanner_state.permission_to_enter
            ),
            created_by = COALESCE(
                NULLIF(excluded.created_by, ''),
                wo_scanner_state.created_by
            ),
            priority = COALESCE(
                NULLIF(excluded.priority, ''),
                wo_scanner_state.priority
            ),
            resident_availability = COALESCE(
                NULLIF(excluded.resident_availability, ''),
                wo_scanner_state.resident_availability
            ),
            scheduling = COALESCE(
                NULLIF(excluded.scheduling, ''),
                wo_scanner_state.scheduling
            ),
            estimates = COALESCE(
                NULLIF(excluded.estimates, ''),
                wo_scanner_state.estimates
            ),
            list_fingerprint = excluded.list_fingerprint,
            detail_fingerprint = excluded.detail_fingerprint,
            last_detail_scanned_at = excluded.last_detail_scanned_at
        """,
        (
            wo_num,
            details.get("page_url")
            or details.get("wo_url", ""),
            details.get("live_status")
            or details.get("status", "UNKNOWN"),
            details.get("classification", ""),
            first_seen_at,
            now,
            details.get("actions_log", ""),
            details.get("source_type", ""),
            details.get("tenant", ""),
            details.get("address", ""),
            details.get("service_request_description", ""),
            details.get("job_desc", ""),
            details.get("issue_type", ""),
            details.get("issue_details", ""),
            details.get("permission_to_enter", ""),
            details.get("created_by", ""),
            details.get("priority", ""),
            details.get("resident_availability", ""),
            details.get("scheduling", ""),
            details.get("estimates", ""),
            details.get("list_fingerprint", ""),
            details.get("detail_fingerprint", ""),
            details.get("last_detail_scanned_at", now),
        ),
    )

    conn.commit()

    saved = conn.execute(
        """
        SELECT *
        FROM wo_scanner_state
        WHERE wo_num = ?
        """,
        (wo_num,),
    ).fetchone()

    result = dict(saved) if saved else {}
    conn.close()

    print(
        f"[ScannerDB] Saved WO #{wo_num} | "
        f"{details.get('classification', 'unclassified')}"
    )

    return result


def record_manager_review_instruction(
    wo_num: str,
    reply_text: str,
) -> dict:
    """
    Record Manager's management instruction for a scanner review request.

    Accepted replies:
    - PROCEED
    - HOLD
    - HANDLED
    - OTHER: specific instructions
    """
    clean_reply = (reply_text or "").strip()

    if not clean_reply:
        print(
            f"[ManagerReview] Empty instruction received "
            f"for WO #{wo_num}."
        )
        return {}

    first_line = next(
        (
            line.strip()
            for line in clean_reply.splitlines()
            if line.strip() and not line.strip().startswith(">")
        ),
        "",
    )

    instruction_upper = first_line.upper()

    if instruction_upper.startswith("PROCEED_WITH:"):
        instruction = first_line
        classification = "manager_authorized_custom_triage"

    elif instruction_upper.startswith("PROCEED"):
        instruction = "PROCEED"
        classification = "manager_authorized_tenant_triage"

    elif instruction_upper.startswith("HOLD"):
        instruction = "HOLD"
        classification = "manager_hold"

    elif instruction_upper.startswith("HANDLED"):
        instruction = "HANDLED"
        classification = "manager_handled"

    elif instruction_upper.startswith("OTHER:"):
        instruction = first_line
        classification = "manager_other_instruction"

    else:
        print(
            f"[ManagerReview] Unrecognized instruction for "
            f"WO #{wo_num}: {first_line}"
        )
        return {}

    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()

    existing = conn.execute(
        """
        SELECT *
        FROM wo_scanner_state
        WHERE wo_num = ?
        """,
        (wo_num,),
    ).fetchone()

    if not existing:
        conn.close()
        print(
            f"[ManagerReview] WO #{wo_num} is not in "
            "wo_scanner_state."
        )
        return {}

    conn.execute(
        """
        UPDATE wo_scanner_state
        SET waiting_for_manager = 0,
            manager_instruction = ?,
            instruction_received_at = ?,
            classification = ?
        WHERE wo_num = ?
        """,
        (
            instruction,
            now,
            classification,
            wo_num,
        ),
    )

    conn.commit()

    saved = conn.execute(
        """
        SELECT *
        FROM wo_scanner_state
        WHERE wo_num = ?
        """,
        (wo_num,),
    ).fetchone()

    result = dict(saved) if saved else {}

    conn.close()

    print(
        f"[ManagerReview] Recorded {instruction} "
        f"for WO #{wo_num}."
    )

    return result

def classify_candidate(details: dict) -> str:
    """
    Classify a NEW or WAITING Work Order using its live status,
    source, and Actions Log.

    Read-only: this function does not contact anyone or modify AppFolio.
    """
    live_status = details.get(
        "live_status",
        details.get("status", "UNKNOWN"),
    ).upper()

    actions_log = details.get(
        "actions_log",
        "",
    ).lower()

    row_lines = [
        str(value).strip().upper()
        for value in details.get("row_lines", [])
    ]

    is_internal = "INTERNAL" in row_lines
    is_resident = "RESIDENT" in row_lines

    staff_activity_terms = (
        "edited –",
        "assigned",
        "scheduled for",
        "work order link emailed",
        "ready to bill",
        "waiting –",
        "estimate",
    )

    has_staff_activity = any(
        term in actions_log
        for term in staff_activity_terms
    )

    if live_status == "NEW":
        if is_internal:
            return "new_internal_needs_manager_direction"

        if has_staff_activity:
            return "new_touched_waiting_for_manager_context"

        if is_resident or "submitted online" in actions_log:
            return "new_untouched_resident"

        return "new_needs_review"

    if live_status == "WAITING":
        return "waiting_needs_review"

    return "not_actionable_for_initial_triage"

def _legacy_full_scan_logged_in_page(page) -> dict:
    """
    Review every open Work Order once, then revisit only active-triage
    or materially changed Work Orders during later scanner cycles.
    """
    raise RuntimeError(
        "Legacy full-detail crawling is disabled; use reconcile_logged_in_page."
    )


def save_list_observation(
    record: dict,
    *,
    fingerprint: str,
    baseline_generation: str,
) -> dict:
    """Persist the cheapest durable WO baseline without erasing detail memory."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO wo_scanner_state (
                wo_num, wo_url, appfolio_status, first_seen_at, last_seen_at,
                list_fingerprint, list_fields_json, baseline_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wo_num) DO UPDATE SET
                wo_url = excluded.wo_url,
                appfolio_status = excluded.appfolio_status,
                last_seen_at = excluded.last_seen_at,
                list_fingerprint = excluded.list_fingerprint,
                list_fields_json = excluded.list_fields_json,
                baseline_generation = excluded.baseline_generation
            """,
            (
                record.get("wo_num", ""),
                record.get("wo_url", ""),
                record.get("status", "UNKNOWN"),
                now,
                now,
                fingerprint,
                json.dumps(record.get("list_fields") or {}, sort_keys=True),
                baseline_generation,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM wo_scanner_state WHERE wo_num = ?",
            (record.get("wo_num", ""),),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def reconcile_logged_in_page(
    page, *, navigation_metrics_provider=None
) -> dict:
    """Refresh durable WO observations without invoking any workflow."""
    started = time.monotonic()
    navigation_start = None
    if navigation_metrics_provider is not None:
        try:
            navigation_start = int(
                navigation_metrics_provider().get("navigations", 0)
            )
        except Exception:
            navigation_start = None

    def finalize_navigation_metric() -> None:
        if navigation_start is None:
            return
        try:
            navigation_end = int(
                navigation_metrics_provider().get("navigations", 0)
            )
            metrics[
                "appfolio_navigations_attributable_to_reconciliation"
            ] = max(0, navigation_end - navigation_start)
        except Exception:
            # Retain the explicit-navigation fallback already accumulated.
            pass

    metrics = {
        "list_pages_scanned": 0,
        "work_orders_listed": 0,
        "targeted_detail_opens": 0,
        "target_reasons": {},
        "unchanged_work_orders_skipped": 0,
        "detail_failures": 0,
        # One explicit list navigation is always required. Each targeted
        # detail attempt adds exactly one more explicit navigation.
        "appfolio_navigations_attributable_to_reconciliation": 1,
        "workflow_actions_attempted": 0,
        "duration": 0.0,
    }
    AppFolioReadAdapter(page, "work_order_reconciliation").navigate_read(
        WO_LIST_URL,
        route_pattern="/maintenance/service_requests/work_orders",
        timeout=60000,
        attempts=2,
    )
    time.sleep(4)
    work_orders = scan_all_pages(page, metrics=metrics)
    metrics["work_orders_listed"] = len(work_orders)
    previous_states = load_scanner_states()
    baseline_generation = datetime.now(timezone.utc).isoformat()
    baseline_usable = bool(previous_states) and any(
        state.get("list_fingerprint") for state in previous_states.values()
    )
    if baseline_usable:
        print(
            f"[Reconciliation] Durable baseline restored records={len(previous_states)}"
        )
    else:
        print("[Reconciliation] Baseline creation required reason=no_usable_baseline")

    candidates = []
    unchanged = 0
    current_wo_numbers = {
        str(record.get("wo_num") or "") for record in work_orders
    }
    for record in work_orders:
        fingerprint = list_record_fingerprint(record)
        previous = previous_states.get(str(record.get("wo_num") or "")) or {}
        previous_fingerprint = previous.get("list_fingerprint") or ""
        reason = ""
        if not previous:
            reason = "unknown_work_order"
        elif not previous_fingerprint:
            reason = "baseline_integrity_missing_fingerprint"
        elif previous_fingerprint != fingerprint:
            reason = "list_fields_changed"

        if reason:
            record["list_fingerprint"] = fingerprint
            record["scan_reason"] = reason
            candidates.append(record)
        else:
            save_list_observation(
                record,
                fingerprint=fingerprint,
                baseline_generation=baseline_generation,
            )
            unchanged += 1

    # A previously open WO disappearing from the open list is itself a
    # material list-level change. Revisit only previously baselined records
    # that still look active, using their saved direct URL.
    closed_status_terms = (
        "CLOSED", "COMPLETED", "CANCELED", "CANCELLED", "DELETED"
    )
    missing_count = 0
    for wo_num, previous in previous_states.items():
        previous_status = str(previous.get("appfolio_status") or "").upper()
        if (
            wo_num in current_wo_numbers
            or not previous.get("list_fingerprint")
            or not previous.get("wo_url")
            or any(term in previous_status for term in closed_status_terms)
        ):
            continue
        try:
            stored_list_fields = json.loads(
                previous.get("list_fields_json") or "{}"
            )
        except (TypeError, ValueError):
            stored_list_fields = {}
        candidates.append(
            {
                "wo_num": wo_num,
                "status": previous_status or "UNKNOWN",
                "row_lines": [],
                "wo_url": previous.get("wo_url", ""),
                "list_fields": stored_list_fields,
                "list_fingerprint": previous.get("list_fingerprint", ""),
                "scan_reason": "missing_from_open_list",
                "missing_from_open_list": True,
            }
        )
        missing_count += 1

    candidates.sort(key=lambda record: str(record.get("wo_num") or ""))
    reason_counts = Counter(
        str(record.get("scan_reason") or "unknown")
        for record in candidates
    )
    metrics["target_reasons"] = dict(sorted(reason_counts.items()))
    metrics["unchanged_work_orders_skipped"] = unchanged
    for record in candidates:
        metrics["targeted_detail_opens"] += 1
        metrics["appfolio_navigations_attributable_to_reconciliation"] += 1
        try:
            details = inspect_work_order_detail(page, record)
        except Exception:
            metrics["detail_failures"] += 1
            metrics["duration"] = round(time.monotonic() - started, 2)
            finalize_navigation_metric()
            assert_zero_write_metrics(
                metrics, context="work_order_reconciliation_failure"
            )
            print(f"[ReconciliationMetrics] {json.dumps(metrics, sort_keys=True)}")
            raise
        wo_num = str(record.get("wo_num") or "")
        previous = previous_states.get(wo_num) or {}
        observed_status = str(
            details.get("live_status") or details.get("status") or "UNKNOWN"
        ).strip().upper()
        details.update(
            {
                # Reconciliation cannot create or overwrite workflow routing.
                "classification": previous.get("classification") or "",
                "triage_origin": "reconciliation",
                "list_fingerprint": record["list_fingerprint"],
                "detail_fingerprint": detail_record_fingerprint(details),
                "last_detail_scanned_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        save_scanner_observation(details)
        if not record.get("missing_from_open_list"):
            save_list_observation(
                record,
                fingerprint=record["list_fingerprint"],
                baseline_generation=baseline_generation,
            )
        print(
            f"[Reconciliation] targeted_detail wo={record.get('wo_num')} "
            f"reason={record.get('scan_reason')} observed_status={observed_status}"
        )

    metrics["duration"] = round(time.monotonic() - started, 2)
    finalize_navigation_metric()
    counts = {
        **metrics,
        # Backward-compatible aliases for existing callers.
        "list_rows": len(work_orders),
        "unchanged_rows": unchanged,
        "missing_rows": missing_count,
        "actions_attempted": 0,
        "baseline_created": not baseline_usable,
    }
    assert_zero_write_metrics(
        counts, context="work_order_reconciliation_complete"
    )
    print(f"[ReconciliationMetrics] {json.dumps(counts, sort_keys=True)}")
    return counts


def scan_logged_in_page(page) -> dict:
    """Backward-compatible entrypoint for lightweight reconciliation."""
    return reconcile_logged_in_page(page)



PACIFIC_TIME = ZoneInfo("America/Los_Angeles")
SCANNER_START_HOUR = 7
SCANNER_END_HOUR = 19
SCANNER_INTERVAL_MINUTES = 30


def next_weekday_start(now: datetime) -> datetime:
    """Return the next Monday-Friday 7:00 a.m. Pacific start time."""
    candidate = now.replace(
        hour=SCANNER_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )

    if now.weekday() < 5 and now < candidate:
        return candidate

    candidate += timedelta(days=1)

    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return candidate


def next_half_hour(now: datetime) -> datetime:
    """Return the next :00 or :30 Pacific scan time."""
    if now.minute < 30:
        return now.replace(
            minute=30,
            second=0,
            microsecond=0,
        )

    return (
        now.replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        + timedelta(hours=1)
    )


def sleep_until(target: datetime) -> None:
    """Sleep efficiently until a timezone-aware target time."""
    while True:
        now = datetime.now(PACIFIC_TIME)
        seconds_remaining = (target - now).total_seconds()

        if seconds_remaining <= 0:
            return

        time.sleep(
            max(
                1,
                min(seconds_remaining, 300),
            )
        )


def run_weekday_scheduler() -> None:
    """Reject the retired scheduler; main.py is the sole recurring owner."""
    raise RuntimeError(
        "The standalone recurring scanner is disabled. main.py owns the "
        "single production AppFolio scan schedule."
    )

    print("=" * 60)
    print("Rachel Work Order Scanner — WEEKDAY SCHEDULER")
    print("=" * 60)

    init_scanner_state_table()

    profile_dir = get_browser_profile_dir()
    os.makedirs(profile_dir, exist_ok=True)

    while True:
        now = datetime.now(PACIFIC_TIME)

        business_start = now.replace(
            hour=SCANNER_START_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        business_end = now.replace(
            hour=SCANNER_END_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        inside_business_window = (
            now.weekday() < 5
            and business_start <= now <= business_end
        )

        if not inside_business_window:
            next_start = next_weekday_start(now)

            print(
                "[Scheduler] Scanner is outside weekday hours. "
                f"Next AppFolio session: "
                f"{next_start.strftime('%A, %B %d at %I:%M %p %Z')}"
            )

            sleep_until(next_start)
            continue

        print(
            "[Scheduler] Starting weekday AppFolio session at "
            f"{now.strftime('%A, %B %d at %I:%M %p %Z')}."
        )

        manager = get_appfolio_browser_manager()
        with manager.operation("disabled_weekday_scanner") as page:
            try:
                print("[Scheduler] AppFolio session is active.")

                current_time = datetime.now(PACIFIC_TIME)

                next_scan = (
                    business_start
                    if current_time <= business_start
                    else current_time
                )

                while True:
                    current_time = datetime.now(PACIFIC_TIME)

                    current_end = current_time.replace(
                        hour=SCANNER_END_HOUR,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )

                    if (
                        current_time.weekday() >= 5
                        or current_time.date() != business_start.date()
                    ):
                        break

                    if current_time >= current_end:
                        print(
                            "[Scheduler] Running the final "
                            "7:00 p.m. scan."
                        )

                        try:
                            scan_logged_in_page(page)
                        except Exception as error:
                            print(
                                "[Scheduler] Final scan failed: "
                                f"{error}"
                            )

                        break

                    if current_time >= next_scan:
                        print(
                            "[Scheduler] Starting scheduled scan at "
                            f"{current_time.strftime('%I:%M %p %Z')}."
                        )

                        try:
                            scan_logged_in_page(page)
                        except Exception as error:
                            print(
                                "[Scheduler] Scheduled scan failed: "
                                f"{error}"
                            )

                        next_scan = next_half_hour(
                            datetime.now(PACIFIC_TIME)
                        )

                        print(
                            "[Scheduler] Next scan scheduled for "
                            f"{next_scan.strftime('%I:%M %p %Z')}."
                        )

                    wait_target = min(next_scan, current_end)
                    seconds_to_wait = (
                        wait_target
                        - datetime.now(PACIFIC_TIME)
                    ).total_seconds()

                    time.sleep(
                        max(
                            1,
                            min(seconds_to_wait, 30),
                        )
                    )

            finally:
                print(
                    "[Scheduler] AppFolio scanner operation ended."
                )


def run_scan_once():
    print("=" * 60)
    print("Rachel Work Order Scanner — READ ONLY")
    print("=" * 60)

    init_scanner_state_table()

    profile_dir = get_browser_profile_dir()
    os.makedirs(profile_dir, exist_ok=True)

    print(f"[Scanner] Headless: {HEADLESS}")
    print(f"[Scanner] Browser profile: {profile_dir}")

    manager = get_appfolio_browser_manager()
    with manager.operation("manual_work_order_scan") as page:
        scan_logged_in_page(page)


if __name__ == "__main__":
    if "--schedule" in sys.argv:
        run_weekday_scheduler()
    else:
        run_scan_once()
