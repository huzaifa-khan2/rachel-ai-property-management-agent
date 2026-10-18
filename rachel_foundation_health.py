"""Offline foundation matrix and runtime subsystem health reporting.

The matrix combines sanitized live DOM contracts with synthetic edge cases.
It intentionally performs no AppFolio navigation.  Live read-only canaries are
separate evidence and never replace representative offline validation.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from rachel_appfolio_ui import (
    APPFOLIO_ACCESSIBLE_NAMES,
    APPFOLIO_DOM_CONTRACTS,
    APPFOLIO_FEATURES,
    APPFOLIO_SELECTORS,
    AppFolioFeatureHealth,
    fixture_directory,
    validate_accessible_name_registry,
    validate_contract_registry,
)


ACTIVE_APPFOLIO_MODULES = (
    "rachel_appfolio_browser.py",
    "rachel_appfolio_communication_sender.py",
    "rachel_appfolio_inbox.py",
    "rachel_scraper_v2.py",
    "rachel_vendor_lifecycle.py",
    "rachel_wo_triage.py",
    "rachel_work_order_scanner.py",
)


FOUNDATION_SUBSYSTEMS = (
    "communication_inbox_polling",
    "communication_thread_reading",
    "ownership_delegation_routing",
    "work_order_reconciliation",
    "portfolio_entities",
    "durable_memory_deduplication",
    "lease_report_ingestion_catchup",
    "authentication_session_recovery",
    "shared_browser_architecture",
    "safety_zero_write",
)


SUBSYSTEM_REQUIREMENTS = {
    "communication_inbox_polling": {
        "fixtures": {"communication_inbox.json"},
        "cases": {
            "empty_full_scope", "human_owned_unread",
            "ownership_unknown_unread", "duplicate_selector_rows",
        },
        "contracts": {"communication_inbox"},
        "circuit_features": {"communication_inbox"},
    },
    "communication_thread_reading": {
        "fixtures": {"communication_thread.json"},
        "cases": {
            "immediate_render", "delayed_mount", "react_node_replacement",
            "ambiguous_roots", "missing_history", "missing_composer",
            "malformed_message", "true_conversation_change",
            "back_to_inbox_restored",
        },
        "contracts": {"communication_thread"},
        "circuit_features": {"communication_thread_reader"},
    },
    "ownership_delegation_routing": {
        "fixtures": {"communication_inbox.json", "portfolio_entities.json"},
        "cases": {
            "new_rachel_owned", "explicit_delegation", "human_owned_unread",
            "ownership_unknown_unread", "preview_changed_after_delegation",
            "portfolio_owner_match", "portfolio_owner_mismatch",
        },
        "contracts": {"communication_inbox", "communication_thread"},
        "circuit_features": set(),
    },
    "work_order_reconciliation": {
        "fixtures": {"work_orders.json"},
        "cases": {
            "empty_list", "paginated_list", "known_unchanged",
            "unknown_work_order", "changed_list_fields", "missing_active_row",
            "historical_statuses", "emergency_text_during_reconciliation",
            "restart_identical", "detail_identity_mismatch",
        },
        "contracts": {"work_order_list", "work_order_detail"},
        "circuit_features": {
            "work_order_list", "work_order_detail",
            "work_order_reconciliation",
        },
    },
    "portfolio_entities": {
        "fixtures": {"portfolio_entities.json", "report_ingestion.json"},
        "cases": {
            "property_with_multiple_units", "unit_with_primary_and_occupant",
            "vacant_unit", "duplicate_resident_match", "active_vendor",
            "inactive_vendor", "vendor_property_link_duplicate",
            "tenant_directory_valid", "tenant_directory_invalid_workbook",
        },
        "contracts": {"report_export", "rent_roll"},
        "circuit_features": {"portfolio_ingestion", "report_export"},
    },
    "durable_memory_deduplication": {
        "fixtures": {"communication_thread.json", "safety_memory.json"},
        "cases": {
            "duplicate_bodies", "communication_dedup_restart",
            "duplicate_message_bodies", "verified_write_memory",
            "unverified_write_memory", "ambiguous_prior_write",
        },
        "contracts": set(),
        "circuit_features": set(),
    },
    "lease_report_ingestion_catchup": {
        "fixtures": {"report_ingestion.json"},
        "cases": {
            "new_download_only", "download_timeout",
            "work_order_history_duplicate_restart",
            "work_order_history_identity_columns_missing",
            "lease_90_day_window", "lease_60_day_window",
            "lease_30_day_window", "lease_downtime_catch_up",
            "monthly_restart_same_period", "dry_run_delivery",
        },
        "contracts": {"report_export"},
        "circuit_features": {"report_export", "portfolio_ingestion"},
    },
    "authentication_session_recovery": {
        "fixtures": {"auth_runtime.json"},
        "cases": {
            "authenticated_session_reused", "complete_login_form",
            "incomplete_login_form", "two_factor_sms",
            "ambiguous_two_factor", "expired_session", "unhealthy_page",
            "profile_lock_contended",
        },
        "contracts": {"authentication"},
        "circuit_features": {"authentication_session"},
    },
    "shared_browser_architecture": {
        "fixtures": {"auth_runtime.json"},
        "cases": {
            "authenticated_session_reused", "unhealthy_page",
            "profile_lock_contended", "feature_failure_isolated",
        },
        "contracts": set(),
        "circuit_features": set(),
    },
    "safety_zero_write": {
        "fixtures": {"safety_memory.json", "work_orders.json"},
        "cases": {
            "global_paused", "test_mode_appfolio_write",
            "reconciliation_zero_write", "read_only_thread_guard",
            "feature_circuit_threshold", "historical_statuses",
            "emergency_text_during_reconciliation",
        },
        "contracts": {"work_order_schedule", "communication_thread"},
        "circuit_features": set(),
    },
}


@dataclass(frozen=True)
class SubsystemHealth:
    subsystem: str
    status: str
    reason: str
    contract_cases: int
    circuit_features: tuple[str, ...]


def _safe_reason(value: object) -> str:
    return re.sub(
        r"[^A-Za-z0-9_.:,-]+", "_", str(value or "")
    ).strip("_")[:240] or "unspecified"


def _walk_values(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_values(nested)
    elif isinstance(value, str):
        yield value


def load_contract_fixtures(root: Optional[Path] = None) -> dict[str, dict]:
    fixture_root = Path(root or fixture_directory())
    result = {}
    for path in sorted(fixture_root.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            result[path.name] = json.load(handle)
    return result


def validate_contract_fixtures(
    fixtures: Optional[dict[str, dict]] = None,
) -> list[str]:
    """Validate fixture schema, sanitization, contracts, and unique cases."""
    loaded = fixtures if fixtures is not None else load_contract_fixtures()
    errors = []
    if not loaded:
        return ["fixture_set_empty"]
    email_pattern = re.compile(r"\b[^\s@]+@[^\s@]+\.[A-Za-z]{2,}\b")
    phone_pattern = re.compile(r"(?<!\d)\+?1?\d{10}(?!\d)")
    url_pattern = re.compile(r"https?://", re.IGNORECASE)
    for name, fixture in loaded.items():
        if fixture.get("schema_version") != 1:
            errors.append(f"{name}:schema_version_invalid")
        if fixture.get("sanitized") is not True:
            errors.append(f"{name}:not_sanitized")
        cases = fixture.get("cases")
        if not isinstance(cases, list) or not cases:
            errors.append(f"{name}:cases_missing")
            continue
        case_ids = [str(case.get("id", "")) for case in cases]
        if any(not value for value in case_ids):
            errors.append(f"{name}:case_id_missing")
        if len(case_ids) != len(set(case_ids)):
            errors.append(f"{name}:duplicate_case_id")
        contract_names = []
        if fixture.get("contract"):
            contract_names.append(fixture["contract"])
        contract_names.extend(fixture.get("contracts") or [])
        for contract_name in contract_names:
            if contract_name not in APPFOLIO_DOM_CONTRACTS:
                errors.append(f"{name}:unknown_contract:{contract_name}")
        for string_value in _walk_values(fixture):
            if email_pattern.search(string_value):
                errors.append(f"{name}:email_like_value_present")
                break
            if phone_pattern.search(string_value):
                errors.append(f"{name}:phone_like_value_present")
                break
            if url_pattern.search(string_value):
                errors.append(f"{name}:url_value_present")
                break
    return sorted(set(errors))


def fixture_case_index(fixtures: dict[str, dict]) -> dict[str, set[str]]:
    return {
        name: {str(case.get("id", "")) for case in data.get("cases", [])}
        for name, data in fixtures.items()
    }


def _function_is_unconditional_immediate_raise(
    node,
) -> bool:
    """
    True only for a function whose first real statement (after an optional
    docstring) is a bare `raise ...` with no surrounding condition - the
    deliberate permanently-disabled pattern used across this codebase
    (originally only on _legacy_-prefixed functions, now checked
    regardless of name). An `if condition: raise ...` first statement -
    ordinary input validation - is an ast.If node, not ast.Raise, so it is
    never mistaken for this pattern.
    """
    statements = list(node.body)
    if (
        statements and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    return bool(statements) and isinstance(statements[0], ast.Raise)


def audit_active_appfolio_architecture(
    root: Optional[Path] = None,
) -> dict:
    """Statically prove the production browser/adapter ownership contract."""
    source_root = Path(root or Path(__file__).resolve().parent)
    errors = []
    login_definitions = 0
    persistent_context_launches = 0
    legacy_paths = 0
    for module_name in ACTIVE_APPFOLIO_MODULES:
        path = source_root / module_name
        if not path.is_file():
            errors.append(f"missing_active_module:{module_name}")
            continue
        source = path.read_text(encoding="utf-8")
        if (
            module_name != "rachel_appfolio_browser.py"
            and "rachel_appfolio_ui" not in source
        ):
            errors.append(f"central_adapter_import_missing:{module_name}")
        if "First City Construction" in source:
            errors.append(f"controlled_canary_special_cased:{module_name}")
        try:
            tree = ast.parse(source, filename=module_name)
        except SyntaxError:
            errors.append(f"syntax_error:{module_name}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "login_appfolio":
                    login_definitions += 1
                if node.name.startswith("_legacy_"):
                    legacy_paths += 1
                    if not _function_is_unconditional_immediate_raise(node):
                        errors.append(
                            f"legacy_path_not_fail_closed:{module_name}:"
                            f"{node.name}"
                        )
                elif _function_is_unconditional_immediate_raise(node):
                    # Fail-closed-by-construction, but not named with the
                    # _legacy_ convention the check above relies on. Same
                    # verified-dead pattern, counted the same way so it
                    # cannot hide as an "unexplained raw" finding.
                    legacy_paths += 1
            if (
                isinstance(node, ast.Attribute)
                and node.attr == ("launch_" + "persistent_context")
            ):
                persistent_context_launches += 1
    if login_definitions != 1:
        errors.append(f"login_implementation_count:{login_definitions}")
    if persistent_context_launches != 1:
        errors.append(
            f"persistent_context_owner_count:{persistent_context_launches}"
        )
    return {
        "status": "healthy" if not errors else "degraded",
        "errors": sorted(set(errors)),
        "active_modules_audited": len(ACTIVE_APPFOLIO_MODULES),
        "central_selector_contracts": len(APPFOLIO_SELECTORS),
        "central_accessible_name_contracts": len(APPFOLIO_ACCESSIBLE_NAMES),
        "dom_contracts": len(APPFOLIO_DOM_CONTRACTS),
        "feature_circuits": len(APPFOLIO_FEATURES),
        "login_implementations": login_definitions,
        "persistent_context_owners": persistent_context_launches,
        "fail_closed_legacy_paths": legacy_paths,
    }


_DOM_INTERACTION_METHODS = frozenset({
    "query_selector", "query_selector_all", "locator",
    "get_by_role", "get_by_text", "get_by_label",
})

# get_by_role's semantic identifier is the name= keyword, not its first
# positional (role) argument. get_by_text/get_by_label take it positionally.
_NAME_KEYWORD_METHODS = frozenset({"get_by_role"})


def _imported_centralizing_names(tree: ast.AST) -> set[str]:
    """Names this module imported from rachel_appfolio_ui - any call to one
    of them, or an alias produced by iterating one, counts as centralized."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "rachel_appfolio_ui":
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _call_is_centralizing(node: ast.Call, centralizing_names: set) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in centralizing_names
    if isinstance(func, ast.Attribute):
        return func.attr in centralizing_names
    return False


