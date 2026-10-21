# Build plan (prototype)

STATUS: v0 built. M0 to M7 are covered by the simulator-based prototype (see README.md). Still open: M1.5 (viaSocket spike),
M8 (real integration), M9 (polish, real test data). When continuing, extend the existing code; do not start over.

Work one milestone at a time. For each: propose a plan, get approval, build, run tests, show the demo step.
Time box: about 3 weeks total. Stop and report at any milestone that is blocked.

## Milestones
M0 Scaffold. Repo layout, pyproject, Makefile (dev, test, demo), .env.example, health endpoint, first test.
   Demo: `make dev` shows an empty app; `make test` passes.
M1 Core model. SQLite schema, state machine with all transitions, append-only audit, fake clock.
   Tests: every valid and invalid transition; audit written for each change.
   Demo: CLI or test script drives a request from SUBMITTED to CLOSED.
M1.5 viaSocket spike (time box: 1 to 2 days, after M1). Use the prompt in docs/VIASOCKET_PROMPT.md with scope "spike M1.5".
   Connect test accounts, make one real call per adapter action, answer the questions in docs/VIASOCKET_INTEGRATION.md,
   and update docs/VIASOCKET_FACTS.md. Stop and report if any answer breaks the plan.
   Demo: a short written report of real responses and a go/no-go per integration.
M2 Directory and routing. Load data/employees.demo.json and data/request_types.demo.json. Resolve approver,
   backup, second approver (cost threshold), fulfiller. Identity resolution by slack_id, email, phone.
   Tests: routing for each type; unknown sender refused; approver never taken from message text.
M3 AI classification. classify() with strict JSON schema, deterministic fallback, injection handling,
   multi-intent split, clarifying question. Eval harness on data/sample_requests.json.
   Tests: schema validation; adversarial items handled safely; accuracy report (target 90% on known types).
M4 Simulator UI and notifications. Employee chat page, email inbox page, approver inbox with
   approve / reject (reason required) / pending, outbox worker, notifications at every state change.
   Demo: stories US1, US2, US3, US5.
M5 Guard. Inbound dedupe, own-message ignore, circuit breaker, idempotent actions with retry/backoff,
   parking and replay, AI budget. Chaos panel with one button per scenario.
   Tests: all chaos scenarios from US6 with zero duplicate effects.
   Demo: US6 live.
M6 Scheduler. Reminders and escalations, accelerated demo clock, "pending with X since Y" status.
   Demo: US4.
M7 Reports and dashboard. Dashboard (counts, average decision time, guard counters), audit page, weekly
   digest preview per approver and employee.
   Demo: US9 and US10.
M8 Real integration. Use the prompt in docs/VIASOCKET_PROMPT.md with scope "M8". Build the viaSocket adapters behind the existing
   interfaces: action runner with result normalisation, event handler forwarding to /ingest/viasocket, Connections admin page
   with Connect buttons, polling fallback. Keep the simulator as default. Only integrations proven in the spike are enabled.
   Run the skill's check script as a test and produce the "Finished when" report.
M9 Demo polish. Seed data, reset button, the demo script below as one pytest, README with run steps.

## Seven-minute demo script
1. (1 min) Employee asks for leave in chat. Show extraction, balance check, manager approval, confirmation.
2. (1.5 min) Employee asks for a Claude seat. Show the rewritten request, approve / reject / pending buttons.
   Approve, then "Done"; employee notified. Repeat with a rejection and show the mandatory reason.
3. (1 min) Email "Need a second monitor". Show the email routed and the email replies per decision.
4. (1 min) Leave a request pending, advance the clock, show the reminder and the escalation.
5. (1.5 min) Chaos panel: deliver one message 4 times (one request); bot replies to itself (ignored); fail the
   HR action twice (completes once); fail permanently (parked, then Replay).
6. (0.5 min) Prompt-injection message and a payroll question: both handled safely.
7. (0.5 min) Dashboard and weekly digest, with the guard counters.

## Prompts to use with Claude Code
Start: "Read CLAUDE.md, README.md and every file in docs/ and data/. A working v0 exists: run `make test` and `make dev`, and do not rewrite it. Do not write new code yet. Tell me (1) what we are
building in your own words, (2) what is in and out of scope, (3) which facts you will treat as unverified,
(4) your plan for the next open milestone, and (5) up to three questions. Wait for my approval."
Each milestone: "Plan milestone Mx from docs/BUILD_PLAN.md, list the tests first, wait for my OK, then build it
and show me the demo step."
After each milestone: "Run the tests, update README, commit, and tell me anything you assumed."
