"""
Rachel - AppFolio Scraper v3.1 (Final Architecture)
Uses two pre-configured saved report URLs. No individual page visits needed.

Data sources:
  1. Tenant Directory report — ALL data in one CSV:
     tenant name, property, unit, lease dates, rent, eligible increase date,
     last increase, next increase, insurance provider/expiration/policy number
  2. Rent Roll — occupancy IDs only (t_NNN links for database keying)

Saved report URLs (permanent):
  TENANT_DIR_URL  = .../32e3650d-7260-11f1-a851-0a10eb9173f5
  LEASE_EXPIRY_URL = .../9f196c69-725e-11f1-a851-0a10eb9173f5  (backup/digest use)
  RENT_ROLL_URL   = .../buffered_reports/rent_roll

Run with:
  python3 rachel_scraper_v2.py           (visible, test mode)
  python3 rachel_scraper_v2.py --headless (production)
"""

import os, re, time, random, sqlite3, json, csv, io, subprocess
from datetime import datetime, date
import sys
from rachel_appfolio_browser import (
    get_appfolio_browser_manager,
    get_downloads_dir,
)
from rachel_appfolio_ui import (
    AppFolioReadAdapter,
    AppFolioUIContractError,
    RentRollPage,
    ReportExportPage,
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

APPFOLIO_URL = os.getenv(
    "APPFOLIO_URL",
    "https://clientco.appfolio.com"
)
APPFOLIO_EMAIL = get_secret("APPFOLIO_EMAIL", "APPFOLIO_EMAIL")
APPFOLIO_PASS = get_secret("APPFOLIO_PASSWORD", "APPFOLIO_PASSWORD")
DB_PATH        = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")
MANAGER_EMAIL   = os.getenv("MANAGER_EMAIL", "manager@clientco.example")
HEADLESS       = "--headless" in sys.argv or bool(os.getenv("RENDER"))

def get_appfolio_2fa_code(max_wait=60):
    """
    Read the AppFolio 2FA code from rachel.clientco@example.com via IMAP.
    Uses Gmail App Password — no OAuth needed.
    
    AppFolio sends SMS to Google Voice (747) 224-6984
    which forwards as email to rachel.clientco@example.com
    from: *@txt.voice.google.com
    body: "XXXXXX Use this code to verify your AppFolio account"
    """
    import imaplib
    import email
    from email.header import decode_header

    gmail_user = get_secret("RACHEL_GMAIL_USER", "RACHEL_GMAIL_USER")
    gmail_pass = get_secret("RACHEL_GMAIL_PASSWORD", "RACHEL_GMAIL_PASSWORD")

    if not gmail_pass:
        print("[2FA] No Gmail app password found in environment variables or Keychain")
        return None

    print("[2FA] Watching rachel.clientco@example.com for AppFolio 2FA code...")

    for attempt in range(max_wait // 3):
        time.sleep(3)
        try:
            # Connect via IMAP
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(gmail_user, gmail_pass)
            mail.select("inbox")

            # Search for recent Google Voice emails
            status, messages = mail.search(
                None, 'FROM', '"txt.voice.google.com"', 'UNSEEN'
            )

            if status == "OK" and messages[0]:
                msg_ids = messages[0].split()
                # Check most recent first
                for msg_id in reversed(msg_ids[-5:]):
                    status, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if status != "OK":
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])

                    # Get body
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(
                                    "utf-8", errors="ignore")
                    else:
                        body = msg.get_payload(decode=True).decode(
                            "utf-8", errors="ignore")

                    # Extract 6-digit AppFolio code
                    code_match = re.search(
                        r"(\d{6})\s+Use this code to verify your AppFolio",
                        body
                    )
                    if code_match:
                        code = code_match.group(1)
                        # Mark as read
                        mail.store(msg_id, "+FLAGS", "\\Seen")
                        mail.logout()
                        print("[2FA] Verification code received")
                        return code

            mail.logout()

        except Exception as e:
            print(f"[2FA] IMAP error: {e}")

        if attempt % 5 == 4:
            print(f"[2FA] Still waiting... ({(attempt+1)*3}s)")

    print("[2FA] Timed out waiting for 2FA code")
    return None



TENANT_DIR_URL  = f"{APPFOLIO_URL}/buffered_reports/32e3650d-7260-11f1-a851-0a10eb9173f5"
WO_REPORT_URL   = f"{APPFOLIO_URL}/buffered_reports/45"
LEASE_EXPIRY_URL = f"{APPFOLIO_URL}/buffered_reports/9f196c69-725e-11f1-a851-0a10eb9173f5"
RENT_ROLL_URL   = f"{APPFOLIO_URL}/buffered_reports/rent_roll"

# Tenant Directory column indices
COL_PROPERTY   = 0
COL_UNIT       = 1
COL_TENANT     = 2
COL_STATUS     = 3
COL_TYPE       = 4   # Financially Responsible / Co-signer / Other Occupant
COL_MOVE_IN    = 7
COL_LEASE_TO   = 8
COL_RENT       = 9
COL_LEASE_FROM = 27
COL_ELIGIBLE   = 30  # Eligible for Rent Increase
COL_LAST_INC   = 31  # Last Rent Increase
COL_NEXT_INC   = 32  # Next Rent Increase Date
COL_INS_PROV   = 50  # Insurance Provider
COL_INS_EXP    = 51  # Insurance Expiration
COL_INS_POL    = 52  # Insurance Policy Number
COL_PRIMARY    = 54  # Primary Tenant (Yes/No)
COL_FIRST      = 55
COL_LAST       = 56


def human_delay(min_s=3, max_s=7):
    time.sleep(random.uniform(min_s, max_s))


