# Deskhand (agent platform, Phase 2: company setup)

An agent that handles employee requests end to end: understands the request, routes it by rule to the right approver,
tracks approve / reject (reason required) / pending, escalates on time, acts through guarded actions and keeps an audit trail.
Many companies can use one Deskhand, each strictly separate. People sign in; the server, not the browser, decides who is acting.
Requests are raised in the Ask chat inside Deskhand. Slack and Gmail notifications are delivered through viaSocket (see below). HR actions still run
against a mock HR system until the Phase 3 leave ledger. The plan for all phases is in `docs/AGENT_PLATFORM_PLAN.md`.

## Run it
```
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
DESKHAND_SIGNUP_CODES=any-code-you-like make dev        # or: uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000. Without `DESKHAND_SIGNUP_CODES` registration is closed and only "Try the demo" works. Needs Python 3.11+. Run the tests with
`make test` (269 tests, including the 7-minute demo script, two-company isolation and a golden check of the router). `make reset` deletes the local data directory (`.deskhand/`).
Requests are understood by a deterministic rules classifier. An admin of a real company can switch on viaSocket AI (a Claude model, no model key needed)
on the Health page; it only proposes the request type and details, waits at most 25 seconds, falls back to the rules on any failure, and never sees salary or
payroll messages. (`requirements.txt` is the deploy list; `requirements-dev.txt` adds pytest.)

## Companies, accounts and sign-in
- **Register a company** at `#register` with an invite code that the operator hands out (`DESKHAND_SIGNUP_CODES`, comma separated, values secret, each code works
  once). You become the **owner**. One email address belongs to one company only.
- **Invite people** on the People page (admins). Nothing is emailed yet: you copy the invite link (`/#join/<token>`) and send it yourself. The link works once,
  expires in 7 days and is shown only at that moment (only its hash is stored). The same screen sets each person's manager, department and access
  (Owner, Admin, Member), deactivates people (signed out at once) and makes password-reset links. Keep two admins in case the owner is locked out.
- **Who sees what.** Employees see their own requests and those assigned to them (as approver, backup or fulfiller). Admins see everything and get People, Health
  and Audit. Inbox, outbox and digest are always "mine". Routing uses the reporting lines set on the People page; a request with no manager goes to an admin.
- **Sessions.** Cookie `dh_session` (HttpOnly, SameSite=Lax, Secure on HTTPS), idle 12 hours, absolute 14 days; only a hash of the token is stored. Every change needs
  the `X-CSRF-Token` header. Passwords: at least 10 characters, hashed with scrypt (standard library). Five wrong passwords for an email in 15 minutes locks that
  email for the window. The browser never says who is acting: the API has no `actor_id` or `sender`.
- **Separate data.** Each company has its own database file, `DATA_DIR/orgs/<id>.db`; people, sessions, invites and codes live in `DATA_DIR/platform.db`. One company's engine
  cannot open another's file. Connections (Slack, Gmail), the AI switch and the delivery cap are per company.
- **The demo company** ("Acme Demo", id `acme-demo`) is a separate company with ten fictional people, entered with **Try the demo** (no password). Its **Demo controls** drawer has
  Act as, the demo clock and Reset; the Reliability lab on Health runs only there. In the demo, connecting apps, the AI switch, inviting people and changing passwords are
  switched off and nothing is ever sent. A real company never has Act as, chaos, reset or the clock.

