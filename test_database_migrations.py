import os
import sqlite3
import tempfile
import unittest

import rachel_db_init
import rachel_seed_vendors


class DatabaseMigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_idempotently_in_temporary_file(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "legacy.db")
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE tenants (
                    id INTEGER PRIMARY KEY,
                    property TEXT
                );
                INSERT INTO tenants (id, property)
                VALUES (7, '100 Oak Street');

                CREATE TABLE wo_triage (
                    wo_num TEXT PRIMARY KEY
                );
                CREATE TABLE wo_scanner_state (
                    wo_num TEXT PRIMARY KEY
                );
                """
            )
            conn.commit()
            conn.close()

            original_path = rachel_db_init.DB_PATH
            rachel_db_init.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                rachel_db_init.initialize_database()
            finally:
                rachel_db_init.DB_PATH = original_path

            conn = sqlite3.connect(db_path)
            try:
                tenant = conn.execute(
                    "SELECT occupancy_id, property_address FROM tenants "
                    "WHERE id = 7"
                ).fetchone()
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                scanner_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(wo_scanner_state)"
                    )
                }
                triage_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(wo_triage)"
                    )
                }
                vendor_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(vendors)"
                    )
                }
                selector_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(appfolio_thread_selector_state)"
                    )
                }
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
            finally:
                conn.close()

            self.assertEqual(tenant, ("legacy-7", "100 Oak Street"))
            self.assertTrue(
                {
                    "internal_users",
                    "communications",
                    "appfolio_communication_state",
                    "appfolio_action_audit",
                    "appfolio_thread_selector_state",
                    "communication_drafts",
                    "communication_send_intents",
                    "appfolio_outbound_intents",
                    "maintenance_schedule_states",
                    "maintenance_schedule_events",
                    "lease_window_events",
                    "lease_window_runs",
                    "rachel_runtime_state",
                }.issubset(tables)
            )
            self.assertTrue(
                {
                    "list_fingerprint",
                    "detail_fingerprint",
                    "last_detail_scanned_at",
                    "list_fields_json",
                    "baseline_generation",
                }.issubset(scanner_columns)
            )
            self.assertTrue(
                {
                    "resident_availability",
                    "proposed_date",
                    "proposed_time",
                    "proposed_duration",
                    "proposed_by",
                    "waiting_for",
                    "proposed_options",
                }.issubset(triage_columns)
            )
            self.assertIn("active", vendor_columns)
            self.assertTrue(
                {"latest_event_key", "last_unread"}.issubset(
                    selector_columns
                )
            )
            self.assertIn("idx_tenants_occupancy_id", indexes)
            self.assertIn("idx_appfolio_action_target", indexes)
            self.assertIn("idx_vendor_work_history_wo", indexes)

    def test_vendor_seed_is_restart_idempotent_and_preserves_deactivation(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "rachel.db")
            original_init_path = rachel_db_init.DB_PATH
            original_seed_path = rachel_seed_vendors.DB_PATH
            rachel_db_init.DB_PATH = db_path
            rachel_seed_vendors.DB_PATH = db_path
            try:
                rachel_db_init.initialize_database()
                rachel_seed_vendors.run()
                rachel_seed_vendors.run()

                conn = sqlite3.connect(db_path)
                first_vendor = rachel_seed_vendors.VENDORS[0]["name"]
                conn.execute(
                    "UPDATE vendors SET active = 0 WHERE name = ?",
                    (first_vendor,),
                )
                conn.commit()
                conn.close()

                rachel_seed_vendors.run()

                conn = sqlite3.connect(db_path)
                try:
                    seeded_history = conn.execute(
                        "SELECT COUNT(*) FROM vendor_work_history "
                        "WHERE wo_num IN ({})".format(
                            ",".join(
                                "?" for _row in rachel_seed_vendors.WO_HISTORY
                            )
                        ),
                        [row["wo_num"] for row in rachel_seed_vendors.WO_HISTORY],
                    ).fetchone()[0]
                    active = conn.execute(
                        "SELECT active FROM vendors WHERE name = ?",
                        (first_vendor,),
                    ).fetchone()[0]
                finally:
                    conn.close()

                self.assertEqual(
                    seeded_history,
                    len(rachel_seed_vendors.WO_HISTORY),
                )
                self.assertEqual(active, 0)
            finally:
                rachel_db_init.DB_PATH = original_init_path
                rachel_seed_vendors.DB_PATH = original_seed_path


if __name__ == "__main__":
    unittest.main()