def parse_date(val):
    if not val:
        return None
    # Handle datetime objects from openpyxl directly
    if hasattr(val, 'strftime'):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if s in ("", "--", "N/A", "None"):
        return None
    # Handle "2027-01-06 00:00:00" format from openpyxl
    if " 00:00:00" in s:
        s = s.replace(" 00:00:00", "")
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%m/%d/%y"]:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_currency(val):
    if not val:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(val)))
    except ValueError:
        return None


def clean_property(raw):
    """Strip 'Property Name - Full Address' duplicate format."""
    if " - " in raw:
        return raw.split(" - ", 1)[1].strip()
    return raw.strip()


def init_db(path):
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tenants (
            occupancy_id         TEXT PRIMARY KEY,
            property_address     TEXT,
            unit                 TEXT,
            primary_tenant       TEXT,
            all_tenants          TEXT,
            lease_type           TEXT,
            lease_start          TEXT,
            lease_end            TEXT,
            current_rent         REAL,
            eligible_increase_on TEXT,
            last_increase_date   TEXT,
            next_increase_date   TEXT,
            last_scraped         TEXT,
            past_lease_end       INTEGER DEFAULT 0,
            notes                TEXT,
            is_active            INTEGER NOT NULL DEFAULT 1,
            inactive_at          TEXT,
            inactive_reason      TEXT
        );
        CREATE TABLE IF NOT EXISTS tenant_history (
            history_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            occupancy_id         TEXT,
            property_address     TEXT,
            unit                 TEXT,
            primary_tenant       TEXT,
            all_tenants          TEXT,
            lease_type           TEXT,
            lease_start          TEXT,
            lease_end            TEXT,
            current_rent         REAL,
            eligible_increase_on TEXT,
            last_increase_date   TEXT,
            next_increase_date   TEXT,
            past_lease_end       INTEGER,
            notes                TEXT,
            source_last_scraped  TEXT,
            archived_at          TEXT NOT NULL,
            archive_reason       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tenant_history_occupancy
        ON tenant_history(occupancy_id);

        CREATE TABLE IF NOT EXISTS insurance_policies (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            occupancy_id     TEXT,
            policyholder     TEXT,
            policy_number    TEXT,
            expiration_date  TEXT,
            status           TEXT,
            provider         TEXT,
            last_scraped     TEXT
        );
        CREATE TABLE IF NOT EXISTS scrape_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date            TEXT,
            occupancies_found   INTEGER,
            occupancies_scraped INTEGER,
            errors              INTEGER,
            duration_seconds    REAL,
            notes               TEXT
        );
    """)

    # Safely upgrade an existing Rachel database without deleting data.
    tenant_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(tenants)")
    }

    required_columns = {
        "is_active": (
            "ALTER TABLE tenants "
            "ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        ),
        "inactive_at": (
            "ALTER TABLE tenants "
            "ADD COLUMN inactive_at TEXT"
        ),
        "inactive_reason": (
            "ALTER TABLE tenants "
            "ADD COLUMN inactive_reason TEXT"
        ),
    }

    for column_name, statement in required_columns.items():
        if column_name not in tenant_columns:
            conn.execute(statement)

    conn.commit()
    return conn


def login(page):
    """Compatibility wrapper around Rachel's one hardened login flow."""
    from rachel_wo_triage import login_appfolio

    return login_appfolio(page)


def export_report_to_excel(page, url, label):
    """
    Navigate to a report URL and export it via Actions -> Export as Excel.
    Returns path to the downloaded Excel file.
    Every AppFolio report has this button — avoids AG Grid rendering issues entirely.
    """
    downloads = get_downloads_dir()
    os.makedirs(downloads, exist_ok=True)

    print(f"[{label}] Loading {url[:60]}...")
    AppFolioReadAdapter(page, "report_export").navigate_read(
        url,
        route_pattern="/buffered_reports/:report",
        timeout=60000,
        attempts=2,
    )
    human_delay(4, 6)

    print(f"[{label}] Waiting for Actions button...")
    time.sleep(2)

    # Snapshot every preexisting file. A timed-out export must never fall back
    # to an older report and overwrite current durable tenant data with it.
    before_files = {
        os.path.join(downloads, filename)
        for filename in os.listdir(downloads)
    }

    # Try auto-click first
    auto_clicked = False
    report_page = ReportExportPage(page)
    try:
        actions = report_page.actions()
        if not actions:
            raise AppFolioUIContractError(
                "report_export", "control_discovery",
                "actions_control_absent",
            )
        actions.click()
        time.sleep(2)

        # Print only the bounded menu labels already visible in the UI.
        visible_texts = report_page.visible_menu_texts()
        print(f"[{label}] Dropdown items visible: {visible_texts[:20]}")

        export_control = report_page.export_excel()
        if not export_control:
            raise AppFolioUIContractError(
                "report_export", "control_discovery",
                "excel_export_control_absent",
            )
        export_control.click()
        print(f"[{label}] Clicked verified Excel export control")
        auto_clicked = True
    except AppFolioUIContractError:
        raise
    except Exception as e:
        raise AppFolioUIContractError(
            "report_export", "control_interaction",
            f"unexpected_{type(e).__name__}",
        ) from e

    if not auto_clicked:
        raise AppFolioUIContractError(
            "report_export", "control_interaction", "export_not_started"
        )

    # Watch for new file — including extensionless files (Playwright behavior)
    print(f"[{label}] Watching for downloaded file...")
    max_wait = 60
    for i in range(max_wait):
        time.sleep(1)
        for f in os.listdir(downloads):
            full_path = os.path.join(downloads, f)
            if full_path in before_files:
                continue
            if not os.path.isfile(full_path):
                continue
            # AppFolio's Excel export is either .xlsx or an extensionless
            # Playwright download. CSV is intentionally rejected because
            # the downstream parser requires a real Excel workbook.
            is_xlsx = f.lower().endswith(".xlsx")
            is_extensionless = (
                "." not in f and os.path.getsize(full_path) > 10000
            )
            if is_xlsx or is_extensionless:
                time.sleep(2)
                # Rename extensionless files so openpyxl can read them
                if is_extensionless:
                    new_path = full_path + ".xlsx"
                    os.rename(full_path, new_path)
                    full_path = new_path
                    f = f + ".xlsx"
                print(f"[{label}] File detected: {f}")
                return full_path
        if i % 10 == 9:
            print(f"[{label}] Still waiting... ({i+1}s)")
            if not auto_clicked:
                print(f"  Click Actions -> Export as Excel in the browser")

    print(f"[{label}] Timed out — no new Excel export was verified")
    return None


