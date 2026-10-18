# Rachel — Live QA Runbook

Sequential, pass/fail procedure for validating Rachel against live AppFolio.
Read `docs/DEVELOPER_LIVE_QA_HANDOFF.md` first — it explains *what changed and
why*; this document is *how to verify it live, one gate at a time*.

**Do the steps in order.** Each step only turns on the gate(s) it names —
every other write/messaging/scheduling gate stays `False` until its own step
authorizes it. Do not skip ahead. Do not enable a gate "just to save time"
for a later step.

No real tenant/vendor credentials, phone numbers, PII, or AppFolio session
artifacts should be pasted into logs, tickets, or this document when
recording evidence — reference them by role ("the test tenant", "the QA
vendor conversation") instead.

## Before you start

- [ ] Pull `main` at the commit named in `docs/DEVELOPER_LIVE_QA_HANDOFF.md`'s
      baseline table.
- [ ] Run `.venv/bin/python -m unittest discover -p "test_*.py"` — must be
      100% green before any live step.
- [ ] Confirm all gates in `rachel_safety.py` are at their defaults (all
      `False` except `RACHEL_TEST_MODE=True`) unless a specific step below
      says otherwise.
- [ ] Confirm you have a **disposable/test AppFolio conversation and work
      order** to use — never run write steps against a real tenant or vendor
      thread.
- [ ] Have Owner's explicit go-ahead for the specific step you're about to
      run. Each step is independently authorized — a prior go-ahead does not
      carry forward to the next step.

**Global STOP condition, applies to every step below:** if AppFolio's UI has
visibly changed shape (new field, renamed button, moved control, different
confirmation dialog) from what the step describes, STOP immediately, do not
attempt to work around it live, and report back with a screenshot/description
instead of guessing a new selector on the fly.

---

## Step 1 — Login and session ownership

**Prerequisites:** none beyond "Before you start."
**Gates enabled:** none (login itself is not gated).
**Procedure:** run Rachel's login path (`rachel_wo_triage.login_appfolio`) in
isolation.
**Expected AppFolio result:** a single authenticated session, landing on the
AppFolio dashboard/inbox.
**Expected Rachel/DB result:** no DB writes from login alone;
`persistent_context_owners: 1` in the foundation health report both before
and after (confirms nothing else is holding a second browser context).
**Evidence to capture:** foundation health report snapshot before/after;
confirmation only one browser process/context exists.
**Duplicate/idempotency checks:** re-run login a second time immediately —
must not open a second concurrent session or duplicate cookies/auth state.
**STOP conditions:** any 2FA/MFA prompt behavior different from what
`login_appfolio` expects; multiple sessions detected.
**Rollback:** close the browser context; no state was written, nothing to
roll back.

---

## Step 2 — Communication Inbox read (read-only)

**Prerequisites:** Step 1 passed.
**Gates enabled:** none required for a plain read; if using the QA-fingerprint
path, `RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED=True` plus a fresh
`RACHEL_APPFOLIO_READONLY_THREAD_QA_TARGET_SELECTOR_KEY` /
`..._EXPECTED_EVENT_KEY` / `..._AUTHORIZATION_ID` matching the row currently
visible on screen (see Part H of the handoff for how this fingerprint is
constructed and why it's deliberately single-use).
**Expected AppFolio result:** the target conversation opens; thread messages
are visible in the UI exactly as read by Rachel.
**Expected Rachel/DB result:** thread persisted via
`_store_verified_thread`/`load_verified_thread_context` under the correct
`conversation_identity_key`; `_verify_semantic_return_to_inbox` reports
route/list-root/composer-inactive/filter-scope all sound after return.
**Evidence to capture:** parsed message count and ordering vs. what's
visible on screen; `conversation_identity_key` value (not the message
content) logged; semantic return-verification result.
**Duplicate/idempotency checks:** re-read the same conversation a second time
— `conversation_identity_key` must be identical both times;
`conversation_identity_links` must not gain a second row for the same
thread.
**STOP conditions:** parsed message count/order disagrees with what's on
screen; identity key changes between the two reads; QA fingerprint is stale
(row no longer matches what's visible) — do not weaken the match or mint a
new fingerprint yourself, stop and report.
**Rollback:** none needed — read-only.

---

## Step 3 — Controlled outbound send (single message, test conversation only)

**Prerequisites:** Steps 1–2 passed. `RACHEL_APPFOLIO_QA_ENABLED=True` and
`RACHEL_APPFOLIO_QA_WO_NUM` / `RACHEL_APPFOLIO_QA_WO_ID` /
`RACHEL_APPFOLIO_QA_TENANT` / `RACHEL_APPFOLIO_QA_VENDOR` all set to the
**disposable test work order/tenant/vendor**, not a real one.
**Gates enabled:** `RACHEL_APPFOLIO_QA_ENABLED`, plus
`RACHEL_APPFOLIO_MESSAGING_ENABLED` for this step only.
**Procedure:** trigger exactly one `vendor_schedule_message` or
`tenant_schedule_message` operation (per `CONTROLLED_APPFOLIO_QA_OPERATIONS`
in `rachel_safety.py`) against the test conversation.
`controlled_appfolio_qa_allowed()` must confirm the operation, work order
number, work order URL, tenant, and vendor all match the configured QA
target exactly before Rachel is allowed to act — this is the narrow-scope
gate that prevents an accidental send to a real conversation.
**Expected AppFolio result:** exactly one new outbound message appears in
the test conversation, matching what Rachel intended to send.
**Expected Rachel/DB result:** exactly one `outbound_intent` row completed
in the write-intent ledger (see `rachel_appfolio_write_intents.py`); no
`prepare_outbound_intent` call left in a `claimed`-but-not-`completed` state.
**Evidence to capture:** ledger row before/after; message content sent (only
in the disposable test conversation, never real PII).
**Duplicate/idempotency checks:** attempt to trigger the same operation
again immediately with the same idempotency key — must be rejected/no-op,
not a second send.
**STOP conditions:** `controlled_appfolio_qa_allowed` returns True for a
target that isn't the intended test conversation; message appears more than
once; message appears in the wrong conversation.
**Rollback:** delete/archive the test message in AppFolio if your test
account permits it; otherwise note it as test noise in the disposable
conversation.

---

## Step 4 — Retry and dedup prevention (idempotent write-intent ledger)

**Prerequisites:** Step 3 passed.
**Gates enabled:** same as Step 3.
**Procedure:** deliberately induce a mid-send failure (e.g. kill the process
right after `claim_outbound_intent` but before `complete_outbound_intent`),
then restart and let Rachel re-process the same intent.
**Expected AppFolio result:** the message is sent **exactly once** overall —
either the original attempt completed and the retry is a no-op, or the
original attempt never reached AppFolio and the retry sends it once.
**Expected Rachel/DB result:** `record_outbound_intent_failure` recorded the
interruption; the ledger shows a single `completed` intent at the end, never
two.
**Evidence to capture:** ledger state at each transition
(prepared→claimed→failed→retried→completed).
**Duplicate/idempotency checks:** this step *is* the duplicate check —
confirm AppFolio shows exactly one message from this test.
**STOP conditions:** two messages appear in AppFolio from one logical
intent.
**Rollback:** same as Step 3.

---

## Step 5 — Vendor assignment

**Prerequisites:** Steps 1–2 passed; disposable test work order available.
**Gates enabled:** `RACHEL_APPFOLIO_QA_ENABLED`,
`RACHEL_APPFOLIO_WRITES_ENABLED` for this step, QA target vars set to the
test work order/vendor.
**Procedure:** run `dispatch_vendor_to_wo` (per
`CONTROLLED_APPFOLIO_QA_OPERATIONS`'s `"dispatch_vendor"`) against the test
work order.
**Expected AppFolio result:** the test work order shows the intended vendor
assigned, matching `WORK_ORDER_DISPATCH_REQUIRED_CHECKBOXES` /
`WORK_ORDER_DISPATCH_PROTECTED_CHECKBOXES` state exactly as intended (no
protected checkbox toggled unintentionally).
**Expected Rachel/DB result:** DB reflects the new vendor assignment for the
test work order only.
**Evidence to capture:** before/after screenshot or DOM state of the
checkboxes; DB row.
**Duplicate/idempotency checks:** re-run the same assignment — must not
create a second vendor-dispatch record or toggle checkboxes twice.
**STOP conditions:** any protected checkbox changes state; assignment lands
on the wrong work order.
**Rollback:** manually unassign the vendor from the test work order in
AppFolio if needed.

---

## Step 6 — Resident Availability lookup

**Prerequisites:** Step 1 passed.
**Gates enabled:** none (read-only lookup) — `RACHEL_APPFOLIO_QA_ENABLED`
only if routed through the same controlled-QA path as other steps.
**Procedure:** trigger the Resident Availability lookup
(`work_order.resident_availability_marker` accessible-name contract) against
the test work order/tenant.
**Expected AppFolio result:** availability data visible in the UI matches
what Rachel parses.
**Expected Rachel/DB result:** parsed availability windows stored/used
correctly for the test tenant only.
**Evidence to capture:** parsed windows vs. what's on screen.
**Duplicate/idempotency checks:** re-read — result must be stable.
**STOP conditions:** parsed data disagrees with the screen.
**Rollback:** none — read-only.

---

## Step 7 — Vendor scheduling (2-hour window)

**Prerequisites:** Steps 5–6 passed.
**Gates enabled:** `RACHEL_APPFOLIO_QA_ENABLED`,
`RACHEL_APPFOLIO_WRITES_ENABLED`, QA target vars set to the test work
order/vendor. This exercises `update_wo_schedule` — the exact function
touched by this round's `combined_selector("work_order.selected_option")`
fix (handoff doc, item 11). Verify this specific control carefully.
**Procedure:** schedule a vendor appointment for a 2-hour window on the test
work order via `update_wo_schedule`.
**Expected AppFolio result:** the test work order shows the exact 2-hour
window selected — confirm the *duration dropdown's selected value* reads
correctly (this is the control the selector fix touched).
**Expected Rachel/DB result:** scheduling state machine
(`rachel_maintenance_scheduling.py`) reflects the scheduled window for the
test work order.
**Evidence to capture:** selected duration value read back from AppFolio
after save; DB scheduling state.
**Duplicate/idempotency checks:** re-run scheduling for the same window —
must not create a second schedule entry.
**STOP conditions:** selected duration in AppFolio doesn't match what was
requested; wrong work order affected.
**Rollback:** clear/reset the test work order's schedule in AppFolio.

---

## Step 8 — Tenant scheduling / confirmation

**Prerequisites:** Step 7 passed.
**Gates enabled:** same as Step 7, `tenant_schedule_message` operation.
**Procedure:** send the tenant-facing scheduling confirmation message for
the test work order/tenant.
**Expected AppFolio result:** one confirmation message in the test tenant
conversation, matching the vendor-scheduled window from Step 7.
**Expected Rachel/DB result:** ledger + scheduling state show the tenant as
notified/confirmed for the test work order only.
**Evidence to capture:** message content sent (test conversation only);
ledger and scheduling-state rows.
**Duplicate/idempotency checks:** re-trigger — must not double-send.
**STOP conditions:** message references the wrong window or wrong work
order.
**Rollback:** same as Step 3/4.

---

## Step 9 — Exact tenant-confirmed 2-hour appointment write

**Prerequisites:** Step 8 passed and the test tenant conversation has an
actual (test) confirmation reply.
**Gates enabled:** same as Step 7/8.
**Procedure:** drive the full "tenant confirmed → write final appointment"
path end to end for the test work order.
**Expected AppFolio result:** the final, tenant-confirmed 2-hour window is
written and visible on the test work order exactly as confirmed — no drift
from the window offered.
**Expected Rachel/DB result:** scheduling state machine reaches its terminal
"confirmed" state for the test work order; no other work order affected.
**Evidence to capture:** full state-machine transition log for this test
work order.
**Duplicate/idempotency checks:** replay the same confirmation event — must
not re-write or duplicate the appointment.
**STOP conditions:** written window differs from confirmed window by any
amount.
**Rollback:** clear the test work order's appointment in AppFolio.

---

## Step 10 — Additional work order creation

**Prerequisites:** Step 1 passed; disposable test property/unit available.
**Gates enabled:** `RACHEL_APPFOLIO_QA_ENABLED`,
`RACHEL_APPFOLIO_WRITES_ENABLED`, `"schedule_work_order"` (or the relevant
QA operation) target vars set to test values.
**Procedure:** run `create_additional_work_order` with a test issue label —
this exercises the new `work_order_issue_option(page, issue_label)` helper
(handoff doc, item 7).
**Expected AppFolio result:** a new test work order is created with exactly
the issue selected — confirm the issue-option match is exact, not a
substring/fuzzy match picking a neighboring option.
**Expected Rachel/DB result:** DB records the new test work order.
**Evidence to capture:** issue label requested vs. issue option actually
selected in AppFolio.
**Duplicate/idempotency checks:** re-run with the same inputs — should not
silently create a second identical work order without an explicit intent to
do so.
**STOP conditions:** wrong issue option selected; work order created against
a real (non-test) unit.
**Rollback:** cancel/delete the test work order in AppFolio if your test
account permits it.

---

## Step 11 — Work order description editing

**Prerequisites:** Step 10 passed (or any test work order available).
**Gates enabled:** same as Step 10.
**Procedure:** run `edit_wo_description` against the test work order.
**Expected AppFolio result:** description field shows exactly the new text,
via the centralized `work_order.job_description_label` /
`work_order.save_button` contracts.
**Expected Rachel/DB result:** DB reflects the updated description for the
test work order.
**Evidence to capture:** description text before/after.
**Duplicate/idempotency checks:** re-run the same edit — idempotent, no
duplicate history entries beyond what AppFolio itself logs.
**STOP conditions:** wrong work order edited; save did not persist.
**Rollback:** revert the description text in AppFolio.

---

## Step 12 — Email control (`work_order.email_link_control`)

**Prerequisites:** Step 1 passed; test work order with an email-capable
control available.
**Gates enabled:** `RACHEL_APPFOLIO_QA_ENABLED`,
`RACHEL_APPFOLIO_MESSAGING_ENABLED`.
**Procedure:** exercise the `work_order.email_link_control` accessible-name
contract (the sibling of the now-removed dead `work_order.email_link`
selector — handoff doc, item 9) on the test work order.
**Expected AppFolio result:** the correct email control activates/opens;
confirm it is *not* confused with any other control on the page (this is
exactly the ambiguity the dead-selector removal was cleaning up).
**Expected Rachel/DB result:** whatever downstream email action Rachel
takes is recorded once in the ledger.
**Evidence to capture:** which control was clicked, confirmed against
`work_order.email_link_control`'s definition.
**Duplicate/idempotency checks:** re-run — same single control activated
each time.
**STOP conditions:** wrong control activated.
**Rollback:** as applicable to whatever the control opened (e.g. close an
unsent email draft).

---

## Step 13 — Failure/retry/idempotency (general, cross-cutting)

**Prerequisites:** Steps 3–12 individually passed.
**Gates enabled:** whichever gate(s) correspond to the write path you're
stress-testing.
**Procedure:** for at least one write path from Steps 3–12, deliberately
inject a failure after the AppFolio-side write succeeds but before Rachel's
ledger records completion (e.g. kill Rachel's process at that exact point).
**Expected AppFolio result:** unchanged from the original successful write
(nothing extra happens on the AppFolio side from the retry).
**Expected Rachel/DB result:** on restart, Rachel's reconciliation logic
(`_work_order_reconciliation_due`, `_mark_work_order_reconciliation_completed`/
`_failed`) detects the ambiguous state and does not blindly re-issue the
write.
**Evidence to capture:** reconciliation log output; final ledger state.
**Duplicate/idempotency checks:** this step's purpose.
**STOP conditions:** any duplicate AppFolio-side effect from the injected
failure.
**Rollback:** as applicable to whichever write path was used.

---

## Step 14 — Deliberate Render restart / resume

**Prerequisites:** an earlier write step (3–12) is in-flight or has just
completed.
**Gates enabled:** whatever was active for the in-flight step.
**Procedure:** trigger a Render service restart (deliberately, via Render's
dashboard/CLI — this is an infra action, treat it with the same care as any
other production-affecting action and confirm with Owner before doing it
against the real Render service) while Rachel has state in flight.
**Expected AppFolio result:** no duplicate or partial writes caused by the
restart itself.
**Expected Rachel/DB result:** `_ensure_runtime_state_table` /
`RACHEL_DB_PATH`-backed state survives the restart; Rachel resumes from
where it left off rather than re-starting from zero or losing track of
in-flight work.
**Evidence to capture:** DB runtime-state rows before/after restart;
process logs showing resume behavior.
**Duplicate/idempotency checks:** confirm via the ledger that nothing
in-flight got double-processed.
**STOP conditions:** any AppFolio-side duplicate; runtime state lost.
**Rollback:** none beyond the write path's own rollback if something did
duplicate.

---

## Step 15 — Scheduler catch-up

**Prerequisites:** Step 14 passed.
**Gates enabled:** as needed for whatever poll/reconciliation path is being
tested (`_appfolio_inbox_poll_due`, `_work_order_reconciliation_due`).
**Procedure:** artificially age the "last completed" timestamps backward (in
a test DB, not production) so a poll/reconciliation cycle is "due," then let
Rachel run its normal schedule loop.
**Expected AppFolio result:** none directly — this step is about catch-up
logic, not new AppFolio writes.
**Expected Rachel/DB result:** exactly one catch-up cycle runs, not a flood
of repeated cycles; `_mark_appfolio_inbox_poll_completed`/
`_mark_work_order_reconciliation_completed` update correctly.
**Evidence to capture:** before/after "due" timestamps; count of cycles
actually executed.
**Duplicate/idempotency checks:** this step's purpose.
**STOP conditions:** multiple redundant cycles fire for one "due" window.
**Rollback:** reset the test DB's timestamps.

---

## Step 16 — Bandwidth measurement

**Prerequisites:** Steps 1–15 passed.
**Gates enabled:** whichever are relevant to the operations being measured.
**Procedure:** measure wall-clock time and AppFolio request volume for a
representative full cycle (login → inbox read → one write path → return to
inbox).
**Expected result:** a documented baseline (not a pass/fail threshold unless
Owner sets one) for how long/how many requests a normal cycle takes — this
becomes the reference point for detecting future slowdowns or runaway
polling.
**Evidence to capture:** timing and request-count numbers, saved alongside
this runbook's results for future comparison.
**STOP conditions:** none inherent — but if request volume looks
unexpectedly high, treat that as a signal to investigate before continuing
to later steps.

---

## Step 17 — Circuit-breaker test

**Prerequisites:** Steps 1–16 passed.
**Gates enabled:** whichever write path is used to induce failures.
**Procedure:** deliberately induce repeated failures on one feature (e.g.
point a QA target at a work order that will make a specific action fail
validation) until `AppFolioFeatureHealth`'s failure counter trips
`FeatureCircuitOpen` for that feature.
**Expected AppFolio result:** no further attempts reach AppFolio for that
feature once the circuit is open.
**Expected Rachel/DB result:** subsequent calls to the tripped feature raise
`FeatureCircuitOpen` immediately (no network/browser action); after the
cooldown period, the circuit allows one more attempt.
**Evidence to capture:** failure count at trip time; behavior during
cooldown; first post-cooldown attempt result.
**Duplicate/idempotency checks:** N/A — this step is about suppression, not
duplication.
**STOP conditions:** circuit never trips despite repeated failures, or
attempts continue reaching AppFolio after the circuit is open.
**Rollback:** reset the feature's failure counter/cooldown state in the test
DB.

---

## Step 18 — Multi-hour soak

**Prerequisites:** Steps 1–17 all passed individually.
**Gates enabled:** the minimal set needed for Rachel's normal operating
loop, at the scope Owner authorizes for the soak (this may still be
read-only/QA-only — confirm explicitly with Owner before enabling any write
gate for an unattended multi-hour run).
**Procedure:** let Rachel run its normal loop unattended for several hours
against the test environment/conversation, with monitoring.
**Expected result:** no unexpected duplicate writes, no circuit breakers
tripping unexpectedly, no runtime-state corruption, stable memory/process
behavior.
**Evidence to capture:** periodic snapshots of ledger state, foundation
health report, and process logs across the soak window.
**STOP conditions:** any duplicate write, any unhandled exception that isn't
fail-closed, any gate silently changing state.
**Rollback:** stop the process; roll back any writes per their individual
steps above.

---

## Step 19 — Gate-by-gate production activation

**Prerequisites:** all prior steps passed against the test
environment/conversation.
**Procedure:** with Owner's explicit, separate authorization for **each**
gate, flip production gates on one at a time in this order (least to most
consequential), re-verifying the corresponding step above against a real
(not test) conversation/work order immediately after each flip, before
moving to the next gate:
1. `RACHEL_APPFOLIO_QA_ENABLED` (still QA-scoped, narrowest blast radius)
2. `RACHEL_APPFOLIO_READONLY_THREAD_QA_ENABLED` (still read-only)
3. `RACHEL_SMS_ENABLED`
4. `RACHEL_APPFOLIO_MESSAGING_ENABLED`
5. `RACHEL_APPFOLIO_WRITES_ENABLED`
6. `RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED`
7. `RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED`
8. `RACHEL_ENABLED` (master switch) / `RACHEL_TEST_MODE=False`
**STOP conditions:** any unexpected behavior at any gate — revert that one
gate to `False` and stop the rollout; do not proceed to the next gate until
the issue is understood.

---

## Step 20 — Rollback verification

**Prerequisites:** Step 19 in progress or complete.
**Procedure:** for at least one enabled gate, deliberately flip it back to
`False` (a real rollback, not just a plan) and confirm Rachel immediately
stops attempting the corresponding action — no in-flight retry ignores the
gate, no cached "gate was on" state persists past the flip.
**Expected result:** gate flip takes effect on the next check without a
restart being required (or, if a restart is required, that this is
documented and expected).
**Evidence to capture:** timestamp of flip vs. timestamp of last attempted
action of that type.
**STOP conditions:** Rachel continues attempting the gated action after the
gate is flipped off.

---

## Recording results

For each step, record: pass/fail, timestamp, evidence captured (per step
above — logs, DB snapshots, screenshots with PII redacted), and any
deviation from expected behavior. Do not proceed past a failed step without
Owner's explicit direction on how to handle it.