## Company setup (Phase 2)
Admins open **Setup** (and **Integrations**, **People**) from the sidebar; on a phone the **Admin** tab opens a list of them. Everything is validated, audited (`setup_changed`: who, what, from and to) and applies to **new** requests.
- **Roles.** IT admin, Office admin, HR lead and Finance lead exist in every company; add custom roles. One person holds a role at a time. If nobody does (or the holder is deactivated), requests that need the role go to the type's backup rule, then to an admin.
- **Request types.** The 8 built-in types are editable, not new ones (that is Phase 5): switch on or off, needs approval, who approves (the requester's manager, a role or a named person), up to two extra approvers each with a condition (the cost is above an amount, or a detail such as the resource mentions words), an automatic allowance for accessories, reminder and escalation hours, who is the backup, who does the manual task, which details are asked. Payroll is always blocked; you only choose who employees are told to contact. A switched-off type creates no request and tells the employee who to ask.
- **Policies.** A new company starts with none, so Acme's texts never leak. Policy questions are answered with the company's own words, else "ask HR". Leave balances are never invented for a company (the leave ledger is Phase 3).
- **Routing test.** Pick a person, a type and optionally a cost or resource to see the approvers, the fulfiller, reminder and escalation times and warnings (empty role, deactivated holder, no manager). It calls the same functions as a real request, and a test compares the two.
- **Checklist.** Six steps that tick themselves from the data: invite people, set managers, choose role holders, review the request types ("Looks right"), connect Slack and Gmail, send a test request.
- The **demo company** shows its prepared rules read-only. Under the hood: tables `roles`, `type_rules`, `policies` in each company's own database file, merged over the template by `Directory.for_company`; `tests/test_setup_directory.py` runs the old hard-coded router against the new data-driven one for over 50,000 combinations.

## Leave record (Phase 3, step 1)
A real company keeps leave in its own **append-only ledger** (database triggers forbid editing or deleting a line). Each person gets the yearly allowance (default casual 12, sick 8, editable under Setup, Leave) the first time leave is looked at in a calendar year; leave taken
and an admin's corrections (Setup, Leave, Adjust: plus or minus days with a reason) are further lines, each with who and why. A balance is the sum of a person's lines for the year; a leave is keyed by its request, so a retry can never deduct twice. Only Monday to Friday count, a half day is 0.5,
and the reader now knows whether two dates are a range ("2 Nov to 6 Nov") or separate days ("Friday and Monday"). The approver's card shows the working days and the balance before and after, and warns about a shortfall or an overlap; the approver decides and the balance may go negative (shown in red).
Weekend-only dates ask again instead of reaching an approver. Everyone has a **Leave** page with their balance and history; policy questions answer from the ledger. The demo company keeps its prepared mock HR. Not done yet: cancelling an applied leave, accrual, holidays, carry-forward.

## What works (and is tested)
- Lifecycle: submitted, needs info, pending approval (one or two approvers), approved, rejected (reason mandatory),
  fulfilling, fulfilled, closed, parked, cancelled. Invalid transitions are refused.
- Routing from the company's people, its business roles and the rules of the 8 request types (defaults in data/request_types.demo.json, edited per company on the Setup page). The approver never comes from message text.
- Notifications to the employee at every state change; "pending with X since Y"; reminder and two escalations. For real companies a background sweep sends them on
  time; the demo moves only with its +1h / +9h / +25h clock buttons.
- Guard: dedupe, own-message ignore, circuit breaker, idempotent actions with retry, parking and replay, read-before-write
  so a lost response cannot double-apply a leave, AI-budget fallback.
- Security: sign-in, CSRF, per-company isolation, admin-only routes, payroll blocked, leave only for the requester, secrets redacted, prompt injection
  never approves anything, append-only audit log (enforced by database triggers).
- Reliability lab (demo only): seven live failure buttons. Dashboard with guard counters. Weekly digest preview (JSON).

## The interface ("Dusk")
Static HTML, CSS and JS in `app/static` (no build step, no dependencies; the Geist font is self-hosted under its OFL licence). Signed out: **Sign in**, **Create a company**
and the join link. Signed in: **Ask** (a chat; every request appears inline with its journey), **My requests**, **Needs you** (the approver queue, with a decision bar made for
one-handed phone use), and for admins **People**, **Health** (dashboard, delivery, Connections) and **Audit**. The account button (top right) has your details, the theme, change password
and Sign out. The request journey shows each stage, who holds it, how long it has waited and what happens next; every status has its
own shape as well as a colour. Light and dark follow the system; reduced motion is respected. `tests/test_ui_contract.py` checks that every API path the script calls exists.

## Connections and delivery (live-only, per company)
Admin only, on Health, and the only setup. The admin connects Slack and picks the notification channel, then connects Gmail, picks the
"send from" mailbox and enters ONE notification email. Sign-in happens on each app's own screen; Deskhand stores only connection ids on the server
(never shown, never logged). There is no switch and no mock: as soon as Slack and its channel are set, every chat notification goes to that channel,
prefixed "[Deskhand to <name>]"; as soon as Gmail and the notification email are set, every email notification goes to that one address with
"To <name>:" in the subject, never any other address. If either is missing, an admin sees a red banner on every tab and everything else keeps working.
Approvals never wait for delivery: a sweep delivers outbox rows, exactly once each (atomic claim, single-flight sweep), and never resends when the
outcome is unknown (failed rows show with a Retry button). Backstop cap: 150 deliveries per UTC day per company. Message text is escaped for Slack; approver messages
carry a link to Deskhand. `make preflight` prints yes/no for every company's setup.

## Hosted (Embarko)
One process, no managed database and no backups, so everything lives in `DATA_DIR` (the root `main.py` points `DESKHAND_DATA_DIR` there). The app has its own sign-in, so the old shared
password is no longer needed; set `DESKHAND_DEMO_PASSWORD` only if you want an extra HTTP Basic gate in front of everything.
Environment variable names: DESKHAND_SIGNUP_CODES, DESKHAND_BASE_URL, VIASOCKET_ORG_ID, VIASOCKET_PROJECT_ID, VIASOCKET_EMBED_SECRET, VIASOCKET_API_URL, VIASOCKET_RUN_URL
(optional: DESKHAND_DEMO_PASSWORD). No ANTHROPIC_API_KEY. Set the values yourself in Embarko's App setup; never put them in a file in the repository. An old
single-company `deskhand.db` found in the data directory becomes the demo company's database the first time the new version starts.
If people report being signed out, the hosted log says why (Embarko: `GET https://ship.embarko.ai/apps/<app>/logs`, with the deploy token): `session refused: <reason> on <method> <path>` where the reason is one of
`no_cookie`, `unknown_session`, `expired`, `idle`, `user_<status>`, `no_user`, `no_org`, `org_inactive`, `org_mismatch` or `company_unavailable`, and `gate refused <method> <path>: no_authorization_header | wrong_password`
when the shared password (not the session) turned a signed-in browser away. Cookies, passwords and tokens are never logged. The browser re-checks `/api/auth/me` and retries once before it signs anyone out on a single 401.

## What is NOT done or NOT verified
- No emails from Deskhand itself: invite and reset links are copied by an admin. A locked-out owner needs another admin to make a reset link. No email verification, no SSO or MFA (Phase 6).
- Leave still runs against the in-memory mock HR; the persisted leave ledger, Slack and Gmail request flow come in Phase 3. Request types cannot be created, only edited (Phase 5).
- A request is understood while the sender waits (about 25 seconds at worst with AI on). Slack inbound is slow (viaSocket polls every 15 minutes), so asking happens in Deskhand chat.
- SQLite on one process with no backups: fine for a pilot, not for real customers (Phase 6 moves to Postgres with backups).
- The rules classifier scores 100% on data/sample_requests.json because it was tuned while looking at that file. That number is optimistic. Measure on real past requests.
- The direct-Anthropic path (app/classifier.py: classify_anthropic) has never been run against the live API and is unused.

## Next steps
Phase 3: leave through Slack and
Gmail end to end with a Deskhand-owned leave ledger. See `docs/AGENT_PLATFORM_PLAN.md`.
