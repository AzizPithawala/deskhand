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
Open http://localhost:8000. Run the tests with `make test` (35 tests). Optional: copy `.env.example` to `.env` and set
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

## What is NOT done or NOT verified
- Real viaSocket integration (Slack, Gmail, Keka). See docs/VIASOCKET_INTEGRATION.md and milestones M1.5 and M8.
- The Claude classification path (app/classifier.py: classify_anthropic) has never been run against the live API. It is
  validated and falls back to the rules on any error, but check it against the current SDK docs before relying on it.
- The rules classifier scores 100% on data/sample_requests.json because it was tuned while looking at that file. That number
  is optimistic. Measure on real past requests.
- The UI has been syntax-checked and driven through its API, but not inspected in a browser. Expect rough edges.
- No login (a header switcher lets you act as anyone), no real email, digest is a preview only, single company.

## Next steps (in order)
1. Click through the demo script in docs/BUILD_PLAN.md and fix whatever looks off.
2. Replace the synthetic samples with 30 to 50 real past requests and re-measure.
3. Run the viaSocket spike (M1.5) with docs/VIASOCKET_PROMPT.md.
4. Build the real adapters (M8) behind the existing interfaces (MockHR in app/adapters.py, outbox in app/engine.py).
5. Add the Anthropic key and verify the AI path.
