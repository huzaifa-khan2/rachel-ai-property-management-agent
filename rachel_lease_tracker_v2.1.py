"""
Rachel - ClientCo Property Management
Lease Renewal & Rent Increase Tracker v2.3

CHANGELOG v2.3:
- One consolidated email grouped by property
- Owner added as TO recipient (QA phase)
- Insurance catches anything within 60 days (not just exact windows)
- Rent control math calculated per tenant
- Rachel title: Assistant Property Manager
- Reply format: Manager adds action after dash on each tenant line

Run with:
  python3 rachel_lease_tracker_v2.1.py           (daily alerts)
  python3 rachel_lease_tracker_v2.1.py --digest   (Monday digest)
"""

import os
import json
import re
import sqlite3
import subprocess
import smtplib
import time
from datetime import date, datetime, timedelta
from typing import Optional
from email.message import EmailMessage
from urllib.parse import urlparse
from io import BytesIO
from pypdf import PdfReader
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic
from rachel_safety import email_allowed, property_fact_sheet_write_allowed
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

DB_PATH           = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")
MANAGER_EMAIL      = os.getenv("MANAGER_EMAIL", "manager@clientco.example")
RACHEL_FROM_EMAIL = os.getenv("RACHEL_FROM_EMAIL", "rachel@clientco.example")
RACHEL_EMAIL_USER = os.getenv("RACHEL_EMAIL_USER", RACHEL_FROM_EMAIL)
RACHEL_EMAIL_PASSWORD = get_secret(
    "RACHEL_EMAIL_PASSWORD",
    "RACHEL_EMAIL_PASSWORD",
)
RENT_CONTROL_SHEET_ID = "1QDz4QxHTz9l6CgbnwqsDWW_x8xjNLvBZJgRI4w7r0eE"

LEASE_NOTICE_WINDOWS     = [90, 60, 30]
MTM_NOTICE_WINDOWS       = [90, 60, 30]
INSURANCE_NOTICE_WINDOWS = [60, 30]
TWO_INCREASE_REGIMES     = ["AB 1482", "LA JCO / AB 1482"]

# Anthropic legal-research cost controls.
# Routine AppFolio, database, spreadsheet, and email work does not use tokens.
RESEARCH_MODEL = os.getenv(
    "RACHEL_RESEARCH_MODEL",
    "claude-sonnet-4-6",
)
RESEARCH_MAX_OUTPUT_TOKENS = int(
    os.getenv("RACHEL_RESEARCH_MAX_OUTPUT_TOKENS", "1400")
)
RESEARCH_MAX_ESTIMATED_INPUT_TOKENS = int(
    os.getenv("RACHEL_RESEARCH_MAX_ESTIMATED_INPUT_TOKENS", "5000")
)
RESEARCH_MAX_WEB_SEARCHES = int(
    os.getenv("RACHEL_RESEARCH_MAX_WEB_SEARCHES", "2")
)
RESEARCH_MAX_PROPERTIES_PER_RUN = int(
    os.getenv("RACHEL_RESEARCH_MAX_PROPERTIES_PER_RUN", "3")
)
RESEARCH_CACHE_DAYS = int(
    os.getenv("RACHEL_RESEARCH_CACHE_DAYS", "180")
)
RESEARCH_REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("RACHEL_RESEARCH_REQUEST_TIMEOUT_SECONDS", "120")
)
RESEARCH_MAX_RETRIES = int(
    os.getenv("RACHEL_RESEARCH_MAX_RETRIES", "0")
)

OFFICIAL_PAGE_TIMEOUT_SECONDS = float(
    os.getenv("RACHEL_OFFICIAL_PAGE_TIMEOUT_SECONDS", "20")
)
OFFICIAL_PAGE_MAX_BYTES = int(
    os.getenv("RACHEL_OFFICIAL_PAGE_MAX_BYTES", "1500000")
)
OFFICIAL_EXCERPT_MAX_CHARS = int(
    os.getenv("RACHEL_OFFICIAL_EXCERPT_MAX_CHARS", "6000")
)
OFFICIAL_BUNDLE_MAX_CHARS = int(
    os.getenv("RACHEL_OFFICIAL_BUNDLE_MAX_CHARS", "12000")
)
OFFICIAL_SOURCE_MIN_CHARS = int(
    os.getenv("RACHEL_OFFICIAL_SOURCE_MIN_CHARS", "200")
)

CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/"
    "geographies/onelineaddress"
)
CENSUS_GEOCODER_TIMEOUT_SECONDS = float(
    os.getenv("RACHEL_CENSUS_GEOCODER_TIMEOUT_SECONDS", "20")
)


def send_email(subject: str, body: str):
    """Send the lease alert through Rachel's Google Workspace mailbox."""
    dry_run = os.getenv(
        "RACHEL_LEASE_EMAIL_DRY_RUN",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}

    if dry_run:
        print(f"[Email][DRY RUN] Delivery suppressed: {subject}")
        return False

    if not RACHEL_EMAIL_USER or not RACHEL_EMAIL_PASSWORD:
        print(
            f"[Email] Rachel Gmail credentials are unavailable\\n"
            f"Subject: {subject}"
        )
        return False

    recipients = [
        MANAGER_EMAIL,
        "owner@clientco.example",
    ]

    blocked_recipients = [
        (recipient, reason)
        for recipient in recipients
        for allowed, reason in [email_allowed(recipient)]
        if not allowed
    ]
    if blocked_recipients:
        print(
            "[Email] Lease alert blocked by safety gate: "
            + ", ".join(
                f"{recipient} ({reason})"
                for recipient, reason in blocked_recipients
            )
        )
        return False

    intent = prepare_outbound_intent(
        operation="lease_email",
        target_id=f"lease-alert:{subject}",
        recipient=",".join(recipients),
        message=f"{subject}\n{body}",
        db_path=DB_PATH,
    )
    if intent["status"] == "sent_verified":
        print("[Email] Duplicate verified lease alert suppressed")
        return True
    if intent["status"] != "prepared":
        print(
            "[Email] Prior lease-alert attempt is unverified; automatic "
            "retry is blocked."
        )
        return False

    try:
        message = EmailMessage()
        message["From"] = f"Rachel — ClientCo <{RACHEL_FROM_EMAIL}>"
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=30,
        ) as server:
            server.login(
                RACHEL_EMAIL_USER,
                RACHEL_EMAIL_PASSWORD,
            )
            claim_outbound_intent(intent["intent_id"], db_path=DB_PATH)
            refused = server.send_message(
                message,
                from_addr=RACHEL_FROM_EMAIL,
                to_addrs=recipients,
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

        print(f"[Email] ✅ Sent to Manager + Owner: {subject}")
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

def load_rent_control(sheet_id: str) -> dict:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

        if not creds_path:
            local_credentials = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "rachel-client-co-272e82c3aad6.json",
            )

            if os.path.exists(local_credentials):
                creds_path = local_credentials

        if not creds_path or not os.path.exists(creds_path):
            print("[Tracker] Google service-account JSON was not found")
            return {}
        scopes = ["https://spreadsheets.google.com/feeds",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)
        data = gc.open_by_key(sheet_id).get_worksheet(0).get_all_records()
        rc_map = {}
        for row in data:
            prop = str(row.get("Property", "")).strip().lower()
            if prop:
                regime = str(row.get("Governing Regime", ""))
                notice_value = row.get("Notice Days", "")
                frequency_value = row.get(
                    "Max Increases Per 12 Months",
                    "",
                )

                try:
                    notice_days = int(float(notice_value))
                except (TypeError, ValueError):
                    notice_days = 30

                try:
                    max_per_year = int(float(frequency_value))
                except (TypeError, ValueError):
                    max_per_year = (
                        2
                        if any(
                            r in regime
                            for r in TWO_INCREASE_REGIMES
                        )
                        else 1
                    )

                rc_map[prop] = {
                    "regime": regime,
                    "cap_pct": str(
                        row.get("Current Increase Cap", "")
                    ),
                    "notice_days": notice_days,
                    "max_per_year": max_per_year,
                    "verification_status": str(
                        row.get("Verification Status", "")
                    ).strip(),
                    "source_url": str(
                        row.get("Source URL", "")
                    ).strip(),
                    "notes": str(
                        row.get("Notes / Manager to Confirm", "")
                    ).strip(),
                    "last_verified": str(
                        row.get("Last Verified", "")
                    ).strip(),
                }
        print(f"[Tracker] Loaded {len(rc_map)} rent control records")
        return rc_map
    except Exception as e:
        print(f"[Tracker] Could not load rent control sheet: {e}")
        return {}


def normalize_property_address(address: str) -> str:
    """
    Normalize harmless AppFolio/Google Sheet address differences
    without performing broad fuzzy matching.
    """
    value = str(address or "").lower().strip()

    value = re.sub(r"[,\.]", " ", value)
    # Remove unit designators while preserving street numbers
    # and ZIP codes.
    value = re.sub(
        r"\s+(?:unit|apt|apartment|suite)\s*#?\s*[a-z0-9-]+\b",
        " ",
        value,
    )
    value = re.sub(
        r"\s+#\s*[a-z0-9-]+\b",
        " ",
        value,
    )

    substitutions = {
        r"\bstreet\b": "st",
        r"\bavenue\b": "ave",
        r"\bdrive\b": "dr",
        r"\broad\b": "rd",
        r"\bboulevard\b": "blvd",
        r"\blane\b": "ln",
        r"\bcourt\b": "ct",
        r"\bplace\b": "pl",
        r"\bhighway\b": "hwy",
    }

    for pattern, replacement in substitutions.items():
        value = re.sub(pattern, replacement, value)

    value = re.sub(r"\s+", " ", value).strip()
    return value




ZIMAS_PUBLIC_URL = "https://zimas.lacity.org/"
ZIMAS_LOOKUP_TIMEOUT_MS = 60000


