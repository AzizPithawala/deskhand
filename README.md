# Deskhand (agent platform, Phase 3: leave through Slack and Gmail)

An agent that handles employee requests end to end: understands the request, routes it by rule to the right approver,
tracks approve / reject (reason required) / pending, escalates on time, acts through guarded actions and keeps an audit trail.
Many companies can use one Deskhand, each strictly separate. People sign in; the server, not the browser, decides who is acting.
Requests are raised in the Ask chat inside Deskhand. Slack and Gmail notifications are delivered through viaSocket (see below). HR actions still run
against a mock HR system in the demo company; real companies keep leave in Deskhand's own ledger. The plan for all phases is in `docs/AGENT_PLATFORM_PLAN.md`.

## Run it
```
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
DESKHAND_SIGNUP_CODES=any-code-you-like make dev        # or: uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000. Without `DESKHAND_SIGNUP_CODES` registration is closed and only "Try the demo" works. Needs Python 3.11+. Run the tests with
`make test` (463 tests, including the 7-minute demo script, two-company isolation and a golden check of the router). `make reset` deletes the local data directory (`.deskhand/`).
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

## After-approval steps (Phase 5)
An admin decides, per request type, what happens in another app once a request is approved: add a row in Google Sheets, create a ticket, and so on, using any app from viaSocket's public catalog.
**Setup, Request types, After approval** (software seat, equipment, accessories, IT help, access; never leave or payroll): add up to three steps. For each, search the app, choose an action, connect the app
(viaSocket's own sign-in popup; Deskhand never sees a password), and fill the form drawn from the action's own definition. Texts can use request fields (requester, summary, the type's details, ...) from a closed
list, never the raw message. Choose whether the step runs by itself right after the final approval or when the approver presses Run; an action that deletes or removes things can only be run by a person.
A step starts untested and off. **Test run** really runs it once with made-up details (it says so, and a destructive action needs its name typed); that teaches Deskhand what a good answer looks like and lets you switch
the step on.

What happens to a request: it keeps the steps it was routed with, whatever is edited later. After the final approval the steps run in order. **In rehearsal and in the demo nothing is sent to the app**: the step is
recorded as "not sent" and the rehearsing admin is told what it would have done. Live, the worker runs them within seconds. Only a rate limit is retried by itself; a timeout or any doubt parks the request as
"outcome unknown" and an admin chooses **Run again** or **Mark as done** (with what they checked). The request page shows each step as a station on its route; a step waiting for Run appears under Needs you for the
approver, the person who completes that kind of request, and admins. By default a person still clicks Done afterwards; a type can close by itself when all steps succeed.

Code: `app/viasocket_catalog.py` (the catalog; a recorded fixture for tests and offline work: `DESKHAND_STEPS_CATALOG=fixture`), `app/steps.py` (validation), `app/step_runner.py` (running). Rule 21 in `CLAUDE.md`.
What viaSocket really does in a real app is in `docs/VIASOCKET_FACTS.md` ("PHASE 5 SPIKE 5A.0"). **Proven:** Google Sheets, add a row. **Not proven:** other apps (the catalog says what each needs; the test run
proves it for your setup), apps that sign in with an API key, an app's answer feeding the next step. Tests never read your `.env` or reach viaSocket.

## Leave record (Phase 3, step 1)
A real company keeps leave in its own **append-only ledger** (database triggers forbid editing or deleting a line). Each person gets the yearly allowance (default casual 12, sick 8, editable under Setup, Leave) the first time leave is looked at in a calendar year; leave taken
and an admin's corrections (Setup, Leave, Adjust: plus or minus days with a reason) are further lines, each with who and why. A balance is the sum of a person's lines for the year; a leave is keyed by its request, so a retry can never deduct twice. Only Monday to Friday count, a half day is 0.5,
and the reader now knows whether two dates are a range ("2 Nov to 6 Nov") or separate days ("Friday and Monday"). The approver's card shows the working days and the balance before and after, and warns about a shortfall or an overlap; the approver decides and the balance may go negative (shown in red).
Weekend-only dates ask again instead of reaching an approver. A weekday named on that very weekday means today ("Friday" said on a Friday), "next Friday" means after today, dates are always listed in date order, and the approver's card says when a leave includes today or has dates already past. Everyone has a **Leave** page with their balance and history; policy questions answer from the ledger. The demo company keeps its prepared mock HR. Not done yet: cancelling an applied leave, accrual, holidays, carry-forward.

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
Static HTML, CSS and JS in `app/static` (no build step, no dependencies; the Atkinson Hyperlegible Next font is self-hosted under its OFL licence). Signed out: a **landing page** at the bare address (what Deskhand does, how a request moves, why nothing gets lost, rehearsal, Slack and Gmail; its hero is the real request journey drawn as an example, and it makes no claim the product cannot back up),
then **Sign in**, **Create a company** and the join link (each shows the same example beside the form on wide screens). A link to a page still goes to sign-in and comes back; a signed-in person never sees the landing. Signed in: **Ask** (a chat; every request appears inline with its journey), **My requests**, **Needs you** (the approver queue, with a decision bar made for
one-handed phone use), **Notifications** (everyone, with an unread badge), and for admins **Setup**, **People**, **Integrations**, **Health**, the **Delivery log** and **Audit**. The account button (top right) has your details, the theme, change password
and Sign out. The request journey shows each stage, who holds it, how long it has waited and what happens next; every status has its
own shape as well as a colour. Light and dark follow the system; reduced motion is respected. **Design system ("Route map", notes in `docs/DESIGN.md`):** every request is a route of stations (the status shapes) joined by a line coloured by request type; the side menu, board rows, request tickets, the Admin line and the landing hero all use the same drawing. One typeface, Atkinson Hyperlegible Next; day and night service themes. **Logo and icons:** the mark (a request handed on: a filled station, a line, a half-filled station) is `app/static/icon.svg` plus the PNG fallbacks `icon-32.png` and `apple-touch-icon.png`, all linked from `index.html`; the same drawing is the theme-aware `#logo` symbol in the header and landing page (construction in `docs/DESIGN.md`). One stylesheet organised as tokens, base, controls, shell, routes, boards and tickets, health and tables, dialogs, admin, notifications, landing.
Every text and background colour pair is at least 4.5:1 in both themes, form-field borders are at least 3:1, there is a visible focus ring, and touch targets are 44px. `tests/test_ui_contract.py` checks that every API path the script calls exists, that the landing page makes only the two calls the sign-in page makes, and that the delivery log page never reads message text.