def cleanup_temporary_export(path: str, label: str) -> bool:
    """Delete only an export inside Rachel's canonical download directory."""
    if not path:
        return True
    export_root = os.path.realpath(get_downloads_dir())
    resolved = os.path.realpath(path)
    try:
        in_export_root = os.path.commonpath(
            (export_root, resolved)
        ) == export_root
    except ValueError:
        in_export_root = False
    if not in_export_root:
        print(f"[{label}] Refused cleanup outside canonical export directory")
        return False
    if not os.path.exists(resolved):
        return True
    try:
        os.remove(resolved)
        print(f"[{label}] Deleted temporary AppFolio export")
        return True
    except OSError as error:
        print(
            f"[{label}] Temporary export cleanup failed: "
            f"{type(error).__name__}"
        )
        return False


def parse_tenant_directory_excel(xlsx_path):
    """
    Parse Tenant Directory Excel export from AppFolio.
    Column mapping confirmed from actual export:
      0=Property, 1=Unit, 2=Tenant, 3=Status, 4=Tenant Type,
      7=Move-in, 8=Lease To, 9=Rent, 27=Lease From,
      30=Eligible for Rent Increase, 31=Last Rent Increase,
      32=Next Rent Increase Date, 50=Insurance Provider,
      51=Insurance Expiration, 52=Insurance Policy Number,
      54=Primary Tenant
    """
    print(f"[TenantDir] xlsx_path = {xlsx_path}")


    # Dependencies are pinned and installed during the image build. Never
    # mutate the runtime environment or download packages from a live worker.
    import openpyxl

    print(f"[TenantDir] Parsing Excel file: {xlsx_path}")
    try:
        wb = openpyxl.load_workbook(xlsx_path)
    except Exception as exc:
        print(
            "[TenantDir] Export is not a readable Excel workbook: "
            f"{type(exc).__name__}"
        )
        return {}

    ws = wb.active

    # Find header row — has "Property" and "Tenant" in first columns
    header_row_idx = None
    col_map = {}
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True), 1):
        if row[0] and "Property" in str(row[0]) and len([c for c in row if c]) > 10:
            header_row_idx = i
            col_map = {str(v).strip(): idx
                       for idx, v in enumerate(row) if v}
            print(f"[TenantDir] Header at row {i}, {len(col_map)} columns")
            break

    if not header_row_idx:
        print("[TenantDir] Could not find header row in Excel")
        wb.close()
        return {}

    # Parse rows grouped by unit
    units = {}  # (property, unit) -> list of rows

    for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
        if not row or not row[0]:
            continue

        prop_raw = str(row[col_map.get("Property", 0)] or "").strip()
        unit     = str(row[col_map.get("Unit", 1)] or "").strip()
        tenant   = str(row[col_map.get("Tenant", 2)] or "").strip()
        status   = str(row[col_map.get("Status", 3)] or "").strip()
        t_type   = str(row[col_map.get("Tenant Type", 4)] or "").strip()
        primary  = str(row[col_map.get("Primary Tenant", 54)] or "").strip()

        if not prop_raw or not tenant or tenant in ("Tenant", "Total", ""):
            continue
        if status not in ("Current", "Notice"):
            continue

        prop = clean_property(prop_raw)
        key = (prop, unit)
        if key not in units:
            units[key] = []
        units[key].append({
            "row": row,
            "tenant": tenant,
            "primary": primary.lower() == "yes",
            "t_type": t_type,
            "col_map": col_map,
        })

    # Build one record per unit
    records = {}
    for (prop, unit), group in units.items():
        primary = (next((t for t in group if t["primary"]), None) or
                   next((t for t in group
                         if t["t_type"] == "Financially Responsible"), None) or
                   group[0])

        row = primary["row"]
        cm  = primary["col_map"]

        # Hardcoded positions confirmed from actual Tenant Directory export
        POSITIONS = {
            "Lease To": 8, "Rent": 9, "Lease From": 27,
            "Eligible for Rent Increase": 30, "Last Rent Increase": 31,
            "Next Rent Increase Date": 32, "Insurance Provider": 50,
            "Insurance Expiration": 51, "Insurance Policy Number": 52,
        }

        def get_val(col_name, default=""):
            # Try name lookup first
            idx = cm.get(col_name, -1)
            if idx >= 0 and idx < len(row) and row[idx] is not None:
                return row[idx]
            # Fall back to hardcoded position
            pos = POSITIONS.get(col_name, -1)
            if pos >= 0 and pos < len(row) and row[pos] is not None:
                return row[pos]
            return default

        lease_from = parse_date(get_val("Lease From"))
        lease_to   = parse_date(get_val("Lease To"))
        rent       = parse_currency(str(get_val("Rent", "")))
        eligible   = parse_date(get_val("Eligible for Rent Increase"))
        last_inc   = parse_date(get_val("Last Rent Increase"))
        next_inc   = parse_date(get_val("Next Rent Increase Date"))
        ins_prov   = str(get_val("Insurance Provider", "")).strip()
        ins_exp    = parse_date(get_val("Insurance Expiration"))
        ins_pol    = str(get_val("Insurance Policy Number", "")).strip()

        # Flip name format
        tenant_raw = primary["tenant"]
        if "," in tenant_raw:
            parts = tenant_raw.split(",", 1)
            tenant_name = f"{parts[1].strip()} {parts[0].strip()}"
        else:
            tenant_name = tenant_raw

        # All tenant names
        all_names = []
        for t in group:
            n = t["tenant"]
            if "," in n:
                p = n.split(",", 1)
                all_names.append(f"{p[1].strip()} {p[0].strip()}")
            else:
                all_names.append(n)

        # Active tenants automatically become month-to-month
        # after their Lease To date has passed.
        past_end = 0
        is_mtm = not lease_to

        if lease_to:
            try:
                lease_end_date = datetime.strptime(
                    lease_to,
                    "%Y-%m-%d",
                ).date()

                is_mtm = lease_end_date < date.today()

            except ValueError:
                is_mtm = False

        ins_status = ""
        if ins_exp:
            try:
                ins_status = ("EXPIRED" if datetime.strptime(
                    ins_exp, "%Y-%m-%d").date() < date.today() else "ACTIVE")
            except Exception:
                ins_status = "UNKNOWN"

        records[(prop, unit)] = {
            "property_address": prop,
            "unit": unit,
            "primary_tenant": tenant_name,
            "all_tenants": json.dumps(all_names),
            "lease_type": "month-to-month" if is_mtm else "fixed",
            "lease_start": lease_from,
            "lease_end": lease_to,
            "current_rent": rent,
            "eligible_increase_on": eligible,
            "last_increase_date": last_inc,
            "next_increase_date": next_inc,
            "past_lease_end": past_end,
            "insurance": {
                "provider": ins_prov,
                "expiration": ins_exp,
                "policy_number": ins_pol,
                "status": ins_status,
                "policyholder": tenant_name,
            } if (ins_prov or ins_exp) else None,
        }

    print(f"[TenantDir] Parsed {len(records)} unique units from Excel")
    wb.close()

    return records


