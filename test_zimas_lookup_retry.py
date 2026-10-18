"""
Offline regression coverage for the ZIMAS RSO lookup retry wrapper in
rachel_lease_tracker_v2.1.py (rent-control research feeding the monthly
lease/rent-increase cycle). No live browser or network involved: the
browser-touching _lookup_zimas_rso_once is stubbed out so only the retry,
backoff, and fallback logic under test.

The module file has a literal dot before .py (rachel_lease_tracker_v2.1.py),
which is not a valid Python module name, so it is loaded by file path
instead of `import`.
"""

import importlib.util
import os
import pathlib
import unittest
from unittest.mock import patch

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent / "rachel_lease_tracker_v2.1.py"
)


def _load_module():
    for key in (
        "ANTHROPIC_API_KEY",
        "APPFOLIO_EMAIL",
        "APPFOLIO_PASSWORD",
        "RACHEL_GMAIL_USER",
        "RACHEL_GMAIL_PASSWORD",
        "RACHEL_EMAIL_PASSWORD",
    ):
        os.environ.setdefault(key, "deterministic-test-value")
    spec = importlib.util.spec_from_file_location(
        "rachel_lease_tracker_zimas_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ZimasLookupRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tracker = _load_module()

    def setUp(self):
        os.environ.pop("RACHEL_ZIMAS_MAX_ATTEMPTS", None)
        self.sleep_patcher = patch.object(self.tracker.time, "sleep")
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_succeeds_on_first_attempt_without_retry(self):
        calls = []

        def fake_once(*, address, matched_address):
            calls.append(address)
            return {"status": "LOOKUP_COMPLETE", "rso_result": "YES"}

        with patch.object(
            self.tracker, "_lookup_zimas_rso_once", side_effect=fake_once
        ):
            result = self.tracker.lookup_zimas_rso("123 Oak St")

        self.assertEqual(result["status"], "LOOKUP_COMPLETE")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(calls), 1)

    def test_retries_transient_failure_then_succeeds(self):
        responses = [
            {"status": "LOOKUP_FAILED", "rso_result": "UNKNOWN"},
            {"status": "LOOKUP_COMPLETE", "rso_result": "NO"},
        ]

        with patch.object(
            self.tracker, "_lookup_zimas_rso_once", side_effect=responses
        ) as stub:
            result = self.tracker.lookup_zimas_rso("456 Elm St")

        self.assertEqual(result["status"], "LOOKUP_COMPLETE")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(stub.call_count, 2)

    def test_non_retryable_status_stops_after_one_attempt(self):
        with patch.object(
            self.tracker,
            "_lookup_zimas_rso_once",
            return_value={"status": "SOME_UNEXPECTED_STATUS"},
        ) as stub:
            result = self.tracker.lookup_zimas_rso("789 Pine St")

        self.assertEqual(stub.call_count, 1)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["rso_result"], "UNKNOWN")

    def test_exhausts_configured_max_attempts_and_falls_back_to_unknown(self):
        os.environ["RACHEL_ZIMAS_MAX_ATTEMPTS"] = "2"
        with patch.object(
            self.tracker,
            "_lookup_zimas_rso_once",
            return_value={"status": "LOOKUP_FAILED", "rso_result": None},
        ) as stub:
            result = self.tracker.lookup_zimas_rso("321 Maple Ave")

        self.assertEqual(stub.call_count, 2)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["rso_result"], "UNKNOWN")

    def test_max_attempts_env_var_cannot_go_below_one(self):
        os.environ["RACHEL_ZIMAS_MAX_ATTEMPTS"] = "0"
        with patch.object(
            self.tracker,
            "_lookup_zimas_rso_once",
            return_value={"status": "LOOKUP_FAILED", "rso_result": "UNKNOWN"},
        ) as stub:
            self.tracker.lookup_zimas_rso("1 Single Attempt Way")

        self.assertEqual(stub.call_count, 1)


if __name__ == "__main__":
    unittest.main()