## Messaging: Slack, email and rehearsal (Phase 3, step 2)
Admins connect Slack and Gmail once on **Integrations** (you sign in on each app's own screen; Deskhand stores only connection ids on the server, never shown or logged). After that every message is addressed to a **person in the directory**:
- **Slack:** a direct message to that person's Slack account. "Match Slack accounts" finds each person's account from their directory email (a batch of 25 per press); a person with no match gets email only, and an admin can also type a Slack user id on the People page.
- **Email:** to the person's directory address, with a link to the request. The request email asking an approver comes from the **requester's own Gmail** and decisions and completion come from the **approver's own Gmail** when they connected one on **Your mailbox** (account menu); otherwise the company mailbox sends. A personal mailbox is used only for messages about that person's own requests, with text Deskhand writes, and if it cannot send, the company mailbox does.
- **What the email looks like:** the chat text stays the shared text for Slack, the in-app list and the log, but each email has its own layout built when it is sent (`app/emailkit.py`): a rehearsal banner when applicable, a headline with the request chip ("Leave #12"), a **"Summary by Deskhand"** sentence, a details table (dates, leave type, reason, working days, balance before and after, warnings and notes highlighted), one button ("Review and decide") with the address printed under it, the requester's original message quoted, and a one-line "why you" ("You are Priya's manager." / "You hold the IT admin role."). Approval requests, reminders and escalations get the rich layout; decisions, completion, tasks and parked notices a short one. The layout data is stored with the email row, so a retry sends the same email; rows from before it existed keep the plain wrapper. viaSocket documents the Gmail `messageBody` as "plain text or HTML" (rendered as formatted content); our own proof is a rehearsal email.
- **The one AI sentence (optional):** when an admin has switched viaSocket AI on, the first email an approver gets may carry one plain sentence drafted from structured facts only (never the original message). It is asked for when the email is sent, at most 3 per sweep, with an 8 second timeout; it is used only if it is plain text of 25 to 240 characters with no link, markup, directive, secret-shaped value or invented number or name, and is then labelled "drafted by AI". Anything else, or any failure, uses a template sentence. The AI budget (`DESKHAND_AI_BUDGET_PER_DAY`) is per day, shared with request understanding.
- **What goes where:** chat replies, policy answers and "request received" stay in the Deskhand chat. Approval needed, reminders, escalations, decisions, completion, tasks and failures go to Slack; the important ones are also emailed (`SLACK_KINDS` and `EMAIL_KINDS` in `app/delivery.py`).
- **Rehearsal first.** Every company starts in rehearsal: all Slack messages and emails go only to one admin, labelled "[Rehearsal] To <person>", so you can see exactly what each person would get. **Go live** (an audited admin action) switches to the real people; nothing written before the switch is sent after it, and **Back to rehearsal** is always available. Admins see a note about the mode and about people with no Slack match.
- **Approve / Reject buttons in Slack** (live mode only, once an admin switches them on): a live approval DM carries buttons. Deskhand builds one viaSocket automation per company from code; each approval starts one run that DMs the buttons and waits for the click, then calls
  `POST /api/hooks/slack-decision` (public, open past the shared password). The call needs a one-time token issued for that very message (only its hash is stored) and the Slack user id Slack reports must belong to a directory person who can act on the request now; Approve decides it,
  Reject sends them to the web page for the mandatory reason, and every repeat, forgery or stale click answers 200 with a status word and changes nothing. If the buttons cannot be started the plain message with the link goes instead; reminders and decisions never have buttons. Verified with a real press on 2026-10-22 (spike S2); an admin checks their own company with "Send me a test approval" on Integrations. Needs `DESKHAND_BASE_URL` to be the public https address.
**Going live on Embarko, in order:** (1) set `DESKHAND_BASE_URL` to the app's public https address and `DESKHAND_SIGNUP_CODES` in Embarko's App setup (admins see a red banner while the address is missing); (2) register the company and invite people on the People page;
(3) on Integrations connect Slack and Gmail and press Match Slack accounts; (4) set managers and role holders on Setup; (5) stay in rehearsal, ask for leave as an employee and look at what the rehearsal admin receives in Slack and email; (6) optionally Switch on Slack buttons and Send me a test approval;
(7) press Go live. Each person may also connect their own Gmail on Your mailbox. A full rehearsal on real accounts (2026-10-22) sent the approval email from the requester's own Gmail and the decision and completion emails from the approver's own Gmail, all labelled "[Rehearsal]" and addressed only to the admin.
Unchanged: approvals never wait for delivery; each row is delivered exactly once (atomic claim, single-flight sweep); a failed delivery is parked with a Retry button and never resent when the outcome is unknown; 150 deliveries per UTC day per company as a backstop; Slack text is escaped and email is HTML-escaped. The Slack DM was verified with a real call on 2026-10-22 (spike S1). `make preflight` prints yes/no per company.

## Notifications and the delivery log (Phase 4)
Everything a person is told is a row in their company's outbox; **Notifications** is the person's own view of it, for everyone including the demo (the badge rides the 4-second clock poll, so it updates by itself).
- **What shows:** everything addressed to the person except the Ask conversation (chat replies, policy answers, "request received", confirmations of their own click). Five categories: *Needs your decision*, *Reminders and escalations*, *About your requests*, *Tasks for you*, *System and admin*
  (`CATEGORIES` in `app/notifications.py`; a test reads the source and fails if the code can write a message kind that is in no category, so a new kind cannot silently vanish). One entry per message, with how its Slack copy and its email copy fared: delivered, sent to the admin (rehearsal), waiting, failed, not sent, or turned off by you.
- **Find things:** All or Unread, category, request type and status, delivered by Slack or email, dates (UTC), and a literal search of subject and text (`%` and `_` are just characters). Newest first, paged with "Show older". On a phone the filters sit behind a Filters button.
- **Read marks:** opening an entry marks it read and goes to the request; opening a request marks what was said about it read; "Mark all read" does the rest. Messages from before this feature start as read.
- **Preferences** (Notifications, Preferences): per category, Slack and email on or off. They apply in **live** mode only (rehearsal still sends everything to the one admin); a copy a person switched off is written as "not sent: turned off in this person's notification settings" and says so in their list. The in-app copy is always kept, and
  "Needs your decision" cannot have both off, so an approval cannot go unseen (the server refuses and says why). Read-only in the demo. No daily digest or quiet hours yet.
- **Delivery log** (admins; Admin hub, linked from Health): what the company tried to send, to whom, who really received it, Slack or email, status, attempts, a redacted error and times. Filters: status chips with totals, channel, person, kind, dates; Retry on failed rows (audited). **Metadata only:** the log never returns a subject or body.
API: `GET /api/notifications`, `POST /api/notifications/read`, `GET/POST /api/notifications/preferences`, `GET /api/delivery/log`; `/api/clock` carries `unread`.

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
- The demo company's leave still runs against a prepared mock HR (real companies use the ledger). Request types cannot be created, only edited (Phase 5).
- Notifications are kept for ever and only paged (no archive); preferences are per category, not per request type; the unread badge counts the signed-in person's own messages only; no push or browser notifications.
- A request is understood while the sender waits (about 25 seconds at worst with AI on). Slack inbound is slow (viaSocket polls every 15 minutes), so asking happens in Deskhand chat.
- SQLite on one process with no backups: fine for a pilot, not for real customers (Phase 6 moves to Postgres with backups).
- The rules classifier scores 100% on data/sample_requests.json because it was tuned while looking at that file. That number is optimistic. Measure on real past requests.
- The direct-Anthropic path (app/classifier.py: classify_anthropic) has never been run against the live API and is unused.

## Next steps
Phase 5 (more catalog actions such as a ticketing tool, new request types and ordered steps, AI-assisted workflow drafting) and Phase 6 (enterprise hardening: Postgres and backups, MFA, SSO). See `docs/AGENT_PLATFORM_PLAN.md`.