def parse_tenant_directory(page_text):
    """
    Parse the Tenant Directory report page text.
    Returns dict keyed by (property, unit) -> tenant record.
    Groups co-tenants under the Primary Tenant.
    """
    print("[TenantDir] Parsing tenant directory...")

    # Find the CSV data in the page text
    # AppFolio renders the report as a table — extract via CSV parsing of page text
    lines = page_text.split("\n")

    # Find header row
    header_idx = None
    for i, line in enumerate(lines):
        if "Property" in line and "Tenant" in line and "Insurance" in line:
            header_idx = i
            break

    if header_idx is None:
        # Try to parse raw CSV from page
        print("[TenantDir] Parsing as raw CSV...")

    records = {}  # key: (clean_property, unit) -> record dict

    # Parse using csv module on the full page text
    try:
        reader = csv.reader(io.StringIO(page_text))
        rows = list(reader)
    except Exception as e:
        print(f"[TenantDir] CSV parse error: {e}")
        return {}

    # Find header row
    header_row = None
    header_row_idx = 0
    for i, row in enumerate(rows):
        if len(row) > 50 and "Property" in str(row[0]) and "Tenant" in str(row[2]):
            header_row = row
            header_row_idx = i
            break

    if not header_row:
        print("[TenantDir] Could not find header row")
        print(f"[TenantDir] First 3 rows: {rows[:3]}")
        return {}

    print(f"[TenantDir] Header found at row {header_row_idx}")

    # Map column names to indices dynamically
    col = {name.strip(): i for i, name in enumerate(header_row)}
    print(f"[TenantDir] Key columns: Property={col.get('Property')}, "
          f"Tenant={col.get('Tenant')}, "
          f"Eligible for Rent Increase={col.get('Eligible for Rent Increase')}, "
          f"Insurance Provider={col.get('Insurance Provider')}")

    tenants_by_unit = {}  # (property, unit) -> list of tenant rows

    for row in rows[header_row_idx + 1:]:
        if len(row) < 10:
            continue

        prop_raw = str(row[col.get("Property", 0)]).strip()
        unit     = str(row[col.get("Unit", 1)]).strip()
        tenant   = str(row[col.get("Tenant", 2)]).strip()
        status   = str(row[col.get("Status", 3)]).strip()
        t_type   = str(row[col.get("Tenant Type", 4)]).strip()
        primary  = str(row[col.get("Primary Tenant", 54)]).strip()

        if not prop_raw or not tenant or tenant == "Tenant":
            continue
        if status not in ("Current", "Notice"):
            continue

        # Clean property address
        prop = clean_property(prop_raw)

        key = (prop, unit)
        if key not in tenants_by_unit:
            tenants_by_unit[key] = []
        tenants_by_unit[key].append({
            "raw": row,
            "tenant": tenant,
            "primary": primary.lower() == "yes",
            "t_type": t_type,
            "status": status,
            "col": col,
        })

    # Build final records — one per unit, primary tenant as the key tenant
    records = {}
    for (prop, unit), group in tenants_by_unit.items():
        # Find primary tenant
        primary = next((t for t in group if t["primary"]), None)
        if not primary:
            # Fall back to Financially Responsible
            primary = next((t for t in group
                           if t["t_type"] == "Financially Responsible"), None)
        if not primary:
            primary = group[0]

        row = primary["raw"]
        col = primary["col"]

        def get(c_name, default=""):
            idx = col.get(c_name, -1)
            if idx < 0 or idx >= len(row):
                return default
            return str(row[idx]).strip()

        # Parse key fields
        lease_from = parse_date(get("Lease From"))
        lease_to   = parse_date(get("Lease To"))
        rent       = parse_currency(get("Rent"))
        eligible   = parse_date(get("Eligible for Rent Increase"))
        last_inc   = parse_date(get("Last Rent Increase"))
        next_inc   = parse_date(get("Next Rent Increase Date"))
        ins_prov   = get("Insurance Provider")
        ins_exp    = parse_date(get("Insurance Expiration"))
        ins_pol    = get("Insurance Policy Number")

        # Flip "Last, First" name format
        tenant_raw = primary["tenant"]
        if "," in tenant_raw:
            parts = tenant_raw.split(",", 1)
            tenant_name = f"{parts[1].strip()} {parts[0].strip()}"
        else:
            tenant_name = tenant_raw

        # All tenant names for this unit
        all_names = []
        for t in group:
            n = t["tenant"]
            if "," in n:
                p = n.split(",", 1)
                all_names.append(f"{p[1].strip()} {p[0].strip()}")
            else:
                all_names.append(n)

        # Active tenants automatically become month-to-month
        # after their Lease To date has passed.
        past_end = 0
        is_mtm = not lease_to

        if lease_to:
            try:
                lease_end_date = datetime.strptime(
                    lease_to,
                    "%Y-%m-%d",
                ).date()

                is_mtm = lease_end_date < date.today()

            except ValueError:
                is_mtm = False

        ins_status = ""
        if ins_exp:
            try:
                ins_status = ("EXPIRED" if datetime.strptime(
                    ins_exp, "%Y-%m-%d").date() < date.today() else "ACTIVE")
            except Exception:
                ins_status = "UNKNOWN"

        records[(prop, unit)] = {
            "property_address": prop,
            "unit": unit,
            "primary_tenant": tenant_name,
            "all_tenants": json.dumps(all_names),
            "lease_type": "month-to-month" if is_mtm else "fixed",
            "lease_start": lease_from,
            "lease_end": lease_to,
            "current_rent": rent,
            "eligible_increase_on": eligible,
            "last_increase_date": last_inc,
            "next_increase_date": next_inc,
            "past_lease_end": past_end,
            "insurance": {
                "provider": ins_prov,
                "expiration": ins_exp,
                "policy_number": ins_pol,
                "status": ins_status,
                "policyholder": tenant_name,
            } if ins_prov or ins_exp else None,
        }

    print(f"[TenantDir] Parsed {len(records)} unique units")
    return records