def _unwrap_centralizing_call(value, centralizing_names: set):
    """A centralizing call may be indexed immediately, e.g.
    selectors('auth.two_factor_sms')[0] - unwrap one Subscript layer so
    that pattern is still recognized as centralized, not raw."""
    if isinstance(value, ast.Subscript):
        value = value.value
    return (
        value if isinstance(value, ast.Call)
        and _call_is_centralizing(value, centralizing_names) else None
    )


def _dom_interaction_argument(call: ast.Call) -> Optional[ast.expr]:
    method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    if method in _NAME_KEYWORD_METHODS:
        for kw in call.keywords:
            if kw.arg == "name":
                return kw.value
        return None
    return call.args[0] if call.args else None


def audit_raw_dom_interactions(root: Optional[Path] = None) -> dict:
    """
    AST-based count of active AppFolio DOM interaction call sites per
    module, classified as centralized (the selector/accessible-name/
    parameterized-helper argument traces to an import from
    rachel_appfolio_ui, directly or via a locally-tracked variable/loop
    target, including one level of subscript unwrapping) or raw. Only
    module-level (top-level) function definitions are scanned as
    independent scopes - a nested closure is walked as part of its
    enclosing top-level function so it is never double-counted, and it
    inherits the enclosing function's dead/active classification (a
    closure only defined inside a function that can never run is itself
    unreachable). Call sites inside a function matching the codebase's
    unconditional-immediate-raise dead-code pattern (see
    _function_is_unconditional_immediate_raise) are tallied separately as
    dead, not raw-active, so a documented dead path can never masquerade
    as unexplained raw AppFolio DOM knowledge. A secondary grep-based
    total is included so the two methods can be cross-checked. Offline
    only: reads source text, never imports or executes the audited
    modules.
    """
    source_root = Path(root or Path(__file__).resolve().parent)
    per_module = {}
    raw_sites = []
    dead_sites = []
    for module_name in ACTIVE_APPFOLIO_MODULES:
        path = source_root / module_name
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        grep_total = len(re.findall(
            r"\.(?:query_selector|query_selector_all|locator|get_by_role|"
            r"get_by_text|get_by_label)\(",
            source,
        ))
        try:
            tree = ast.parse(source, filename=module_name)
        except SyntaxError:
            per_module[module_name] = {
                "total_call_sites": 0, "centralized": 0, "raw": 0,
                "dead": 0, "grep_total": grep_total, "parse_error": True,
            }
            continue
        centralizing_names = _imported_centralizing_names(tree)
        total = 0
        centralized = 0
        dead = 0
        for func_node in tree.body:
            if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_dead = _function_is_unconditional_immediate_raise(func_node)
            tracked_locals: set = set()
            for node in ast.walk(func_node):
                if isinstance(node, ast.Assign):
                    unwrapped = _unwrap_centralizing_call(
                        node.value, centralizing_names
                    )
                    if unwrapped is not None:
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                tracked_locals.add(target.id)
                if isinstance(node, ast.For) and isinstance(
                    node.iter, ast.Call
                ) and _call_is_centralizing(node.iter, centralizing_names):
                    if isinstance(node.target, ast.Name):
                        tracked_locals.add(node.target.id)
            for node in ast.walk(func_node):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _DOM_INTERACTION_METHODS
                ):
                    continue
                total += 1
                argument = _dom_interaction_argument(node)
                is_centralized = False
                if isinstance(argument, ast.Call) and _call_is_centralizing(
                    argument, centralizing_names
                ):
                    is_centralized = True
                elif (
                    isinstance(argument, ast.Name)
                    and argument.id in tracked_locals
                ):
                    is_centralized = True
                site = (
                    f"{module_name}:{node.lineno}:{func_node.name}:"
                    f"{node.func.attr}"
                )
                if is_dead:
                    dead += 1
                    dead_sites.append(site)
                elif is_centralized:
                    centralized += 1
                else:
                    raw_sites.append(site)
        per_module[module_name] = {
            "total_call_sites": total,
            "centralized": centralized,
            "raw": total - centralized - dead,
            "dead": dead,
            "grep_total": grep_total,
        }
    return {
        "per_module": per_module,
        "total_call_sites": sum(
            m["total_call_sites"] for m in per_module.values()
        ),
        "total_centralized": sum(
            m["centralized"] for m in per_module.values()
        ),
        "total_raw": sum(m["raw"] for m in per_module.values()),
        "total_dead": sum(m["dead"] for m in per_module.values()),
        "raw_call_sites": sorted(raw_sites),
        "dead_call_sites": sorted(dead_sites),
    }


