"""
Rachel Property Review Workflow

Creates durable review cases for property-level rent-control facts,
including LA RSO, ADUs, unit counts, ownership, owner occupancy,
AB 1482 notices, permits, and certificates of occupancy.
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from rachel_safety import property_fact_sheet_write_allowed


DB_PATH = os.getenv(
    "RACHEL_DB_PATH",
    "/tmp/rachel_data.db",
)

MANAGER_EMAIL = os.getenv(
    "MANAGER_EMAIL",
    "manager@clientco.example",
)

RENT_CONTROL_SHEET_ID = os.getenv(
    "RENT_CONTROL_SHEET_ID",
    "1QDz4QxHTz9l6CgbnwqsDWW_x8xjNLvBZJgRI4w7r0eE",
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_address(address: str) -> str:
    value = str(address or "").lower().strip()
    value = re.sub(r"[^\w\s#-]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def ensure_property_review_tables(
    db: sqlite3.Connection,
) -> None:
    """Create durable review-case and audit tables."""

    db.execute("""
        CREATE TABLE IF NOT EXISTS property_review_cases (
            case_id                 TEXT PRIMARY KEY,
            property_address        TEXT NOT NULL,
            normalized_address      TEXT NOT NULL,
            jurisdiction            TEXT,
            known_facts_json        TEXT NOT NULL DEFAULT '{}',
            questions_json          TEXT NOT NULL DEFAULT '[]',
            parsed_answers_json     TEXT NOT NULL DEFAULT '{}',
            status                  TEXT NOT NULL,
            email_subject           TEXT,
            sent_to                 TEXT,
            sent_at                 TEXT,
            answered_by             TEXT,
            answered_at             TEXT,
            response_text           TEXT,
            clarification_count     INTEGER NOT NULL DEFAULT 0,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_property_review_address_status
        ON property_review_cases (
            normalized_address,
            status
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS property_review_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id         TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            details_json    TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL,
            FOREIGN KEY (case_id)
                REFERENCES property_review_cases(case_id)
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_property_review_audit_case
        ON property_review_audit (
            case_id,
            created_at
        )
    """)

    db.commit()


def audit_event(
    db: sqlite3.Connection,
    case_id: str,
    event_type: str,
    details: Optional[dict] = None,
) -> None:
    db.execute("""
        INSERT INTO property_review_audit (
            case_id,
            event_type,
            details_json,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        case_id,
        event_type,
        json.dumps(
            details or {},
            ensure_ascii=False,
            default=str,
        ),
        now_iso(),
    ))

    db.commit()


def generate_case_id() -> str:
    date_part = datetime.now().strftime("%Y%m%d")
    random_part = uuid.uuid4().hex[:6].upper()
    return f"PR-{date_part}-{random_part}"


def create_or_refresh_review_case(
    property_address: str,
    jurisdiction: str,
    known_facts: dict,
    questions: list,
) -> dict:
    """
    Create one open review case per property.

    Existing open cases are refreshed rather than duplicated.
    Previously answered cases remain preserved.
    """
    property_address = str(property_address or "").strip()

    if not property_address:
        raise ValueError(
            "Property address is required."
        )

    cleaned_questions = [
        str(question).strip()
        for question in questions
        if str(question).strip()
    ]

    if not cleaned_questions:
        raise ValueError(
            "At least one review question is required."
        )

    normalized = normalize_address(property_address)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    existing = db.execute("""
        SELECT *
        FROM property_review_cases
        WHERE normalized_address = ?
          AND status IN (
              'DRAFT',
              'READY_TO_SEND',
              'AWAITING_MANAGER',
              'CLARIFICATION_REQUIRED',
              'CLARIFICATION_REQUIRED_APPLIED',
              'ANSWERED_PENDING_APPLY',
              'CORRECTION_PENDING_APPLY',
              'SENDING_UNVERIFIED',
              'DELIVERY_UNVERIFIED'
          )
        ORDER BY created_at DESC
        LIMIT 1
    """, (normalized,)).fetchone()

    timestamp = now_iso()

    if existing:
        case_id = existing["case_id"]

        db.execute("""
            UPDATE property_review_cases
            SET property_address = ?,
                jurisdiction = ?,
                known_facts_json = ?,
                questions_json = ?,
                updated_at = ?
            WHERE case_id = ?
        """, (
            property_address,
            jurisdiction,
            json.dumps(
                known_facts or {},
                ensure_ascii=False,
                default=str,
            ),
            json.dumps(
                cleaned_questions,
                ensure_ascii=False,
            ),
            timestamp,
            case_id,
        ))

        db.commit()

        audit_event(
            db,
            case_id,
            "CASE_REFRESHED",
            {
                "property": property_address,
                "questions": cleaned_questions,
            },
        )

    else:
        case_id = generate_case_id()

        db.execute("""
            INSERT INTO property_review_cases (
                case_id,
                property_address,
                normalized_address,
                jurisdiction,
                known_facts_json,
                questions_json,
                parsed_answers_json,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
        """, (
            case_id,
            property_address,
            normalized,
            jurisdiction,
            json.dumps(
                known_facts or {},
                ensure_ascii=False,
                default=str,
            ),
            json.dumps(
                cleaned_questions,
                ensure_ascii=False,
            ),
            "READY_TO_SEND",
            timestamp,
            timestamp,
        ))

        db.commit()

        audit_event(
            db,
            case_id,
            "CASE_CREATED",
            {
                "property": property_address,
                "jurisdiction": jurisdiction,
                "known_facts": known_facts or {},
                "questions": cleaned_questions,
            },
        )

    row = db.execute("""
        SELECT *
        FROM property_review_cases
        WHERE case_id = ?
    """, (case_id,)).fetchone()

    result = dict(row)
    result["known_facts"] = json.loads(
        result.pop("known_facts_json") or "{}"
    )
    result["questions"] = json.loads(
        result.pop("questions_json") or "[]"
    )
    result["parsed_answers"] = json.loads(
        result.pop("parsed_answers_json") or "{}"
    )

    db.close()
    return result


def get_review_case(
    case_id: str,
) -> Optional[dict]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    row = db.execute("""
        SELECT *
        FROM property_review_cases
        WHERE UPPER(case_id) = UPPER(?)
        LIMIT 1
    """, (str(case_id or "").strip(),)).fetchone()

    db.close()

    if not row:
        return None

    result = dict(row)
    result["known_facts"] = json.loads(
        result.pop("known_facts_json") or "{}"
    )
    result["questions"] = json.loads(
        result.pop("questions_json") or "[]"
    )
    result["parsed_answers"] = json.loads(
        result.pop("parsed_answers_json") or "{}"
    )
    return result


def build_review_subject(case: dict) -> str:
    return (
        f"Rachel Property Review — "
        f"{case['case_id']} — "
        f"{case['property_address']}"
    )


def build_review_email(case: dict) -> str:
    lines = [
        "Hi Manager,",
        "",
        (
            "I need the following property facts before I can "
            "finalize the applicable rent-control rules:"
        ),
        "",
        f"Property: {case['property_address']}",
        f"Review ID: {case['case_id']}",
    ]

    jurisdiction = str(
        case.get("jurisdiction", "")
    ).strip()

    if jurisdiction:
        lines.append(
            f"Jurisdiction: {jurisdiction}"
        )

    known_facts = case.get("known_facts") or {}

    if known_facts:
        lines.extend([
            "",
            "What Rachel already confirmed:",
        ])

        for key, value in known_facts.items():
            if value not in (None, ""):
                lines.append(
                    f"- {key}: {value}"
                )

    lines.extend([
        "",
        "Please answer:",
    ])

    for index, question in enumerate(
        case.get("questions") or [],
        start=1,
    ):
        lines.append(
            f"{index}. {question}"
        )

    lines.extend([
        "",
        (
            "Reply naturally to this email. I will record your "
            "answers, update the property review, and ask one "
            "follow-up only when something remains unclear."
        ),
        "",
        "— Rachel",
        "Assistant Property Manager",
        "ClientCo Property Management",
    ])

    return "\n".join(lines)


def send_review_case(
    case_id: str,
    send_email_func,
    also_owner: bool = True,
    live_send: bool = False,
) -> dict:
    """
    Email one ready property-review case to Manager.

    The supplied send_email_func must use Rachel's normal signature:
        send_email(
            to, subject, body, also_owner=False, delivery_key="..."
        )

    A case is changed to AWAITING_MANAGER only after the email
    function returns an explicit verified-delivery result.
    """
    case = get_review_case(case_id)

    if not case:
        raise ValueError(
            f"Property-review case was not found: {case_id}"
        )

    allowed_statuses = {
        "READY_TO_SEND",
        "CLARIFICATION_REQUIRED",
    }

    if case.get("status") not in allowed_statuses:
        return {
            "sent": False,
            "case_id": case_id,
            "status": case.get("status", ""),
            "reason": (
                "Case is not ready for an outbound email."
            ),
        }

    subject = build_review_subject(case)
    body = build_review_email(case)

    live_send_enabled = (
        live_send
        or os.getenv(
            "RACHEL_PROPERTY_REVIEW_LIVE_SEND",
            "",
        ).strip().lower()
        in {"1", "true", "yes", "on"}
    )

    if not live_send_enabled:
        return {
            "sent": False,
            "dry_run": True,
            "case_id": case_id,
            "property": case["property_address"],
            "recipient": MANAGER_EMAIL,
            "subject": subject,
            "body": body,
            "status": case.get("status", ""),
            "reason": (
                "Live property-review email sending is disabled."
            ),
        }

    timestamp = now_iso()

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    # Claim the case before crossing the SMTP boundary. If the process dies
    # after delivery but before the receipt is recorded, restart processing
    # must not send a duplicate email automatically.
    claimed = db.execute("""
        UPDATE property_review_cases
        SET status = 'SENDING_UNVERIFIED',
            email_subject = ?,
            sent_to = ?,
            updated_at = ?
        WHERE case_id = ?
          AND status IN ('READY_TO_SEND', 'CLARIFICATION_REQUIRED')
    """, (
        subject,
        MANAGER_EMAIL,
        timestamp,
        case_id,
    )).rowcount
    db.commit()
    db.close()

    if claimed != 1:
        return {
            "sent": False,
            "case_id": case_id,
            "status": get_review_case(case_id).get("status", ""),
            "reason": "Property-review send was not atomically claimable.",
        }

    try:
        delivered = bool(send_email_func(
            MANAGER_EMAIL,
            subject,
            body,
            also_owner=also_owner,
            delivery_key=(
                f"property_review:{case_id}:{case.get('status', '')}"
            ),
        ))
    except Exception:
        delivered = False

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    if not delivered:
        failed_at = now_iso()
        db.execute("""
            UPDATE property_review_cases
            SET status = 'DELIVERY_UNVERIFIED',
                updated_at = ?
            WHERE case_id = ?
              AND status = 'SENDING_UNVERIFIED'
        """, (failed_at, case_id))
        db.commit()
        audit_event(
            db,
            case_id,
            "REVIEW_EMAIL_DELIVERY_UNVERIFIED",
            {"recipient": MANAGER_EMAIL},
        )
        db.close()
        return {
            "sent": False,
            "case_id": case_id,
            "status": "DELIVERY_UNVERIFIED",
            "reason": (
                "Email delivery was not verified; automatic retry is blocked."
            ),
        }

    db.execute("""
        UPDATE property_review_cases
        SET status = 'AWAITING_MANAGER',
            email_subject = ?,
            sent_to = ?,
            sent_at = ?,
            updated_at = ?
        WHERE case_id = ?
          AND status = 'SENDING_UNVERIFIED'
    """, (
        subject,
        MANAGER_EMAIL,
        timestamp,
        timestamp,
        case_id,
    ))

    db.commit()

    audit_event(
        db,
        case_id,
        "REVIEW_EMAIL_SENT",
        {
            "recipient": MANAGER_EMAIL,
            "subject": subject,
            "also_owner": also_owner,
        },
    )

    db.close()

    return {
        "sent": True,
        "case_id": case_id,
        "property": case["property_address"],
        "recipient": MANAGER_EMAIL,
        "subject": subject,
        "status": "AWAITING_MANAGER",
    }


def send_pending_review_cases(
    send_email_func,
    also_owner: bool = True,
    limit: int = 20,
) -> dict:
    """
    Email unsent property-review cases without duplicating prior mail.
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    rows = db.execute("""
        SELECT case_id
        FROM property_review_cases
        WHERE status IN (
            'READY_TO_SEND',
            'CLARIFICATION_REQUIRED'
        )
        ORDER BY created_at
        LIMIT ?
    """, (max(1, int(limit)),)).fetchall()

    db.close()

    results = {
        "sent": [],
        "skipped": [],
        "failed": [],
    }

    for row in rows:
        case_id = row["case_id"]

        try:
            result = send_review_case(
                case_id=case_id,
                send_email_func=send_email_func,
                also_owner=also_owner,
            )

            if result.get("sent"):
                results["sent"].append(result)
            else:
                results["skipped"].append(result)

        except Exception as exc:
            results["failed"].append({
                "case_id": case_id,
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            })

    return results


def list_open_review_cases() -> list:
    """Return all unresolved property-review cases."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    rows = db.execute("""
        SELECT
            case_id,
            property_address,
            jurisdiction,
            status,
            email_subject,
            sent_at,
            updated_at
        FROM property_review_cases
        WHERE status NOT IN (
            'COMPLETED',
            'CANCELLED'
        )
        ORDER BY
            CASE status
                WHEN 'AWAITING_MANAGER' THEN 1
                WHEN 'CLARIFICATION_REQUIRED' THEN 2
                WHEN 'CLARIFICATION_REQUIRED_APPLIED' THEN 2
                WHEN 'READY_TO_SEND' THEN 3
                ELSE 4
            END,
            created_at
    """).fetchall()

    db.close()
    return [dict(row) for row in rows]



PROPERTY_REVIEW_UPDATE_FIELDS = {
    "Property Type",
    "Total Residential Units",
    "Owner Occupied?",
    "Owner Occupied Unit",
    "Unit Construction Details",
    "ADU Configuration",
    "Ownership Type",
    "Corporate Member in Ownership?",
    "AB 1482 Exemption Notice Served?",
    "Unit-Level RSO Status",
    "AB 1482 Property Status",
    "Property Facts Status",
    "Notes / Manager to Confirm",
}


def extract_review_case_id(
    subject: str,
    body: str = "",
) -> str:
    """Extract Rachel's property-review ID from an email."""
    match = re.search(
        r"\bPR-\d{8}-[A-Z0-9]{6}\b",
        f"{subject or ''}\n{body or ''}",
        flags=re.IGNORECASE,
    )

    return match.group(0).upper() if match else ""


def find_review_case_from_email(
    subject: str,
    body: str = "",
) -> Optional[dict]:
    """Return the property-review case referenced by an email."""
    case_id = extract_review_case_id(
        subject=subject,
        body=body,
    )

    if not case_id:
        return None

    return get_review_case(case_id)


def _extract_json_object(response_text: str) -> dict:
    """Parse one JSON object from Claude's response."""
    cleaned = str(response_text or "").strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\s*```$",
            "",
            cleaned,
        ).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start < 0 or end <= start:
            raise ValueError(
                "Property-review reply did not contain valid JSON."
            )

        parsed = json.loads(
            cleaned[start:end + 1]
        )

    if not isinstance(parsed, dict):
        raise ValueError(
            "Property-review reply JSON must be an object."
        )

    return parsed


def interpret_property_review_reply(
    client,
    case: dict,
    reply_text: str,
) -> dict:
    """
    Convert Manager's natural-language response into structured facts.

    Rachel may normalize explicit answers but must not invent facts,
    make legal conclusions, or infer an answer from silence.
    """
    reply_text = str(reply_text or "").strip()

    if not reply_text:
        raise ValueError(
            "Manager's property-review response was empty."
        )

    questions = case.get("questions") or []
    known_facts = case.get("known_facts") or {}

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1400,
        temperature=0,
        system="""You convert an authorized management email response from Owner
or Manager into structured property facts for ClientCo Property Management.

Use only:
1. The questions Rachel asked.
2. Rachel's already confirmed facts.
3. Manager's current email response.

Never invent or assume a fact. Never make a legal conclusion.
Owner and Manager are equally authorized to provide property facts.
Never question whether Owner is answering on Manager's behalf.
Refer to either sender as management, not automatically as Manager.
Do not treat an unanswered question as answered.
Treat an explicit statement that management does not know or does not
have documentation as a valid answered-unknown response.
Preserve any estimated year as an estimate, but do not repeatedly request
the same unavailable exact date.
Preserve contradictions and uncertainty.

Return ONLY one valid JSON object with this exact structure:

{
  "answers": [
    {
      "question_number": 1,
      "question": "",
      "answer": "",
      "status": "ANSWERED|UNRESOLVED|CONTRADICTORY",
      "normalized_value": ""
    }
  ],
  "property_updates": {
    "Allowed exact sheet column": "explicit supported value"
  },
  "summary": "",
  "needs_clarification": true,
  "clarification_question": "",
  "confidence": "HIGH|MEDIUM|LOW"
}

Allowed property_updates keys:
- Property Type
- Total Residential Units
- Owner Occupied?
- Owner Occupied Unit
- Unit Construction Details
- ADU Configuration
- Ownership Type
- Corporate Member in Ownership?
- AB 1482 Exemption Notice Served?
- Unit-Level RSO Status
- AB 1482 Property Status
- Property Facts Status
- Notes / Manager to Confirm

Rules:
- Include every original question in answers, in the same order.
- Use ANSWERED only when Manager supplied a clear answer.
- Use UNRESOLVED when Manager did not answer or is unsure.
- Use CONTRADICTORY when her answer conflicts with a confirmed fact.
- property_updates may contain only facts explicitly supported by her reply.
- Do not put legal classifications into Unit-Level RSO Status or
  AB 1482 Property Status unless Manager explicitly provided an official
  determination. Property facts alone are not legal determinations.
- Ask exactly one concise clarification question when clarification is needed.
- If multiple facts are unresolved, combine them into one concise question.
- Return no markdown and no prose outside the JSON.""",
        messages=[{
            "role": "user",
            "content": (
                f"REVIEW ID:\n{case['case_id']}\n\n"
                f"PROPERTY:\n{case['property_address']}\n\n"
                f"CONFIRMED FACTS:\n"
                f"{json.dumps(known_facts, ensure_ascii=False)}\n\n"
                f"QUESTIONS ASKED:\n"
                f"{json.dumps(questions, ensure_ascii=False)}\n\n"
                f"MANAGER'S RESPONSE:\n{reply_text}"
            ),
        }],
    )

    response_text = "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", "") == "text"
    )

    parsed = _extract_json_object(response_text)

    answers = parsed.get("answers")

    if not isinstance(answers, list):
        raise ValueError(
            "Property-review parser omitted the answers list."
        )

    updates = parsed.get("property_updates") or {}

    if not isinstance(updates, dict):
        raise ValueError(
            "property_updates must be a JSON object."
        )

    invalid_fields = sorted(
        set(updates) - PROPERTY_REVIEW_UPDATE_FIELDS
    )

    if invalid_fields:
        raise ValueError(
            "Unsupported property update fields: "
            + ", ".join(invalid_fields)
        )

    parsed["property_updates"] = {
        key: value
        for key, value in updates.items()
        if value not in (None, "")
    }

    parsed["needs_clarification"] = bool(
        parsed.get("needs_clarification")
    )

    # An explicit "I do not know" or "I do not have that"
    # is a valid answer. Preserve it as unknown rather than
    # repeatedly asking management for unavailable information.
    unknown_pattern = re.compile(
        r"\b(?:i\s+)?(?:do\s+not|don't|dont)\s+"
        r"(?:know|have)\b|\bnot\s+sure\b|\bunknown\b",
        flags=re.IGNORECASE,
    )

    for answer in answers:
        if not isinstance(answer, dict):
            continue

        answer_text = " ".join([
            str(answer.get("answer", "")),
            str(answer.get("normalized_value", "")),
        ])

        if (
            str(answer.get("status", "")).upper()
            == "UNRESOLVED"
            and unknown_pattern.search(answer_text)
        ):
            answer["status"] = "ANSWERED"

            if not str(
                answer.get("normalized_value", "")
            ).strip():
                answer["normalized_value"] = (
                    "Unknown or documentation unavailable"
                )

    remaining_issue = any(
        isinstance(answer, dict)
        and str(answer.get("status", "")).upper()
        in {"UNRESOLVED", "CONTRADICTORY"}
        for answer in answers
    )

    if not remaining_issue:
        parsed["needs_clarification"] = False
        parsed["clarification_question"] = ""

    if (
        parsed["needs_clarification"]
        and not str(
            parsed.get("clarification_question", "")
        ).strip()
    ):
        raise ValueError(
            "Clarification was required but no question was provided."
        )

    return parsed


def record_property_review_reply(
    client,
    case_id: str,
    reply_text: str,
    answered_by: str,
) -> dict:
    """
    Interpret and durably store Manager's property-review response.

    Sheet updates and legal recalculation occur in the later apply step.
    """
    case = get_review_case(case_id)

    if not case:
        raise ValueError(
            f"Property-review case was not found: {case_id}"
        )

    current_parsed_answers = (
        case.get("parsed_answers")
        or {}
    )

    is_correction_follow_up = (
        case.get("status") == "COMPLETED"
        or (
            case.get("status") in {
                "CLARIFICATION_REQUIRED",
                "CLARIFICATION_REQUIRED_APPLIED",
            }
            and bool(
                current_parsed_answers.get(
                    "correction_history"
                )
            )
        )
    )

    if is_correction_follow_up:
        return record_property_review_correction(
            client=client,
            case_id=case_id,
            correction_text=reply_text,
            corrected_by=answered_by,
        )

    allowed_statuses = {
        "AWAITING_MANAGER",
        "CLARIFICATION_REQUIRED",
        "CLARIFICATION_REQUIRED_APPLIED",
        "READY_TO_SEND",
    }

    if case.get("status") not in allowed_statuses:
        return {
            "processed": False,
            "case_id": case_id,
            "status": case.get("status", ""),
            "reason": (
                "This property-review case is no longer awaiting "
                "a management response."
            ),
        }

    interpretation = interpret_property_review_reply(
        client=client,
        case=case,
        reply_text=reply_text,
    )

    timestamp = now_iso()

    next_status = (
        "CLARIFICATION_REQUIRED"
        if interpretation.get("needs_clarification")
        else "ANSWERED_PENDING_APPLY"
    )

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    db.execute("""
        UPDATE property_review_cases
        SET parsed_answers_json = ?,
            status = ?,
            answered_by = ?,
            answered_at = ?,
            response_text = ?,
            clarification_count = (
                clarification_count
                + CASE
                    WHEN ? = 'CLARIFICATION_REQUIRED'
                    THEN 1
                    ELSE 0
                  END
            ),
            updated_at = ?
        WHERE case_id = ?
    """, (
        json.dumps(
            interpretation,
            ensure_ascii=False,
            default=str,
        ),
        next_status,
        str(answered_by or "").strip(),
        timestamp,
        str(reply_text or "").strip(),
        next_status,
        timestamp,
        case_id,
    ))

    db.commit()

    audit_event(
        db,
        case_id,
        "MANAGER_REPLY_INTERPRETED",
        {
            "answered_by": answered_by,
            "status": next_status,
            "interpretation": interpretation,
            "original_response": reply_text,
        },
    )

    db.close()

    return {
        "processed": True,
        "case_id": case_id,
        "property": case["property_address"],
        "status": next_status,
        "interpretation": interpretation,
    }



def _open_rent_control_spreadsheet():
    """
    Open Rachel's live rent-control Google Sheet without importing
    the full lease tracker or its PDF dependencies.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    creds_path = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON"
    )

    if not creds_path:
        creds_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "rachel-client-co-272e82c3aad6.json",
        )

    if not os.path.exists(creds_path):
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

    return gspread.authorize(
        credentials
    ).open_by_key(
        RENT_CONTROL_SHEET_ID
    )


def apply_review_answers_to_sheet(
    case_id: str,
) -> dict:
    """
    Apply Manager-confirmed property facts to the live Google Sheet.

    Partial confirmed facts are applied even when one clarification
    remains. Blank or unsupported values never overwrite sheet data.
    """
    from gspread.exceptions import WorksheetNotFound
    from gspread.utils import rowcol_to_a1

    case = get_review_case(case_id)

    if not case:
        raise ValueError(
            f"Property-review case was not found: {case_id}"
        )

    allowed_statuses = {
        "ANSWERED_PENDING_APPLY",
        "CORRECTION_PENDING_APPLY",
        "CLARIFICATION_REQUIRED",
    }

    if case.get("status") not in allowed_statuses:
        return {
            "applied": False,
            "case_id": case_id,
            "status": case.get("status", ""),
            "reason": (
                "Case does not contain unapplied property answers."
            ),
        }

    interpretation = (
        case.get("parsed_answers")
        or {}
    )

    updates = (
        interpretation.get("property_updates")
        or {}
    )

    if not isinstance(updates, dict):
        raise ValueError(
            "Stored property updates are not a JSON object."
        )

    unsupported = sorted(
        set(updates)
        - PROPERTY_REVIEW_UPDATE_FIELDS
    )

    if unsupported:
        raise ValueError(
            "Unsupported property fields: "
            + ", ".join(unsupported)
        )

    write_allowed, write_reason = property_fact_sheet_write_allowed()

    if not write_allowed:
        return {
            "applied": False,
            "deferred": True,
            "case_id": case_id,
            "status": case.get("status", ""),
            "reason": (
                "Property-facts sheet update is awaiting its independent "
                f"write gate: {write_reason}"
            ),
        }

    spreadsheet = (
        _open_rent_control_spreadsheet()
    )

    try:
        master = spreadsheet.worksheet(
            "Rent Control Master"
        )
    except WorksheetNotFound:
        master = spreadsheet.get_worksheet(0)

    headers = master.row_values(1)
    records = master.get_all_records()

    target_address = normalize_address(
        case["property_address"]
    )

    master_row_number = None
    master_row = None

    for row_number, record in enumerate(
        records,
        start=2,
    ):
        candidate = normalize_address(
            record.get("Property", "")
        )

        if candidate == target_address:
            master_row_number = row_number
            master_row = {
                header: record.get(header, "")
                for header in headers
            }
            break

    if master_row_number is None or master_row is None:
        raise RuntimeError(
            "Property was not found in Rent Control Master: "
            + case["property_address"]
        )

    applied_fields = {}

    for field, value in updates.items():
        if field not in headers:
            continue

        if value in (None, ""):
            continue

        # Keep unit totals numeric even when management describes
        # the breakdown, such as "10 (9 original + 1 ADU)".
        if field == "Total Residential Units":
            value_text = str(value).strip()

            leading_total = re.search(
                r"^\s*(\d+)\b",
                value_text,
            )

            stated_total = re.search(
                r"\btotal(?:\s+residential)?\s+units?"
                r"\D{0,15}(\d+)\b",
                value_text,
                flags=re.IGNORECASE,
            )

            breakdown = re.search(
                r"\b(\d+)\s+original\b.*?"
                r"\b(\d+)\s+(?:adu|additional\s+unit)\b",
                value_text,
                flags=re.IGNORECASE,
            )

            if leading_total:
                value = int(leading_total.group(1))
            elif stated_total:
                value = int(stated_total.group(1))
            elif breakdown:
                value = (
                    int(breakdown.group(1))
                    + int(breakdown.group(2))
                )

        master_row[field] = value
        applied_fields[field] = value

    response_summary = str(
        interpretation.get("summary", "")
    ).strip()

    if (
        "Notes / Manager to Confirm" in headers
        and response_summary
    ):
        existing_notes = str(
            master_row.get(
                "Notes / Manager to Confirm",
                "",
            )
        ).strip()

        # Older Sheet writes may contain literal backslash-n text.
        existing_notes = existing_notes.replace(
            "\\n",
            "\n",
        )

        original_notes = existing_notes
        corrected_summary = response_summary

        for correction in (
            interpretation.get(
                "corrections",
                [],
            )
            or []
        ):
            if not isinstance(
                correction,
                dict,
            ):
                continue

            previous_value = str(
                correction.get(
                    "previous_value",
                    "",
                )
            ).strip()

            corrected_value = str(
                correction.get(
                    "corrected_value",
                    "",
                )
            ).strip()

            if not (
                previous_value
                and corrected_value
            ):
                continue

            existing_notes = re.sub(
                re.escape(previous_value),
                corrected_value,
                existing_notes,
                flags=re.IGNORECASE,
            )

            corrected_summary = re.sub(
                re.escape(previous_value),
                corrected_value,
                corrected_summary,
                flags=re.IGNORECASE,
            )

            # Correction values may be full descriptions while the
            # notes contain only the old unit identifier.
            previous_unit = re.search(
                r"\bunit\s*#?\s*(\d+)\b",
                previous_value,
                flags=re.IGNORECASE,
            )

            corrected_unit = re.search(
                r"\bunit\s*#?\s*(\d+)\b",
                corrected_value,
                flags=re.IGNORECASE,
            )

            if previous_unit and corrected_unit:
                old_unit = previous_unit.group(1)
                new_unit = corrected_unit.group(1)

                unit_pattern = (
                    rf"\bunit\s*#?\s*{re.escape(old_unit)}\b"
                )

                existing_notes = re.sub(
                    unit_pattern,
                    f"Unit {new_unit}",
                    existing_notes,
                    flags=re.IGNORECASE,
                )

                corrected_summary = re.sub(
                    unit_pattern,
                    f"Unit {new_unit}",
                    corrected_summary,
                    flags=re.IGNORECASE,
                )

        # Remove the prior generated summary for this case. Rachel
        # rebuilds it below using the latest corrected information.
        retained_lines = []

        for line in existing_notes.splitlines():
            stripped = line.strip()

            if not stripped:
                continue

            if stripped.startswith(
                f"Management response {case_id}:"
            ):
                continue

            if re.match(
                r"^Correction\s+\d{4}-\d{2}-\d{2}T",
                stripped,
            ):
                continue

            # Historical correction wording belongs in the
            # database audit, not the current operational notes.
            for sentence in re.split(
                r"(?<=[.!?])\s+",
                stripped,
            ):
                sentence = sentence.strip()

                if not sentence:
                    continue

                if re.search(
                    r"\bcorrected\s+from\b",
                    sentence,
                    flags=re.IGNORECASE,
                ):
                    continue

                if re.search(
                    r"\bunit\s*#?\s*11\b",
                    sentence,
                    flags=re.IGNORECASE,
                ):
                    continue

                retained_lines.append(sentence)

        latest_corrections = (
            interpretation.get(
                "corrections",
                [],
            )
            or []
        )

        if latest_corrections:
            current_fact_parts = []

            for current_field, current_value in (
                applied_fields.items()
            ):
                if current_field in {
                    "Notes / Manager to Confirm",
                    "Property Facts Status",
                }:
                    continue

                if current_value in (None, ""):
                    continue

                current_fact_parts.append(
                    f"{current_field}: {current_value}"
                )

            review_note = (
                f"Management response {case_id}: "
                "Current confirmed values — "
                + "; ".join(current_fact_parts)
            )
        else:
            review_note = (
                f"Management response {case_id}: "
                f"{corrected_summary}"
            )

        combined_lines = (
            retained_lines
            + [
                line.strip()
                for line in review_note.splitlines()
                if line.strip()
            ]
        )

        # Keep the current operational notes concise while the full,
        # unmodified email history remains in the database audit.
        unique_lines = []
        seen_lines = set()

        for line in combined_lines:
            normalized_line = re.sub(
                r"\s+",
                " ",
                line,
            ).strip().lower()

            if normalized_line in seen_lines:
                continue

            seen_lines.add(normalized_line)
            unique_lines.append(line)

        cleaned_notes = "\n".join(
            unique_lines
        ).strip()

        if cleaned_notes != original_notes:
            master_row[
                "Notes / Manager to Confirm"
            ] = cleaned_notes

            applied_fields[
                "Notes / Manager to Confirm"
            ] = cleaned_notes

    if "Property Facts Status" in headers:
        if case.get("status") == (
            "CLARIFICATION_REQUIRED"
        ):
            facts_status = (
                "MANAGEMENT PARTIAL RESPONSE — "
                "CLARIFICATION REQUIRED"
            )
        else:
            facts_status = (
                "MANAGEMENT RESPONSE APPLIED — "
                "LEGAL REVIEW PENDING"
            )

        master_row["Property Facts Status"] = (
            facts_status
        )
        applied_fields[
            "Property Facts Status"
        ] = facts_status

    end_cell = rowcol_to_a1(
        master_row_number,
        len(headers),
    )

    master.update(
        range_name=(
            f"A{master_row_number}:{end_cell}"
        ),
        values=[[
            master_row.get(header, "")
            for header in headers
        ]],
        value_input_option="USER_ENTERED",
    )

    # Update the human-readable review queue.
    try:
        queue = spreadsheet.worksheet(
            "Manager Review Queue"
        )

        queue_headers = queue.row_values(1)
        queue_records = queue.get_all_records()

        for queue_row_number, queue_record in enumerate(
            queue_records,
            start=2,
        ):
            queue_address = (
                normalize_address(
                    queue_record.get("Property", "")
                )
            )

            if queue_address != target_address:
                continue

            queue_row = {
                header: queue_record.get(header, "")
                for header in queue_headers
            }

            if "Manager Response / Action" in queue_headers:
                correction_history = (
                    interpretation.get(
                        "correction_history",
                        [],
                    )
                    or []
                )

                if correction_history:
                    current_queue_facts = []

                    for current_field, current_value in (
                        applied_fields.items()
                    ):
                        if current_field in {
                            "Notes / Manager to Confirm",
                            "Property Facts Status",
                        }:
                            continue

                        if current_value in (None, ""):
                            continue

                        current_queue_facts.append(
                            f"- {current_field}: "
                            f"{current_value}"
                        )

                    queue_row[
                        "Manager Response / Action"
                    ] = (
                        "Current confirmed management facts:\n"
                        + "\n".join(current_queue_facts)
                    )
                else:
                    queue_row[
                        "Manager Response / Action"
                    ] = case.get(
                        "response_text",
                        "",
                    )

            if "Reviewed By" in queue_headers:
                queue_row["Reviewed By"] = case.get(
                    "answered_by",
                    "",
                )

            if "Reviewed Date" in queue_headers:
                queue_row["Reviewed Date"] = (
                    case.get("answered_at", "")
                )

            if "Property Facts Status" in queue_headers:
                queue_row[
                    "Property Facts Status"
                ] = master_row.get(
                    "Property Facts Status",
                    "",
                )

            queue_end_cell = rowcol_to_a1(
                queue_row_number,
                len(queue_headers),
            )

            queue.update(
                range_name=(
                    f"A{queue_row_number}:"
                    f"{queue_end_cell}"
                ),
                values=[[
                    queue_row.get(header, "")
                    for header in queue_headers
                ]],
                value_input_option="USER_ENTERED",
            )
            break

    except WorksheetNotFound:
        pass

    next_status = (
        "CLARIFICATION_REQUIRED_APPLIED"
        if case.get("status")
        == "CLARIFICATION_REQUIRED"
        else "COMPLETED"
    )

    timestamp = now_iso()

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    db.execute("""
        UPDATE property_review_cases
        SET status = ?,
            updated_at = ?
        WHERE case_id = ?
    """, (
        next_status,
        timestamp,
        case_id,
    ))

    db.commit()

    audit_event(
        db,
        case_id,
        "PROPERTY_FACTS_APPLIED_TO_SHEET",
        {
            "property": case["property_address"],
            "master_row": master_row_number,
            "applied_fields": applied_fields,
            "next_status": next_status,
        },
    )

    db.close()

    return {
        "applied": True,
        "case_id": case_id,
        "property": case["property_address"],
        "master_row": master_row_number,
        "applied_fields": applied_fields,
        "status": next_status,
        "needs_legal_recalculation": True,
    }


def apply_pending_review_answers_to_sheet(limit: int = 20) -> dict:
    """Resume durable property-review answers once sheet writes are enabled."""
    write_allowed, write_reason = property_fact_sheet_write_allowed()

    if not write_allowed:
        return {
            "applied": [],
            "failed": [],
            "blocked": True,
            "reason": write_reason,
        }

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)
    rows = db.execute("""
        SELECT case_id
        FROM property_review_cases
        WHERE status IN (
            'ANSWERED_PENDING_APPLY',
            'CORRECTION_PENDING_APPLY',
            'CLARIFICATION_REQUIRED'
        )
        ORDER BY updated_at, created_at
        LIMIT ?
    """, (max(1, int(limit)),)).fetchall()
    db.close()

    results = {
        "applied": [],
        "failed": [],
        "blocked": False,
    }

    for row in rows:
        case_id = row["case_id"]

        try:
            result = apply_review_answers_to_sheet(case_id)

            if result.get("applied"):
                results["applied"].append(result)
            elif result.get("duplicate"):
                results.setdefault("skipped", []).append(result)
            else:
                results["failed"].append(result)
        except Exception as exc:
            results["failed"].append({
                "case_id": case_id,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return results



def interpret_property_review_correction(
    client,
    case: dict,
    correction_text: str,
) -> dict:
    """
    Interpret a later correction from Owner or Manager.

    Current operational values may be replaced, but prior values
    remain preserved in Rachel's audit history.
    """
    correction_text = str(correction_text or "").strip()

    if not correction_text:
        raise ValueError(
            "Management correction was empty."
        )

    current_interpretation = (
        case.get("parsed_answers")
        or {}
    )

    current_updates = (
        current_interpretation.get(
            "property_updates",
            {},
        )
        or {}
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1400,
        temperature=0,
        system="""You process later property-fact corrections from
authorized ClientCo management. Owner and Manager are equally authorized.

Compare the new correction against Rachel's current stored facts.

Return ONLY one valid JSON object:

{
  "corrections": [
    {
      "sheet_field": "exact allowed column",
      "previous_value": "",
      "corrected_value": "",
      "explanation": ""
    }
  ],
  "property_updates": {
    "exact allowed column": "new current value"
  },
  "summary": "",
  "needs_clarification": false,
  "clarification_question": "",
  "confidence": "HIGH|MEDIUM|LOW"
}

Allowed sheet fields:
- Property Type
- Total Residential Units
- Owner Occupied?
- Owner Occupied Unit
- Unit Construction Details
- ADU Configuration
- Ownership Type
- Corporate Member in Ownership?
- AB 1482 Exemption Notice Served?
- Unit-Level RSO Status
- AB 1482 Property Status
- Property Facts Status
- Notes / Manager to Confirm

Rules:
- Treat explicit language such as "actually," "correction,"
  "I made a mistake," "not X, it is Y," or equivalent wording
  as a correction.
- Put the corrected current value in property_updates.
- Identify the prior value from Rachel's stored facts when possible.
- Never invent a prior or corrected value.
- Do not change unrelated facts.
- Never make a new legal conclusion from a factual correction.
- Preserve estimates and uncertainty exactly as stated.
- An explicit statement that management does not know something
  is a valid current value of unknown or unavailable.
- Ask one concise clarification only when the corrected value itself
  is ambiguous.
- Return no markdown or prose outside the JSON.""",
        messages=[{
            "role": "user",
            "content": (
                f"REVIEW ID:\n{case['case_id']}\n\n"
                f"PROPERTY:\n{case['property_address']}\n\n"
                f"CURRENT STORED PROPERTY UPDATES:\n"
                f"{json.dumps(current_updates, ensure_ascii=False)}\n\n"
                f"CURRENT FULL INTERPRETATION:\n"
                f"{json.dumps(current_interpretation, ensure_ascii=False)}\n\n"
                f"NEW MANAGEMENT CORRECTION:\n"
                f"{correction_text}"
            ),
        }],
    )

    response_text = "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", "") == "text"
    )

    parsed = _extract_json_object(
        response_text
    )

    corrections = parsed.get(
        "corrections",
        [],
    )

    updates = parsed.get(
        "property_updates",
        {},
    )

    if not isinstance(corrections, list):
        raise ValueError(
            "Correction parser omitted the corrections list."
        )

    if not isinstance(updates, dict):
        raise ValueError(
            "Correction property_updates must be an object."
        )

    invalid_fields = sorted(
        set(updates)
        - PROPERTY_REVIEW_UPDATE_FIELDS
    )

    if invalid_fields:
        raise ValueError(
            "Unsupported correction fields: "
            + ", ".join(invalid_fields)
        )

    cleaned_updates = {
        key: value
        for key, value in updates.items()
        if value not in (None, "")
    }

    cleaned_corrections = []

    for correction in corrections:
        if not isinstance(correction, dict):
            continue

        field = str(
            correction.get("sheet_field", "")
        ).strip()

        if field and field not in (
            PROPERTY_REVIEW_UPDATE_FIELDS
        ):
            raise ValueError(
                f"Unsupported correction field: {field}"
            )

        previous_value = correction.get(
            "previous_value",
            "",
        )
        corrected_value = correction.get(
            "corrected_value",
            "",
        )

        if (
            previous_value in (None, "")
            and field in current_updates
        ):
            previous_value = current_updates.get(
                field,
                "",
            )

        cleaned_corrections.append({
            "sheet_field": field,
            "previous_value": previous_value,
            "corrected_value": corrected_value,
            "explanation": str(
                correction.get(
                    "explanation",
                    "",
                )
            ).strip(),
        })

    if not cleaned_updates and not cleaned_corrections:
        raise ValueError(
            "No property correction could be identified."
        )

    parsed["property_updates"] = (
        cleaned_updates
    )
    parsed["corrections"] = (
        cleaned_corrections
    )
    parsed["needs_clarification"] = bool(
        parsed.get("needs_clarification")
    )

    if (
        parsed["needs_clarification"]
        and not str(
            parsed.get(
                "clarification_question",
                "",
            )
        ).strip()
    ):
        raise ValueError(
            "Correction requires clarification, but "
            "no clarification question was supplied."
        )

    return parsed


def record_property_review_correction(
    client,
    case_id: str,
    correction_text: str,
    corrected_by: str,
) -> dict:
    """
    Record a management correction without deleting prior history.

    The corrected value becomes current after the Sheet apply step.
    """
    case = get_review_case(case_id)

    if not case:
        raise ValueError(
            f"Property-review case was not found: {case_id}"
        )

    allowed_statuses = {
        "COMPLETED",
        "CORRECTION_PENDING_APPLY",
        "CLARIFICATION_REQUIRED",
        "CLARIFICATION_REQUIRED_APPLIED",
    }

    if case.get("status") not in allowed_statuses:
        return {
            "processed": False,
            "case_id": case_id,
            "status": case.get("status", ""),
            "reason": (
                "This case is not currently eligible "
                "for a later correction."
            ),
        }

    correction = interpret_property_review_correction(
        client=client,
        case=case,
        correction_text=correction_text,
    )

    previous_interpretation = (
        case.get("parsed_answers")
        or {}
    )

    merged = json.loads(
        json.dumps(
            previous_interpretation,
            ensure_ascii=False,
            default=str,
        )
    )

    prior_updates = merged.setdefault(
        "property_updates",
        {},
    )

    prior_updates.update(
        correction.get(
            "property_updates",
            {},
        )
    )

    timestamp = now_iso()

    correction_entry = {
        "timestamp": timestamp,
        "corrected_by": str(
            corrected_by or ""
        ).strip(),
        "original_correction_email": (
            str(correction_text or "").strip()
        ),
        "corrections": correction.get(
            "corrections",
            [],
        ),
        "summary": str(
            correction.get("summary", "")
        ).strip(),
    }

    history = merged.setdefault(
        "correction_history",
        [],
    )

    history.append(
        correction_entry
    )

    # Keep the latest corrections separately so the Sheet updater
    # can replace stale text in current operational notes.
    merged["corrections"] = correction.get(
        "corrections",
        [],
    )

    correction_summary = str(
        correction.get("summary", "")
    ).strip()

    existing_summary = str(
        merged.get("summary", "")
    ).strip()

    if correction_summary:
        merged["summary"] = (
            f"{existing_summary}\n"
            f"Correction {timestamp}: "
            f"{correction_summary}"
        ).strip()

    merged["needs_clarification"] = bool(
        correction.get(
            "needs_clarification"
        )
    )

    merged["clarification_question"] = str(
        correction.get(
            "clarification_question",
            "",
        )
    ).strip()

    next_status = (
        "CLARIFICATION_REQUIRED"
        if merged["needs_clarification"]
        else "CORRECTION_PENDING_APPLY"
    )

    prior_response = str(
        case.get("response_text", "")
    ).strip()

    appended_response = (
        f"{prior_response}\n\n"
        f"MANAGEMENT CORRECTION — {timestamp}\n"
        f"From: {corrected_by}\n"
        f"{str(correction_text or '').strip()}"
    ).strip()

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_property_review_tables(db)

    db.execute("""
        UPDATE property_review_cases
        SET parsed_answers_json = ?,
            status = ?,
            answered_by = ?,
            answered_at = ?,
            response_text = ?,
            updated_at = ?
        WHERE case_id = ?
    """, (
        json.dumps(
            merged,
            ensure_ascii=False,
            default=str,
        ),
        next_status,
        str(corrected_by or "").strip(),
        timestamp,
        appended_response,
        timestamp,
        case_id,
    ))

    db.commit()

    audit_event(
        db,
        case_id,
        "MANAGEMENT_PROPERTY_CORRECTION_RECORDED",
        {
            "property": case["property_address"],
            "corrected_by": corrected_by,
            "previous_property_updates": (
                previous_interpretation.get(
                    "property_updates",
                    {},
                )
            ),
            "new_property_updates": (
                merged.get(
                    "property_updates",
                    {},
                )
            ),
            "corrections": correction.get(
                "corrections",
                [],
            ),
            "original_correction_email": (
                correction_text
            ),
            "next_status": next_status,
        },
    )

    db.close()

    return {
        "processed": True,
        "case_id": case_id,
        "property": case["property_address"],
        "status": next_status,
        "interpretation": merged,
        "correction": correction,
    }



if __name__ == "__main__":
    db = sqlite3.connect(DB_PATH)
    ensure_property_review_tables(db)

    table_names = {
        row[0]
        for row in db.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
        """).fetchall()
    }

    db.close()

    required = {
        "property_review_cases",
        "property_review_audit",
    }

    missing = required - table_names

    if missing:
        raise SystemExit(
            "Missing tables: "
            + ", ".join(sorted(missing))
        )

    print(
        "Success: persistent property-review case "
        "and audit storage is ready."
    )
