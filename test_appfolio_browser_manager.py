import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rachel_appfolio_browser import (
    AppFolioBrowserManager,
    get_browser_profile_dir,
)


class FakePage:
    def __init__(self):
        self.url = "https://example.appfolio.com/dashboard"
        self.main_frame = self
        self.events = {}

    def evaluate(self, _script):
        return True

    def on(self, name, callback):
        self.events[name] = callback


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.chromium = self
        self.launches = []
        self.contexts = []
        self.stopped = False

    def start(self):
        return self

    def launch_persistent_context(self, **kwargs):
        self.launches.append(kwargs)
        context = FakeContext(FakePage())
        self.contexts.append(context)
        return context

    def stop(self):
        self.stopped = True


class AppFolioBrowserManagerTests(unittest.TestCase):
    def make_manager(self, root, fake, login_callback=None):
        return AppFolioBrowserManager(
            profile_dir=os.path.join(root, "profile"),
            downloads_dir=os.path.join(root, "downloads"),
            headless=True,
            playwright_factory=lambda: fake,
            login_callback=login_callback or (lambda _page: True),
        )

    def test_reuses_one_context_and_login(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakePlaywright()
            login_calls = []
            manager = self.make_manager(
                root,
                fake,
                login_callback=lambda page: login_calls.append(page) or True,
            )

            with manager.operation("first") as first_page:
                self.assertIsNotNone(first_page)
            with manager.operation("second") as second_page:
                self.assertIs(first_page, second_page)

            self.assertEqual(len(fake.launches), 1)
            self.assertEqual(len(login_calls), 1)
            self.assertEqual(manager.metrics["context_starts"], 1)
            self.assertEqual(manager.metrics["successful_logins"], 1)
            manager.close()
            self.assertTrue(fake.contexts[0].closed)
            self.assertTrue(fake.stopped)

    def test_serializes_operations(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakePlaywright()
            manager = self.make_manager(root, fake)
            active = 0
            maximum_active = 0
            guard = threading.Lock()

            def worker(name):
                nonlocal active, maximum_active
                with manager.operation(name):
                    with guard:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    time.sleep(0.05)
                    with guard:
                        active -= 1

            threads = [
                threading.Thread(target=worker, args=(f"worker_{index}",))
                for index in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(maximum_active, 1)
            self.assertEqual(len(fake.launches), 1)
            manager.close()

    def test_profile_lock_blocks_a_second_process_owner(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.make_manager(root, FakePlaywright())
            second = self.make_manager(root, FakePlaywright())

            with first.operation("owner"):
                with self.assertRaisesRegex(RuntimeError, "Another process"):
                    with second.operation("contender"):
                        pass
            first.close()
            second.close()

    def test_playwright_start_failure_releases_profile_lock(self):
        with tempfile.TemporaryDirectory() as root:
            failed = AppFolioBrowserManager(
                profile_dir=os.path.join(root, "profile"),
                downloads_dir=os.path.join(root, "downloads"),
                headless=True,
                playwright_factory=lambda: (_ for _ in ()).throw(
                    RuntimeError("playwright startup failed")
                ),
                login_callback=lambda _page: True,
            )
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                with failed.operation("failed_start"):
                    self.fail("A failed Playwright start must not yield a page")

            recovered = self.make_manager(root, FakePlaywright())
            with recovered.operation("recovered") as page:
                self.assertIsNotNone(page)
            recovered.close()
            failed.close()

    def test_restart_creates_a_fresh_context(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakePlaywright()
            manager = self.make_manager(root, fake)
            with manager.operation("before_restart"):
                pass
            manager.restart("unit_test")
            with manager.operation("after_restart"):
                pass

            self.assertEqual(len(fake.launches), 2)
            self.assertEqual(manager.metrics["restarts"], 1)
            manager.close()

    def test_unhealthy_page_between_operations_releases_old_context_and_lock(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakePlaywright()
            manager = self.make_manager(root, fake)
            with manager.operation("healthy") as first_page:
                pass

            def unhealthy(_script):
                raise RuntimeError("page closed")

            first_page.evaluate = unhealthy
            with manager.operation("recovered") as recovered_page:
                self.assertIsNot(first_page, recovered_page)

            self.assertEqual(len(fake.launches), 2)
            self.assertTrue(fake.contexts[0].closed)
            self.assertEqual(manager.metrics["restarts"], 1)
            manager.close()

    def test_login_page_triggers_a_relogin(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakePlaywright()
            login_calls = []
            manager = self.make_manager(
                root,
                fake,
                login_callback=lambda page: login_calls.append(page) or True,
            )
            with manager.operation("initial") as page:
                page.url = "https://example.appfolio.com/login"
            with manager.operation("expired_session"):
                pass

            self.assertEqual(len(login_calls), 2)
            self.assertEqual(manager.metrics["successful_logins"], 2)
            manager.close()

    def test_rejected_login_fails_closed_and_is_not_cached(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakePlaywright()
            login_calls = []
            manager = self.make_manager(
                root,
                fake,
                login_callback=lambda page: login_calls.append(page) or False,
            )

            for operation_name in ("rejected_once", "rejected_again"):
                with self.assertRaisesRegex(RuntimeError, "login failed"):
                    with manager.operation(operation_name):
                        self.fail("A rejected login must never yield a page")

            self.assertEqual(len(fake.launches), 1)
            self.assertEqual(len(login_calls), 2)
            self.assertEqual(manager.metrics["successful_logins"], 0)
            manager.close()

    def test_render_default_profile_path(self):
        with patch.dict(
            os.environ,
            {"RENDER": "true"},
            clear=False,
        ):
            os.environ.pop("RACHEL_BROWSER_PROFILE", None)
            self.assertEqual(
                get_browser_profile_dir(),
                "/var/data/rachel_browser_profile",
            )


if __name__ == "__main__":
    unittest.main()
