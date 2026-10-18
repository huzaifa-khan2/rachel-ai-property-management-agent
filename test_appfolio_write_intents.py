import os
import sqlite3
import tempfile
import unittest

from rachel_appfolio_write_intents import (
    AppFolioWriteIntentError,
    claim_outbound_intent,
    complete_outbound_intent,
    prepare_outbound_intent,
    record_outbound_intent_failure,
)


class AppFolioWriteIntentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "rachel.db")

    def tearDown(self):
        self.tempdir.cleanup()

    def _prepare(self):
        return prepare_outbound_intent(
            operation="vendor_schedule_message",
            target_id="2000-1",
            recipient="Safe Vendor",
            message="One specific message",
            db_path=self.db_path,
        )

    def test_preparation_is_idempotent_and_content_bound(self):
        first = self._prepare()
        second = self._prepare()
        changed = prepare_outbound_intent(
            operation="vendor_schedule_message",
            target_id="2000-1",
            recipient="Safe Vendor",
            message="Different message",
            db_path=self.db_path,
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["intent_id"], second["intent_id"])
        self.assertNotEqual(first["intent_id"], changed["intent_id"])

    def test_claim_survives_restart_and_cannot_be_claimed_twice(self):
        intent = self._prepare()
        claimed = claim_outbound_intent(
            intent["intent_id"], db_path=self.db_path
        )
        self.assertEqual(claimed["status"], "sending_unverified")
        self.assertEqual(claimed["attempt_count"], 1)
        with self.assertRaisesRegex(
            AppFolioWriteIntentError,
            "outbound_intent_not_prepared_sending_unverified",
        ):
            claim_outbound_intent(intent["intent_id"], db_path=self.db_path)

    def test_verified_completion_is_terminal(self):
        intent = self._prepare()
        claim_outbound_intent(intent["intent_id"], db_path=self.db_path)
        complete = complete_outbound_intent(
            intent["intent_id"],
            verification_receipt="verified-form-closed",
            db_path=self.db_path,
        )
        self.assertEqual(complete["status"], "sent_verified")
        with self.assertRaises(AppFolioWriteIntentError):
            claim_outbound_intent(intent["intent_id"], db_path=self.db_path)

    def test_failure_reason_is_sanitized_and_state_remains_ambiguous(self):
        intent = self._prepare()
        claim_outbound_intent(intent["intent_id"], db_path=self.db_path)
        record_outbound_intent_failure(
            intent["intent_id"],
            reason="raw private detail must not persist",
            db_path=self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        status, failure = conn.execute(
            "SELECT status, failure_reason FROM appfolio_outbound_intents"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "sending_unverified")
        self.assertEqual(failure, "outbound_transport_failure")


if __name__ == "__main__":
    unittest.main()
