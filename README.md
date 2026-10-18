# Rachel — Production-Ready AI Property Management Agent

An autonomous AI operations agent for residential property management. Rachel monitors
multiple inbound channels (property-management platform inbox, email, tenant portal),
triages maintenance work orders, routes them through a human-in-the-loop approval flow,
dispatches vetted vendors, and verifies scheduling — with a safety-first architecture
designed for real production use.

> **Note:** This is a sanitized portfolio version of a production system. All client
> names, emails, credentials, and internal URLs have been replaced with generic
> placeholders. The technical architecture and logic are preserved exactly.

---

## What it does

| Capability | Description |
|---|---|
| **Inbox monitoring** | Polls the AppFolio communication inbox and Gmail for tenant messages, work-order updates, and vendor replies |
| **Work-order triage** | Classifies incoming maintenance issues by trade, urgency, and eligibility; builds structured work-order records |
| **Human-in-the-loop approval** | Management-review requests with deduplication — the same work order is never asked about twice |
| **Vendor dispatch & lifecycle** | Seeds, vets, and dispatches vendors within two-hour response windows; tracks the full vendor lifecycle |
| **Maintenance scheduling** | Schedules approved work, verifies the schedule against the platform, and reconciles state |
| **Lease tracking** | 90/60/30-day lease-window processor with catch-up runs |
| **Safety gates** | Every mutation path ships behind environment safety switches and dry-run modes |

---

## Technical highlights

### 1. Playwright automation for AppFolio
`rachel_appfolio_browser.py` drives the AppFolio property-management platform through a
managed Playwright browser: authenticated sessions with persistent profiles, DOM-based
inbox traversal, thread reading, and controlled write intents. Write operations are
gated behind separate QA flags (`RACHEL_APPFOLIO_WRITES_ENABLED`,
`RACHEL_APPFOLIO_MESSAGING_ENABLED`) so reads and writes can be enabled independently.

### 2. Custom state-tracking engine — solving the "retry storm"
Polling loops that retry on failure can spiral into a **retry storm**: every failed poll
schedules another immediate retry, hammering the target system. Rachel solves this with
a persistent state-tracking engine (`rachel_event_memory.py`, `rachel_inbox_monitor.py`):
every poll failure writes a backoff timestamp to durable storage, so the next cycle
computes its wait from persisted state instead of retrying blindly. Backoff windows are
configurable per poller via environment variables
(`RACHEL_WO_RECONCILIATION_BACKOFF_MINUTES`, `RACHEL_APPFOLIO_INBOX_BACKOFF_MINUTES`).

### 3. Exponential backoff and SMS → Email failover
Outbound tenant/vendor notifications use tiered delivery: SMS first, with automatic
failover to email when SMS delivery fails or is disabled. Retries use exponential
backoff with jitter, and every attempt is logged with its channel, so the delivery
path of any message is fully auditable. All sends are disabled by default
(`RACHEL_SMS_ENABLED=false`) until explicitly authorized.

### 4. Four-stage validation pipeline
Every work order flows through four explicit stages, each with its own module and
safety checks:

1. **Triage** (`rachel_wo_triage.py`) — classify the issue, verify eligibility, build the work-order record
2. **Approval** (`rachel_work_order_scanner.py`) — request management direction with dedup guards; record the instruction
3. **Dispatch** (`rachel_vendor_lifecycle.py`) — match and dispatch a vendor inside the response window
4. **Scheduling** (`rachel_maintenance_scheduling.py`) — schedule the work and verify it against AppFolio

A work order cannot skip a stage, and reconciliation jobs (`rachel_wo_triage.py`,
`test_wo_reconciliation.py`) continuously verify that platform state matches internal
state.

### 5. Safety-first by default
- Every destructive capability is behind an environment switch, defaulting to **off**
- Dry-run modes for email and lease processing
- Semantic verification of automated actions before they count as complete
- Login and session safety checks before any browser automation runs

---

## Architecture

```
main.py                          Serialized scheduler (single owner of all recurring work)
│
├── rachel_inbox_monitor.py       Gmail + AppFolio polling, retry-storm-safe backoff
├── rachel_appfolio_browser.py    Playwright session manager (auth, profiles, DOM access)
├── rachel_appfolio_inbox.py      Inbox traversal + thread reading (read-only paths)
├── rachel_appfolio_ui.py         UI interaction helpers (accessible-name based)
├── rachel_appfolio_write_intents.py   Controlled write intents (gated)
├── rachel_communication_sender.py     Outbound messaging (SMS → Email failover)
├── rachel_communication_workflow.py   Draft planning, ownership classification
├── rachel_wo_triage.py           Work-order triage + 4-stage pipeline orchestration
├── rachel_work_order_scanner.py  Management-review requests (deduped)
├── rachel_vendor_lifecycle.py    Vendor seeding, vetting, dispatch, 2-hour windows
├── rachel_maintenance_scheduling.py  Scheduling + verification
├── rachel_lease_tracker_v2.1.py / rachel_lease_window_processor.py
│                                 Lease-window tracking (90/60/30-day)
├── rachel_event_memory.py        Persistent state-tracking engine (anti-retry-storm)
├── rachel_safety.py              Safety gates and guards
├── rachel_foundation_health.py   Health checks
├── rachel_scraper_v2.py          Report scraping + ingestion
├── rachel_inbox_monitor.py       Lease/inbox email monitoring
├── rachel_property_review.py / rachel_manual_lease_review.py
│                                 Review workflows
├── rachel_db_init.py             Database initialization + migrations
└── rachel_seed_vendors.py        Vendor seed data
```

**Scheduler cadence** (`main.py`): lease tracker on the 1st business day, daily
lease-window catch-up, daily vendor follow-ups, and per-minute Gmail/AppFolio polling —
all owned by one serialized scheduler to prevent overlapping runs.

---

## Tech stack

Python · Playwright (browser automation) · Anthropic API (LLM reasoning) · SQLite
(state engine) · Gmail API · Google Sheets API · `schedule` (job scheduling) ·
Docker + Render (deployment)

---

## Tests

28 test modules covering: browser architecture, inbox traversal safety, communication
sender/poll flows, work-order routing and reconciliation, vendor lifecycle windows,
retry/backoff behavior, runtime recovery, lease processing, event memory, and import
side effects. Tests use deterministic placeholder credentials — no real secrets.

```bash
pip install -r requirements.txt
python -m pytest
```

---

## Configuration

All secrets are injected via environment variables — none are committed:

```bash
export APPFOLIO_EMAIL="you@example.com"
export APPFOLIO_PASSWORD="YOUR_PASSWORD_HERE"
export ANTHROPIC_API_KEY="YOUR_API_KEY_HERE"
export RACHEL_GMAIL_USER="you@example.com"
export RACHEL_GMAIL_PASSWORD="YOUR_PASSWORD_HERE"
```

See `render.yaml` for the full list of configuration keys and their safe defaults,
and `rachel_setup.sh` for a local setup template.

---

## Deployment

Ships as a Dockerized background worker (`Dockerfile`, `render.yaml`). The deploy
configuration keeps every mutation capability disabled until its separately
authorized QA pass completes — the agent can observe production on day one and
earn write access progressively.