def get_occupancy_ids(page):
    """
    Get tenant name -> occupancy ID from rent roll via scroll accumulation.
    Collects IDs as rows appear while scrolling — never loses previously found ones.
    """
    print("[RentRoll] Getting occupancy IDs via scroll accumulation...")
    adapter = AppFolioReadAdapter(page, "portfolio_ingestion")
    adapter.navigate_read(
        RENT_ROLL_URL,
        route_pattern="/buffered_reports/:report",
        timeout=45000,
        attempts=2,
    )
    human_delay(4, 6)
    rent_roll = RentRollPage(page)

    id_map = {}  # tenant_name -> occupancy_id

    def collect():
        items = adapter.read(
            "rent_roll_ledger_items",
            rent_roll.ledger_items,
            attempts=3,
        )
        added = 0
        for item in items:
            href = item.get("href", "")
            name = item.get("text", "").strip()
            if not name or name == "Vacant-Unrented":
                continue
            m = re.search(r"party_ids(?:%5D|\[party_ids\])=t_(\d+)", href)
            if m:
                occupancy_id = m.group(1)
                if name not in id_map:
                    id_map[name] = occupancy_id
                    added += 1
                elif id_map[name] != occupancy_id:
                    # Duplicate display names cannot safely identify one
                    # occupancy. Preserve the ambiguity instead of silently
                    # binding every same-name tenant to the first ledger.
                    id_map[name] = None
        return added

    collect()
    print(f"[RentRoll] Initial: {len(id_map)} IDs")

    no_new = 0
    for _ in range(200):
        rent_roll.scroll_step()
        time.sleep(0.3)
        added = collect()
        if added > 0:
            no_new = 0
            print(f"[RentRoll] +{added} → {len(id_map)} total")
        else:
            no_new += 1
            if no_new >= 10:
                break

    print(f"[RentRoll] Done — {len(id_map)} occupancy IDs")
    return id_map


def match_occupancy_id(tenant_name, property_addr, id_map):
    """Match one tenant only when the ledger identity is unambiguous."""
    del property_addr  # The current rent-roll extract has no property field.

    def name_key(value):
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    normalized = name_key(tenant_name)
    if not normalized:
        return None
    parts = normalized.split()
    reversed_key = (
        name_key(f"{parts[-1]}, {' '.join(parts[:-1])}")
        if len(parts) >= 2 else ""
    )
    exact_ids = {
        str(occupancy_id)
        for ledger_name, occupancy_id in id_map.items()
        if occupancy_id
        and name_key(ledger_name) in {normalized, reversed_key}
    }
    if len(exact_ids) == 1:
        return exact_ids.pop()
    if len(exact_ids) > 1:
        return None

    # A last-name fallback is permitted only when it resolves to exactly one
    # ledger. Substring-first matching could attach a tenant to an unrelated
    # same/similar-name occupancy and is deliberately rejected.
    last_name = parts[-1]
    fallback_ids = {
        str(occupancy_id)
        for ledger_name, occupancy_id in id_map.items()
        if occupancy_id and last_name in name_key(ledger_name).split()
    }
    return fallback_ids.pop() if len(fallback_ids) == 1 else None


