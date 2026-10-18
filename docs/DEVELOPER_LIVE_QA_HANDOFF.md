# Rachel — Developer Live-QA Handoff

This document is written for the independent developer who previously reviewed
commit `f89a4f43a711c3d2a220015fa047cf9c7518324b`. It covers everything that
changed after that review, why, how it's tested, and what still requires live
AppFolio verification. Read this before pulling `main` and before starting
live QA — pair it with `docs/LIVE_QA_RUNBOOK.md` for the step-by-step
procedure.

## Baseline

| | Commit | Tests |
|---|---|---|
| Prior reviewed commit | `f89a4f43a711c3d2a220015fa047cf9c7518324b` | 332/332 passing |
| Intermediate checkpoint (not separately reviewed) | `d492385ff452e404fe5f60f3b0d7899e520dd4e5` — "Harden AppFolio UI contracts and conversation identity" | — |
| Final hardening commit (this handoff) | `<PLACEHOLDER — filled in at commit time, see final report>` | 398/398 passing |

Both the 332 and 398 counts above were verified by direct `unittest discover`
runs against the respective commit's test tree (not recalled from memory) —
you can reproduce the 398 with:

```
.venv/bin/python -m unittest discover -p "test_*.py"
```

66 net new tests were added since the reviewed baseline (5 new test files,
plus additions to `test_appfolio_inbox_thread_readonly.py` and
`test_vendor_lifecycle_two_hour_windows.py`). No test was deleted or weakened.

## Complete change inventory since your review

### 1. P0 fix — import-time AppFolio side effect

**Files:** all production modules (audited codebase-wide); the actual defect
was in a module-level code path that executed live Playwright/login behavior
merely from being *imported*, independent of being called.
**Previous behavior:** importing a production module for audit/inspection
purposes could open a real browser and type the dummy `APPFOLIO_EMAIL` value
into the AppFolio login form — a live side effect from a read-only static
action.
**New behavior:** all browser/login-triggering code is confined to functions;
importing any module performs no I/O, no browser launch, no network call.
**Safety reason:** static analysis, audits, and `python -c "import ..."`
sanity checks must never be able to touch a live system.
**Tests:** `test_import_side_effects.py` (import-time regression guard —
present at baseline, 1 test; still green).
**Live verification required:** No — this is a static/offline guarantee,
provable by import alone.

### 2. Communication Inbox read-only thread parser + deep scrolling/traversal

