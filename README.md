# Deskhand prototype (v0)

An agent that handles employee requests end to end: understands the request, routes it by rule to the right approver,
tracks approve / reject (reason required) / pending, escalates on time, acts through guarded actions and keeps an audit trail.
Everything runs on simulated channels (chat and email) and a mock HR system. No real Slack, Gmail or Keka yet.

## Run it
```
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
make dev                                                # or: uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000. Needs Python 3.11+. Run the tests with `make test` (39 tests, including the 7-minute demo script as one test). Optional: copy `.env.example` to `.env` and set
`ANTHROPIC_API_KEY` to use Claude for understanding requests. Without a key a deterministic rules classifier is used.

## What works (and is tested)
- Lifecycle: submitted, needs info, pending approval (one or two approvers), approved, rejected (reason mandatory),
  fulfilling, fulfilled, closed, parked, cancelled. Invalid transitions are refused.
- Routing from data/request_types.demo.json and data/employees.demo.json. The approver never comes from message text.
- Notifications to the employee at every state change; "pending with X since Y"; reminder and two escalations (use the
  +1h / +9h / +25h clock buttons).
- Guard: dedupe, own-message ignore, circuit breaker, idempotent actions with retry, parking and replay, read-before-write
  so a lost response cannot double-apply a leave, AI-budget fallback.
- Security: unknown senders refused, payroll blocked, leave only for the requester, secrets redacted, prompt injection
  never approves anything, append-only audit log (enforced by database triggers).
- Chaos tab: seven live failure buttons. Dashboard with guard counters. Weekly digest preview (JSON).

## Connections tab and delivery (live-only)
Admin only (act as Arjun Patel), and the only setup. The admin connects Slack and picks the notification channel, then connects Gmail, picks the
"send from" mailbox and enters ONE notification email. Sign-in happens on each app's own screen; Deskhand stores only connection ids on the server
(never shown, never logged). There is no switch and no mock: as soon as Slack and its channel are set, every chat notification goes to that channel,
prefixed "[Deskhand to <name>]"; as soon as Gmail and the notification email are set, every email notification goes to that one address with
"To <name>:" in the subject, never any other address. If either is missing, a red banner shows on every tab and everything else keeps working.
Approvals never wait for delivery: a sweep delivers outbox rows, exactly once each (atomic claim, single-flight sweep), and never resends when the
outcome is unknown (failed rows show on the Connections tab with a Retry button). Backstop cap: 150 deliveries per UTC day, shown as used / 150 on
the dashboard, with a banner at the cap. Chaos runs and Reset demo never send (their rows are marked skipped); Reset keeps connections and settings.
Message text is escaped for Slack; approver messages carry a link to their Deskhand inbox. `make preflight` prints yes/no for the setup.

## Hosted demo (Embarko)
Embarko has no login, so the app has its own password gate (HTTP Basic, any user name, password in `DESKHAND_DEMO_PASSWORD`) on every route. When
hosted and no password is set, nothing is served. Data lives in `DATA_DIR` (main.py points the database there). One process only.
Environment variable names: DESKHAND_BASE_URL, DESKHAND_TENANT_ID, DESKHAND_DEMO_PASSWORD, VIASOCKET_ORG_ID, VIASOCKET_PROJECT_ID, VIASOCKET_EMBED_SECRET,
VIASOCKET_API_URL, VIASOCKET_RUN_URL. No ANTHROPIC_API_KEY. The notification email is entered on the Connections tab, not as a variable.

## What is NOT done or NOT verified
- Real viaSocket integration (Slack, Gmail, Keka). See docs/VIASOCKET_INTEGRATION.md and milestones M1.5 and M8.
- The Claude classification path (app/classifier.py: classify_anthropic) has never been run against the live API. It is
  validated and falls back to the rules on any error, but check it against the current SDK docs before relying on it.
- The rules classifier scores 100% on data/sample_requests.json because it was tuned while looking at that file. That number
  is optimistic. Measure on real past requests.
- The UI was clicked through in a browser against the demo script (docs/BUILD_PLAN.md) and polished: inline reason dialogs instead of browser pop-ups, UTC times that match the demo clock, readable details, email-style replies. Chaos runs use Neha Gupta's requests so the other demo people stay clean. The burst test floods a separate channel so it leaves no junk requests, and the AI-budget test restores the budget.
- No login (a header switcher lets you act as anyone), no real email, digest is a preview only, single company.

## Next steps (in order)
1. Click through the demo script in docs/BUILD_PLAN.md and fix whatever looks off.
2. Replace the synthetic samples with 30 to 50 real past requests and re-measure.
3. Run the viaSocket spike (M1.5) with docs/VIASOCKET_PROMPT.md.
4. Build the real adapters (M8) behind the existing interfaces (MockHR in app/adapters.py, outbox in app/engine.py).
5. Add the Anthropic key and verify the AI path.