def save_record(conn, occ_id, record):
    """Save tenant and insurance records to database."""
    insurance = record.pop("insurance", None)
    next_inc   = record.pop("next_increase_date", None)

    conn.execute("""
        INSERT INTO tenants (
            occupancy_id, property_address, unit, primary_tenant,
            all_tenants, lease_type, lease_start, lease_end,
            current_rent, eligible_increase_on, last_increase_date,
            next_increase_date, last_scraped, past_lease_end, notes
        ) VALUES (
            :occupancy_id, :property_address, :unit, :primary_tenant,
            :all_tenants, :lease_type, :lease_start, :lease_end,
            :current_rent, :eligible_increase_on, :last_increase_date,
            :next_increase_date, :last_scraped, :past_lease_end, ''
        )
        ON CONFLICT(occupancy_id) DO UPDATE SET
            property_address     = excluded.property_address,
            unit                 = excluded.unit,
            primary_tenant       = excluded.primary_tenant,
            all_tenants          = excluded.all_tenants,
            lease_type           = excluded.lease_type,
            lease_start          = excluded.lease_start,
            lease_end            = excluded.lease_end,
            current_rent         = excluded.current_rent,
            eligible_increase_on = excluded.eligible_increase_on,
            last_increase_date   = excluded.last_increase_date,
            next_increase_date   = excluded.next_increase_date,
            last_scraped         = excluded.last_scraped,
            past_lease_end       = excluded.past_lease_end,
            is_active            = 1,
            inactive_at          = NULL,
            inactive_reason      = NULL
    """, {**record, "occupancy_id": occ_id, "next_increase_date": next_inc,
          "last_scraped": datetime.now().isoformat()})

    conn.execute(
        "DELETE FROM insurance_policies WHERE occupancy_id = ?", (occ_id,))
    if insurance and (insurance.get("provider") or insurance.get("expiration")):
        conn.execute("""
            INSERT INTO insurance_policies
            (occupancy_id, policyholder, policy_number, expiration_date,
             status, provider, last_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (occ_id, insurance.get("policyholder"), insurance.get("policy_number"),
              insurance.get("expiration"), insurance.get("status"),
              insurance.get("provider"), datetime.now().isoformat()))
    conn.commit()


def archive_missing_tenants(conn, current_occupancy_ids):
    """
    Archive tenants no longer present in a complete current
    AppFolio Tenant Directory scrape.

    Historical records are preserved; nothing is deleted.
    """
    active_rows = conn.execute("""
        SELECT *
        FROM tenants
        WHERE is_active = 1
    """).fetchall()

    missing_rows = [
        row for row in active_rows
        if row["occupancy_id"] not in current_occupancy_ids
    ]

    archived_at = datetime.now().isoformat()
    reason = "Not present in current successful Tenant Directory scrape"

    for row in missing_rows:
        conn.execute("""
            INSERT INTO tenant_history (
                occupancy_id,
                property_address,
                unit,
                primary_tenant,
                all_tenants,
                lease_type,
                lease_start,
                lease_end,
                current_rent,
                eligible_increase_on,
                last_increase_date,
                next_increase_date,
                past_lease_end,
                notes,
                source_last_scraped,
                archived_at,
                archive_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["occupancy_id"],
            row["property_address"],
            row["unit"],
            row["primary_tenant"],
            row["all_tenants"],
            row["lease_type"],
            row["lease_start"],
            row["lease_end"],
            row["current_rent"],
            row["eligible_increase_on"],
            row["last_increase_date"],
            row["next_increase_date"],
            row["past_lease_end"],
            row["notes"],
            row["last_scraped"],
            archived_at,
            reason,
        ))

        conn.execute("""
            UPDATE tenants
            SET is_active = 0,
                inactive_at = ?,
                inactive_reason = ?
            WHERE occupancy_id = ?
        """, (
            archived_at,
            reason,
            row["occupancy_id"],
        ))

    conn.commit()
    return len(missing_rows)