def _lookup_zimas_rso_once(
    address: str,
    matched_address: str = "",
) -> dict:
    """
    Look up a City of Los Angeles parcel in the public ZIMAS system.

    Returns the parcel-level LA RSO, JCO, Ellis Act, APN, and site
    address results. This does not by itself determine whether every
    individual unit on a multi-unit or ADU parcel has identical status.
    """
    from playwright.sync_api import sync_playwright

    raw_address = str(address or "").strip()
    census_address = str(matched_address or "").strip()

    search_source = census_address or raw_address

    if not search_source:
        return {
            "status": "INVALID_ADDRESS",
            "rso_result": "UNKNOWN",
            "error": "No property address was supplied.",
        }

    # Census matched addresses normally use:
    # STREET ADDRESS, CITY, STATE, ZIP
    # ZIMAS requires only the street address.
    search_address = search_source.split(",", 1)[0].strip()

    # Remove unit designators because ZIMAS searches the parcel.
    search_address = re.sub(
        r"\s+(?:#|unit|apt|apartment|suite|ste)\s*[\w-]+.*$",
        "",
        search_address,
        flags=re.IGNORECASE,
    ).strip()

    result = {
        "status": "LOOKUP_FAILED",
        "submitted_address": raw_address,
        "matched_address": census_address,
        "search_address": search_address,
        "site_addresses": [],
        "apn": "",
        "rso_result": "UNKNOWN",
        "jco_result": "UNKNOWN",
        "ellis_act_result": "UNKNOWN",
        "lookup_date": date.today().isoformat(),
        "source_url": ZIMAS_PUBLIC_URL,
        "use_code": "",
        "property_type_hint": "",
        "adu_configuration": "",
        "building_records": [],
        "zimas_total_units": None,
        "earliest_building_year": None,
        "permit_history_review_required": False,
        "error": "",
    }

    profile_dir = os.getenv(
        "RACHEL_ZIMAS_PROFILE_DIR",
        os.path.expanduser(
            "~/Desktop/Rachel/zimas_browser_profile"
        ),
    )
    os.makedirs(profile_dir, exist_ok=True)

    headless_value = os.getenv(
        "RACHEL_ZIMAS_HEADLESS",
        "true",
    ).strip().lower()

    headless = headless_value not in {
        "0",
        "false",
        "no",
        "off",
    }

    try:
        with sync_playwright() as playwright:
            context = (
                playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=headless,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                    viewport={
                        "width": 1280,
                        "height": 1000,
                    },
                )
            )

            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            page.goto(
                ZIMAS_PUBLIC_URL,
                wait_until="domcontentloaded",
                timeout=ZIMAS_LOOKUP_TIMEOUT_MS,
            )
            page.wait_for_timeout(5000)

            accept = page.get_by_text(
                "Accept",
                exact=True,
            )

            if (
                accept.count()
                and accept.first.is_visible()
            ):
                checkbox = page.locator(
                    "#checkSaveAcceptTerms"
                )

                if (
                    checkbox.count()
                    and checkbox.is_visible()
                    and not checkbox.is_checked()
                ):
                    checkbox.check(force=True)

                accept.first.click(force=True)
                page.wait_for_timeout(2500)

            address_tab = page.locator("#search-tab0")

            address_input = address_tab.locator(
                'input[name="inputADDR"]'
            )

            address_input.wait_for(
                state="visible",
                timeout=30000,
            )
            address_input.fill(search_address)

            address_tab.locator(
                'input[aria-label="Start Search"]'
            ).click(force=True)

            page.wait_for_timeout(9000)

            housing_matches = page.locator(
                "xpath=//*[normalize-space(text())='Housing']"
            )

            housing_button = None

            for index in range(housing_matches.count()):
                candidate = housing_matches.nth(index)

                if candidate.is_visible():
                    housing_button = candidate
                    break

            if housing_button is None:
                result["status"] = "NO_ZIMAS_MATCH"
                result["error"] = (
                    "ZIMAS did not display a Housing section."
                )
                context.close()
                return result

            housing_button.click(force=True)
            page.wait_for_timeout(3000)

            # Open every parcel-information section so assessor,
            # building, zoning, and housing facts become visible.
            accordion_buttons = page.locator(
                "button.accordion-button:visible"
            )

            for accordion_index in range(
                accordion_buttons.count()
            ):
                button = accordion_buttons.nth(
                    accordion_index
                )

                try:
                    if button.get_attribute(
                        "aria-expanded"
                    ) != "true":
                        button.click(force=True)
                        page.wait_for_timeout(250)
                except Exception:
                    continue

            page.wait_for_timeout(1000)

            body_text = page.locator(
                "body"
            ).inner_text()

            body_lines = [
                line.strip()
                for line in body_text.splitlines()
                if line.strip()
            ]

            use_code_match = re.search(
                r"Use Code:\s*([^\n]+)",
                body_text,
                flags=re.IGNORECASE,
            )

            if use_code_match:
                result["use_code"] = (
                    use_code_match.group(1).strip()
                )

            normalized_use_code = result[
                "use_code"
            ].lower()

            if "attached adu" in normalized_use_code:
                result["property_type_hint"] = (
                    "SFR + Attached ADU"
                )
                result["adu_configuration"] = "Attached"
            elif "detached adu" in normalized_use_code:
                result["property_type_hint"] = (
                    "SFR + Detached ADU"
                )
                result["adu_configuration"] = "Detached"
            elif (
                "accessory dwelling unit"
                in normalized_use_code
                or re.search(
                    r"\badu\b",
                    normalized_use_code,
                )
            ):
                result["property_type_hint"] = "SFR + ADU"
                result["adu_configuration"] = "Unknown"
            elif (
                "single family"
                in normalized_use_code
            ):
                result["property_type_hint"] = "SFR"

            building_records = []

            field_labels = {
                "Year Built:": "year_built",
                "Building Class:": "building_class",
                "Number of Units:": "number_of_units",
                "Number of Bedrooms:": "bedrooms",
                "Number of Bathrooms:": "bathrooms",
                "Building Square Footage:": (
                    "square_footage"
                ),
            }

            for line_index, line in enumerate(body_lines):
                building_match = re.fullmatch(
                    r"Building\s+(\d+)",
                    line,
                    flags=re.IGNORECASE,
                )

                if not building_match:
                    continue

                building = {
                    "building_number": int(
                        building_match.group(1)
                    )
                }

                for detail_line in body_lines[
                    line_index + 1:line_index + 12
                ]:
                    if re.match(
                        r"Building\s+\d+",
                        detail_line,
                        flags=re.IGNORECASE,
                    ):
                        break

                    for label, key in field_labels.items():
                        if detail_line.lower().startswith(
                            label.lower()
                        ):
                            value = detail_line[
                                len(label):
                            ].strip()

                            building[key] = value
                            break

                if len(building) > 1:
                    building_records.append(building)

            result["building_records"] = building_records

            unit_counts = []
            building_years = []

            for building in building_records:
                unit_match = re.search(
                    r"\d+",
                    str(
                        building.get(
                            "number_of_units",
                            "",
                        )
                    ),
                )

                if unit_match:
                    unit_counts.append(
                        int(unit_match.group())
                    )

                year_match = re.search(
                    r"\b(?:18|19|20)\d{2}\b",
                    str(
                        building.get(
                            "year_built",
                            "",
                        )
                    ),
                )

                if year_match:
                    building_years.append(
                        int(year_match.group())
                    )

            if unit_counts:
                result["zimas_total_units"] = sum(
                    unit_counts
                )

            if building_years:
                result["earliest_building_year"] = min(
                    building_years
                )

            if (
                result["adu_configuration"]
                or len(building_records) > 1
            ):
                result[
                    "permit_history_review_required"
                ] = True

            site_addresses = re.findall(
                r"Site Address:\s*([^\n]+)",
                body_text,
                flags=re.IGNORECASE,
            )

            result["site_addresses"] = [
                value.strip()
                for value in site_addresses
                if value.strip()
            ]

            apn_match = re.search(
                r"Assessor Parcel No\.\s*\(APN\):\s*"
                r"([0-9-]+)",
                body_text,
                flags=re.IGNORECASE,
            )

            if not apn_match:
                apn_match = re.search(
                    r"Rent Stabilization Ordinance\s*"
                    r"\(RSO\):\s*(?:Yes|No)\s*"
                    r"\[APN:\s*([0-9-]+)\]",
                    body_text,
                    flags=re.IGNORECASE,
                )

            if apn_match:
                result["apn"] = apn_match.group(1).strip()

            rso_match = re.search(
                r"Rent Stabilization Ordinance\s*"
                r"\(RSO\):\s*(Yes|No)",
                body_text,
                flags=re.IGNORECASE,
            )

            jco_match = re.search(
                r"Just Cause For Eviction Ordinance\s*"
                r"\(JCO\):\s*(Yes|No)",
                body_text,
                flags=re.IGNORECASE,
            )

            ellis_match = re.search(
                r"Ellis Act Property:\s*(Yes|No)",
                body_text,
                flags=re.IGNORECASE,
            )

            if rso_match:
                result["rso_result"] = (
                    rso_match.group(1).upper()
                )

            if jco_match:
                result["jco_result"] = (
                    jco_match.group(1).upper()
                )

            if ellis_match:
                result["ellis_act_result"] = (
                    ellis_match.group(1).upper()
                )

            if result["rso_result"] in {"YES", "NO"}:
                result["status"] = "LOOKUP_COMPLETE"
            else:
                result["status"] = "RSO_RESULT_NOT_FOUND"
                result["error"] = (
                    "The parcel loaded, but the RSO Yes/No "
                    "result could not be parsed."
                )

            context.close()
            return result

    except Exception as exc:
        result["status"] = "LOOKUP_FAILED"
        result["error"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return result




def lookup_zimas_rso(
    address: str,
    matched_address: str = "",
) -> dict:
    """
    Retry transient ZIMAS failures automatically.

    A failed attempt never means RSO No. Rachel retries with a fresh
    browser session and returns UNKNOWN only after all attempts fail.
    """
    max_attempts = max(
        1,
        int(os.getenv("RACHEL_ZIMAS_MAX_ATTEMPTS", "3")),
    )

    retryable_statuses = {
        "LOOKUP_FAILED",
        "NO_ZIMAS_MATCH",
        "RSO_RESULT_NOT_FOUND",
    }

    last_result = None

    for attempt in range(1, max_attempts + 1):
        print(
            f"[ZIMAS] Attempt {attempt}/{max_attempts}: "
            f"{address}"
        )

        result = _lookup_zimas_rso_once(
            address=address,
            matched_address=matched_address,
        )

        result["attempts"] = attempt
        last_result = result

        if result.get("status") == "LOOKUP_COMPLETE":
            if attempt > 1:
                print(
                    f"[ZIMAS] Lookup succeeded on attempt "
                    f"{attempt}: {address}"
                )

            return result

        status = result.get("status", "LOOKUP_FAILED")
        error = result.get("error", "")

        print(
            f"[ZIMAS] Attempt {attempt} did not complete: "
            f"{status}"
            + (f" — {error}" if error else "")
        )

        if (
            status not in retryable_statuses
            or attempt >= max_attempts
        ):
            break

        # Increasing pause before opening a new browser session.
        time.sleep(attempt * 3)

    if last_result is None:
        last_result = {
            "status": "LOOKUP_FAILED",
            "rso_result": "UNKNOWN",
            "error": "No ZIMAS lookup attempt was completed.",
            "attempts": 0,
        }

    last_result["rso_result"] = (
        last_result.get("rso_result") or "UNKNOWN"
    )

    return last_result



PROPERTY_FACT_WORKFLOW_HEADERS = [
    "Property Type",
    "Total Residential Units",
    "Owner Occupied?",
    "Owner Occupied Unit",
    "Unit Construction Details",
    "ADU Configuration",
    "Earliest ZIMAS Building Year",
    "Ownership Type",
    "Corporate Member in Ownership?",
    "AB 1482 Exemption Notice Served?",
    "ZIMAS APN",
    "ZIMAS Site Addresses",
    "ZIMAS LA RSO Result",
    "ZIMAS JCO Result",
    "ZIMAS Ellis Act Result",
    "ZIMAS Lookup Date",
    "ZIMAS Permit Review Required?",
    "Unit-Level RSO Status",
    "AB 1482 Property Status",
    "Property Facts Status",
]


def ensure_property_fact_workflow_columns(sheet) -> list:
    """
    Add Rachel's property-level legal workflow columns when missing.

    Existing columns and data are never replaced or rearranged.
    """
    allowed, reason = property_fact_sheet_write_allowed()
    if not allowed:
        raise RuntimeError(f"property_fact_sheet_write_blocked: {reason}")

    from gspread.utils import rowcol_to_a1

    headers = sheet.row_values(1)

    missing_headers = [
        header
        for header in PROPERTY_FACT_WORKFLOW_HEADERS
        if header not in headers
    ]

    if not missing_headers:
        return headers

    first_column = len(headers) + 1
    last_column = first_column + len(missing_headers) - 1

    sheet.update(
        range_name=(
            f"{rowcol_to_a1(1, first_column)}:"
            f"{rowcol_to_a1(1, last_column)}"
        ),
        values=[missing_headers],
        value_input_option="USER_ENTERED",
    )

    headers.extend(missing_headers)

    print(
        "[Property Facts] Added master-sheet columns: "
        + ", ".join(missing_headers)
    )

    return headers


def sync_missing_properties_to_sheet() -> list:
    """
    Add active AppFolio properties missing from the rent-control
    master sheet.

    New rows are marked for research and Manager verification.
    Existing rows are never overwritten.
    """
    allowed, reason = property_fact_sheet_write_allowed()
    if not allowed:
        raise RuntimeError(f"property_fact_sheet_write_blocked: {reason}")

    import gspread
    from google.oauth2.service_account import Credentials

    creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not creds_path:
        local_credentials = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "rachel-client-co-272e82c3aad6.json",
        )

        if os.path.exists(local_credentials):
            creds_path = local_credentials

    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError(
            "Google service-account JSON was not found."
        )

    credentials = Credentials.from_service_account_file(
        creds_path,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    sheet = gspread.authorize(credentials).open_by_key(
        RENT_CONTROL_SHEET_ID
    ).get_worksheet(0)

    headers = ensure_property_fact_workflow_columns(
        sheet
    )
    existing_rows = sheet.get_all_records()

    existing_addresses = {
        normalize_property_address(
            row.get("Property", "")
        )
        for row in existing_rows
        if row.get("Property")
    }

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    ensure_occupancy_classifications(db)

    active_properties = db.execute("""
        SELECT DISTINCT t.property_address
        FROM tenants t
        WHERE t.is_active = 1
          AND t.property_address IS NOT NULL
          AND TRIM(t.property_address) != ''
          AND NOT EXISTS (
              SELECT 1
              FROM occupancy_classifications oc
              WHERE oc.occupancy_id = t.occupancy_id
                AND oc.exclude_from_legal_research = 1
          )
        ORDER BY t.property_address
    """).fetchall()

    db.close()

    missing = [
        row["property_address"]
        for row in active_properties
        if normalize_property_address(
            row["property_address"]
        ) not in existing_addresses
    ]

    if not missing:
        print(
            "[Rent Control Sync] No missing active properties."
        )
        return []

    rows_to_append = []

    for address in missing:
        row = {header: "" for header in headers}
        row["Property"] = address
        row["Verification Status"] = "NEEDS RESEARCH"
        row["Notes / Manager to Confirm"] = (
            "Added automatically from the current AppFolio "
            "Tenant Directory. Rachel must research governing "
            "law and identify any property-specific facts that "
            "Manager must verify."
        )

        rows_to_append.append([
            row.get(header, "")
            for header in headers
        ])

    sheet.append_rows(
        rows_to_append,
        value_input_option="USER_ENTERED",
    )

    print(
        f"[Rent Control Sync] Added "
        f"{len(missing)} missing properties."
    )

    for address in missing:
        print(f"  + {address}")

    return missing



def research_and_update_pending_sheet_rows(
    client=None,
    max_properties: Optional[int] = None,
) -> dict:
    """
    Research pending master-sheet properties and update the same rows.

    Existing Manager-verified rows are never overwritten. Jurisdiction
    caching prevents repeated Anthropic use for properties in one city.
    """
    allowed, reason = property_fact_sheet_write_allowed()
    if not allowed:
        raise RuntimeError(f"property_fact_sheet_write_blocked: {reason}")

    import gspread
    from google.oauth2.service_account import Credentials
    from gspread.utils import rowcol_to_a1

    if max_properties is None:
        max_properties = RESEARCH_MAX_PROPERTIES_PER_RUN

    creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not creds_path:
        local_credentials = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "rachel-client-co-272e82c3aad6.json",
        )

        if os.path.exists(local_credentials):
            creds_path = local_credentials

    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError(
            "Google service-account JSON was not found."
        )

    credentials = Credentials.from_service_account_file(
        creds_path,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    sheet = gspread.authorize(credentials).open_by_key(
        RENT_CONTROL_SHEET_ID
    ).get_worksheet(0)

    headers = ensure_property_fact_workflow_columns(
        sheet
    )
    records = sheet.get_all_records()

    required_headers = {
        "Property",
        "Rent Control",
        "Governing Regime",
        "Current Increase Cap",
        "Cap Effective Dates",
        "AB 1482 Exempt?",
        "Last Verified",
        "Source URL",
        "Notes / Manager to Confirm",
        "Verification Status",
        "Notice Days",
        "Max Increases Per 12 Months",
    }

    missing_headers = sorted(
        required_headers - set(headers)
    )

    if missing_headers:
        raise RuntimeError(
            "Master sheet is missing columns: "
            + ", ".join(missing_headers)
        )

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_research_tracking(db)

    if client is None:
        anthropic_api_key = get_secret(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        )

        if not anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY was not found."
            )

        client = Anthropic(api_key=anthropic_api_key)

    # Revisit every status controlled by Rachel. This allows
    # improved jurisdiction resolution, newly configured sources,
    # and refreshed legal research to correct older sheet results.
    # Human-verified statuses remain protected.
    pending_statuses = {
        "NEEDS RESEARCH",
        "RESEARCH IN PROGRESS",
        "SOURCE SETUP REQUIRED",
        "JURISDICTION REVIEW REQUIRED",
        "ADDRESS VERIFICATION REQUIRED",
        "RACHEL RESEARCHED — MANAGER REVIEW",
    }

    results = {
        "researched": [],
        "address_verification": [],
        "jurisdiction_review": [],
        "source_setup_required": [],
        "failed": [],
    }

    researched_count = 0

    for sheet_index, existing_row in enumerate(
        records,
        start=2,
    ):
        status = str(
            existing_row.get("Verification Status", "")
        ).strip().upper()

        if status not in pending_statuses:
            continue

        address = str(
            existing_row.get("Property", "")
        ).strip()

        if not address:
            continue

        row = {
            header: existing_row.get(header, "")
            for header in headers
        }

        jurisdiction = resolve_property_jurisdiction(address)
        zimas_result = None

        if (
            jurisdiction
            and jurisdiction.get("jurisdiction_key")
            == "ca_los_angeles"
        ):
            zimas_result = lookup_zimas_rso(
                address=address,
                matched_address=jurisdiction.get(
                    "matched_address",
                    "",
                ),
            )

            if zimas_result.get("status") == "LOOKUP_COMPLETE":
                def fill_blank(column: str, value) -> None:
                    if (
                        column in row
                        and not str(row.get(column, "")).strip()
                        and value not in (None, "")
                    ):
                        row[column] = value

                fill_blank(
                    "Property Type",
                    zimas_result.get("property_type_hint", ""),
                )
                fill_blank(
                    "Total Residential Units",
                    zimas_result.get("zimas_total_units"),
                )
                fill_blank(
                    "ADU Configuration",
                    zimas_result.get("adu_configuration", ""),
                )
                fill_blank(
                    "Earliest ZIMAS Building Year",
                    zimas_result.get(
                        "earliest_building_year",
                    ),
                )
                fill_blank(
                    "ZIMAS APN",
                    zimas_result.get("apn", ""),
                )
                fill_blank(
                    "ZIMAS Site Addresses",
                    "\n".join(
                        zimas_result.get(
                            "site_addresses",
                            [],
                        )
                    ),
                )

                row["ZIMAS LA RSO Result"] = (
                    zimas_result.get("rso_result", "UNKNOWN")
                )
                row["ZIMAS JCO Result"] = (
                    zimas_result.get("jco_result", "UNKNOWN")
                )
                row["ZIMAS Ellis Act Result"] = (
                    zimas_result.get(
                        "ellis_act_result",
                        "UNKNOWN",
                    )
                )
                row["ZIMAS Lookup Date"] = (
                    zimas_result.get("lookup_date", "")
                )
                row["ZIMAS Permit Review Required?"] = (
                    "Yes"
                    if zimas_result.get(
                        "permit_history_review_required"
                    )
                    else "No"
                )

                building_records = zimas_result.get(
                    "building_records",
                    [],
                )

                building_details = []

                for building in building_records:
                    details = [
                        f"Building {building.get('building_number')}",
                    ]

                    if building.get("year_built"):
                        details.append(
                            f"year {building['year_built']}"
                        )

                    if building.get("number_of_units"):
                        details.append(
                            f"{building['number_of_units']} unit(s)"
                        )

                    if building.get("bedrooms"):
                        details.append(
                            f"{building['bedrooms']} bed"
                        )

                    if building.get("bathrooms"):
                        details.append(
                            f"{building['bathrooms']} bath"
                        )

                    if building.get("square_footage"):
                        details.append(
                            building["square_footage"]
                        )

                    building_details.append(
                        ", ".join(details)
                    )

                fill_blank(
                    "Unit Construction Details",
                    "\n".join(building_details),
                )

                rso_result = zimas_result.get(
                    "rso_result",
                    "UNKNOWN",
                )
                total_units = (
                    zimas_result.get("zimas_total_units")
                    or 0
                )
                has_adu = bool(
                    zimas_result.get("adu_configuration")
                )

                if rso_result == "YES":
                    if total_units > 1 or has_adu:
                        row["Unit-Level RSO Status"] = (
                            "PARCEL RSO YES — RENTED UNIT "
                            "VERIFICATION REQUIRED"
                        )
                    else:
                        row["Unit-Level RSO Status"] = (
                            "LA RSO YES — ZIMAS PARCEL RESULT"
                        )
                elif rso_result == "NO":
                    row["Unit-Level RSO Status"] = (
                        "LA RSO NO — ZIMAS PARCEL RESULT"
                    )
                else:
                    row["Unit-Level RSO Status"] = (
                        "ZIMAS RESULT UNCLEAR — REVIEW REQUIRED"
                    )

                if (
                    total_units > 1
                    or has_adu
                    or zimas_result.get(
                        "permit_history_review_required"
                    )
                ):
                    row["Property Facts Status"] = (
                        "ZIMAS COMPLETE — MANAGER FACTS REQUIRED"
                    )
                else:
                    row["Property Facts Status"] = (
                        "ZIMAS COMPLETE"
                    )

            else:
                row["ZIMAS LA RSO Result"] = "LOOKUP FAILED"
                row["ZIMAS Lookup Date"] = (
                    zimas_result.get("lookup_date", "")
                )
                row["Property Facts Status"] = (
                    "ZIMAS RETRY REQUIRED"
                )

        if jurisdiction is None:
            row["Verification Status"] = (
                "JURISDICTION REVIEW REQUIRED"
            )
            row["Notes / Manager to Confirm"] = (
                "Rachel could not determine the governing "
                "jurisdiction from the AppFolio address. "
                "Manager: Please confirm or correct the address."
            )

            results["jurisdiction_review"].append(address)

        elif (
            jurisdiction.get("status")
            == "NEEDS_ADDRESS_VERIFICATION"
        ):
            row["Verification Status"] = (
                "ADDRESS VERIFICATION REQUIRED"
            )
            row["Notes / Manager to Confirm"] = (
                jurisdiction.get("warning", "")
            )

            results["address_verification"].append(address)

        elif not jurisdiction.get("source_urls"):
            row["Verification Status"] = (
                "SOURCE SETUP REQUIRED"
            )
            row["Notes / Manager to Confirm"] = (
                "Rachel identified "
                f"{jurisdiction['jurisdiction_name']}, but the "
                "approved official-source bundle has not yet been "
                "configured. No Anthropic tokens were used."
            )

            results["source_setup_required"].append(address)

        elif researched_count >= max_properties:
            continue

        else:
            row["Verification Status"] = (
                "RESEARCH IN PROGRESS"
            )

            end_cell = rowcol_to_a1(
                sheet_index,
                len(headers),
            )

            sheet.update(
                range_name=f"A{sheet_index}:{end_cell}",
                values=[[
                    row.get(header, "")
                    for header in headers
                ]],
                value_input_option="USER_ENTERED",
            )

            try:
                research = research_jurisdiction_profile(
                    client=client,
                    db=db,
                    jurisdiction=jurisdiction,
                )

                ab_rule = str(
                    research.get(
                        "ab_1482_general_rule",
                        "",
                    )
                )

                if (
                    "does not apply" in ab_rule.lower()
                    or "not applicable" in ab_rule.lower()
                ):
                    ab_exempt_value = "Not applicable"
                else:
                    ab_exempt_value = (
                        "Unknown — Manager to confirm"
                    )

                notes = str(
                    research.get(
                        "notes_manager_to_confirm",
                        "",
                    )
                ).strip()

                next_cap = str(
                    research.get(
                        "next_increase_cap",
                        "",
                    )
                ).strip()

                next_dates = str(
                    research.get(
                        "next_cap_effective_dates",
                        "",
                    )
                ).strip()

                if (
                    next_cap
                    and "not established" not in next_cap.lower()
                ):
                    upcoming_note = (
                        f"Upcoming cap: {next_cap}"
                    )

                    if next_dates:
                        upcoming_note += (
                            f" effective {next_dates}"
                        )

                    notes = (
                        f"{notes} {upcoming_note}."
                    ).strip()

                row["Rent Control"] = research.get(
                    "rent_control",
                    "",
                )
                row["Governing Regime"] = research.get(
                    "governing_regime",
                    "",
                )
                row["Current Increase Cap"] = research.get(
                    "current_increase_cap",
                    "",
                )
                row["Cap Effective Dates"] = research.get(
                    "current_cap_effective_dates",
                    "",
                )
                row["AB 1482 Exempt?"] = ab_exempt_value
                row["Last Verified"] = research.get(
                    "last_verified",
                    date.today().isoformat(),
                )
                row["Source URL"] = "\n".join(
                    research.get("source_urls", [])
                )
                row["Notes / Manager to Confirm"] = notes
                row["Verification Status"] = (
                    "RACHEL RESEARCHED — MANAGER REVIEW"
                )
                row["Notice Days"] = research.get(
                    "notice_days",
                    "",
                )
                row["Max Increases Per 12 Months"] = (
                    research.get(
                        "max_increases_per_12_months",
                        "",
                    )
                )

                researched_count += 1
                results["researched"].append(address)

            except Exception as exc:
                row["Verification Status"] = (
                    "RESEARCH FAILED — RETRY REQUIRED"
                )
                row["Notes / Manager to Confirm"] = (
                    f"Rachel could not complete research: "
                    f"{type(exc).__name__}: {exc}"
                )

                results["failed"].append({
                    "property": address,
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                })

        end_cell = rowcol_to_a1(
            sheet_index,
            len(headers),
        )

        sheet.update(
            range_name=f"A{sheet_index}:{end_cell}",
            values=[[
                row.get(header, "")
                for header in headers
            ]],
            value_input_option="USER_ENTERED",
        )

    db.close()

    print(
        "[Rent Control Research] "
        f"Researched: {len(results['researched'])}; "
        f"address verification: "
        f"{len(results['address_verification'])}; "
        f"source setup required: "
        f"{len(results['source_setup_required'])}; "
        f"failed: {len(results['failed'])}."
    )

    return results


