"""
Centralized operational safety controls for Rachel.

All outbound email, SMS, AppFolio writes, vendor contact, and other
consequential actions should pass through this module.
"""

import os
from typing import Tuple


TRUE_VALUES = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in TRUE_VALUES


RACHEL_ENABLED = env_bool("RACHEL_ENABLED", default=False)
RACHEL_TEST_MODE = env_bool("RACHEL_TEST_MODE", default=True)
RACHEL_OWNER_QA_MODE = env_bool("RACHEL_OWNER_QA_MODE", default=False)
RACHEL_SMS_ENABLED = env_bool("RACHEL_SMS_ENABLED", default=False)
RACHEL_APPFOLIO_WRITES_ENABLED = env_bool(
    "RACHEL_APPFOLIO_WRITES_ENABLED", default=False
)
RACHEL_APPFOLIO_MESSAGING_ENABLED = env_bool(
    "RACHEL_APPFOLIO_MESSAGING_ENABLED", default=False
)
RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED = env_bool(
    "RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED", default=False
)
RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED = env_bool(
    "RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED", default=False
)
RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_SELECTOR_KEY = os.getenv(
    "RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_SELECTOR_KEY", ""
).strip().lower()
RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_AUTHORIZATION_ID = os.getenv(
    "RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_AUTHORIZATION_ID", ""
).strip()

OWNER_QA_EMAIL = os.getenv(
    "RACHEL_OWNER_QA_EMAIL",
    "owner@clientco.example",
).strip().lower()

# A controlled real-AppFolio test remains disabled unless every value below
# is explicitly configured. Only explicitly allowlisted operations may run,
# and only against the exact configured work order, tenant, and vendor.
RACHEL_APPFOLIO_QA_ENABLED = env_bool(
    "RACHEL_APPFOLIO_QA_ENABLED",
    default=False,
)
RACHEL_APPFOLIO_QA_WO_NUM = os.getenv(
    "RACHEL_APPFOLIO_QA_WO_NUM",
    "",
).strip()
RACHEL_APPFOLIO_QA_WO_ID = os.getenv(
    "RACHEL_APPFOLIO_QA_WO_ID",
    "",
).strip()
RACHEL_APPFOLIO_QA_TENANT = os.getenv(
    "RACHEL_APPFOLIO_QA_TENANT",
    "",
).strip().lower()
RACHEL_APPFOLIO_QA_VENDOR = os.getenv(
    "RACHEL_APPFOLIO_QA_VENDOR",
    "",
).strip().lower()
RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED = env_bool(
    "RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED",
    default=False,
)
RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY = os.getenv(
    "RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY",
    "",
).strip().lower()
RACHEL_APPFOLIO_READONLY_THREAD_QA_EXPECTED_EVENT_KEY = os.getenv(
    "RACHEL_APPFOLIO_READONLY_THREAD_QA_EXPECTED_EVENT_KEY",
    "",
).strip().lower()
RACHEL_APPFOLIO_READONLY_THREAD_QA_DELEGATED_BY = os.getenv(
    "RACHEL_APPFOLIO_READONLY_THREAD_QA_DELEGATED_BY",
    "",
).strip()
RACHEL_APPFOLIO_READONLY_THREAD_QA_AUTHORIZATION_ID = os.getenv(
    "RACHEL_APPFOLIO_READONLY_THREAD_QA_AUTHORIZATION_ID",
    "",
).strip()


CONTROLLED_APPFOLIO_QA_OPERATIONS = {
    "dispatch_vendor",
    "vendor_schedule_message",
    "tenant_schedule_message",
    "schedule_work_order",
}


def controlled_appfolio_qa_allowed(
    operation: str = "",
    wo_num: str = "",
    wo_url: str = "",
    tenant: str = "",
    vendor_name: str = "",
) -> Tuple[bool, str]:
    """Allow only explicitly configured, narrowly scoped AppFolio QA writes."""
    if not RACHEL_APPFOLIO_QA_ENABLED:
        return False, "RACHEL_APPFOLIO_QA_ENABLED=false"

    if operation not in CONTROLLED_APPFOLIO_QA_OPERATIONS:
        return False, f"operation not permitted: {operation or 'unspecified'}"

    expected_values = {
        "work order number": RACHEL_APPFOLIO_QA_WO_NUM,
        "work order ID": RACHEL_APPFOLIO_QA_WO_ID,
        "tenant": RACHEL_APPFOLIO_QA_TENANT,
        "vendor": RACHEL_APPFOLIO_QA_VENDOR,
    }

    missing = [
        label
        for label, value in expected_values.items()
        if not value
    ]
    if missing:
        return False, "controlled QA configuration incomplete: " + ", ".join(missing)

    if (wo_num or "").strip() != RACHEL_APPFOLIO_QA_WO_NUM:
        return False, "work order number does not match controlled QA target"

    expected_path = f"/work_orders/{RACHEL_APPFOLIO_QA_WO_ID}"
    if expected_path not in (wo_url or ""):
        return False, "work order URL does not match controlled QA target"

    if (tenant or "").strip().lower() != RACHEL_APPFOLIO_QA_TENANT:
        return False, "tenant does not match controlled QA target"

    if (vendor_name or "").strip().lower() != RACHEL_APPFOLIO_QA_VENDOR:
        return False, "vendor does not match controlled QA target"

    return True, "CONTROLLED APPFOLIO QA"


