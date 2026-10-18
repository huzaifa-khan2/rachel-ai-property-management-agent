"""
Guards against the class of defect where `import some_module` performs live
side effects instead of only defining classes/functions/constants.

Regression case: main_before_aug1_lease_cycle.py and
main_before_centralized_database_init.py (legacy pre-refactor snapshots of
main.py, now moved to legacy_snapshots/*.py.snapshot so they can never be
imported) ran their entire bootstrap - DB seed, an inbox-monitor daemon
thread, and an infinite scheduler loop - as bare top-level script code with
no `if __name__ == "__main__":` guard. A blanket `import` of every *.py file
in the repo therefore launched a real browser, navigated to AppFolio, typed
a dummy credential into the live login form, and hung forever.

Each candidate module is imported in a fresh subprocess with the dangerous
entry points listed above patched to raise instead of act, plus a wall-clock
timeout so an infinite loop at import time fails fast with a clear message
instead of hanging the test run.
"""

import pathlib
import subprocess
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent

IMPORT_TIMEOUT_SECONDS = 10

_GUARD_SCRIPT = """
import sys

def _blocked(what):
    def _raise(*a, **k):
        raise RuntimeError(f"BLOCKED_AT_IMPORT: {what} called while importing {sys.argv[1]!r}")
    return _raise

import threading
threading.Thread.start = _blocked("threading.Thread.start")

import subprocess as _subprocess
_subprocess.Popen.__init__ = _blocked("subprocess.Popen")

import socket
socket.socket.connect = _blocked("socket.socket.connect")
socket.create_connection = _blocked("socket.create_connection")

import smtplib
smtplib.SMTP.__init__ = _blocked("smtplib.SMTP")
smtplib.SMTP_SSL.__init__ = _blocked("smtplib.SMTP_SSL")

try:
    import playwright.sync_api as _pw
    _pw.sync_playwright = _blocked("playwright.sync_api.sync_playwright")
except ImportError:
    pass

import importlib
importlib.import_module(sys.argv[1])
"""

_DUMMY_ENV_VALUES = {
    "ANTHROPIC_API_KEY": "deterministic-test-value",
    "APPFOLIO_EMAIL": "deterministic-test-value",
    "APPFOLIO_PASSWORD": "deterministic-test-value",
    "RACHEL_GMAIL_USER": "deterministic-test-value",
    "RACHEL_GMAIL_PASSWORD": "deterministic-test-value",
    "RACHEL_EMAIL_PASSWORD": "deterministic-test-value",
}


def _production_modules():
    modules = []
    for path in sorted(REPO_ROOT.glob("*.py")):
        name = path.name
        if name.startswith("test_"):
            continue
        if "_before_" in name:
            continue
        stem = path.stem
        if not stem.isidentifier():
            # e.g. rachel_lease_tracker_v2.1.py - the literal dot makes this
            # an invalid module name, so Python can never `import` it. It is
            # only ever invoked as a subprocess script (see main.py's
            # run_script("rachel_lease_tracker_v2.1.py")), which is outside
            # the blind-import hazard this test guards against.
            continue
        modules.append(stem)
    return modules


class ImportSideEffectTests(unittest.TestCase):
    def test_every_production_module_imports_without_live_side_effects(self):
        import os

        env = dict(os.environ)
        env.update(_DUMMY_ENV_VALUES)
        env.pop("RACHEL_ENABLED", None)
        env.pop("RACHEL_APPFOLIO_QA_ENABLED", None)
        env.pop("RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED", None)

        failures = []
        for module_name in _production_modules():
            try:
                result = subprocess.run(
                    [sys.executable, "-c", _GUARD_SCRIPT, module_name],
                    cwd=str(REPO_ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=IMPORT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                failures.append(
                    f"{module_name}: import did not return within "
                    f"{IMPORT_TIMEOUT_SECONDS}s (likely a blocking loop or "
                    f"live network/browser call at import time)"
                )
                continue

            if result.returncode != 0:
                failures.append(
                    f"{module_name}: exit {result.returncode}\n"
                    f"{result.stderr[-2000:]}"
                )

        if failures:
            self.fail(
                f"{len(failures)} module(s) have import-time side effects:\n\n"
                + "\n\n".join(failures)
            )


if __name__ == "__main__":
    unittest.main()