def get_rc(rc_map: dict, address: str) -> Optional[dict]:
    normalized_address = normalize_property_address(address)

    normalized_records = {
        normalize_property_address(key): value
        for key, value in rc_map.items()
    }

    if normalized_address in normalized_records:
        return normalized_records[normalized_address]

    return None


def parse_cap(cap_str: str) -> Optional[float]:
    if not cap_str or any(x in cap_str for x in ["TBD", "NO CAP", "EXEMPT"]):
        return None
    match = re.search(r"[\d.]+", cap_str)
    return float(match.group()) if match else None


def increase_line(rc: Optional[dict], current_rent: Optional[float]) -> str:
    if not rc:
        return "Unknown — check rent control master sheet"
    cap_str = rc.get("cap_pct", "")
    if "NO CAP" in cap_str or "EXEMPT" in cap_str:
        return "NO CAP — AB 1482 exempt (verify with attorney)"
    cap_pct = parse_cap(cap_str)
    if cap_pct and current_rent:
        inc = current_rent * (cap_pct / 100)
        return (f"{cap_str} cap = +${inc:,.2f}/mo "
                f"→ new max ${current_rent + inc:,.2f}/mo")
    return f"{cap_str} — verify current rate before issuing notice"



def ensure_occupancy_classifications(db) -> None:
    """Preserve manual exclusions for owner-held, internal, or test occupancies."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS occupancy_classifications (
            occupancy_id                TEXT PRIMARY KEY,
            classification              TEXT NOT NULL,
            exclude_from_lease_reports  INTEGER NOT NULL DEFAULT 0,
            exclude_from_legal_research INTEGER NOT NULL DEFAULT 0,
            notes                       TEXT,
            updated_at                  TEXT NOT NULL
        )
    """)
    db.commit()