def execute_validation_matrix(
    fixtures: Optional[dict[str, dict]] = None,
) -> dict:
    """Execute structural expectations for every representative fixture."""
    loaded = fixtures if fixtures is not None else load_contract_fixtures()
    errors = (
        validate_contract_registry()
        + validate_accessible_name_registry()
        + validate_contract_fixtures(loaded)
    )
    cases_executed = 0
    case_results = []
    for fixture_name, fixture in loaded.items():
        for case in fixture.get("cases", []):
            cases_executed += 1
            reasons = []
            case_id = str(case.get("id", ""))
            if not any(
                key in case for key in (
                    "expected", "expected_open_count", "expected_detail_opens",
                    "expected_authorization", "workflow_actions_attempted",
                    "expected_writes", "expected_rows",
                    "expected_unique_fingerprints",
                )
            ):
                reasons.append("expected_outcome_missing")
            if "workflow_actions_attempted" in case and int(
                case["workflow_actions_attempted"]
            ) != 0:
                reasons.append("workflow_action_nonzero")
            for write_key in (
                "expected_writes", "typing", "sending", "assignment",
                "toggle", "scheduling",
            ):
                if write_key in case and int(case[write_key]) != 0:
                    reasons.append(f"{write_key}_nonzero")
            if case_id in {
                "human_owned_unread", "ownership_unknown_unread"
            } and int(case.get("expected_open_count", -1)) != 0:
                reasons.append("unsafe_thread_open_expected")
            if case_id == "preview_changed_after_delegation" and (
                case.get("expected") != "fail_closed"
            ):
                reasons.append("delegation_transferred_to_new_event")
            if case_id in {"known_unchanged", "restart_identical"} and int(
                case.get("expected_detail_opens", -1)
            ) != 0:
                reasons.append("incremental_reconciliation_broken")
            case_results.append({
                "fixture": fixture_name,
                "case": _safe_reason(case_id),
                "status": "failed" if reasons else "passed",
                "reason": ",".join(reasons) if reasons else "contract_satisfied",
            })
            errors.extend(
                f"{fixture_name}:{case_id}:{reason}" for reason in reasons
            )
    return {
        "status": "healthy" if not errors else "degraded",
        "cases_executed": cases_executed,
        "cases_passed": sum(
            result["status"] == "passed" for result in case_results
        ),
        "errors": sorted(set(errors)),
        "case_results": case_results,
    }