def appfolio_readonly_thread_qa_allowed(
    target_selector_key: str,
    delegated_by: str,
    authorization_id: str,
) -> Tuple[bool, str]:
    """Permit one manually fingerprinted read-only inbox-thread QA."""
    normalized = (target_selector_key or "").strip().lower()
    if not RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED:
        return False, "RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED=false"
    if RACHEL_ENABLED:
        return False, "RACHEL_ENABLED must be false for read-only thread QA"
    if not RACHEL_TEST_MODE:
        return False, "RACHEL_TEST_MODE must be true for read-only thread QA"
    if RACHEL_APPFOLIO_QA_ENABLED:
        return False, "AppFolio write QA must be disabled"
    expected = RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        return False, "read-only thread QA selector fingerprint is invalid"
    if normalized != expected:
        return False, "read-only thread QA selector fingerprint mismatch"
    if not (delegated_by or "").strip():
        return False, "read-only thread QA delegation identity is missing"
    if not (authorization_id or "").strip():
        return False, "read-only thread QA authorization ID is missing"
    return True, "CONTROLLED READ-ONLY APPFOLIO THREAD QA"


def environment_label() -> str:
    if not RACHEL_ENABLED:
        return "PAUSED"
    if RACHEL_TEST_MODE and RACHEL_OWNER_QA_MODE:
        return "OWNER-ONLY SYNTHETIC QA"
    if RACHEL_TEST_MODE:
        return "TEST"
    return "PRODUCTION"


def email_allowed(recipient: str) -> Tuple[bool, str]:
    normalized = (recipient or "").strip().lower()

    if not RACHEL_ENABLED:
        return False, "RACHEL_ENABLED=false"

    if RACHEL_TEST_MODE:
        if not RACHEL_OWNER_QA_MODE:
            return False, "test mode active without Owner QA mode"

        if normalized != OWNER_QA_EMAIL:
            return False, "Owner-only QA blocks all other recipients"

    return True, environment_label()


def sms_allowed() -> Tuple[bool, str]:
    if not RACHEL_ENABLED:
        return False, "RACHEL_ENABLED=false"

    if not RACHEL_SMS_ENABLED:
        return False, "RACHEL_SMS_ENABLED=false"

    if RACHEL_TEST_MODE:
        return False, "SMS is blocked in test mode"

    return True, environment_label()


def property_fact_sheet_write_allowed() -> Tuple[bool, str]:
    """Gate consequential writes to the shared property-facts sheet."""
    if not RACHEL_ENABLED:
        return False, "RACHEL_ENABLED=false"
    if RACHEL_TEST_MODE:
        return False, "property-facts sheet writes are blocked in test mode"
    if not RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED:
        return False, "RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED=false"
    return True, environment_label()


def appfolio_write_allowed(
    synthetic_qa: bool = False,
    operation: str = "",
    wo_num: str = "",
    wo_url: str = "",
    tenant: str = "",
    vendor_name: str = "",
) -> Tuple[bool, str]:
    if not RACHEL_ENABLED:
        return False, "RACHEL_ENABLED=false"

    if synthetic_qa:
        return False, "synthetic QA must never write to AppFolio"

    if RACHEL_TEST_MODE:
        return controlled_appfolio_qa_allowed(
            operation=operation,
            wo_num=wo_num,
            wo_url=wo_url,
            tenant=tenant,
            vendor_name=vendor_name,
        )

    if not RACHEL_APPFOLIO_WRITES_ENABLED:
        return False, "RACHEL_APPFOLIO_WRITES_ENABLED=false"

    return True, environment_label()


def appfolio_communication_send_allowed(
    target_selector_key: str = "",
    authorization_evidence_id: str = "",
) -> Tuple[bool, str]:
    """Gate AppFolio Communication sends separately from other writes."""
    if not RACHEL_ENABLED:
        return False, "RACHEL_ENABLED=false"
    if RACHEL_TEST_MODE:
        if not RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED:
            return False, "AppFolio Communication sends are blocked in test mode"
        expected_selector = (
            RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_SELECTOR_KEY
        )
        if (
            len(expected_selector) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_selector
            )
        ):
            return False, "controlled Communication send selector is invalid"
        if (target_selector_key or "").strip().lower() != expected_selector:
            return False, "controlled Communication send selector mismatch"
        expected_authorization = (
            RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_AUTHORIZATION_ID
        )
        if not expected_authorization:
            return False, "controlled Communication send authorization missing"
        if (
            authorization_evidence_id or ""
        ).strip() != expected_authorization:
            return False, "controlled Communication send authorization mismatch"
        return True, "CONTROLLED APPFOLIO COMMUNICATION SEND QA"
    if not RACHEL_APPFOLIO_WRITES_ENABLED:
        return False, "RACHEL_APPFOLIO_WRITES_ENABLED=false"
    if not RACHEL_APPFOLIO_MESSAGING_ENABLED:
        return False, "RACHEL_APPFOLIO_MESSAGING_ENABLED=false"
    return True, environment_label()


def vendor_contact_allowed(
    synthetic_qa: bool = False,
    operation: str = "",
    wo_num: str = "",
    wo_url: str = "",
    tenant: str = "",
    vendor_name: str = "",
) -> Tuple[bool, str]:
    if not RACHEL_ENABLED:
        return False, "RACHEL_ENABLED=false"

    if synthetic_qa:
        return False, "synthetic QA must never contact a vendor"

    if RACHEL_TEST_MODE:
        return controlled_appfolio_qa_allowed(
            operation=operation,
            wo_num=wo_num,
            wo_url=wo_url,
            tenant=tenant,
            vendor_name=vendor_name,
        )

    if not RACHEL_APPFOLIO_WRITES_ENABLED:
        return False, "RACHEL_APPFOLIO_WRITES_ENABLED=false"

    return True, environment_label()