def ensure_research_tracking(db) -> None:
    """
    Create persistent jurisdiction caching and Anthropic usage logs.

    Rachel can reuse prior jurisdiction research instead of paying
    to research the same city for every property.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS rent_control_research_cache (
            jurisdiction_key  TEXT PRIMARY KEY,
            jurisdiction_name TEXT NOT NULL,
            research_json     TEXT NOT NULL,
            researched_at     TEXT NOT NULL,
            expires_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS anthropic_usage_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            feature             TEXT NOT NULL,
            property_address    TEXT,
            jurisdiction_key    TEXT,
            model               TEXT NOT NULL,
            input_tokens        INTEGER NOT NULL DEFAULT 0,
            output_tokens       INTEGER NOT NULL DEFAULT 0,
            web_search_requests INTEGER NOT NULL DEFAULT 0,
            recorded_at         TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_anthropic_usage_recorded
        ON anthropic_usage_log(recorded_at);
    """)
    db.commit()


def lookup_census_jurisdiction(
    address: str,
) -> Optional[dict]:
    """
    Resolve the legal city/county geography using the official
    U.S. Census Geocoder.

    This uses no Anthropic tokens and requires no API key.
    """
    address = str(address or "").strip()

    if not address:
        return None

    response = requests.get(
        CENSUS_GEOCODER_URL,
        params={
            "address": address,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        },
        timeout=CENSUS_GEOCODER_TIMEOUT_SECONDS,
        headers={
            "User-Agent": (
                "Rachel-Client-Co/1.0 "
                "(property jurisdiction validation)"
            )
        },
    )

    response.raise_for_status()
    payload = response.json()

    matches = (
        payload.get("result", {})
        .get("addressMatches", [])
    )

    if not matches:
        return None

    match = matches[0]
    geographies = match.get("geographies", {})

    def geography_records(*possible_names):
        for geography_name, records in geographies.items():
            normalized_name = geography_name.lower()

            if any(
                possible.lower() in normalized_name
                for possible in possible_names
            ):
                return records or []

        return []

    incorporated_records = geography_records(
        "incorporated place",
    )

    # Select the actual Counties geography, not a County Census
    # Division such as "Los Angeles CCD".
    county_records = []

    for geography_name, records in geographies.items():
        normalized_name = geography_name.strip().lower()

        if normalized_name == "counties":
            county_records = records or []
            break

    cdp_records = geography_records(
        "census designated place",
    )

    incorporated_name = (
        str(incorporated_records[0].get("NAME", "")).strip()
        if incorporated_records
        else ""
    )
    county_name = (
        str(county_records[0].get("NAME", "")).strip()
        if county_records
        else ""
    )
    cdp_name = (
        str(cdp_records[0].get("NAME", "")).strip()
        if cdp_records
        else ""
    )

    # Census names commonly end with " city" or " CDP".
    incorporated_city = re.sub(
        r"\s+(?:city|town|village)$",
        "",
        incorporated_name,
        flags=re.IGNORECASE,
    ).strip()

    community_name = re.sub(
        r"\s+CDP$",
        "",
        cdp_name,
        flags=re.IGNORECASE,
    ).strip()

    matched_address = str(
        match.get("matchedAddress", "")
    ).strip()

    coordinates = match.get("coordinates", {})

    if incorporated_city:
        jurisdiction_type = "INCORPORATED_CITY"
        legal_jurisdiction = incorporated_city
    elif county_name:
        jurisdiction_type = "UNINCORPORATED_COUNTY"
        legal_jurisdiction = county_name
    else:
        jurisdiction_type = "UNKNOWN"
        legal_jurisdiction = ""

    return {
        "submitted_address": address,
        "matched_address": matched_address,
        "jurisdiction_type": jurisdiction_type,
        "legal_jurisdiction": legal_jurisdiction,
        "incorporated_city": incorporated_city,
        "county": county_name,
        "community": community_name,
        "latitude": coordinates.get("y"),
        "longitude": coordinates.get("x"),
        "source": "U.S. Census Geocoder",
        "status": (
            "RESOLVED"
            if legal_jurisdiction
            else "NEEDS_REVIEW"
        ),
    }


JURISDICTION_PROFILES = [
    {
        "jurisdiction_key": "ca_long_beach",
        "jurisdiction_name": "Long Beach, California",
        "statewide_cap_area": "Los Angeles Area",
        "city_pattern": r"\blong beach\b",
        "expected_zip_prefixes": ("908",),
        "allowed_domains": [
            "longbeach.gov",
            "oag.ca.gov",
            "housing.ca.gov",
            "leginfo.legislature.ca.gov",
        ],
        "source_urls": [
            "https://www.longbeach.gov/lbcd/hn/tenant-landlord-resources/",
            "https://oag.ca.gov/rentcaps",
            "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1947.12.",
        ],
    },
    {
        "jurisdiction_key": "ca_los_angeles",
        "jurisdiction_name": "Los Angeles, California",
        "statewide_cap_area": "Los Angeles Area",
        "city_pattern": r"\b(?:los angeles|granada hills)\b",
        "expected_zip_prefixes": ("900", "913"),
        "allowed_domains": [
            "lacity.gov",
            "housing2.lacity.org",
            "oag.ca.gov",
            "housing.ca.gov",
            "leginfo.legislature.ca.gov",
        ],
        "source_urls": [
            "https://oag.ca.gov/rentcaps",
            "https://housing.lacity.gov/wp-content/uploads/2022/01/Allowable-Rent-Increase-Bulletin-English.pdf",
            "https://housing.lacity.gov/wp-content/uploads/2023/06/LA-Renter-protections-Notification.pdf",
        ],
    },
    {
        "jurisdiction_key": "ca_santa_monica",
        "jurisdiction_name": "Santa Monica, California",
        "statewide_cap_area": "Los Angeles Area",
        "city_pattern": r"\bsanta monica\b",
        "expected_zip_prefixes": ("904",),
        "allowed_domains": [
            "santamonica.gov",
            "oag.ca.gov",
            "housing.ca.gov",
            "leginfo.legislature.ca.gov",
        ],
        "source_urls": [
            "https://www.santamonica.gov/rent-control-charter-amendment-regulations",
            "https://www.santamonica.gov/maximum-lawful-rent",
            "https://www.santamonica.gov/process-explainers/how-to-complete-the-notice-of-change-in-terms-of-tenancy",
            "https://oag.ca.gov/rentcaps",
        ],
    },
    {
        "jurisdiction_key": "ca_beverly_hills",
        "jurisdiction_name": "Beverly Hills, California",
        "statewide_cap_area": "Los Angeles Area",
        "city_pattern": r"\bbeverly hills\b",
        "expected_zip_prefixes": ("902",),
        "allowed_domains": [
            "beverlyhills.org",
            "oag.ca.gov",
            "housing.ca.gov",
            "leginfo.legislature.ca.gov",
        ],
        "source_urls": [
            "https://www.beverlyhills.org/1090/Rent-Stabilization-Housing",
            "https://www.beverlyhills.org/1098/Rent-Stabilization-Ordinance",
            "https://www.beverlyhills.org/1505/Rental-Unit-Registration-Appeals",
            "https://www.beverlyhills.org/DocumentCenter/View/5624/RSO-FAQs-PDF",
            "https://oag.ca.gov/rentcaps",
        ],
    },
    {
        "jurisdiction_key": "ca_west_hollywood",
        "jurisdiction_name": "West Hollywood, California",
        "city_pattern": r"\bwest hollywood\b",
        "expected_zip_prefixes": ("900",),
        "allowed_domains": [
            "weho.org",
            "oag.ca.gov",
            "housing.ca.gov",
            "leginfo.legislature.ca.gov",
        ],
    },
    {
        "jurisdiction_key": "ca_pasadena",
        "jurisdiction_name": "Pasadena, California",
        "city_pattern": r"\bpasadena\b",
        "expected_zip_prefixes": ("911",),
        "allowed_domains": [
            "cityofpasadena.net",
            "oag.ca.gov",
            "housing.ca.gov",
            "leginfo.legislature.ca.gov",
        ],
    },
    {
        "jurisdiction_key": "ca_inglewood",
        "jurisdiction_name": "Inglewood, California",
        "city_pattern": r"\binglewood\b",
        "expected_zip_prefixes": ("903",),
        "allowed_domains": [
            "cityofinglewood.org",
            "oag.ca.gov",
            "housing.ca.gov",
            "leginfo.legislature.ca.gov",
        ],
    },
    {
        "jurisdiction_key": "il_chicago",
        "jurisdiction_name": "Chicago, Illinois",
        "city_pattern": r"\bchicago\b",
        "expected_zip_prefixes": ("606",),
        "allowed_domains": [
            "chicago.gov",
            "illinois.gov",
            "ilga.gov",
        ],
    },
]


