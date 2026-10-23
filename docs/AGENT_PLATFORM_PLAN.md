# Deskhand Agent Platform: master plan v2 (replaces the earlier enterprise plan)

## Context: what we are really building
The earlier plan drifted toward "an admin portal with Jira automations", a shared mailbox, and a Table as the database. That is not what you described. This plan
goes back to the product you described, then solves the hard problems it raises.

**Deskhand is an agent that handles employee requests end to end, for many companies:**
1. An employee asks in plain words (Slack DM or mention, or chat inside Deskhand): "I need the new monitor plus a type-C connector."
2. The agent **understands** it (type, details) and **finds the right person** from the company's own configuration (who is manager, HR, IT, equipment approver...).
3. It **contacts that person where they are**: a Slack DM with Approve/Reject buttons **and** an email, both with a link to the same request in Deskhand.
4. When the person approves or rejects, the agent **does what the admin defined** for that kind of request (for example apply leave in Keka for that employee), then tells the requestor in Slack **and** by email, sent from the approving manager's own mailbox when they have connected one.
5. Everything is tracked, escalated on time, audited, and never lost or done twice.

Company side: an admin registers the company, configures people, roles, request types and workflows, connects the company's apps, and invites employees, who then sign in.

## Decisions you made (all of them, so nothing is assumed)
| Topic | Decision |
|---|---|
| Tenancy | Multi-tenant SaaS: one app, many companies, strictly isolated |
| Sign-in | Email + password, admin invites employees (invite link first, then email); Google/Microsoft SSO and admin MFA later |
| Database | **Our own database is the engine's source of truth** (SQLite now, Postgres later). The viaSocket Table is **off the critical path**: at most an optional "send my requests and audit to my Table" mirror later. It cannot give atomic updates, duplicate protection or tamper-proof audit |
| Acting for people | One **org-level connection per app** made by the admin (a service account is recommended), plus **optional per-person connections** (for example a manager's own mailbox) |
| Who decides what the agent does | **Admin defines the workflow per request type; the AI helps the admin draft it, and the admin approves it once.** At run time our code executes exactly that. The AI only understands the message and fills validated values. **Rule 9 stays** (the AI never chooses which action runs) |
| Inbound (updated after the spikes) | **Chat inside Deskhand is the main, instant way to ask.** Slack is used for notifications and Approve/Reject buttons (instant). Asking in Slack is an optional slow mode (viaSocket polls every 15 minutes), and we ask the viaSocket team about a faster Slack inbound without blocking. The Email simulator tab goes away. Other chat tools later (Microsoft Teams is not in viaSocket's catalog) |
| Approving | **Slack buttons** (needs a spike) **plus the Deskhand web page as fallback** |
| Emails | **Request email to the approver:** sent from the **requester's own mailbox when they have connected Gmail**, otherwise from the company mailbox with the requester's name and Reply-To set to them. **Completion email to the requestor:** from **the approver's own mailbox when connected**, otherwise the company mailbox |
| Several items in one message | **Split into one request per item** (as today): monitor = Equipment, connector = Accessories, each routed by its own rule |
| First apps | **Slack and Gmail.** No HR tool yet (no Keka test account); Keka or another HR tool is added later through the same action catalog |
| First milestone | **Leave via Slack and Gmail, end to end**, recorded in Deskhand with balances (no HR tool) |
| Leave without an HR tool | Deskhand keeps its own leave record per employee: balance checked on request, deducted exactly once on approval; a connected HR tool later receives the same record |
| Jira | Only an example. It becomes one catalog entry among others, nothing in the design is shaped around it |
| Branch | New branch, then delete the old `enterprise-foundation` (local and GitHub) |

## The hard question: how can the agent act for every person when only the admin connected?
**Answer: the agent does not impersonate people. It acts as the company's integration account, and our code limits what it may do for whom.**
- A viaSocket connection belongs to whoever signed in through the Connect popup, scoped by the id we put in the token (`unique_identifier`). We use **the company id** for org-level connections and **company id + person id** for personal ones.
- Business systems (Keka, Jira, HRIS, ticketing) are built for this: their actions take **the employee as a field**. Keka's "Create Leave Request" takes the employee's email or employee ID, so **one admin-authorised connection can apply leave for any employee**. The admin picks what that account may do (the Connect popup asks only for the scopes of the actions we use).
- **Our code is the permission layer.** Examples: leave is applied only for the requester (rule 4); the target employee comes from the directory and the signed-in or Slack-verified identity, never from message text; an action runs only if the admin put it in that request type's workflow and only after the approvals it requires.
- **Per-person connections only where the app must act as the individual**: sending the completion email from the manager's own mailbox. Each such person connects once from "My connections" (popup). If they have not, we fall back to the company mailbox, so nothing is lost.
- **If an app cannot act for someone else**, that workflow step becomes a manual task ("Done") for the named person, the way software seats work today.
- **Safety:** least-privilege scopes, an admin-visible list of "what the agent can do", a per-action daily cap, a kill switch per workflow and per app, every action audited with the approver and the request, idempotent with read-before-write, retried then parked.

**Worked example, Keka leave (later, when an HR tool is connected):** Slack message → verified Slack user → employee by email → agent understands "Fri and Mon off" → manager (from the directory) gets Slack buttons + email → manager approves → step 1 look up the employee in Keka by email, step 2 map our leave type to Keka's leave type (admin picked once), step 3 check balance, step 4 "does this leave already exist?" (read first), step 5 create the leave, step 6 store the Keka leave id on the request, step 7 notify the employee in Slack and by email from the manager. A failure parks that step and tells the admin; the approval itself is never undone.

**Milestone story without an HR tool (current first milestone):** employee in Slack: "I need Friday and Monday off" → verified Slack user → employee by email → agent understands it and checks the leave balance → the manager from the directory gets a Slack DM with buttons, plus an email sent from the **employee's own mailbox** (company mailbox if they have not connected one) → manager approves or rejects → on approval Deskhand deducts the leave once → the employee gets a Slack message and an email sent from the **manager's own mailbox** (company mailbox if not connected).

## Architecture (target)
- **Tenancy:** an `org_id` on every table, one tenant-scoped data layer, isolation tests on every route.
- **Identity:** the session decides the actor (the API stops trusting `actor_id`). Inbound Slack identity is resolved to an employee by email and the event is authenticated (shared-secret header, dedupe by event id, bot's own messages ignored: the guard already exists).
- **Config as data (per org, versioned, audited):** people and reporting lines, roles and holders (HR, IT, equipment approver, finance, custom), request types, approval rules, workflows, connections. The demo JSON becomes a seed template.
- **Workflow engine:** request type → ordered steps: collect missing details → approval chain (with conditions on validated details such as cost) → actions → completion messages. Steps are data; our code runs them with the existing guard (idempotency key per request+step, backoff, park, replay) and normalised results (rule 10).
- **Action catalog (verified):** each app action is a JSON entry: app, ids, inputs, where each value comes from (admin picker, fixed, validated per run), idempotency method, risk level. An action appears in the admin UI only after a real spike proved it. Keka leave, Slack DM with buttons, Gmail send, and Jira create issue are all just entries.
- **AI-assisted setup:** the admin describes a request type in words; the AI drafts the routing and workflow as validated JSON from the catalog only; the admin reviews and approves it. Runtime is deterministic.
- **Channels (provider interface):** Slack and email first. Inbound classification becomes **asynchronous** (a Slack event must be acknowledged in about 3 seconds; AI takes 5 to 17), so a request is created at once and understood in the background.
- **Notifications:** every message is also stored per person, shown in a **Notifications tab** with filters.
- **Hosting:** SQLite on Embarko for the pilot; storage and worker behind interfaces so the Postgres move is a swap. Embarko has one process and no backups, so the Postgres move is mandatory before real customers.

## Who gets what, when
| Event | Who | Channels |
|---|---|---|
| Request created | requestor | Slack reply, Deskhand |
| Approval needed | approver | Slack DM with buttons + email (from the requester's mailbox when connected, else company mailbox) + web link |
| Reminder / escalation | approver, backup, requestor | Slack + email |
| Approved or rejected (with reason) | requestor | Slack + email from the approver's mailbox (fallback: company mailbox) |
| Action done (for example leave applied) | requestor | Slack + email |
| Step failed or parked | admin | Slack + email + Notifications tab |
Recipients are always people from the company's directory. We never email an address that came from message text.

## Admin experience (sidebar)
Employee: Ask, My requests, Notifications. Approver: + Needs you. Admin: + People & roles, Request types & workflows, **Integrations** (top level, not inside Health), Health, Audit, Settings. First run shows a setup checklist: profile → invite people → roles and managers → connect Slack and Keka → review request types → send a test request. Everyone: My connections (optional personal mailbox).

## Phases (each ships on its own with tests, a demo step, README updated)
| Phase | What | Notes |
|---|---|---|
| 0 | **Branch, then spikes (real calls, no product code)** | See below. Each result recorded in `docs/VIASOCKET_FACTS.md`; a spike that fails changes the plan, it is not worked around |
| 1 | Multi-tenant foundation and accounts: `org_id` everywhere, migrations, registration, login, sessions, invites, access roles, `actor_id` removed, demo mode kept behind a flag | Also the isolation test harness |
| 2 | Company setup: people, roles, request types, approval rules, workflow editor v1, routing simulator, top-level Integrations (connect Slack, Keka, Gmail), onboarding checklist | Rule-based; AI drafting comes in Phase 5 |
| 3 | **Leave via Slack and Gmail, end to end**: inbound Slack and in-app chat, async understanding, leave balance and record in Deskhand, Slack DM with buttons + email from the requester's mailbox, approval, completion messages from the manager's mailbox | The first real demo of the agent. An HR tool (Keka) is a later catalog entry |
| 4 | Notifications tab with unread badge, filters (unread, category, request type, status, date, channel, search), preferences, org delivery log for admins | Can run beside Phase 3 |
| 5 | Generalise: more catalog actions (a ticketing tool such as Jira, equipment manual tasks, HR sync of people and managers), AI-assisted workflow drafting, My connections for personal mailboxes | Each new action needs its own spike |
| 6 | Enterprise hardening: Postgres and backups, background worker, MFA, SSO, SCIM, encryption of stored connection ids, rate limits, observability, optional Table mirror | Before onboarding real companies |

**Phase 0 spikes (test accounts available: a Slack workspace where you are admin, two or more Gmail accounts; no Keka test tenant yet):**
1. Slack inbound: payload of a DM and a mention, a stable event id, acknowledge time.
2. Slack buttons: send an interactive message, receive the button click, who clicked.
3. Personal mailbox (the key new spike): connect two Gmail accounts as two different people (tokens with company id + person id), send an email from each, and check they arrive from the right address and that the connections stay separate.
4. (Deferred until a test account exists) Keka: employee lookup with the manager, leave types and balances, create leave by email, duplicate behaviour, scopes.
5. (Optional, later) viaSocket Table: connect, write and read a row, measure latency and limits.
**Fallbacks:** if Slack button clicks cannot be delivered, link buttons to the web page; if a personal mailbox cannot send, use the company mailbox; when an HR tool is added later and cannot act for another employee, that step becomes a manual HR task.

## Risks and things still unverified
Slack interactive events via viaSocket; Keka manager sync and permissions; personal-mailbox sending; viaSocket AI latency and credits; Embarko limits (one process, no backups); Microsoft Teams unsupported by viaSocket; invite and password-reset emails need a sender before a company connects Gmail (invite link first); this is a long programme, so the phases are the cut lines.

## Rules for CLAUDE.md (additions, nothing relaxed)
Org-level credentials act for people only inside admin-defined workflows and only after required approvals; the target person always comes from the directory or a verified channel identity, never message text; recipients are directory people only; inbound events are authenticated and deduplicated; rule 9 (code picks the action, AI never does) is unchanged.

## First steps after you approve this plan
1. Create the new branch `agent-platform` from the deployed code state (`122aab2`), copy this plan to `docs/AGENT_PLATFORM_PLAN.md`, commit and push.
2. Delete the old `enterprise-foundation` branch locally and on GitHub (you confirmed). It only contains the superseded plan document.
3. Stop and wait. Phase 0 spikes need your Slack and Keka test access, so I will ask before any real call.

## Verification
Per phase: `make test` green including tenant-isolation tests, a browser demo of the step, README updated, no secrets in code, logs or UI. Milestone (end of Phase 3): a newly registered company configures itself and connects Slack and Gmail, an employee asks for leave in Slack, the manager gets Slack buttons and an email from the employee's mailbox, approves, the leave is deducted once in Deskhand's record, and the employee is told in Slack and by an email from the manager's mailbox; a second company registered alongside sees none of it.


## Phase 0 results (2026-10-22) and what they change
- **Slack buttons: confirmed.** Approve and Reject clicks work through a viaSocket automation that waits for the click and returns the clicker's Slack id and the button value.
- **Per-person Gmail: confirmed.** Emails really leave from the requester's and the approver's own mailboxes.
- **Slack inbound: not real time.** viaSocket polls every 15 minutes (2.8 to 13.2 minutes observed), gives no channel id, and the app does not accept DMs. "Ask in Slack" as the main way to raise a request cannot feel instant with this route.
- **Gmail connect for outside accounts** can be blocked until viaSocket's Google app is verified.

**Decision after the spikes:** employees ask in Deskhand chat; Slack stays for notifications and button approvals; Slack-as-the-ask-channel is optional slow mode. Phase 3's milestone story therefore reads: employee asks in Deskhand, the manager gets a Slack DM with buttons and an email from the employee's mailbox, approves in Slack, and the employee gets a Slack message and an email from the manager's mailbox.

## Status
- **Phase 0 (spikes):** done. Results above and in `docs/VIASOCKET_FACTS.md`.
- **Phase 1 (companies, accounts, sign-in):** done; PR #1.
- **Phase 2 (company setup):** built on branch `phase-2-company-setup`. Business roles with one holder each, editable rules for the 8 built-in request types (a primary approver plus up to two conditional extra approvers),
  company policies, a routing test, a computed checklist, and a top-level Integrations page. Deliberate scope choices: no new request types (Phase 5), Keka skipped (no test tenant), and the "workflow editor"
  is this per-type rule editor; ordered free-form steps and AI-drafted workflows stay in Phase 5.
- **Phase 3 (leave through Slack and Gmail):** built on branch `phase-3-leave-slack-gmail`. A Deskhand-owned append-only leave ledger (simple yearly allowance, Monday to Friday, half days, a low balance warns the approver); messages go to people (Slack DM by Slack id, email to the directory address) from the requester's or the approver's own mailbox when connected;
  every company starts in rehearsal (all messages to one admin) until an admin goes live; Approve and Reject buttons in Slack through a per-company viaSocket automation and a one-time-token callback. Spikes S1 (Slack DM), S2 (a real button press reaches Deskhand) and S3 (interactive DM) passed on the test workspace (docs/VIASOCKET_FACTS.md).
  Not done: Slack asking (slow, later), cancelling an applied leave, accrual, holidays, carry-forward, a nudge on the Ask page to connect a mailbox. Needs on Embarko: `DESKHAND_BASE_URL` (public https address) and a signup code.
- **Phase 4 (notifications):** built on branch `phase-4-notifications`. A Notifications page for everyone with an unread badge, filters (unread, category, request type and status, delivered by, dates, search) and paging; per-category Slack and email preferences that never hide an approval (applied in live mode, ignored in rehearsal);
  the outbox now records who a message was really sent to and when it was read; an admin Delivery log, metadata only, with totals, filters and Retry. Decisions: everything addressed to the person except Ask replies; per-category Slack/email choices; admin log without message text. Not done: daily digest and quiet hours, archive, per-request-type preferences, push.
- **Emails and dates (branch `email-layout-and-dates`):** every emailed kind has its own HTML layout built from a stored payload (`app/emailkit.py`); an optional, checked one-sentence AI summary for approval emails (template fallback, per-day shared AI budget, which now really restarts each day); a weekday that is today means today, dates are always in date order, and the approver is told when a leave includes today or has past dates. The suite no longer depends on the day it runs (it pins "today"; `DESKHAND_TEST_TODAY` overrides it).
- **Next:** Phase 5, generalise: more catalog actions (each needs its own spike), new request types and ordered steps, AI-assisted workflow drafting (AI only drafts, code picks the action).