def parse_wo_report_and_update_vendors(excel_path, db_path):
    """
    Parse the Work Order Excel report and update the vendor database
    with historical vendor assignments per property.
    """
    import openpyxl
    print(f"\n[WO History] Parsing {excel_path}...")

    try:
        wb = openpyxl.load_workbook(excel_path, read_only=True)
        ws = wb.active
    except Exception as e:
        print(f"[WO History] Could not open file: {e}")
        return False

    # Find the header row (skip AppFolio metadata rows)
    header_row = None
    header_idx = None
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=25, values_only=True), 1):
        non_null = [v for v in row if v]
        if len(non_null) > 5 and "Property" in str(non_null[0]):
            header_row = row
            header_idx = i
            break

    if not header_row:
        print("[WO History] Could not find header row")
        wb.close()
        return False

    # Map column names to indices
    col = {}
    for idx, v in enumerate(header_row):
        if v:
            col[str(v).strip()] = idx

    required_columns = {"Property", "Vendor", "Work Order Number"}
    missing_columns = sorted(required_columns - set(col))
    if missing_columns:
        print(
            "[WO History] Required columns are missing: "
            + ", ".join(missing_columns)
        )
        wb.close()
        return False

    print(f"[WO History] Header at row {header_idx}")
    print(f"[WO History] Columns: {list(col.keys())[:10]}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Ensure tables exist
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            phone TEXT, email TEXT, trade TEXT,
            notes TEXT, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS property_vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_address TEXT, vendor_id INTEGER,
            trade TEXT, is_preferred INTEGER DEFAULT 1,
            source TEXT,
            UNIQUE(property_address, vendor_id)
        );
        CREATE TABLE IF NOT EXISTS vendor_work_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id INTEGER, property_address TEXT,
            wo_num TEXT UNIQUE, issue_type TEXT,
            description TEXT, status TEXT,
            done_on TEXT, amount REAL
        );
        CREATE INDEX IF NOT EXISTS idx_vendor_work_history_wo
        ON vendor_work_history(wo_num);
    """)
    vendor_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(vendors)").fetchall()
    }
    if "active" not in vendor_columns:
        conn.execute(
            "ALTER TABLE vendors ADD COLUMN active "
            "INTEGER NOT NULL DEFAULT 1"
        )
    conn.commit()

    def detect_trade(text):
        text = text.lower()
        if any(k in text for k in ["plumb", "pipe", "water", "toilet", "faucet", "drain", "sink"]):
            return "plumbing"
        if any(k in text for k in ["hvac", "ac ", "heat", "furnace", "air", "cool"]):
            return "hvac"
        if any(k in text for k in ["electric", "outlet", "light", "power"]):
            return "electrical"
        if any(k in text for k in ["paint", "stain"]):
            return "painting"
        if any(k in text for k in ["appliance", "stove", "oven", "fridge", "dishwasher"]):
            return "appliance"
        if any(k in text for k in ["pest", "roach", "termite"]):
            return "pest_control"
        if any(k in text for k in ["roof", "gutter"]):
            return "roofing"
        if any(k in text for k in ["lock", "key", "door"]):
            return "lockout"
        if any(k in text for k in ["clean"]):
            return "cleaning"
        if any(k in text for k in ["landscape", "garden"]):
            return "landscaping"
        return "general"

    new_vendors = 0
    new_links = 0
    new_history = 0
    row_failures = 0

    for row in ws.iter_rows(min_row=header_idx + 1, values_only=True):
        if not row or not row[col.get("Property", 0)]:
            continue

        prop_raw = str(row[col.get("Property", 0)] or "").strip()
        vendor = str(row[col.get("Vendor", 8)] or "").strip()
        issue = str(row[col.get("Work Order Issue", -1)] or "").strip() if "Work Order Issue" in col else ""
        status = str(row[col.get("Status", -1)] or "").strip() if "Status" in col else ""
        wo_num = str(row[col.get("Work Order Number", -1)] or "").strip() if "Work Order Number" in col else ""
        desc = str(row[col.get("Job Description", -1)] or "").strip()[:150] if "Job Description" in col else ""
        done_on = row[col.get("Work Done On", -1)] if "Work Done On" in col else None
        amount = row[col.get("Amount", -1)] if "Amount" in col else None

        if not vendor or vendor in ("nan", "", "ClientCo Property Management Inc."):
            continue

        # Clean property name
        prop = prop_raw.split(" - ")[1].strip() if " - " in prop_raw else prop_raw
        trade = detect_trade(issue + " " + desc)

        # Get or create vendor
        existing = conn.execute(
            "SELECT id FROM vendors WHERE name = ?", (vendor,)).fetchone()
        if existing:
            vid = existing[0]
        else:
            conn.execute(
                "INSERT OR IGNORE INTO vendors (name, trade, created_at) "
                "VALUES (?, ?, ?)",
                (vendor, trade, datetime.now().isoformat()))
            vid_row = conn.execute(
                "SELECT id FROM vendors WHERE name = ?", (vendor,)).fetchone()
            vid = vid_row[0] if vid_row else None
            if vid:
                new_vendors += 1

        if not vid:
            continue

        # Link vendor to property
        try:
            inserted_link = conn.execute(
                "INSERT OR IGNORE INTO property_vendors "
                "(property_address, vendor_id, trade, source) "
                "VALUES (?, ?, ?, ?)",
                (prop, vid, trade, "wo_history"),
            ).rowcount
            if inserted_link == 1:
                new_links += 1
        except Exception as error:
            row_failures += 1
            print(
                "[WO History] Property-vendor link failed: "
                f"{type(error).__name__}"
            )

        # Log work history
        if wo_num:
            try:
                existing_history = conn.execute(
                    "SELECT 1 FROM vendor_work_history "
                    "WHERE wo_num = ? LIMIT 1",
                    (wo_num,),
                ).fetchone()
                if not existing_history:
                    inserted_history = conn.execute(
                        "INSERT INTO vendor_work_history "
                        "(vendor_id, property_address, wo_num, issue_type, "
                        "description, status, done_on, amount) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (vid, prop, wo_num, issue, desc, status,
                         str(done_on) if done_on else "",
                         parse_currency(amount) or 0),
                    ).rowcount
                    if inserted_history == 1:
                        new_history += 1
                else:
                    conn.execute(
                        "UPDATE vendor_work_history SET vendor_id = ?, "
                        "property_address = ?, issue_type = ?, description = ?, "
                        "status = ?, done_on = ?, amount = ? WHERE wo_num = ?",
                        (
                            vid, prop, issue, desc, status,
                            str(done_on) if done_on else "",
                            parse_currency(amount) or 0, wo_num,
                        ),
                    )
            except Exception as error:
                row_failures += 1
                print(
                    "[WO History] Work-history row failed: "
                    f"{type(error).__name__}"
                )

    if row_failures:
        conn.rollback()
        wb.close()
        conn.close()
        print(
            "[WO History] Import rolled back because "
            f"{row_failures} row operation(s) failed"
        )
        return False

    conn.commit()

    total_vendors = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    total_links = conn.execute("SELECT COUNT(*) FROM property_vendors").fetchone()[0]
    total_history = conn.execute("SELECT COUNT(*) FROM vendor_work_history").fetchone()[0]

    wb.close()
    conn.close()

    print(f"[WO History] ✅ Updated vendor database:")
    print(f"  New vendors: {new_vendors} (total: {total_vendors})")
    print(f"  New property links: {new_links} (total: {total_links})")
    print(f"  New WO history: {new_history} (total: {total_history})")
    return True


def run():
    print("\n" + "="*60)
    print("  RACHEL - AppFolio Scraper v3.1")
    print("  ClientCo Property Management")
    print("="*60)
    print("Mode:", "Headless" if HEADLESS else "Visible (test)")
    print("DB:", DB_PATH)
    print()

    conn = init_db(DB_PATH)
    start_time = time.time()
    saved = errors = 0

    manager = get_appfolio_browser_manager()
    with manager.operation(
        "monthly_appfolio_scrape", track_feature_health=False
    ) as page:
        rachel_exports = get_downloads_dir()
        os.makedirs(rachel_exports, exist_ok=True)

        # Step 1: Export Tenant Directory report as Excel
        with manager.feature_scope("report_export"):
            td_path = export_report_to_excel(
                page, TENANT_DIR_URL, "TenantDir"
            )
            if not td_path:
                print("ERROR: Could not get Tenant Directory Excel file")
                conn.close()
                raise AppFolioUIContractError(
                    "report_export", "download",
                    "tenant_directory_unverified",
                )
        with manager.feature_scope("portfolio_ingestion"):
            try:
                records = parse_tenant_directory_excel(td_path)
            finally:
                cleanup_temporary_export(td_path, "TenantDir")

            if not records:
                print("ERROR: No tenant records parsed from Tenant Directory")
                conn.close()
                raise AppFolioUIContractError(
                    "portfolio_ingestion", "tenant_directory_parse",
                    "no_verified_records",
                )

        # Step 2: Get occupancy IDs from rent roll (scroll accumulation)
        with manager.feature_scope("portfolio_ingestion"):
            id_map = get_occupancy_ids(page)
            if not id_map:
                conn.close()
                raise AppFolioUIContractError(
                    "portfolio_ingestion", "rent_roll_parse",
                    "no_verified_occupancy_ids",
                )

        # Step 3: Export Work Order report and update vendor database
        print("\n--- Exporting Work Order Report ---")
        with manager.feature_scope("report_export"):
            wo_path = export_report_to_excel(
                page, WO_REPORT_URL, "WorkOrders"
            )
            if not wo_path:
                print("[WO History] Could not export Work Order report")
                conn.close()
                raise AppFolioUIContractError(
                    "report_export", "download",
                    "work_order_history_unverified",
                )
        with manager.feature_scope("portfolio_ingestion"):
            try:
                wo_history_updated = parse_wo_report_and_update_vendors(
                    wo_path, DB_PATH
                )
            finally:
                cleanup_temporary_export(wo_path, "WO History")
            if not wo_history_updated:
                print("[WO History] Export could not be parsed safely")
                conn.close()
                raise AppFolioUIContractError(
                    "portfolio_ingestion", "work_order_history_parse",
                    "workbook_contract_failed",
                )

    # Step 4: Match records to occupancy IDs and save
    print(f"\n[Match] Matching {len(records)} units to occupancy IDs...")
    matched = 0
    unmatched = []
    current_occupancy_ids = set()

    for (prop, unit), record in records.items():
        tenant_name = record["primary_tenant"]
        occ_id = match_occupancy_id(tenant_name, prop, id_map)

        if occ_id:
            current_occupancy_ids.add(occ_id)
            save_record(conn, occ_id, record.copy())
            saved += 1
            matched += 1
            flag = " PAST END" if record.get("past_lease_end") else ""
            print(f"  ✅ {tenant_name} | {prop[:40]} | "
                  f"ends: {record.get('lease_end') or 'M2M'} | "
                  f"eligible: {record.get('eligible_increase_on') or '?'}"
                  f"{flag}")
        else:
            unmatched.append(f"{tenant_name} ({prop[:30]})")
            errors += 1

    archived_count = 0

    if errors == 0 and matched == len(records):
        archived_count = archive_missing_tenants(
            conn,
            current_occupancy_ids,
        )
        print(
            f"\n[Archive] Historical tenants archived: "
            f"{archived_count}"
        )
    else:
        print(
            "\n[Archive] Skipped because the current scrape "
            "was not fully matched."
        )

    duration = time.time() - start_time

    print("\n" + "="*60)
    print("SCRAPE COMPLETE")
    print("="*60)
    print(f"Units parsed:      {len(records)}")
    print(f"Matched + saved:   {saved}")
    print(f"Unmatched:         {errors}")
    print(f"Duration:          {round(duration/60, 1)} minutes")
    print(f"Database:          {DB_PATH}")

    if unmatched:
        print(f"\nUnmatched tenants ({len(unmatched)}):")
        for u in unmatched[:10]:
            print(f"  {u}")

    past_end = conn.execute(
        "SELECT * FROM tenants "
        "WHERE past_lease_end = 1 AND is_active = 1"
    ).fetchall()
    if past_end:
        print(f"\nPAST LEASE END — {len(past_end)} tenants:")
        for t in past_end:
            print(f"  {t['property_address']} — "
                  f"{t['primary_tenant']} — ended {t['lease_end']}")

    exp_ins = conn.execute("""
        SELECT ip.*, t.property_address, t.primary_tenant
        FROM insurance_policies ip
        JOIN tenants t ON ip.occupancy_id = t.occupancy_id
        WHERE ip.status = 'EXPIRED'
        AND t.is_active = 1
    """).fetchall()
    if exp_ins:
        print(f"\nEXPIRED INSURANCE — {len(exp_ins)} policies:")
        for p in exp_ins:
            print(f"  {p['property_address']} — "
                  f"{p['policyholder']} — expired {p['expiration_date']}")

    print("\nSample (first 5):")
    rows = conn.execute("""
        SELECT primary_tenant, lease_type, lease_end,
               current_rent, eligible_increase_on, next_increase_date
        FROM tenants LIMIT 5
    """).fetchall()
    for row in rows:
        print(f"  {row['primary_tenant']} | {row['lease_type']} | "
              f"ends: {row['lease_end'] or 'M2M'} | "
              f"${row['current_rent']} | "
              f"eligible: {row['eligible_increase_on'] or 'TBD'} | "
              f"next: {row['next_increase_date'] or 'TBD'}")

    conn.execute("""
        INSERT INTO scrape_log
        (run_date, occupancies_found, occupancies_scraped, errors, duration_seconds)
        VALUES (?, ?, ?, ?, ?)
    """, (date.today().isoformat(), len(records), saved,
          errors, round(duration, 1)))
    conn.commit()
    conn.close()
    return errors == 0 and matched == len(records)


if __name__ == "__main__":
    if not APPFOLIO_EMAIL or not APPFOLIO_PASS:
        print("ERROR: Run: source ~/Downloads/rachel_setup.sh first")
    else:
        run()