def assert_zero_write_metrics(metrics: dict, *, context: str) -> None:
    """Fail if a read-only path reports any attempted consequential action."""
    write_keys = (
        "workflow_actions_attempted",
        "appfolio_writes_attempted",
        "outbound_messages_attempted",
        "composer_inputs_attempted",
        "read_state_toggles_attempted",
        "assignment_changes_attempted",
        "overflow_actions_attempted",
        "scheduling_actions_attempted",
    )
    nonzero = {
        key: int(metrics.get(key, 0) or 0)
        for key in write_keys
        if int(metrics.get(key, 0) or 0) != 0
    }
    if nonzero:
        raise RuntimeError(
            f"{_safe_reason(context)}:zero_write_invariant_failed:"
            + ",".join(sorted(nonzero))
        )


def build_foundation_health_report(
    *,
    fixture_root: Optional[Path] = None,
    feature_health: Optional[AppFolioFeatureHealth] = None,
) -> dict:
    fixtures = load_contract_fixtures(fixture_root)
    matrix = execute_validation_matrix(fixtures)
    case_index = fixture_case_index(fixtures)
    contract_errors = validate_contract_registry()
    fixture_errors = validate_contract_fixtures(fixtures)
    architecture = audit_active_appfolio_architecture()
    subsystems = []
    for subsystem in FOUNDATION_SUBSYSTEMS:
        requirement = SUBSYSTEM_REQUIREMENTS[subsystem]
        missing_fixtures = sorted(
            requirement["fixtures"] - set(fixtures)
        )
        available_cases = set().union(*(
            case_index.get(name, set()) for name in requirement["fixtures"]
        )) if requirement["fixtures"] else set()
        missing_cases = sorted(requirement["cases"] - available_cases)
        missing_contracts = sorted(
            requirement["contracts"] - set(APPFOLIO_DOM_CONTRACTS)
        )
        reasons = []
        if missing_fixtures:
            reasons.append("missing_fixtures:" + ",".join(missing_fixtures))
        if missing_cases:
            reasons.append("missing_cases:" + ",".join(missing_cases))
        if missing_contracts:
            reasons.append("missing_contracts:" + ",".join(missing_contracts))
        if contract_errors:
            reasons.append("contract_registry_invalid")
        if fixture_errors:
            reasons.append("sanitized_fixture_set_invalid")
        if (
            subsystem == "shared_browser_architecture"
            and architecture["errors"]
        ):
            reasons.extend(
                "architecture:" + error
                for error in architecture["errors"]
            )
        status = "degraded" if reasons else "healthy"
        if feature_health:
            for feature in sorted(requirement["circuit_features"]):
                feature_state = feature_health.get(feature)
                if feature_state["status"] == "disabled":
                    status = "disabled"
                    reasons.append(
                        f"circuit_open:{feature}:{feature_state['reason']}"
                    )
                elif feature_state["status"] == "degraded" and (
                    feature_state.get("consecutive_failures", 0)
                ):
                    if status != "disabled":
                        status = "degraded"
                    reasons.append(
                        f"circuit_degraded:{feature}:{feature_state['reason']}"
                    )
        covered_count = len(requirement["cases"] - set(missing_cases))
        reason = ";".join(reasons) if reasons else (
            f"{covered_count}_representative_contract_cases_passed"
        )
        subsystems.append(SubsystemHealth(
            subsystem=subsystem,
            status=status,
            reason=_safe_reason(reason),
            contract_cases=covered_count,
            circuit_features=tuple(sorted(requirement["circuit_features"])),
        ))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sanitized": True,
        "live_activity_performed": False,
        "matrix": {
            "status": matrix["status"],
            "cases_executed": matrix["cases_executed"],
            "cases_passed": matrix["cases_passed"],
            "errors": matrix["errors"],
        },
        "architecture": architecture,
        "subsystems": [asdict(item) for item in subsystems],
        "summary": {
            status: sum(item.status == status for item in subsystems)
            for status in ("healthy", "degraded", "disabled")
        },
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Rachel's offline foundation health matrix"
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = build_foundation_health_report()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output).resolve()
        allowed_root = Path(
            os.getenv(
                "RACHEL_EXPORTS_DIR",
                str(Path(__file__).resolve().parent / "exports"),
            )
        ).resolve()
        if allowed_root not in output.parents:
            raise RuntimeError("health_report_output_outside_exports")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["matrix"]["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
