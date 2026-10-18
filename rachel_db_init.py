"""
Rachel centralized database initialization and migration.

Runs safely on every deployment before Rachel's scheduler and inbox
monitor start. Existing data is preserved. Missing tables, columns,
and indexes are added idempotently.
"""

import os
import sqlite3
from pathlib import Path
from datetime import datetime
from rachel_event_memory import initialize_event_memory


DB_PATH = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = table_columns(conn, table)

    for name, definition in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )
            print(f"[Database] Added {table}.{name}")


def initialize_database() -> None:
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    initialize_event_memory(DB_PATH)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
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

            CREATE TABLE IF NOT EXISTS insurance_policies (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                occupancy_id    TEXT,
                policyholder    TEXT,
                policy_number   TEXT,
                expiration_date TEXT,
                status          TEXT,
                provider        TEXT,
                last_scraped    TEXT
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

            CREATE TABLE IF NOT EXISTS wo_triage (
                wo_num          TEXT PRIMARY KEY,
                wo_url          TEXT,
                tenant          TEXT,
                address         TEXT,
                job_desc        TEXT,
                issue_type      TEXT,
                conversation    TEXT,
                issues          TEXT,
                status          TEXT,
                created_at      TEXT,
                updated_at      TEXT,
                vendor_name     TEXT,
                nudge_count     INTEGER DEFAULT 0,
                last_nudge_at   TEXT,
                estimate_amount REAL,
                scheduled_for   TEXT
            );

            CREATE TABLE IF NOT EXISTS wo_scanner_state (
                wo_num                      TEXT PRIMARY KEY,
                wo_url                      TEXT,
                appfolio_status             TEXT,
                classification              TEXT,
                first_seen_at               TEXT,
                last_seen_at                TEXT,
                manager_asked_at              TEXT,
                waiting_for_manager           INTEGER DEFAULT 0,
                manager_instruction           TEXT,
                instruction_received_at      TEXT,
                last_actions_log             TEXT,
                source_type                  TEXT,
                tenant                      TEXT,
                address                     TEXT,
                service_request_description TEXT,
                job_desc                    TEXT,
                issue_type                  TEXT,
                issue_details               TEXT,
                permission_to_enter          TEXT,
                created_by                  TEXT,
                priority                    TEXT,
                resident_availability       TEXT,
                scheduling                  TEXT,
                estimates                   TEXT
            );

            CREATE TABLE IF NOT EXISTS processed_emails (
                msg_uid      TEXT PRIMARY KEY,
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS processed_replies (
                msg_uid      TEXT PRIMARY KEY,
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS management_email_audit (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                message_uid    TEXT,
                direction      TEXT NOT NULL,
                sender         TEXT,
                recipient      TEXT,
                subject        TEXT,
                body           TEXT,
                related_wo     TEXT,
                classification TEXT,
                recorded_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vendors (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                phone      TEXT,
                email      TEXT,
                trade      TEXT,
                notes      TEXT,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS property_vendors (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                property_address TEXT,
                vendor_id        INTEGER,
                trade            TEXT,
                is_preferred     INTEGER DEFAULT 1,
                source           TEXT,
                UNIQUE(property_address, vendor_id)
            );

            CREATE TABLE IF NOT EXISTS vendor_work_history (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor_id        INTEGER,
                property_address TEXT,
                wo_num           TEXT,
                issue_type       TEXT,
                description      TEXT,
                status           TEXT,
                done_on          TEXT,
                amount           REAL
            );

            CREATE TABLE IF NOT EXISTS property_review_cases (
                case_id             TEXT PRIMARY KEY,
                property_address    TEXT NOT NULL,
                normalized_address  TEXT NOT NULL,
                jurisdiction        TEXT,
                known_facts_json    TEXT NOT NULL DEFAULT '{}',
                questions_json      TEXT NOT NULL DEFAULT '[]',
                parsed_answers_json TEXT NOT NULL DEFAULT '{}',
                status              TEXT NOT NULL,
                email_subject       TEXT,
                sent_to             TEXT,
                sent_at             TEXT,
                answered_by         TEXT,
                answered_at         TEXT,
                response_text       TEXT,
                clarification_count INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS property_review_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id      TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS occupancy_classifications (
                occupancy_id                TEXT PRIMARY KEY,
                classification              TEXT NOT NULL,
                exclude_from_lease_reports  INTEGER NOT NULL DEFAULT 0,
                exclude_from_legal_research INTEGER NOT NULL DEFAULT 0,
                notes                       TEXT,
                updated_at                  TEXT NOT NULL
            );

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
            """
        )

        ensure_columns(
            conn,
            "tenants",
            {
                "occupancy_id": "TEXT",
                "property_address": "TEXT",
                "unit": "TEXT",
                "primary_tenant": "TEXT",
                "all_tenants": "TEXT",
                "lease_type": "TEXT",
                "lease_start": "TEXT",
                "lease_end": "TEXT",
                "current_rent": "REAL",
                "eligible_increase_on": "TEXT",
                "last_increase_date": "TEXT",
                "next_increase_date": "TEXT",
                "last_scraped": "TEXT",
                "past_lease_end": "INTEGER DEFAULT 0",
                "notes": "TEXT",
                "is_active": "INTEGER NOT NULL DEFAULT 1",
                "inactive_at": "TEXT",
                "inactive_reason": "TEXT",
            },
        )

        ensure_columns(
            conn,
            "vendors",
            {
                "active": "INTEGER NOT NULL DEFAULT 1",
            },
        )

        ensure_columns(
            conn,
            "wo_triage",
            {
                "wo_url": "TEXT",
                "tenant": "TEXT",
                "address": "TEXT",
                "job_desc": "TEXT",
                "issue_type": "TEXT",
                "conversation": "TEXT",
                "issues": "TEXT",
                "status": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
                "vendor_name": "TEXT",
                "nudge_count": "INTEGER DEFAULT 0",
                "last_nudge_at": "TEXT",
                "estimate_amount": "REAL",
                "scheduled_for": "TEXT",
                "resident_availability": "TEXT",
                "proposed_date": "TEXT",
                "proposed_time": "TEXT",
                "proposed_duration": "TEXT",
                "proposed_by": "TEXT",
                "waiting_for": "TEXT",
                "proposed_options": "TEXT",
            },
        )

        ensure_columns(
            conn,
            "wo_scanner_state",
            {
                "wo_url": "TEXT",
                "appfolio_status": "TEXT",
                "classification": "TEXT",
                "first_seen_at": "TEXT",
                "last_seen_at": "TEXT",
                "manager_asked_at": "TEXT",
                "waiting_for_manager": "INTEGER DEFAULT 0",
                "manager_instruction": "TEXT",
                "instruction_received_at": "TEXT",
                "last_actions_log": "TEXT",
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
            },
        )

        # Preserve useful information from the oldest legacy tenants schema.
        tenant_cols = table_columns(conn, "tenants")

        if "property" in tenant_cols and "property_address" in tenant_cols:
            conn.execute(
                """
                UPDATE tenants
                SET property_address = property
                WHERE (
                    property_address IS NULL
                    OR TRIM(property_address) = ''
                )
                  AND property IS NOT NULL
                """
            )

        if "id" in tenant_cols and "occupancy_id" in tenant_cols:
            conn.execute(
                """
                UPDATE tenants
                SET occupancy_id = 'legacy-' || id
                WHERE (
                    occupancy_id IS NULL
                    OR TRIM(occupancy_id) = ''
                )
                  AND id IS NOT NULL
                """
            )

        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_tenants_occupancy_id
            ON tenants(occupancy_id);

            CREATE INDEX IF NOT EXISTS
            idx_tenant_history_occupancy
            ON tenant_history(occupancy_id);

            CREATE INDEX IF NOT EXISTS
            idx_wo_scanner_last_seen
            ON wo_scanner_state(last_seen_at);

            CREATE INDEX IF NOT EXISTS
            idx_wo_triage_updated
            ON wo_triage(updated_at);

            CREATE INDEX IF NOT EXISTS
            idx_vendor_work_history_wo
            ON vendor_work_history(wo_num);

            CREATE INDEX IF NOT EXISTS
            idx_management_email_recorded
            ON management_email_audit(recorded_at);

            CREATE INDEX IF NOT EXISTS
            idx_property_review_address_status
            ON property_review_cases(normalized_address, status);

            CREATE INDEX IF NOT EXISTS
            idx_property_review_audit_case
            ON property_review_audit(case_id, created_at);

            CREATE INDEX IF NOT EXISTS
            idx_anthropic_usage_recorded
            ON anthropic_usage_log(recorded_at);
            """
        )

        conn.commit()

        tables = [
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]

        print(
            f"[Database] Rachel database ready at {DB_PATH}"
        )
        print(
            f"[Database] {len(tables)} application tables initialized"
        )
        print(
            "[Database] Tables: " + ", ".join(tables)
        )

    finally:
        conn.close()


if __name__ == "__main__":
    initialize_database()
