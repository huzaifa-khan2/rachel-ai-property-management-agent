import json
import os
import sqlite3
import tempfile
import unittest

from rachel_event_memory import (
    find_related_work_order,
    inbox_event_key,
    internal_user_has_access,
    initialize_event_memory,
    record_communication_state,
    record_verified_appfolio_change,
    store_communication,
    unseen_communication_rows,
)


class EventMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "rachel.db")
        initialize_event_memory(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE wo_triage (
                wo_num TEXT PRIMARY KEY, wo_url TEXT, tenant TEXT,
                address TEXT, vendor_name TEXT, status TEXT,
                scheduled_for TEXT, updated_at TEXT
            );
            CREATE TABLE wo_scanner_state (
                wo_num TEXT PRIMARY KEY, wo_url TEXT, tenant TEXT,
                address TEXT, appfolio_status TEXT, last_seen_at TEXT
            );
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def sample_row(self):
        return {
            "thread_id": "thread-17",
            "source_message_id": "message-9",
            "href": "/texting/inbox/thread-17",
            "preview": "The neighbor's dog is barking again",
            "timestamp": "August 8, 2026 10:00 AM",
        }

    def test_new_communication_detected(self):
        rows = unseen_communication_rows([self.sample_row()], self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_key"], inbox_event_key(self.sample_row()))

    def test_previously_seen_communication_ignored(self):
        row = self.sample_row()
        record_communication_state(row, "stored", db_path=self.db_path)
        self.assertEqual(unseen_communication_rows([row], self.db_path), [])

    def test_non_work_order_communication_stored_without_forced_link(self):
        communication_id = store_communication(
            {
                "source": "appfolio_communication_inbox",
                "source_thread_id": "thread-dog",
                "contact_name": "Jamie Tenant",
                "contact_role": "tenant",
                "property_address": "100 Oak Street",
                "unit": "2",
                "direction": "inbound",
                "message_text": "The neighbor's dog is a nuisance.",
                "processing_status": "stored",
            },
            self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT related_wo, message_text FROM communications "
            "WHERE communication_id = ?",
            (communication_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "")
        self.assertIn("dog", row[1])

    def test_explicit_work_order_communication_associated(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wo_triage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1638-1", "/work_orders/99", "Jamie Tenant",
                "100 Oak Street Unit 2", "", "awaiting_reply", "", "now",
            ),
        )
        conn.commit()
        conn.close()
        related = find_related_work_order(
            message_text="This is about WO #1638-1",
            contact_name="Jamie Tenant",
            contact_role="tenant",
            db_path=self.db_path,
        )
        self.assertEqual(related["wo_num"], "1638-1")

    def test_contextual_work_order_link_requires_exactly_one_active_match(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wo_triage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1700-1", "/work_orders/1700", "Jamie Tenant",
                "100 Oak Street Unit 2", "", "awaiting_reply", "", "now",
            ),
        )
        conn.commit()
        conn.close()
        unique = find_related_work_order(
            message_text="A contextual reply",
            contact_name="Jamie Tenant",
            contact_role="tenant",
            property_address="100 Oak Street",
            unit="2",
            db_path=self.db_path,
        )
        self.assertEqual(unique["wo_num"], "1700-1")

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wo_triage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1700-2", "/work_orders/1701", "Jamie Tenant",
                "100 Oak Street Unit 2", "", "awaiting_reply", "", "later",
            ),
        )
        conn.commit()
        conn.close()
        ambiguous = find_related_work_order(
            message_text="Still contextual",
            contact_name="Jamie Tenant",
            contact_role="tenant",
            property_address="100 Oak Street",
            unit="2",
            db_path=self.db_path,
        )
        self.assertIsNone(ambiguous)

    def test_verified_write_updates_memory_and_audit(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wo_triage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2000-1", "/work_orders/20", "T", "A", "", "new", "", "now"),
        )
        conn.commit()
        conn.close()
        audit_id = record_verified_appfolio_change(
            operation="dispatch_vendor",
            target_type="work_order",
            target_id="2000-1",
            new_state={
                "workflow_status": "vendor_assigned",
                "vendor_name": "Vendor One",
            },
            internal_user_id="regional-manager",
            db_path=self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        status, vendor = conn.execute(
            "SELECT status, vendor_name FROM wo_triage WHERE wo_num = '2000-1'"
        ).fetchone()
        audit = conn.execute(
            "SELECT internal_user_id, new_state_json FROM appfolio_action_audit WHERE id = ?",
            (audit_id,),
        ).fetchone()
        conn.close()
        self.assertEqual((status, vendor), ("vendor_assigned", "Vendor One"))
        self.assertEqual(audit[0], "regional-manager")
        self.assertEqual(json.loads(audit[1])["vendor_name"], "Vendor One")

    def test_unverified_write_cannot_mutate_memory_or_create_audit(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wo_triage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2000-2", "/work_orders/21", "T", "A", "", "new", "", "now"),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "unverified AppFolio change"):
            record_verified_appfolio_change(
                operation="dispatch_vendor",
                target_type="work_order",
                target_id="2000-2",
                new_state={"workflow_status": "vendor_assigned"},
                verification_result="sending_unverified",
                db_path=self.db_path,
            )
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM wo_triage WHERE wo_num = '2000-2'"
        ).fetchone()[0]
        audits = conn.execute(
            "SELECT COUNT(*) FROM appfolio_action_audit "
            "WHERE target_id = '2000-2'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "new")
        self.assertEqual(audits, 0)

    def test_verified_write_requires_complete_nonempty_target_and_state(self):
        for changes in (
            {"operation": ""},
            {"target_type": ""},
            {"target_id": ""},
            {"new_state": {}},
        ):
            kwargs = {
                "operation": "dispatch_vendor",
                "target_type": "work_order",
                "target_id": "2000-3",
                "new_state": {"workflow_status": "vendor_assigned"},
                "db_path": self.db_path,
            }
            kwargs.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                record_verified_appfolio_change(**kwargs)

    def test_company_user_seed_is_scoped_not_single_instance(self):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT role, portfolio_scope, permission_scope FROM internal_users "
            "WHERE user_id = 'regional-manager'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "property_manager")
        self.assertEqual(json.loads(row[1]), ["*"])
        self.assertIn("property_management", json.loads(row[2]))
        self.assertTrue(
            internal_user_has_access(
                "regional-manager",
                permission="property_management",
                property_key="100 Oak Street",
                db_path=self.db_path,
            )
        )
        self.assertFalse(
            internal_user_has_access(
                "regional-manager",
                permission="system_administration",
                property_key="100 Oak Street",
                db_path=self.db_path,
            )
        )

    def test_selector_state_schema_migrates_existing_database_in_place(self):
        legacy_path = os.path.join(self.tempdir.name, "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE appfolio_thread_selector_state ("
            "selector_key TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
            "processing_status TEXT NOT NULL, last_error TEXT NOT NULL "
            "DEFAULT '', updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO appfolio_thread_selector_state VALUES "
            "('selector-1', 'thread-1', 'thread_read_verified', '', 'now')"
        )
        conn.commit()
        conn.close()
        initialize_event_memory(legacy_path)
        conn = sqlite3.connect(legacy_path)
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(appfolio_thread_selector_state)"
            ).fetchall()
        }
        row = conn.execute(
            "SELECT latest_event_key, last_unread, processing_status "
            "FROM appfolio_thread_selector_state WHERE selector_key = ?",
            ("selector-1",),
        ).fetchone()
        conn.close()
        self.assertTrue({"latest_event_key", "last_unread"} <= columns)
        self.assertEqual(row, ("", 0, "thread_read_verified"))

    def test_limited_user_cannot_cross_portfolio_boundary(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO internal_users (
                user_id, display_name, email, role, portfolio_scope,
                permission_scope, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                "casey-pm",
                "Casey PM",
                "casey@example.com",
                "property_manager",
                '["100 Oak Street"]',
                '["property_management"]',
                "now",
                "now",
            ),
        )
        conn.commit()
        conn.close()
        self.assertTrue(
            internal_user_has_access(
                "casey-pm",
                permission="property_management",
                property_key="100 Oak Street Unit 2",
                db_path=self.db_path,
            )
        )
        self.assertFalse(
            internal_user_has_access(
                "casey-pm",
                permission="property_management",
                property_key="200 Pine Avenue",
                db_path=self.db_path,
            )
        )


if __name__ == "__main__":
    unittest.main()