def resolve_property_jurisdiction(
    address: str,
) -> Optional[dict]:
    """
    Resolve the property's legal jurisdiction without Anthropic.

    The U.S. Census Geocoder is the primary authority. This correctly
    maps Los Angeles neighborhoods to the City of Los Angeles and
    distinguishes incorporated cities from unincorporated LA County.

    The original text/ZIP resolver remains as a fallback when Census
    cannot match the address or is temporarily unavailable.
    """
    raw_address = str(address or "").strip()

    if not raw_address:
        return None

    def normalize_place_name(value: str) -> str:
        value = str(value or "").lower().strip()
        value = re.sub(
            r",?\s+(?:california|illinois)$",
            "",
            value,
        )
        value = re.sub(
            r"\s+(?:city|county)$",
            "",
            value,
        )
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    def profile_result(
        profile: dict,
        census_result: Optional[dict] = None,
    ) -> dict:
        result = {
            "address": raw_address,
            "jurisdiction_key": profile["jurisdiction_key"],
            "jurisdiction_name": profile["jurisdiction_name"],
            "allowed_domains": list(
                profile.get("allowed_domains", [])
            ),
            "source_urls": list(
                profile.get("source_urls", [])
            ),
            "statewide_cap_area": profile.get(
                "statewide_cap_area",
                "",
            ),
            "status": "READY_FOR_RESEARCH",
            "warning": "",
            "resolution_source": (
                "U.S. Census Geocoder"
                if census_result
                else "AppFolio address fallback"
            ),
        }

        if census_result:
            result.update({
                "submitted_address": census_result.get(
                    "submitted_address",
                    raw_address,
                ),
                "matched_address": census_result.get(
                    "matched_address",
                    "",
                ),
                "jurisdiction_type": census_result.get(
                    "jurisdiction_type",
                    "",
                ),
                "incorporated_city": census_result.get(
                    "incorporated_city",
                    "",
                ),
                "county": census_result.get("county", ""),
                "community": census_result.get(
                    "community",
                    "",
                ),
                "latitude": census_result.get("latitude"),
                "longitude": census_result.get("longitude"),
            })

        return result

    # Primary resolution: official Census geography.
    try:
        census_result = lookup_census_jurisdiction(
            raw_address
        )
    except Exception as exc:
        print(
            "[Jurisdiction] Census lookup unavailable; "
            f"using address fallback: {type(exc).__name__}: {exc}"
        )
        census_result = None

    if (
        census_result
        and census_result.get("status") == "RESOLVED"
    ):
        legal_name = (
            census_result.get("incorporated_city")
            or census_result.get("legal_jurisdiction")
            or ""
        )
        normalized_legal_name = normalize_place_name(
            legal_name
        )

        # Match the Census legal city to a configured profile.
        for profile in JURISDICTION_PROFILES:
            profile_name = normalize_place_name(
                profile.get("jurisdiction_name", "")
            )

            if normalized_legal_name == profile_name:
                return profile_result(
                    profile,
                    census_result,
                )

        # Census resolved an unincorporated Los Angeles County
        # property, but its official-source bundle may still need
        # to be configured.
        county_name = normalize_place_name(
            census_result.get("county", "")
        )

        if (
            census_result.get("jurisdiction_type")
            == "UNINCORPORATED_COUNTY"
            and county_name == "los angeles"
        ):
            return {
                "address": raw_address,
                "submitted_address": census_result.get(
                    "submitted_address",
                    raw_address,
                ),
                "matched_address": census_result.get(
                    "matched_address",
                    "",
                ),
                "jurisdiction_key": (
                    "ca_los_angeles_county_unincorporated"
                ),
                "jurisdiction_name": (
                    "Unincorporated Los Angeles County, California"
                ),
                "jurisdiction_type": "UNINCORPORATED_COUNTY",
                "incorporated_city": "",
                "county": census_result.get("county", ""),
                "community": census_result.get(
                    "community",
                    "",
                ),
                "allowed_domains": [
                    "lacounty.gov",
                    "dcba.lacounty.gov",
                    "oag.ca.gov",
                    "housing.ca.gov",
                    "leginfo.legislature.ca.gov",
                ],
                "source_urls": [],
                "statewide_cap_area": "Los Angeles Area",
                "status": "READY_FOR_RESEARCH",
                "warning": "",
                "resolution_source": "U.S. Census Geocoder",
                "latitude": census_result.get("latitude"),
                "longitude": census_result.get("longitude"),
            }

        # Census resolved a real incorporated jurisdiction that does
        # not yet have an approved research-source profile.
        state_prefix = (
            "ca"
            if re.search(
                r"\bCA\b",
                raw_address,
                flags=re.IGNORECASE,
            )
            else "resolved"
        )

        jurisdiction_slug = re.sub(
            r"[^a-z0-9]+",
            "_",
            normalized_legal_name,
        ).strip("_")

        return {
            "address": raw_address,
            "submitted_address": census_result.get(
                "submitted_address",
                raw_address,
            ),
            "matched_address": census_result.get(
                "matched_address",
                "",
            ),
            "jurisdiction_key": (
                f"{state_prefix}_{jurisdiction_slug}"
            ),
            "jurisdiction_name": legal_name,
            "jurisdiction_type": census_result.get(
                "jurisdiction_type",
                "",
            ),
            "incorporated_city": census_result.get(
                "incorporated_city",
                "",
            ),
            "county": census_result.get("county", ""),
            "community": census_result.get(
                "community",
                "",
            ),
            "allowed_domains": [],
            "source_urls": [],
            "statewide_cap_area": (
                "Los Angeles Area"
                if county_name == "los angeles"
                else ""
            ),
            "status": "READY_FOR_RESEARCH",
            "warning": "",
            "resolution_source": "U.S. Census Geocoder",
            "latitude": census_result.get("latitude"),
            "longitude": census_result.get("longitude"),
        }

    # Fallback: original city-name and ZIP matching.
    searchable = raw_address.lower()
    zip_match = re.search(
        r"\b(\d{5})(?:-\d{4})?\b",
        searchable,
    )
    zip_code = zip_match.group(1) if zip_match else ""

    for profile in JURISDICTION_PROFILES:
        if not re.search(
            profile["city_pattern"],
            searchable,
        ):
            continue

        result = profile_result(profile)
        result["zip_code"] = zip_code

        expected_prefixes = profile.get(
            "expected_zip_prefixes",
            (),
        )

        if (
            zip_code
            and expected_prefixes
            and not zip_code.startswith(expected_prefixes)
        ):
            result["status"] = (
                "NEEDS_ADDRESS_VERIFICATION"
            )
            result["warning"] = (
                f"Address text suggests "
                f"{profile['jurisdiction_name']}, but ZIP "
                f"{zip_code} is inconsistent and Census could "
                "not verify the legal jurisdiction. Manager: "
                "Please confirm or correct the address."
            )

        return result

    return None


def get_cached_jurisdiction_research(
    db,
    jurisdiction_key: str,
) -> Optional[dict]:
    """Return unexpired jurisdiction research without using Anthropic tokens."""
    row = db.execute("""
        SELECT research_json
        FROM rent_control_research_cache
        WHERE jurisdiction_key = ?
          AND expires_at >= ?
    """, (
        jurisdiction_key,
        datetime.now().isoformat(timespec="seconds"),
    )).fetchone()

    if not row:
        return None

    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None


def save_jurisdiction_research(
    db,
    jurisdiction_key: str,
    jurisdiction_name: str,
    research: dict,
) -> None:
    """Cache jurisdiction-level research for reuse across properties."""
    researched_at = datetime.now()
    expires_at = researched_at + timedelta(
        days=RESEARCH_CACHE_DAYS
    )

    cache_valid_until = research.get(
        "cache_valid_until",
        "",
    )

    if cache_valid_until:
        try:
            legal_period_end = datetime.strptime(
                cache_valid_until,
                "%Y-%m-%d",
            ).replace(
                hour=23,
                minute=59,
                second=59,
            )

            if legal_period_end < expires_at:
                expires_at = legal_period_end

        except ValueError:
            pass

    db.execute("""
        INSERT INTO rent_control_research_cache (
            jurisdiction_key,
            jurisdiction_name,
            research_json,
            researched_at,
            expires_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(jurisdiction_key) DO UPDATE SET
            jurisdiction_name = excluded.jurisdiction_name,
            research_json = excluded.research_json,
            researched_at = excluded.researched_at,
            expires_at = excluded.expires_at
    """, (
        jurisdiction_key,
        jurisdiction_name,
        json.dumps(research, ensure_ascii=False),
        researched_at.isoformat(timespec="seconds"),
        expires_at.isoformat(timespec="seconds"),
    ))
    db.commit()


