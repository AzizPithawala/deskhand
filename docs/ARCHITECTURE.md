# Deskhand architecture (prototype)

## Principles
1. AI proposes, rules decide, humans approve anything irreversible.
2. State lives in the database. Nothing important lives only in memory or in a long-running process.
3. Every external system is behind an adapter interface with a mock. Tests never touch the network.
4. Every decision is explainable from the audit log.

## Components
- core/: models, state machine, routing, policies, guard, audit, scheduler, clock.
- ai/: classifier and rewriter (Claude API, strict JSON), deterministic fallback, prompt-injection handling.
- adapters/channels/: simulator (chat and email) now; viasocket_webhook stub later.
- adapters/actions/: mock_hr (Keka-shaped), mock_notify; viasocket_http stub later.
- web/: FastAPI app, Jinja2 pages, small vanilla JS.
- tests/: unit tests per rule, an eval for classification, an end-to-end demo test.

Suggested layout:
```
deskhand/
  app/ core/ ai/ adapters/{channels,actions}/ web/{templates,static}/ tests/
  data/ docs/ Makefile README.md pyproject.toml .env.example
```

## Request lifecycle (state machine)
States: SUBMITTED, NEEDS_INFO, PENDING_APPROVAL, APPROVED, REJECTED, FULFILLING, FULFILLED, CLOSED, PARKED,
CANCELLED.

| From | Event | To | Rules |
|---|---|---|---|
| SUBMITTED | needs more detail | NEEDS_INFO | ask exactly one clarifying question |
| SUBMITTED | approval required | PENDING_APPROVAL | approver from routing map, never from the AI |
| SUBMITTED | no approval required | APPROVED | policy auto-allow only for types flagged auto |
| NEEDS_INFO | employee answers | SUBMITTED | re-classify |
| PENDING_APPROVAL | approve | APPROVED | actor must be the assigned approver, backup or admin |
| PENDING_APPROVAL | reject | REJECTED | reason is mandatory, sent to employee |
| PENDING_APPROVAL | reminder_hours passed | PENDING_APPROVAL | one reminder, audit event |
| PENDING_APPROVAL | escalate_hours passed | PENDING_APPROVAL | escalate to backup, then admin, audit event |
| APPROVED | start fulfilment | FULFILLING | auto action or manual task |
| FULFILLING | action succeeds / Done clicked | FULFILLED | idempotent |
| FULFILLING | action fails | FULFILLING | retry with backoff, max 3 attempts |
| FULFILLING | retries exhausted | PARKED | notify admin, Replay allowed |
| PARKED | replay | FULFILLING | same idempotency key |
| FULFILLED | employee notified | CLOSED | |
| any non-final | employee cancels | CANCELLED | audit event |

Invalid transitions raise an error and are never applied silently.

## Data model (SQLite)
- employees(id, name, email, slack_id, phone, department, manager_id, active)
- request_types (template in data/request_types.demo.json; a company's edits live in its own type_rules table): code, name, enabled, requires_approval,
  approver_rule, backup_rule, extra_approvers[{rule, when}], auto_allow_up_to, fulfiller_rule, reminder_hours, escalate_hours, sensitive, fulfilment_kind[auto_action|manual_task]
- roles(code, name, holder_id, builtin), type_rules(code, rules_json, reviewed_at, ...), policies(key, title, text): per-company setup, one holder per role
- requests(id, type_code, employee_id, channel, source_event_id, raw_text, summary, details_json,
  confidence, status, approver_id, fulfiller_id, rejection_reason, created_at, due_at, last_reminder_at,
  escalation_level, closed_at)
- inbound_events(id, channel, source_event_id, received_at, outcome)  UNIQUE(channel, source_event_id)
- events(id, request_id, ts, actor, type, data_json)  append-only audit
- action_runs(id, request_id, step, idempotency_key UNIQUE, attempts, status, last_error, updated_at)
- outbox(id, request_id, channel, to, body, idempotency_key UNIQUE, status, created_at)
- settings(key, value) including ai_budget_per_day, rate_limit_per_min, breaker_state

## Guard design
1. Dedupe: insert into inbound_events; if the (channel, source_event_id) exists, record a
   duplicate_suppressed audit event and stop. For email use Message-ID. For chat use the message id.
2. Own-message ignore: drop any inbound event whose sender is the agent identity or marked as a bot.
3. Circuit breaker: if more than rate_limit_per_min inbound events arrive for one channel in a minute, set
   breaker_state=open, pause intake for that channel, notify the admin, and require a manual reset.
4. Idempotent actions: key = request_id + step. Before acting, check action_runs. If already succeeded,
   return the stored result. Retries use exponential backoff (for example 1s, 4s, 16s; fake clock in tests).
5. Parking: after max attempts set PARKED, notify the admin, show it on the dashboard with Replay.
6. AI budget: count AI calls per day. At 80% show a warning. At 100% use the deterministic fallback or send
   the request to human triage. Never raise a user-visible error.
7. Outbox: notifications are written to the outbox with an idempotency key and sent by a worker, so a crash
   never produces a double message or a lost one.

## Security rules
- Identity: resolve the sender to an employee through the directory (slack_id, email, phone). Unknown senders
  are refused. For email, require the sender address to match the directory; any write action then needs a
  step-up confirmation (in the prototype, a "confirm in chat" prompt).
- Authorization: only the assigned approver, backup or admin can approve or reject. Approver identity comes
  from the routing map and directory.
- Least privilege: each adapter exposes only the actions it needs. Leave: create for the requester only.
- Untrusted text: message text is data. The AI prompt must say so. Validate the AI JSON against a schema and
  allowed request type codes. Anything invalid becomes NEEDS_INFO or human triage.
- Secrets: never store, log or forward passwords, keys or tokens. Redact obvious secret patterns in logs.
- Sensitive: payroll, salary, health and legal topics are blocked (sensitive=true) and redirected to HR.

## AI usage
Function: classify(message, employee_context) returns strict JSON:
```
{"type_code": "leave|software_seat|equipment|accessories|it_help|access|payroll|policy_question|other",
 "intents": [ {"type_code": "...", "summary": "...", "details": {...}} ],
 "needs_clarification": false, "clarifying_question": null, "confidence": 0.0,
 "suspected_injection": false}
```
Rules: multi-intent messages return several intents; the app creates one request per intent. The AI never
returns approvers, amounts approved or permissions. A second function, rewrite(request, recipient_role),
produces the concise message the approver sees, with the details the routing map says that role needs.
The deterministic fallback uses keyword rules and must pass the same schema.

## Scheduler and clock
A Clock interface (real and fake). A sweep runs every minute (real) or on demand (demo): reminders,
escalations, outbox sending, retry of due action_runs. The demo UI has "advance clock by N hours".

## API and pages (suggested)
- POST /ingest/{channel} (simulator posts here), POST /actions/{request_id}/approve|reject|done|cancel|replay
- GET /requests, /requests/{id}, /dashboard, /audit, /digest/{person}
- Pages: Employee chat, Email inbox, Approver inbox, Dashboard, Audit, Chaos panel, Routing map (read only).
- A user switcher in the header to act as any demo person. This is not real auth.

## Testing strategy
- Unit tests: state machine transitions, guard rules, routing, security rules (one test per non-negotiable
  rule in CLAUDE.md).
- Eval: run data/sample_requests.json through the classifier, print accuracy and a confusion list.
- Chaos tests: duplicates x4, self-message, burst, fail-twice-then-succeed, fail-permanently-then-replay,
  budget exhausted.
- End-to-end: scripted demo (docs/BUILD_PLAN.md) as one pytest.