**Files:** `rachel_appfolio_inbox.py`.
**Previous behavior:** inbox row/thread reading assumed a shallow, single-page
row list.
**New behavior:** deep traversal across virtualized/scrolled inbox content,
frame-aware row reading (AppFolio's inbox list can live inside an iframe),
and a hardened thread parser that dedupes and orders messages.
**Safety reason:** the scheduling/reply logic downstream depends on seeing
the *complete* thread, not a partial one; a truncated read could cause Rachel
to miss context an operator would have seen.
**Tests:** `test_inbox_target_row_traversal.py`, `test_inbox_delivery_safety.py`,
`test_appfolio_inbox_thread_readonly.py` (extended this round, +31 lines).
**Live verification required:** Yes — covered by the Communication Inbox read
step in the runbook.

### 3. Conversation identity model — identity vs. state vs. QA fingerprint

**Files:** `rachel_event_memory.py` (`conversation_identity_key`,
`conversation_state_key`, `qa_authorization_fingerprint` — lines 546–581 of
that file).
**Previous behavior:** a single row-derived key was used both to recognize
"this is the same conversation" and to detect "something changed," which
conflated durable identity with volatile state (preview text, unread count,
timestamp).
**New behavior:** three distinct, purpose-built keys:
- `conversation_identity_key(row)` — durable; prefers AppFolio's native
  thread ID when present, else a stable-field hash that deliberately excludes
  preview text, timestamp, and unread state.
- `conversation_state_key(row)` — volatile; changes whenever new activity
  arrives, used to detect "there's something new to look at."
- `qa_authorization_fingerprint(row)` — deliberately volatile and
  preview-inclusive; used **only** to authorize a single, one-time QA/canary
  action against the exact row a human is currently looking at. It is never
  used as durable identity and never persisted as a linkage.
**Safety reason:** durable identity must not silently change when a
conversation gets a new message (which would let Rachel "lose track" of a
thread), and QA authorization must not silently persist past the one
human-observed row it was issued for (which could let a stale authorization
apply to the wrong, later conversation).
**Tests:** `test_conversation_identity_model.py` (new, 378 lines).
**Live verification required:** Yes — the two-pass First City canary in
Part H specifically re-proves identity stability across a second read in the
same browser context.

### 4. Canonical conversation identity storage, migration, and linking

**Files:** `rachel_event_memory.py` — new `conversation_identity_links` table
(schema at line 109, index at line 288), `resolve_and_link_conversation_identity`
(742), `backfill_conversation_identity_for_legacy_thread` (795),
`link_conversation_identities` (826).
**Previous behavior:** legacy threads recorded before native AppFolio thread
IDs were available had no durable link to a canonical identity; a thread
could only be found again by legacy `thread_id`.
**New behavior:** a dedicated linkage table maps legacy thread IDs to
canonical `conversation_identity_key`s, backfilled lazily as native IDs
become observable, with a manual reconciliation path
(`link_conversation_identities`) for edge cases. Existing legacy rows are
never rewritten in place — only linked forward.
**Safety reason:** preserves message history continuity across the
legacy→native-ID transition without ever mutating or discarding a
pre-existing durable record.
**Tests:** `test_conversation_identity_storage.py` (new, 424 lines) —
includes migration-safety tests.
**Live verification required:** Yes — Part H's canary verifies stable
conversation identity linkage against a real thread.

### 5. Draft-planning identity consolidation

**Files:** `rachel_communication_workflow.py` —
`create_response_draft_plan` (364), `load_verified_thread_context`.
**Previous behavior:** draft planning and thread-context loading used
separate, not-fully-aligned notions of "which conversation is this."
**New behavior:** `create_response_draft_plan` takes an explicit
`conversation_identity_key` and threads it through to
`load_verified_thread_context`, so a draft plan's idempotency key
(`hashlib.sha256` over thread_id + latest message fingerprint + context
digest + classification + authorization) is always computed against the
same canonical identity used for durable storage. The function still never
touches a composer (`draft_only: True`, `composer_actions_attempted: 0`,
`send_actions_attempted: 0` are asserted in the returned plan).
**Safety reason:** prevents a draft plan from being silently computed against
a different conversation than the one whose history it claims to summarize.
**Tests:** `test_draft_planning_identity.py` (new, 218 lines).
**Live verification required:** No new live behavior — this is planning-only
and produces no AppFolio-visible action; still exercised indirectly by the
canary's read paths.

### 6. Semantic (non-row-count) return-to-inbox verification

**Files:** `rachel_appfolio_inbox.py` —
`_INBOX_SEMANTIC_RETURN_VERIFICATION_SCRIPT` (2763),
`_verify_semantic_return_to_inbox` (2814), `_verify_thread_identity` (3189),
`_semantic_thread_postcheck` (3545), `_return_to_inbox` (3636).
**Previous behavior:** "did we successfully return to the inbox after
reading a thread" was inferred from row-count deltas, which is unreliable —
row counts change for reasons unrelated to navigation (new messages
arriving, filter changes, pagination).
**New behavior:** `_verify_semantic_return_to_inbox` checks structural,
route-level facts instead: current route, presence of the inbox list root,
that no composer/thread-detail view is active, and that the active filter
scope is structurally sound. Row-count changes are now treated as
informational telemetry only, never as pass/fail criteria.
**Safety reason:** a prior read-only canary attempt showed row-count-based
verification could report a false negative (or false positive) purely from
unrelated inbox activity — semantic checks are robust to that.
**Tests:** `test_semantic_return_verification.py` (new, 224 lines).
**Live verification required:** Yes — this is exactly what Part H's canary
exercises after each read pass.

### 7. AppFolio selector/accessible-name centralization

**Files:** `rachel_appfolio_ui.py`, `rachel_wo_triage.py`,
`rachel_vendor_lifecycle.py`.
**Previous behavior:** individual workflow functions in `rachel_wo_triage.py`
constructed raw Playwright locators inline (`page.get_by_role("button",
name="Send", exact=True)`, `page.get_by_role("option", name=issue_label.strip(),
exact=True)`, raw CSS strings like `"option:checked"`), scattering AppFolio
DOM knowledge across the workflow file instead of one contract module.
**New behavior:** `rachel_appfolio_ui.py` is the single source of truth for
AppFolio DOM knowledge, exposed through three mechanisms:
- `APPFOLIO_SELECTORS` (CSS/XPath tuples) via `selectors()` /
  `combined_selector()` / `first_visible()` / `unique_visible()` for fixed,
  non-role-based contracts.
- `APPFOLIO_ACCESSIBLE_NAMES` (7 entries) via `accessible_name(name)` for
  fixed Playwright accessibility-role lookups (`get_by_role`/`get_by_label`/
  `get_by_text`), now including `"work_order.send_button": "Send"`.
- `work_order_issue_option(page_or_locator, issue_label)` — a parameterized
  adapter for the one dynamic, business-value-driven lookup (the workflow
  passes only the issue label; the underlying accessibility-role mechanism is
  centralized).
See the full selector change table below for every individual call site.
**Safety reason:** Owner's explicit instruction — one place to update if
AppFolio's DOM changes, one place to audit for correctness, and a documented
seam between "business logic" and "how we find things in the page."
**Tests:** `test_appfolio_ui_accessible_names.py` (expanded to 20 tests, incl.
`WorkOrderIssueOptionHelperTests`, `SendButtonAccessibleNameTests`),
`test_vendor_lifecycle_two_hour_windows.py` (+2 source-inspection tests
proving no raw `"option:checked"` literal remains in the module).
**Live verification required:** Yes for the two lookups migrated this round
(Send button, issue-option) and for the pre-existing centralized contracts —
see "known residual risks" below; these were relocated/centralized, not
rewritten, so prior manual verification of their matching behavior still
applies, but a live pass is still the honest bar before write-path QA.

### 8. Accessible-name registry offline validation (new)

**Files:** `rachel_appfolio_ui.py` — `REQUIRED_ACCESSIBLE_NAMES`,
`ACCEPTED_DUPLICATE_ACCESSIBLE_NAME_VALUES`,
`_accessible_names_source_duplicate_keys()`, `validate_accessible_name_registry()`.
**Previous behavior:** no offline check existed for the accessible-name
registry's internal consistency; a duplicate dict key would be silently
dropped by Python at runtime with no signal.
**New behavior:** `validate_accessible_name_registry()` proves, entirely
offline (no browser, no network): no duplicate keys in the registry's own
source text (checked via `ast`, since Python's runtime dict would already
have silently collapsed a duplicate before any check could see it), all
required contract names are present and non-empty, any duplicate *values*
are either absent or explicitly documented as accepted, and unknown-name
lookups fail closed (`accessible_name("__validation_probe_unknown_name__")`
must raise `AppFolioUIContractError`).
**Safety reason:** catches registry authoring mistakes (typos, accidental
duplicate keys, empty values) before they ever reach a live page.
**Tests:** `AccessibleNameRegistryValidationTests` in
`test_appfolio_ui_accessible_names.py` (8 tests), including a structural
proof via `inspect.getsource` that the validator itself references no
`playwright`/`requests`/`socket`/`urlopen` symbols.
**Integration:** wired into `execute_validation_matrix()` in
`rachel_foundation_health.py` — `validate_accessible_name_registry()` is now
one of the checks the foundation health report runs.
**Live verification required:** No — offline-only by design.

### 9. Dead selector removal

**Files:** `rachel_appfolio_ui.py`.
**Previous behavior:** `APPFOLIO_SELECTORS` contained
`"work_order.email_link": ('a:has-text("Email")',)`, unused anywhere in the
codebase, alongside the still-active `"work_order.email_link_control"`
accessible-name contract.
**New behavior:** removed. Confirmed zero references codebase-wide (grep +
AST call-site audit, see Part D below) before deletion. The active
`work_order.email_link_control` contract is untouched.
**Safety reason:** dead selector entries are a latent risk — someone could
wire them up later without realizing they were never verified against a live
page, and exact-match hygiene degrades over time if unused entries
accumulate.
**Tests:** `DeadSelectorRemovalTests` in `test_appfolio_ui_accessible_names.py`
(3 tests) proving the key is absent and the active control contract is
unaffected.
**Live verification required:** No — this was a removal of dead code, not a
behavior change.

### 10. Codebase-wide raw-DOM-interaction audit tooling (new)

**Files:** `rachel_foundation_health.py` — `_DOM_INTERACTION_METHODS`,
`_imported_centralizing_names()`, `_call_is_centralizing()`,
`_unwrap_centralizing_call()`, `_dom_interaction_argument()`,
`_function_is_unconditional_immediate_raise()`,
`audit_raw_dom_interactions()`.
**Previous behavior:** no automated way existed to classify every AppFolio
DOM-interaction call site across the whole codebase as centralized, raw, or
provably-dead; prior audits were manual/partial (`rachel_wo_triage.py` only).
**New behavior:** an AST-based static audit walks every top-level function in
every module listed in `ACTIVE_APPFOLIO_MODULES`, classifies each
`get_by_role`/`get_by_label`/`get_by_text`/`query_selector`/
`query_selector_all`/etc. call site by whether its selector/name argument
traces back to an import from `rachel_appfolio_ui`, and separately flags call
sites inside functions that immediately and unconditionally `raise` (dead,
fail-closed code) so they're not miscounted as live raw usage. This surfaced
two genuine findings this round: a broader class of dead-code (not just
`_legacy_`-prefixed) in `rachel_appfolio_inbox.py`'s `send_reply_on_page`,
and a real duplicate raw selector in `rachel_vendor_lifecycle.py` (item 11
below).
**Safety reason:** this is the tool that produces Part D's audit numbers
below — it's what lets both of us trust "0 unjustified raw contracts" rather
than take it on faith.
**Tests:** covered indirectly via `test_foundation_health.py`'s architecture
audit assertions; the tool's own correctness was verified manually against
`rachel_wo_triage.py` line-by-line during development (see residual risks —
this is process, not a shipped test suite item, since it's an internal
audit-time tool, not a runtime code path).
**Live verification required:** No — audit-only, offline.

### 11. Real duplicate selector fixed in the live scheduling write path

**Files:** `rachel_vendor_lifecycle.py` — `update_wo_schedule`.
**Previous behavior:** hardcoded the raw CSS string `"option:checked"`
directly, even though this exact contract already existed centrally as
`work_order.selected_option` and was used elsewhere.
**New behavior:** `dur.locator(combined_selector("work_order.selected_option"))`
— same underlying selector string, sourced from the single centralized
contract instead of a second hand-typed copy.
**Safety reason:** two independent copies of the same selector string is
exactly the kind of drift risk this whole hardening effort exists to
eliminate — if AppFolio's markup changed, only one copy might get updated.
Found by the new audit tool (item 10), not by inspection.
**Tests:** `test_selected_duration_lookup_uses_the_centralized_selector`,
`test_no_raw_option_checked_literal_remains_in_module` in
`test_vendor_lifecycle_two_hour_windows.py` (source-inspection tests — chosen
over mock-based tests because a mock in isolation wouldn't prove
`update_wo_schedule` itself was fixed).
**Live verification required:** Yes — this is a live, active write path
(2-hour vendor scheduling). The underlying selector string is unchanged
(`"option:checked"` either way), so no new live behavior is introduced, but
it sits on a write path and should be exercised in the runbook's vendor
scheduling step regardless.

### 12. Browser/session ownership, feature circuit breakers, write-intent ledger, scheduling state machine — unchanged this round, listed for completeness

These pre-existing architecture pieces were **not modified** in this
hardening pass but are load-bearing for live QA and are referenced by the
runbook:
- **Persistent browser/session ownership** — one owner
  (`persistent_context_owners: 1` in the foundation health report).
- **Feature circuit breakers** — `FeatureCircuitOpen`,
  `AppFolioFeatureHealth` (`rachel_appfolio_ui.py:597,605`) — durable
  per-feature failure counters with cooldown; a feature that fails
  repeatedly stops attempting live actions until the cooldown clears.
- **Idempotent write-intent ledger** — `prepare_outbound_intent`,
  `claim_outbound_intent`, `complete_outbound_intent`,
  `record_outbound_intent_failure` (`rachel_appfolio_write_intents.py`) —
  prevents duplicate consequential writes (e.g. a message sent twice) across
  a Render restart mid-operation.
- **Two-hour vendor-scheduling state machine** — `rachel_maintenance_scheduling.py`,
  `rachel_vendor_lifecycle.py`.
These are exercised in the runbook's retry/idempotency, restart/resume, and
scheduling steps — see `docs/LIVE_QA_RUNBOOK.md`.

## Selector change table

| Old pattern | New centralized contract | Behavior changed or relocated only? | Test | Live verification path |
|---|---|---|---|---|
| `form.get_by_role("button", name="Send", exact=True)` | `form.get_by_role("button", name=accessible_name("work_order.send_button"), exact=True)` | Relocated only — same literal string `"Send"` | `SendButtonAccessibleNameTests` | Runbook: vendor/tenant text send step |
| `page.get_by_role("option", name=issue_label.strip(), exact=True)` | `work_order_issue_option(page, issue_label)` (helper does the `.strip()` internally) | Relocated only — same matching semantics | `WorkOrderIssueOptionHelperTests` | Runbook: additional WO creation step |
| `dur.locator("option:checked")` | `dur.locator(combined_selector("work_order.selected_option"))` | Relocated only — same literal string `"option:checked"`; removes a duplicate copy | `test_selected_duration_lookup_uses_the_centralized_selector`, `test_no_raw_option_checked_literal_remains_in_module` | Runbook: vendor scheduling step |
| `APPFOLIO_SELECTORS["work_order.email_link"]` (dead, zero references) | removed | N/A — deletion of unreachable code | `DeadSelectorRemovalTests` | None needed — never live |
| (already centralized prior to this round: `work_order.add_control`, `work_order.job_description_label`, `work_order.save_button`, `work_order.edit_control`, `work_order.resident_availability_marker`, `work_order.email_link_control`) | unchanged this round | — | pre-existing tests | Covered by runbook's existing WO-editing/vendor-dispatch/email steps |

Everything in this table is a **relocation**, not a rewrite — no selector's
matching string or matching semantics changed. Centralization removes
duplication and gives one offline-checkable place to look; it does not by
itself prove correctness against the live DOM, hence "live verification
required: yes" markers throughout this document for anything on an active
call path.

## Codebase-wide AppFolio DOM audit (Part D acceptance numbers)

Produced by `rachel_foundation_health.audit_raw_dom_interactions()`, run
against every module in `ACTIVE_APPFOLIO_MODULES` (not just
`rachel_wo_triage.py`):

| Metric | Count |
|---|---|
| Active modules audited | 7 |
| Total DOM-interaction call sites | 81 |
| Centralized (fixed-selector or accessible-name contract, or the parameterized adapter) | 54 |
| Centralized fixed-selector contracts (`APPFOLIO_SELECTORS`) | 58 |
| Centralized accessible-name contracts (`APPFOLIO_ACCESSIBLE_NAMES`) | 7 |
| **Active raw call sites** | **1** (documented exception below) |
| Unreachable / fail-closed dead call sites | 26 (all inside functions whose first executable statement is an unconditional `raise` — 23 across `rachel_wo_triage.py`'s four `_legacy_*` functions, 3 in `rachel_appfolio_inbox.py`'s `send_reply_on_page`) |
| Fail-closed legacy paths (function-level) | 10 |
| **Unjustified duplicate active contracts** | **0** (the one found — `rachel_vendor_lifecycle.py`'s `update_wo_schedule` — was fixed this round, item 11 above) |

**The one documented raw call site:** `rachel_wo_triage.py:888`, inside
`login_appfolio`'s nested `find_visible(selectors)` closure. This closure
receives a *list of selector tuples as a parameter* and iterates it looking
for the first visible match — it is itself a small, local, correctly-scoped
selector-resolution helper, not a hardcoded DOM reference. All of its actual
selector values still originate from `APPFOLIO_SELECTORS` at the call site.
It was manually verified line-by-line rather than centralized further,
per the explicit instruction not to speculatively rewrite working selectors.
It is flagged here for live QA attention (login flow) but is not a
centralization gap.

Both target metrics from the acceptance criteria are met:
**unjustified active raw contracts = 0** (the 1 raw site is documented and
justified above) and **unjustified duplicate active contracts = 0**.

## Known residual risks — read this section honestly

This hardening pass does **not** make Rachel immune to future AppFolio DOM
changes, and nothing below should be read as such a claim:

1. **Centralization ≠ correctness proof.** Every selector/accessible-name
   value in `rachel_appfolio_ui.py` was either previously verified live
   (relocated, not rewritten) or has never been exercised against a live
   page in this hardening pass. Centralizing them makes future breakage
   easier to *find and fix in one place* — it does not reduce the chance
   that AppFolio changes its markup out from under any individual contract.
2. **The audit tool only sees what it's told to look at.** `ACTIVE_APPFOLIO_MODULES`
   is a maintained list; a new module added to the codebase without being
   added to that list would silently be excluded from the raw-DOM audit.
3. **The one documented raw call site** (`login_appfolio`'s `find_visible`
   closure) has not been re-verified against a live login page in this
   round — login was not re-exercised live this session beyond the existing
   read-only canary history.
4. **Write paths are untested live in this round.** All write/messaging/
   scheduling gates remain OFF (see below); this entire hardening pass was
   offline-only plus one read-only canary. Nothing here substitutes for the
   full `docs/LIVE_QA_RUNBOOK.md` write-path validation.
5. **The accessible-name and selector registries are hand-authored.** Offline
   validation catches internal inconsistency (duplicates, missing entries,
   fail-closed lookups) — it cannot catch "this string used to match the
   live DOM and might not anymore."
6. **Conversation identity's native-ID preference is only as good as
   AppFolio's own thread-ID stability.** If AppFolio ever reuses or
   reassigns a native thread ID, `conversation_identity_key` would need
   reassessment — this has not been observed in practice, only guarded
   against structurally by preferring native ID when present and falling
   back to a stable-field hash otherwise.
7. **Test coverage proves behavior on the inputs the tests construct.** The
   398 passing tests are all offline/mocked; the two-pass First City canary
   (Part H) is the only piece of this round's work that touched live
   AppFolio at all, and it is deliberately read-only and narrow in scope.

## Current production gate state

All gates default OFF (`env_bool(..., default=False)` in `rachel_safety.py`,
confirmed by direct source read, not memory):

| Gate | Env var | Default |
|---|---|---|
| Rachel enabled at all | `RACHEL_ENABLED` | `False` |
| Test mode | `RACHEL_TEST_MODE` | `True` |
| Owner QA mode | `RACHEL_OWNER_QA_MODE` | `False` |
| SMS sending | `RACHEL_SMS_ENABLED` | `False` |
| AppFolio writes | `RACHEL_APPFOLIO_WRITES_ENABLED` | `False` |
| AppFolio messaging | `RACHEL_APPFOLIO_MESSAGING_ENABLED` | `False` |
| AppFolio communication-send QA | `RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED` | `False` |
| Property fact-sheet writes | `RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED` | `False` |
| AppFolio general QA | `RACHEL_APPFOLIO_QA_ENABLED` | `False` |
| AppFolio read-only thread QA | `RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED` | `False` |

`render.yaml` defines these as Render-managed environment keys with no
value committed to source — the comment in `render.yaml` states explicitly:
"Release-candidate safety gates. Keep AppFolio mutations disabled until their
separately authorized controlled-write QA has passed." Nothing in this
hardening pass changed any gate's default or wiring.

## How to verify this handoff yourself

```
# Full offline suite
.venv/bin/python -m unittest discover -p "test_*.py"

# Foundation health / architecture audit
.venv/bin/python -c "import rachel_foundation_health as fh, json; print(json.dumps(fh.audit_active_appfolio_architecture(), indent=2, default=str))"
.venv/bin/python -c "import rachel_foundation_health as fh, json; print(json.dumps(fh.audit_raw_dom_interactions(), indent=2, default=str))"

# Diff against your prior review
git diff --stat f89a4f43a711c3d2a220015fa047cf9c7518324b -- .
```

See `docs/LIVE_QA_RUNBOOK.md` for the live-QA procedure itself.