def log_anthropic_usage(
    db,
    feature: str,
    property_address: str,
    jurisdiction_key: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    web_search_requests: int,
) -> None:
    """Record Anthropic usage so research costs can be audited."""
    db.execute("""
        INSERT INTO anthropic_usage_log (
            feature,
            property_address,
            jurisdiction_key,
            model,
            input_tokens,
            output_tokens,
            web_search_requests,
            recorded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        feature,
        property_address,
        jurisdiction_key,
        model,
        max(0, int(input_tokens or 0)),
        max(0, int(output_tokens or 0)),
        max(0, int(web_search_requests or 0)),
        datetime.now().isoformat(timespec="seconds"),
    ))
    db.commit()


LEGAL_RESEARCH_TERMS = (
    "rent increase",
    "rent stabilization",
    "rent control",
    "tenant protection",
    "ab 1482",
    "1947.12",
    "1946.2",
    "consumer price index",
    "cpi",
    "notice",
    "exempt",
    "exemption",
    "certificate of occupancy",
    "just cause",
    "effective",
    "maximum increase",
)


def fetch_official_source_text(
    source_url: str,
    allowed_domains: list,
) -> str:
    """
    Download and clean an official HTML webpage locally.

    This function does not call Anthropic and uses no AI tokens.
    """
    if not official_source_url(source_url, allowed_domains):
        raise ValueError(
            f"Source is not on an approved government domain: {source_url}"
        )

    response = requests.get(
        source_url,
        timeout=OFFICIAL_PAGE_TIMEOUT_SECONDS,
        headers={
            "User-Agent": (
                "Rachel-Client-Co/1.0 "
                "(property-management legal research)"
            )
        },
    )
    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type",
        "",
    ).lower()

    content_length = len(response.content)

    if content_length > OFFICIAL_PAGE_MAX_BYTES:
        raise ValueError(
            f"Official source is too large: {content_length} bytes"
        )

    is_pdf = (
        "application/pdf" in content_type
        or source_url.lower().split("?", 1)[0].endswith(".pdf")
    )

    if is_pdf:
        reader = PdfReader(BytesIO(response.content))
        page_text = []

        for page in reader.pages:
            extracted = page.extract_text() or ""
            extracted = extracted.strip()

            if extracted:
                page_text.append(extracted)

        cleaned_pdf = "\n".join(page_text)
        cleaned_pdf = re.sub(
            r"[ \t]+",
            " ",
            cleaned_pdf,
        )
        cleaned_pdf = re.sub(
            r"\n{3,}",
            "\n\n",
            cleaned_pdf,
        ).strip()

        if not cleaned_pdf:
            raise ValueError(
                "PDF contained no extractable text."
            )

        return cleaned_pdf

    if "text/html" not in content_type:
        raise ValueError(
            f"Unsupported source type: {content_type or 'unknown'}"
        )

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup([
        "script",
        "style",
        "nav",
        "header",
        "footer",
        "form",
        "svg",
        "noscript",
        "iframe",
    ]):
        tag.decompose()

    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.body
        or soup
    )

    blocks = []

    for element in main_content.find_all(
        ["h1", "h2", "h3", "h4", "p", "li", "th", "td"]
    ):
        value = " ".join(
            element.get_text(" ", strip=True).split()
        )

        if value and value not in blocks:
            blocks.append(value)

    return "\n".join(blocks)


def select_relevant_legal_excerpt(
    cleaned_text: str,
    max_chars: int = OFFICIAL_EXCERPT_MAX_CHARS,
) -> str:
    """
    Keep only likely rent-law passages before sending text to Claude.
    """
    blocks = [
        block.strip()
        for block in str(cleaned_text or "").splitlines()
        if block.strip()
    ]

    selected = []
    selected_chars = 0

    for index, block in enumerate(blocks):
        lowered = block.lower()

        if not any(
            term in lowered
            for term in LEGAL_RESEARCH_TERMS
        ):
            continue

        start = max(0, index - 1)
        end = min(len(blocks), index + 2)

        for nearby in blocks[start:end]:
            if nearby in selected:
                continue

            additional_length = len(nearby) + 1

            if selected_chars + additional_length > max_chars:
                return "\n".join(selected)

            selected.append(nearby)
            selected_chars += additional_length

    if selected:
        return "\n".join(selected)

    return "\n".join(blocks)[:max_chars]


def parse_oag_statewide_cap_table(
    cleaned_text: str,
    area_name: str,
    effective_on=None,
) -> dict:
    """
    Parse the California Attorney General statewide-cap table
    deterministically instead of asking Claude to interpret it.
    """
    effective_on = effective_on or date.today()

    lines = [
        line.strip()
        for line in str(cleaned_text or "").splitlines()
        if line.strip()
    ]

    period_labels = [
        line
        for line in lines
        if line.lower().startswith("increase starts between")
    ][:2]

    area_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lower().startswith(area_name.lower())
        ),
        None,
    )

    if area_index is None or len(period_labels) < 2:
        return {}

    caps = []

    for line in lines[area_index + 1:area_index + 12]:
        if re.fullmatch(r"\d+(?:\.\d+)?%", line):
            caps.append(line)

        if len(caps) == 2:
            break

    if len(caps) < 2:
        return {}

    periods = []

    for label, cap in zip(period_labels, caps):
        matches = re.findall(
            r"(\d{1,2})/(\d{1,2})/(\d{2,4})",
            label,
        )

        if len(matches) != 2:
            continue

        def build_date(parts):
            month, day, year = map(int, parts)

            if year < 100:
                year += 2000

            return date(year, month, day)

        start_date = build_date(matches[0])
        end_date = build_date(matches[1])

        periods.append({
            "label": label.replace(
                "Increase starts between ",
                "",
            ),
            "cap": cap,
            "start_date": start_date,
            "end_date": end_date,
        })

    if not periods:
        return {}

    current = next(
        (
            period
            for period in periods
            if period["start_date"] <= effective_on
            <= period["end_date"]
        ),
        None,
    )

    upcoming = next(
        (
            period
            for period in periods
            if period["start_date"] > effective_on
        ),
        None,
    )

    return {
        "area": area_name,
        "current_cap": current["cap"] if current else "",
        "current_period": current["label"] if current else "",
        "current_start": (
            current["start_date"].isoformat()
            if current else ""
        ),
        "current_end": (
            current["end_date"].isoformat()
            if current else ""
        ),
        "next_cap": upcoming["cap"] if upcoming else "",
        "next_period": upcoming["label"] if upcoming else "",
        "next_start": (
            upcoming["start_date"].isoformat()
            if upcoming else ""
        ),
        "next_end": (
            upcoming["end_date"].isoformat()
            if upcoming else ""
        ),
    }


def parse_lahd_rso_bulletin(
    cleaned_text: str,
    effective_on=None,
) -> dict:
    """
    Parse the City of Los Angeles RSO annual-adjustment bulletin
    deterministically instead of relying on Claude.
    """
    effective_on = effective_on or date.today()

    normalized = str(cleaned_text or "")
    normalized = normalized.replace("–", "-")
    normalized = normalized.replace("—", "-")
    normalized = re.sub(r"\s+", " ", normalized)

    period_pattern = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*-\s*"
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*"
        r"(\d+(?:\.\d+)?)%",
        flags=re.IGNORECASE,
    )

    periods = []

    def parse_date(value: str) -> date:
        month, day, year = [
            int(part)
            for part in value.split("/")
        ]

        if year < 100:
            year += 2000

        return date(year, month, day)

    for start_text, end_text, cap_text in period_pattern.findall(
        normalized
    ):
        try:
            start_date = parse_date(start_text)
            end_date = parse_date(end_text)
        except (TypeError, ValueError):
            continue

        # Ignore historical table rows that cannot apply today.
        if end_date.year < 2024:
            continue

        periods.append({
            "start_date": start_date,
            "end_date": end_date,
            "cap": f"{cap_text}%",
            "period": f"{start_text} - {end_text}",
        })

    current = next(
        (
            period
            for period in periods
            if period["start_date"]
            <= effective_on
            <= period["end_date"]
        ),
        None,
    )

    upcoming = next(
        (
            period
            for period in sorted(
                periods,
                key=lambda item: item["start_date"],
            )
            if period["start_date"] > effective_on
        ),
        None,
    )

    notice_days = ""

    if re.search(
        r"written\s+30-day\s+notice",
        normalized,
        flags=re.IGNORECASE,
    ):
        notice_days = "30"

    once_per_year = bool(
        re.search(
            r"(?:once each year|one increase.*12.month)",
            normalized,
            flags=re.IGNORECASE,
        )
    )

    return {
        "regime": "City of Los Angeles RSO",
        "current_cap": (
            current["cap"] if current else ""
        ),
        "current_period": (
            current["period"] if current else ""
        ),
        "current_start": (
            current["start_date"].isoformat()
            if current else ""
        ),
        "current_end": (
            current["end_date"].isoformat()
            if current else ""
        ),
        "next_cap": (
            upcoming["cap"] if upcoming else ""
        ),
        "next_period": (
            upcoming["period"] if upcoming else ""
        ),
        "notice_days": notice_days,
        "max_increases_per_12_months": (
            "1" if once_per_year else ""
        ),
        "applicability": (
            "Applies only if the unit is subject to the "
            "City of Los Angeles Rent Stabilization Ordinance. "
            "Property-specific RSO status must be confirmed."
        ),
    }


def fetch_jurisdiction_source_bundle(
    jurisdiction: dict,
) -> list:
    """
    Fetch and clean approved official sources locally.

    This function uses no Anthropic tokens. The total returned text
    is capped before it can later be submitted for AI analysis.
    """
    source_urls = jurisdiction.get("source_urls", [])
    allowed_domains = jurisdiction.get("allowed_domains", [])

    if not source_urls:
        raise ValueError(
            f"No approved source URLs configured for "
            f"{jurisdiction.get('jurisdiction_name', 'jurisdiction')}."
        )

    results = []
    total_chars = 0

    for source_url in source_urls:
        try:
            cleaned = fetch_official_source_text(
                source_url,
                allowed_domains,
            )
            structured_facts = {}
            local_structured_facts = {}

            if (
                "oag.ca.gov/rentcaps" in source_url
                and jurisdiction.get("statewide_cap_area")
            ):
                structured_facts = (
                    parse_oag_statewide_cap_table(
                        cleaned_text=cleaned,
                        area_name=jurisdiction[
                            "statewide_cap_area"
                        ],
                        effective_on=date.today(),
                    )
                )

            if (
                "Allowable-Rent-Increase-Bulletin-English.pdf"
                in source_url
            ):
                local_structured_facts = (
                    parse_lahd_rso_bulletin(
                        cleaned_text=cleaned,
                        effective_on=date.today(),
                    )
                )

            excerpt = select_relevant_legal_excerpt(cleaned)

            if structured_facts:
                deterministic_summary = (
                    "DETERMINISTIC OAG STATEWIDE CAP TABLE:\n"
                    f"Area: {structured_facts['area']}\n"
                    f"Current cap: "
                    f"{structured_facts['current_cap']}\n"
                    f"Current effective period: "
                    f"{structured_facts['current_period']}\n"
                    f"Next cap: "
                    f"{structured_facts['next_cap']}\n"
                    f"Next effective period: "
                    f"{structured_facts['next_period']}\n"
                )

                excerpt = (
                    deterministic_summary
                    + "\n"
                    + excerpt
                )

            if local_structured_facts:
                local_summary = (
                    "DETERMINISTIC CITY OF LOS ANGELES RSO DATA:\n"
                    f"Current RSO cap: "
                    f"{local_structured_facts['current_cap']}\n"
                    f"Current RSO effective period: "
                    f"{local_structured_facts['current_period']}\n"
                    f"Notice days: "
                    f"{local_structured_facts['notice_days']}\n"
                    f"Maximum annual increases: "
                    f"{local_structured_facts['max_increases_per_12_months']}\n"
                    f"Applicability: "
                    f"{local_structured_facts['applicability']}\n"
                )

                excerpt = (
                    local_summary
                    + "\n"
                    + excerpt
                )

            remaining = OFFICIAL_BUNDLE_MAX_CHARS - total_chars

            if remaining <= 0:
                break

            excerpt = excerpt[:remaining].strip()

            if len(excerpt) < OFFICIAL_SOURCE_MIN_CHARS:
                print(
                    f"[Official Source] Skipped insufficient content "
                    f"({len(excerpt)} characters): {source_url}"
                )
                continue

            results.append({
                "url": source_url,
                "excerpt": excerpt,
                "characters": len(excerpt),
                "structured_facts": structured_facts,
                "local_structured_facts": (
                    local_structured_facts
                ),
            })

            total_chars += len(excerpt)

            print(
                f"[Official Source] Loaded {len(excerpt)} characters: "
                f"{source_url}"
            )

        except Exception as exc:
            print(
                f"[Official Source] Skipped {source_url}: "
                f"{type(exc).__name__}: {exc}"
            )

    if not results:
        raise RuntimeError(
            "No approved official sources could be downloaded."
        )

    print(
        f"[Official Sources] Bundle contains {len(results)} sources "
        f"and {total_chars} total characters."
    )

    return results


def extract_research_json(response_text: str) -> dict:
    """Extract one JSON object, including responses wrapped in JSON fences."""
    cleaned = str(response_text or "").strip()

    cleaned = re.sub(
        r"^```(?:json)?\\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start < 0 or end <= start:
        raise ValueError("Anthropic response did not contain a JSON object.")

    return json.loads(cleaned[start:end + 1])


def official_source_url(
    source_url: str,
    allowed_domains: list,
) -> bool:
    """Allow only URLs hosted by the jurisdiction's approved domains."""
    try:
        hostname = (
            urlparse(str(source_url)).hostname or ""
        ).lower()

        return any(
            hostname == domain
            or hostname.endswith("." + domain)
            for domain in allowed_domains
        )
    except Exception:
        return False


def research_jurisdiction_profile(
    client,
    db,
    jurisdiction: dict,
) -> dict:
    """
    Analyze locally downloaded official government excerpts.

    Anthropic web search is not used. Valid cached jurisdiction
    research is returned without downloading pages or using tokens.
    """
    if jurisdiction.get("status") != "READY_FOR_RESEARCH":
        raise ValueError(
            jurisdiction.get("warning")
            or "Jurisdiction is not approved for automatic research."
        )

    jurisdiction_key = jurisdiction["jurisdiction_key"]
    jurisdiction_name = jurisdiction["jurisdiction_name"]
    allowed_domains = jurisdiction["allowed_domains"]

    cached = get_cached_jurisdiction_research(
        db,
        jurisdiction_key,
    )

    if cached:
        print(
            f"[Legal Research] Cache used for "
            f"{jurisdiction_name} — zero Anthropic tokens."
        )
        return cached

    # Download, clean, filter, and limit official webpages locally.
    # This stage uses no Anthropic tokens.
    source_bundle = fetch_jurisdiction_source_bundle(
        jurisdiction
    )

    source_material = "\n\n".join(
        (
            f"OFFICIAL SOURCE {index}\n"
            f"URL: {source['url']}\n"
            f"EXCERPT:\n{source['excerpt']}"
        )
        for index, source in enumerate(
            source_bundle,
            start=1,
        )
    )

    research_date = date.today().isoformat()

    prompt = f"""
Analyze only the official government excerpts supplied below.

Jurisdiction: {jurisdiction_name}
Research date: {research_date}

Determine the residential rent-increase and rent-control rules for
this jurisdiction. Do not use outside knowledge or sources.

Do not assume any property-specific facts, including year built,
certificate-of-occupancy date, number of units, property type,
ownership structure, entity type, owner occupancy, condominium
status, exemption notices, or prior rent increases.

A Housing Authority source may describe Housing Choice Voucher
processing. Do not generalize voucher-specific policies to all
rental properties unless another supplied official source supports
that conclusion.

Return ONLY one concise valid JSON object with these exact keys:

{{
  "jurisdiction_name": "",
  "rent_control": "",
  "governing_regime": "",
  "current_increase_cap": "",
  "current_cap_effective_dates": "",
  "next_increase_cap": "",
  "next_cap_effective_dates": "",
  "ab_1482_general_rule": "",
  "notice_days": "",
  "max_increases_per_12_months": "",
  "source_urls": [],
  "notes_manager_to_confirm": "",
  "confidence": ""
}}

Requirements:
- "Current" means the cap applicable to a rent increase whose
  effective date is {research_date}.
- "Next" means a cap whose effective date begins after {research_date}.
- When an official table contains multiple effective-date columns,
  select the applicable column using the rent increase effective date,
  not the webpage publication date or CPI publication date.
- Use "Not established by supplied sources" when the excerpts do
  not establish a conclusion.
- For a non-California jurisdiction, state that AB 1482 does not
  apply.
- Keep every field concise enough for a property-management sheet.
- Do not write a legal memorandum.
- Do not include markdown outside the JSON.
- List only URLs supplied below.

OFFICIAL GOVERNMENT EXCERPTS:

{source_material}
""".strip()

    # Conservative local estimate: approximately four characters
    # per token. No API call is needed for this preflight check.
    estimated_input_tokens = max(
        1,
        (len(prompt) + 3) // 4,
    )

    if (
        estimated_input_tokens
        > RESEARCH_MAX_ESTIMATED_INPUT_TOKENS
    ):
        raise RuntimeError(
            "Research prompt was stopped before Anthropic use: "
            f"estimated {estimated_input_tokens} input tokens exceeds "
            f"the {RESEARCH_MAX_ESTIMATED_INPUT_TOKENS}-token limit."
        )

    response = client.with_options(
        timeout=RESEARCH_REQUEST_TIMEOUT_SECONDS,
        max_retries=RESEARCH_MAX_RETRIES,
    ).messages.create(
        model=RESEARCH_MODEL,
        max_tokens=RESEARCH_MAX_OUTPUT_TOKENS,
        temperature=0,
        messages=[{
            "role": "user",
            "content": prompt,
        }],
    )

    usage = response.usage
    input_tokens = int(
        getattr(usage, "input_tokens", 0) or 0
    )
    output_tokens = int(
        getattr(usage, "output_tokens", 0) or 0
    )

    log_anthropic_usage(
        db=db,
        feature="JURISDICTION_LOCAL_SOURCE_ANALYSIS",
        property_address="",
        jurisdiction_key=jurisdiction_key,
        model=RESEARCH_MODEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        web_search_requests=0,
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Anthropic output reached the token limit before "
            "completing the legal-research JSON."
        )

    response_text = "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", "") == "text"
    )

    research = extract_research_json(response_text)

    oag_facts = next(
        (
            source.get("structured_facts")
            for source in source_bundle
            if source.get("structured_facts")
        ),
        {},
    )

    if oag_facts:
        research["current_increase_cap"] = (
            oag_facts.get("current_cap")
            or "Not established by supplied sources"
        )
        research["current_cap_effective_dates"] = (
            oag_facts.get("current_period")
            or "Not established by supplied sources"
        )
        research["next_increase_cap"] = (
            oag_facts.get("next_cap")
            or "Not established by supplied sources"
        )
        research["next_cap_effective_dates"] = (
            oag_facts.get("next_period")
            or "Not established by supplied sources"
        )

        if oag_facts.get("current_end"):
            research["cache_valid_until"] = (
                oag_facts["current_end"]
            )

    local_rso_facts = next(
        (
            source.get("local_structured_facts")
            for source in source_bundle
            if source.get("local_structured_facts")
        ),
        {},
    )

    if (
        jurisdiction_key == "ca_los_angeles"
        and local_rso_facts
    ):
        rso_cap = local_rso_facts.get(
            "current_cap",
            "",
        )
        rso_period = local_rso_facts.get(
            "current_period",
            "",
        )
        statewide_cap = oag_facts.get(
            "current_cap",
            "",
        )
        statewide_period = oag_facts.get(
            "current_period",
            "",
        )
        statewide_next_cap = oag_facts.get(
            "next_cap",
            "",
        )
        statewide_next_period = oag_facts.get(
            "next_period",
            "",
        )

        research["rent_control"] = (
            "Property-specific: City of Los Angeles RSO "
            "if covered; otherwise JCO and applicable "
            "California state protections."
        )

        research["governing_regime"] = (
            "LA RSO if the unit is RSO-covered; otherwise "
            "City of Los Angeles JCO plus AB 1482 when applicable."
        )

        research["current_increase_cap"] = (
            f"{rso_cap} if RSO-covered"
            + (
                f"; {statewide_cap} if non-RSO and "
                "AB 1482-covered"
                if statewide_cap
                else ""
            )
        )

        research["current_cap_effective_dates"] = (
            f"RSO: {rso_period}"
            + (
                f"; AB 1482: {statewide_period}"
                if statewide_period
                else ""
            )
        )

        research["next_increase_cap"] = (
            f"{rso_cap} if RSO-covered"
            + (
                f"; {statewide_next_cap} if non-RSO and "
                "AB 1482-covered"
                if statewide_next_cap
                else ""
            )
        )

        research["next_cap_effective_dates"] = (
            f"RSO remains {rso_cap} through "
            f"{local_rso_facts.get('current_end', '')}"
            + (
                f"; AB 1482: {statewide_next_period}"
                if statewide_next_period
                else ""
            )
        )

        if local_rso_facts.get("notice_days"):
            research["notice_days"] = (
                local_rso_facts["notice_days"]
            )

        research["max_increases_per_12_months"] = (
            "1 if RSO-covered; verify prior increases and "
            "applicable frequency if governed by AB 1482."
        )

        existing_notes = str(
            research.get(
                "notes_manager_to_confirm",
                "",
            )
        ).strip()

        rso_note = (
            "Manager: Confirm whether this property/unit is "
            "registered and covered by the City of Los Angeles "
            "RSO. Do not choose the RSO or AB 1482 cap solely "
            "from the mailing address."
        )

        research["notes_manager_to_confirm"] = (
            f"{existing_notes} {rso_note}"
        ).strip()

        if local_rso_facts.get("current_end"):
            research["cache_valid_until"] = min(
                [
                    value
                    for value in (
                        research.get("cache_valid_until", ""),
                        local_rso_facts["current_end"],
                    )
                    if value
                ]
            )

    required_keys = {
        "jurisdiction_name",
        "rent_control",
        "governing_regime",
        "current_increase_cap",
        "current_cap_effective_dates",
        "next_increase_cap",
        "next_cap_effective_dates",
        "ab_1482_general_rule",
        "notice_days",
        "max_increases_per_12_months",
        "source_urls",
        "notes_manager_to_confirm",
        "confidence",
    }

    missing_keys = sorted(
        required_keys - set(research)
    )

    if missing_keys:
        raise ValueError(
            "Research JSON is missing keys: "
            + ", ".join(missing_keys)
        )

    bundle_urls = [
        source["url"]
        for source in source_bundle
        if official_source_url(
            source["url"],
            allowed_domains,
        )
    ]

    if not bundle_urls:
        raise ValueError(
            "No validated official source URLs remain."
        )

    # Do not trust or pay to validate URLs generated by the model.
    # Store the exact official URLs Rachel actually downloaded.
    research["source_urls"] = bundle_urls
    research["last_verified"] = research_date
    research["jurisdiction_key"] = jurisdiction_key
    research["estimated_input_tokens"] = (
        estimated_input_tokens
    )

    save_jurisdiction_research(
        db=db,
        jurisdiction_key=jurisdiction_key,
        jurisdiction_name=jurisdiction_name,
        research=research,
    )

    print(
        f"[Legal Research] Completed {jurisdiction_name}: "
        f"{input_tokens} input tokens, "
        f"{output_tokens} output tokens, "
        "0 Anthropic web searches."
    )

    return research


