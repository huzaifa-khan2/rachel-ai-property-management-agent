"""Single-owner AppFolio browser lifecycle for Rachel production workflows."""

from __future__ import annotations

import atexit
import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from rachel_appfolio_ui import (
    AppFolioFeatureHealth,
    FeatureCircuitOpen,
    operation_feature,
)


log = logging.getLogger("rachel.appfolio_browser")


def get_browser_profile_dir() -> str:
    """Return the one canonical persistent profile used by production."""
    configured = os.getenv("RACHEL_BROWSER_PROFILE", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if os.getenv("RENDER"):
        return "/var/data/rachel_browser_profile"
    return str(Path(__file__).resolve().parent / "browser_profile")


def get_downloads_dir() -> str:
    configured = os.getenv("RACHEL_EXPORTS_DIR", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if os.getenv("RENDER"):
        return "/var/data/rachel_exports"
    return str(Path(__file__).resolve().parent / "exports")


class AppFolioBrowserManager:
    """Own one Playwright instance, persistent context, page, and operation lock."""

    def __init__(
        self,
        *,
        profile_dir: Optional[str] = None,
        downloads_dir: Optional[str] = None,
        headless: Optional[bool] = None,
        playwright_factory: Optional[Callable[[], object]] = None,
        login_callback: Optional[Callable[[object], bool]] = None,
        feature_health: Optional[AppFolioFeatureHealth] = None,
    ) -> None:
        self.profile_dir = profile_dir or get_browser_profile_dir()
        self.downloads_dir = downloads_dir or get_downloads_dir()
        self.headless = (
            headless
            if headless is not None
            else os.getenv(
                "RACHEL_HEADLESS",
                "true" if os.getenv("RENDER") else "false",
            ).strip().lower() in {"1", "true", "yes", "on"}
        )
        self._playwright_factory = playwright_factory
        self._login_callback = login_callback
        self._feature_health = feature_health
        self._lock = threading.RLock()
        self._playwright = None
        self._context = None
        self._page = None
        self._authenticated = False
        self._context_starts = 0
        self._navigation_count = 0
        self._restart_count = 0
        self._login_count = 0
        self._profile_lock_file = None
        self._active_operation = "idle"

    def _default_playwright_factory(self):
        from playwright.sync_api import sync_playwright

        return sync_playwright()

    def _default_login_callback(self):
        # Deferred import avoids a module cycle while preserving the hardened
        # login and 2FA flow already maintained by rachel_wo_triage.
        from rachel_wo_triage import login_appfolio

        return login_appfolio

    def _on_frame_navigated(self, frame) -> None:
        try:
            if frame != self._page.main_frame:
                return
        except Exception:
            return
        self._navigation_count += 1
        log.info(
            "AppFolio navigation operation=%s count=%s",
            self._active_operation,
            self._navigation_count,
        )

    def _start_locked(self) -> None:
        if self._context is not None:
            if self._page_is_healthy_locked():
                return
            # A page can be closed or crash between serialized operations.
            # Tear down the old context and release its profile lock before
            # acquiring a replacement; otherwise the manager can leak the
            # old browser and contend with its own persistent-profile lock.
            self._restart_locked("unhealthy_page_before_operation")

        os.makedirs(self.profile_dir, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)

        lock_path = f"{self.profile_dir}.lock"
        self._profile_lock_file = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(
                self._profile_lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self._profile_lock_file.close()
            self._profile_lock_file = None
            raise RuntimeError(
                "Another process owns the AppFolio browser profile"
            ) from exc

        try:
            factory = (
                self._playwright_factory or self._default_playwright_factory
            )
            controller = factory()
            self._playwright = (
                controller.start()
                if hasattr(controller, "start")
                else controller
            )
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir,
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                accept_downloads=True,
                downloads_path=self.downloads_dir,
                viewport={"width": 1280, "height": 900},
            )
        except Exception:
            # Release the profile lock even when Playwright fails before a
            # browser context exists, so the next scheduler cycle can recover.
            self._close_locked()
            raise
        self._page = (
            self._context.pages[0]
            if getattr(self._context, "pages", None)
            else self._context.new_page()
        )
        if hasattr(self._page, "on"):
            self._page.on("framenavigated", self._on_frame_navigated)
        self._authenticated = False
        self._context_starts += 1
        log.info(
            "AppFolio browser context started count=%s headless=%s",
            self._context_starts,
            self.headless,
        )

    def _page_is_healthy_locked(self) -> bool:
        if self._page is None:
            return False
        try:
            self._page.evaluate("() => true")
            return True
        except Exception:
            return False

    def _ensure_login_locked(
        self,
        login_callback: Optional[Callable[[object], bool]],
    ) -> None:
        if self._authenticated:
            current_url = str(getattr(self._page, "url", "")).lower()
            if not any(
                marker in current_url
                for marker in ("login", "authenticate", "account.appfolio")
            ):
                return
            self._authenticated = False
        callback = (
            login_callback
            or self._login_callback
            or self._default_login_callback()
        )
        if not callback(self._page):
            raise RuntimeError("AppFolio login failed")
        self._authenticated = True
        self._login_count += 1
        login_kind = "initial" if self._login_count == 1 else "relogin"
        log.info(
            "AppFolio login successful kind=%s operation=%s count=%s",
            login_kind,
            self._active_operation,
            self._login_count,
        )

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        login: bool = True,
        login_callback: Optional[Callable[[object], bool]] = None,
        track_feature_health: bool = True,
    ) -> Iterator[object]:
        """Serialize one complete AppFolio operation and provide its page."""
        started = time.monotonic()
        with self._lock:
            previous_operation = self._active_operation
            self._active_operation = name
            feature = operation_feature(name)
            operation_succeeded = False
            log.info("AppFolio operation started name=%s", name)
            try:
                if self._feature_health and track_feature_health:
                    self._feature_health.before(feature)
                self._start_locked()
                if login:
                    self._ensure_login_locked(login_callback)
                yield self._page
                operation_succeeded = True
            except FeatureCircuitOpen:
                raise
            except Exception as error:
                if self._feature_health and track_feature_health:
                    self._feature_health.record_failure(feature, error)
                if self._context is not None and not self._page_is_healthy_locked():
                    self._restart_locked("unhealthy_page")
                raise
            finally:
                if (
                    operation_succeeded and self._feature_health
                    and track_feature_health
                ):
                    self._feature_health.record_success(
                        feature, "operation_completed"
                    )
                duration = time.monotonic() - started
                log.info(
                    "AppFolio operation finished name=%s duration_seconds=%.2f",
                    name,
                    duration,
                )
                self._active_operation = previous_operation

    @contextmanager
    def feature_scope(self, feature: str) -> Iterator[None]:
        """Attribute one sub-workflow without creating another browser."""
        if not self._feature_health:
            yield
            return
        self._feature_health.before(feature)
        try:
            yield
        except FeatureCircuitOpen:
            raise
        except Exception as error:
            self._feature_health.record_failure(feature, error)
            raise
        else:
            self._feature_health.record_success(
                feature, "semantic_contract_completed"
            )

    def mark_login_required(self) -> None:
        with self._lock:
            self._authenticated = False

    def _restart_locked(self, reason: str) -> None:
        self._restart_count += 1
        log.warning(
            "AppFolio browser restart reason=%s count=%s",
            reason,
            self._restart_count,
        )
        self._close_locked()

    def restart(self, reason: str = "requested") -> None:
        with self._lock:
            self._restart_locked(reason)

    def _close_locked(self) -> None:
        context, playwright = self._context, self._playwright
        profile_lock_file = self._profile_lock_file
        self._page = None
        self._context = None
        self._playwright = None
        self._profile_lock_file = None
        self._authenticated = False
        if context is not None:
            try:
                context.close()
            except Exception:
                log.exception("AppFolio browser context close failed")
        if playwright is not None and hasattr(playwright, "stop"):
            try:
                playwright.stop()
            except Exception:
                log.exception("Playwright stop failed")
        if profile_lock_file is not None:
            try:
                fcntl.flock(profile_lock_file.fileno(), fcntl.LOCK_UN)
                profile_lock_file.close()
            except Exception:
                log.exception("AppFolio profile lock release failed")

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    @property
    def metrics(self) -> dict:
        with self._lock:
            return {
                "context_starts": self._context_starts,
                "navigations": self._navigation_count,
                "restarts": self._restart_count,
                "successful_logins": self._login_count,
            }


_MANAGER = AppFolioBrowserManager(feature_health=AppFolioFeatureHealth())
atexit.register(_MANAGER.close)


def get_appfolio_browser_manager() -> AppFolioBrowserManager:
    return _MANAGER