class RachelLeaseTracker:
    def __init__(self):
        anthropic_api_key = get_secret(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        )

        if not anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY was not found in the "
                "environment or macOS Keychain."
            )

        self.client = Anthropic(
            api_key=anthropic_api_key
        )
        self.today  = date.today()
        self.db     = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        ensure_occupancy_classifications(self.db)
        ensure_research_tracking(self.db)
        self.rc_map = load_rent_control(RENT_CONTROL_SHEET_ID)
        self.past_end_alerts  = []
        self.renewal_alerts   = []
        self.mtm_alerts       = []
        self.insurance_alerts = []

    def run(self):
        print(f"\n[Rachel Lease Tracker] Running — {self.today}")

        # Keep the master property-facts sheet synchronized before
        # calculating renewals and rent increases.
        #
        # This workflow may:
        #   1. Add active AppFolio properties missing from the sheet.
        #   2. Research rows controlled by Rachel.
        #   3. Fill blank property-fact fields and update research status.
        #
        # Rows marked as human-verified are excluded from automated
        # research updates. Rachel does not delete rows or erase the
        # approved historical record.
        sheet_writes_allowed, sheet_write_reason = (
            property_fact_sheet_write_allowed()
        )
        if sheet_writes_allowed:
            try:
                added_properties = sync_missing_properties_to_sheet()
                print(
                    "[Rent Control Sync] Missing-property sync complete: "
                    f"{len(added_properties)} added."
                )
            except Exception as exc:
                print(
                    "[Rent Control Sync] WARNING: Could not synchronize "
                    f"missing properties: {exc}"
                )

            try:
                research_results = research_and_update_pending_sheet_rows(
                    client=self.client,
                )
                researched_count = len(
                    research_results.get("researched", [])
                )
                print(
                    "[Rent Control Research] Automated research complete: "
                    f"{researched_count} researched."
                )
            except Exception as exc:
                print(
                    "[Rent Control Research] WARNING: Automated research "
                    f"could not complete: {exc}"
                )
        else:
            print(
                "[Rent Control Sync] Property-facts sheet writes blocked: "
                f"{sheet_write_reason}"
            )

        # Reload the sheet after synchronization and research so Manager's
        # consolidated email uses the latest approved/researched data.
        try:
            self.rc_map = load_rent_control(
                RENT_CONTROL_SHEET_ID
            )
            print(
                "[Rent Control Sync] Latest master-sheet data loaded "
                "for lease and rent-increase analysis."
            )
        except Exception as exc:
            print(
                "[Rent Control Sync] WARNING: Could not reload the "
                f"master sheet; using previously loaded data: {exc}"
            )

        self._collect_past_lease_end()
        self._collect_lease_renewals()
        self._collect_mtm_increases()
        self._collect_insurance()
        return self._send_consolidated_email()

    def _collect_past_lease_end(self):
        rows = self.db.execute("""
            SELECT t.*
            FROM tenants t
            WHERE t.past_lease_end = 1
              AND t.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM occupancy_classifications oc
                  WHERE oc.occupancy_id = t.occupancy_id
                    AND oc.exclude_from_lease_reports = 1
              )
        """).fetchall()
        for t in rows:
            try:
                end = datetime.strptime(t["lease_end"], "%Y-%m-%d").date()
                days_past = (self.today - end).days
                unit_str = f" {t['unit']}" if t["unit"] else ""
                self.past_end_alerts.append({
                    "tenant": t["primary_tenant"],
                    "property": t["property_address"] + unit_str,
                    "unit": t["unit"] or "",
                    "ended": end.strftime("%B %d, %Y"),
                    "days_past": days_past,
                })
            except (ValueError, TypeError):
                continue

    def _collect_lease_renewals(self):
        rows = self.db.execute("""
            SELECT t.*
            FROM tenants t
            WHERE t.lease_type = 'fixed'
              AND t.lease_end IS NOT NULL
              AND t.past_lease_end = 0
              AND t.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM occupancy_classifications oc
                  WHERE oc.occupancy_id = t.occupancy_id
                    AND oc.exclude_from_lease_reports = 1
              )
        """).fetchall()
        for t in rows:
            try:
                end = datetime.strptime(t["lease_end"], "%Y-%m-%d").date()
                days_out = (end - self.today).days
                if not 0 <= days_out <= max(LEASE_NOTICE_WINDOWS):
                    continue
                rc = get_rc(self.rc_map, t["property_address"])
                unit_str = f" {t['unit']}" if t["unit"] else ""
                self.renewal_alerts.append({
                    "tenant": t["primary_tenant"],
                    "property": t["property_address"] + unit_str,
                    "unit": t["unit"] or "",
                    "lease_end": end.strftime("%B %d, %Y"),
                    "days_out": days_out,
                    "rent": t["current_rent"],
                    "regime": rc["regime"] if rc else "Unknown",
                    "notice_days": rc["notice_days"] if rc else 30,
                })
            except (ValueError, TypeError):
                continue

    def _collect_mtm_increases(self):
        rows = self.db.execute("""
            SELECT t.*
            FROM tenants t
            WHERE t.lease_type = 'month-to-month'
              AND t.eligible_increase_on IS NOT NULL
              AND t.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM occupancy_classifications oc
                  WHERE oc.occupancy_id = t.occupancy_id
                    AND oc.exclude_from_lease_reports = 1
              )
        """).fetchall()
        for t in rows:
            try:
                eligible = datetime.strptime(
                    t["eligible_increase_on"], "%Y-%m-%d").date()
                days_out = (eligible - self.today).days
                if days_out > max(MTM_NOTICE_WINDOWS):
                    continue
                rc = get_rc(self.rc_map, t["property_address"])
                unit_str = f" {t['unit']}" if t["unit"] else ""
                self.mtm_alerts.append({
                    "tenant": t["primary_tenant"],
                    "property": t["property_address"] + unit_str,
                    "unit": t["unit"] or "",
                    "eligible": eligible.strftime("%B %d, %Y"),
                    "days_out": days_out,
                    "rent": t["current_rent"],
                    "regime": rc["regime"] if rc else "Unknown",
                    "notice_days": rc["notice_days"] if rc else 30,
                    "max_per_year": rc["max_per_year"] if rc else 1,
                })
            except (ValueError, TypeError):
                continue

    def _collect_insurance(self):
        cutoff = (self.today + timedelta(days=60)).isoformat()
        policies = self.db.execute("""
            SELECT ip.*, t.property_address, t.unit, t.primary_tenant
            FROM insurance_policies ip
            JOIN tenants t ON ip.occupancy_id = t.occupancy_id
            WHERE ip.expiration_date <= ?
              AND ip.status != 'RESOLVED'
              AND t.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM occupancy_classifications oc
                  WHERE oc.occupancy_id = t.occupancy_id
                    AND oc.exclude_from_lease_reports = 1
              )
        """, (cutoff,)).fetchall()

        seen = set()
        for p in policies:
            key = (p["occupancy_id"], p["expiration_date"])
            if key in seen:
                continue
            seen.add(key)

            unit_str = f" {p['unit']}" if p["unit"] else ""
            try:
                exp_date = datetime.strptime(p["expiration_date"], "%Y-%m-%d").date()
                exp_str = exp_date.strftime("%B %d, %Y")
                days_out = (exp_date - self.today).days
            except Exception:
                exp_str = p["expiration_date"]
                days_out = 0

            if days_out < 0:
                urgency = f"EXPIRED {abs(days_out)} days ago"
            elif days_out == 0:
                urgency = "EXPIRES TODAY"
            elif days_out <= 14:
                urgency = f"expires in {days_out} days — URGENT"
            else:
                urgency = f"expires in {days_out} days"

            self.insurance_alerts.append({
                "tenant": p["primary_tenant"],
                "property": p["property_address"] + unit_str,
                "unit": p["unit"] or "",
                "policyholder": p["policyholder"] or p["primary_tenant"],
                "provider": p["provider"] or "Unknown",
                "policy_number": p["policy_number"] or "",
                "expiration": exp_str,
                "days_out": days_out,
                "urgency": urgency,
                "status": p["status"],
            })

    def _send_consolidated_email(self):
        total = (len(self.past_end_alerts) + len(self.renewal_alerts) +
                 len(self.mtm_alerts) + len(self.insurance_alerts))

        if total == 0:
            print("[Tracker] No alerts today — no email sent")
            return True

        today_str = self.today.strftime("%B %d, %Y")
        lines = []
        lines.append("Hi Manager,")
        lines.append("")
        lines.append(f"Here is your lease & rent increase summary for {today_str}.")
        lines.append(f"Total items requiring attention: {total}")
        lines.append("")
        lines.append("=" * 60)
        lines.append("HOW TO REPLY — add your action at the end of each tenant line:")
        lines.append("")
        lines.append("  Examples:")
        lines.append("    — tenant is vacating [date optional]")
        lines.append("    — lease has been renewed")
        lines.append("    — convert to M2M")
        lines.append("    — non-renewal notice sent")
        lines.append("    — insurance resolved")
        lines.append("    — no action needed")
        lines.append("=" * 60)
        lines.append("")

        def base_prop(prop_str):
            return re.split(r" [Uu]nit ", prop_str)[0].strip()

        by_property = {}
        for a in self.past_end_alerts:
            by_property.setdefault(base_prop(a["property"]), []).append(("past_end", a))
        for a in self.renewal_alerts:
            by_property.setdefault(base_prop(a["property"]), []).append(("renewal", a))
        for a in self.mtm_alerts:
            by_property.setdefault(base_prop(a["property"]), []).append(("mtm", a))
        for a in self.insurance_alerts:
            by_property.setdefault(base_prop(a["property"]), []).append(("insurance", a))

        for prop, alerts in sorted(by_property.items()):
            has_urgent = any(t == "past_end" for t, _ in alerts)
            emoji = "🚨" if has_urgent else "📋"
            short_prop = prop.split(",")[0]
            lines.append(f"{emoji} {short_prop}")
            lines.append("-" * 60)

            for atype, a in sorted(alerts, key=lambda x:
                    {"past_end": 0, "renewal": 1, "mtm": 2, "insurance": 3}[x[0]]):

                unit = a.get("unit", "")
                unit_str = f" (Unit {unit})" if unit else ""

                if atype == "past_end":
                    lines.append(f"  ⚠️  PAST LEASE END — {a['tenant']}{unit_str}")
                    lines.append(f"     Ended: {a['ended']} ({a['days_past']} days ago)")
                    lines.append(f"     Status: Holdover — no renewal on record —")
                    lines.append("")

                elif atype == "renewal":
                    rent_str = f"${a['rent']:,.2f}/mo" if a["rent"] else "unknown"
                    rc = get_rc(self.rc_map, a["property"])
                    inc = increase_line(rc, a["rent"])
                    lines.append(f"  📋 LEASE RENEWAL — {a['tenant']}{unit_str} ({a['days_out']} days)")
                    lines.append(f"     Ends: {a['lease_end']} | Rent: {rent_str}")
                    lines.append(f"     Regime: {a['regime']} | Max increase: {inc}")
                    lines.append(f"     Notice required: {a['notice_days']} days —")
                    lines.append("")

                elif atype == "mtm":
                    rent_str = f"${a['rent']:,.2f}/mo" if a["rent"] else "unknown"
                    rc = get_rc(self.rc_map, a["property"])
                    inc = increase_line(rc, a["rent"])
                    lines.append(f"  💰 RENT INCREASE — {a['tenant']}{unit_str} ({a['days_out']} days)")
                    lines.append(f"     Rent: {rent_str} | Eligible: {a['eligible']}")
                    lines.append(f"     Regime: {a['regime']} | Max increase: {inc}")
                    lines.append(f"     Max {a['max_per_year']}x/year | {a['notice_days']} days notice —")
                    lines.append("")

                elif atype == "insurance":
                    lines.append(f"  🔒 INSURANCE — {a['tenant']}{unit_str}")
                    lines.append(f"     {a.get('urgency', a['expiration'])}")
                    lines.append(f"     Provider: {a['provider']}"
                                 + (f" #{a['policy_number']}" if a["policy_number"] else ""))
                    lines.append(f"     Expiration: {a['expiration']} —")
                    lines.append("")

            lines.append("")

        lines.append("— Rachel")
        lines.append("Assistant Property Manager")
        lines.append("ClientCo Property Management")
        lines.append("rachel@clientco.example")

        body = "\n".join(lines)

        parts = []
        if self.past_end_alerts:
            parts.append(f"{len(self.past_end_alerts)} past lease end")
        if self.renewal_alerts:
            parts.append(f"{len(self.renewal_alerts)} renewal")
        if self.mtm_alerts:
            parts.append(f"{len(self.mtm_alerts)} rent increase")
        if self.insurance_alerts:
            parts.append(f"{len(self.insurance_alerts)} insurance")

        emoji = "🚨" if self.past_end_alerts else "📋"
        subject = f"{emoji} Rachel Alert — {today_str}: {', '.join(parts)}"

        print(f"\n[Tracker] Sending 1 consolidated email to Manager + Owner...")
        print(f"[Tracker] Subject: {subject}")
        print(f"[Tracker] Total alerts: {total}")
        delivered = bool(send_email(subject, body))
        # Only a verified SMTP acceptance completes the monthly job. A dry
        # run must remain pending so restart catch-up can deliver it after the
        # gate is explicitly enabled.
        return delivered

    def generate_weekly_digest(self) -> str:
        upcoming = []
        cutoff = (self.today + timedelta(days=90)).isoformat()

        for row in self.db.execute("""
            SELECT property_address, unit, primary_tenant, lease_end, current_rent
            FROM tenants t
            WHERE t.lease_end BETWEEN ? AND ?
              AND t.past_lease_end = 0
              AND t.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM occupancy_classifications oc
                  WHERE oc.occupancy_id = t.occupancy_id
                    AND oc.exclude_from_lease_reports = 1
              )
            ORDER BY t.lease_end
        """, (self.today.isoformat(), cutoff)).fetchall():
            try:
                days = (datetime.strptime(
                    row["lease_end"], "%Y-%m-%d").date() - self.today).days
                upcoming.append({
                    "type": "LEASE_END",
                    "property": row["property_address"],
                    "tenant": row["primary_tenant"],
                    "date": row["lease_end"],
                    "days_out": days,
                    "rent": row["current_rent"]
                })
            except Exception:
                continue

        for row in self.db.execute("""
            SELECT property_address, unit, primary_tenant,
                   eligible_increase_on, current_rent
            FROM tenants t
            WHERE t.eligible_increase_on BETWEEN ? AND ?
              AND t.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM occupancy_classifications oc
                  WHERE oc.occupancy_id = t.occupancy_id
                    AND oc.exclude_from_lease_reports = 1
              )
            ORDER BY t.eligible_increase_on
        """, (self.today.isoformat(), cutoff)).fetchall():
            try:
                days = (datetime.strptime(
                    row["eligible_increase_on"], "%Y-%m-%d").date() - self.today).days
                upcoming.append({
                    "type": "RENT_INCREASE_ELIGIBLE",
                    "property": row["property_address"],
                    "tenant": row["primary_tenant"],
                    "date": row["eligible_increase_on"],
                    "days_out": days,
                    "rent": row["current_rent"]
                })
            except Exception:
                continue

        for row in self.db.execute("""
            SELECT t.*
            FROM tenants t
            WHERE t.past_lease_end = 1
              AND t.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM occupancy_classifications oc
                  WHERE oc.occupancy_id = t.occupancy_id
                    AND oc.exclude_from_lease_reports = 1
              )
        """).fetchall():
            try:
                days_past = (self.today - datetime.strptime(
                    row["lease_end"], "%Y-%m-%d").date()).days
                upcoming.append({
                    "type": "PAST_LEASE_END",
                    "property": row["property_address"],
                    "tenant": row["primary_tenant"],
                    "date": row["lease_end"],
                    "days_out": -days_past
                })
            except Exception:
                continue

        if not upcoming:
            digest = (f"Good morning Manager! No lease or rent increase events "
                      f"in the next 90 days as of "
                      f"{self.today.strftime('%B %d, %Y')}. "
                      f"Have a great week!\n\n— Rachel\n"
                      f"Assistant Property Manager\nClientCo Property Management")
        else:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                messages=[{"role": "user", "content": f"""
You are Rachel, Assistant Property Manager at ClientCo Property Management.
Today is {self.today.strftime('%A, %B %d, %Y')}.

Write a concise Monday morning digest for Manager about upcoming
lease and rent increase events. Group by urgency: past-end first,
then 30-day, 60-day, 90-day windows. Be direct and actionable.
Under 400 words. Sign as Rachel, Assistant Property Manager.

Events:
{json.dumps(upcoming, indent=2)}
"""}]
            )
            digest = response.content[0].text.strip()

        subject = f"📋 Rachel Weekly Digest — {self.today.strftime('%B %d, %Y')}"
        send_email(subject, digest)
        return digest


if __name__ == "__main__":
    import sys
    tracker = RachelLeaseTracker()
    if "--digest" in sys.argv:
        print("\n=== MONDAY DIGEST ===\n")
        print(tracker.generate_weekly_digest())
    else:
        raise SystemExit(0 if tracker.run() else 1)
