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
| Inbound | Slack DM or mention, chat inside Deskhand. The Email simulator tab goes away. Other chat tools later (Microsoft Teams is not in viaSocket's catalog and is not a first customer need) |
| Approving | **Slack buttons** (needs a spike) **plus the Deskhand web page as fallback** |
| Emails | Approver's request email from the **company mailbox**; completion email to the requestor from **the approver's own mailbox when connected, company mailbox otherwise** |
| Several items in one message | **Split into one request per item** (as today): monitor = Equipment, connector = Accessories, each routed by its own rule |
| First apps | Slack, and HR tools (Keka first) |
| First milestone | **Leave in Keka via Slack**, end to end |
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

**Worked example, Keka leave:** Slack message → verified Slack user → employee by email → agent understands "Fri and Mon off" → manager (from the directory) gets Slack buttons + email → manager approves → step 1 look up the employee in Keka by email, step 2 map our leave type to Keka's leave type (admin picked once), step 3 check balance, step 4 "does this leave already exist?" (read first), step 5 create the leave, step 6 store the Keka leave id on the request, step 7 notify the employee in Slack and by email from the manager. A failure parks that step and tells the admin; the approval itself is never undone.

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
| Approval needed | approver | Slack DM with buttons + email (company mailbox) + web link |
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
| 3 | **Leave in Keka via Slack, end to end**: inbound Slack and in-app chat, async understanding, Slack DM with buttons + email, approval, Keka action, completion messages from the manager's mailbox | The first real demo of the agent |
| 4 | Notifications tab with unread badge, filters (unread, category, request type, status, date, channel, search), preferences, org delivery log for admins | Can run beside Phase 3 |
| 5 | Generalise: more catalog actions (a ticketing tool such as Jira, equipment manual tasks, HR sync of people and managers), AI-assisted workflow drafting, My connections for personal mailboxes | Each new action needs its own spike |
| 6 | Enterprise hardening: Postgres and backups, background worker, MFA, SSO, SCIM, encryption of stored connection ids, rate limits, observability, optional Table mirror | Before onboarding real companies |

**Phase 0 spikes (need test accounts from you: a Slack workspace where the viaSocket app can be installed with interactivity, a Keka test tenant):**
1. Slack inbound: payload of a DM and a mention, a stable event id, acknowledge time.
2. Slack buttons: send an interactive message, receive the button click, who clicked.
3. Keka: does "Find Employees" return the reporting manager; list leave types and balances; create leave by email; what a duplicate create does; what scope the connection needs.
4. Personal mailbox: connect Gmail as a second person (token with company id + person id), send from it, and see it in the connections list.
5. (Optional, later) viaSocket Table: connect, write and read a row, measure latency and limits.
**Fallbacks:** if Slack button clicks cannot be delivered, link buttons to the web page; if Keka cannot create leave for another employee, the step becomes a manual HR task; if a personal mailbox cannot send, use the company mailbox.

## Risks and things still unverified
Slack interactive events via viaSocket; Keka manager sync and permissions; personal-mailbox sending; viaSocket AI latency and credits; Embarko limits (one process, no backups); Microsoft Teams unsupported by viaSocket; invite and password-reset emails need a sender before a company connects Gmail (invite link first); this is a long programme, so the phases are the cut lines.

## Rules for CLAUDE.md (additions, nothing relaxed)
Org-level credentials act for people only inside admin-defined workflows and only after required approvals; the target person always comes from the directory or a verified channel identity, never message text; recipients are directory people only; inbound events are authenticated and deduplicated; rule 9 (code picks the action, AI never does) is unchanged.

## First steps after you approve this plan
1. Create the new branch `agent-platform` from the deployed code state (`122aab2`), copy this plan to `docs/AGENT_PLATFORM_PLAN.md`, commit and push.
2. Delete the old `enterprise-foundation` branch locally and on GitHub (you confirmed). It only contains the superseded plan document.
3. Stop and wait. Phase 0 spikes need your Slack and Keka test access, so I will ask before any real call.

## Verification
Per phase: `make test` green including tenant-isolation tests, a browser demo of the step, README updated, no secrets in code, logs or UI. Milestone (end of Phase 3): a newly registered company configures itself, connects Slack and Keka, an employee asks for leave in Slack, the manager gets Slack buttons and an email, approves, leave appears in Keka for that employee exactly once, and the employee is told in Slack and by email from the manager; a second company registered alongside sees none of it.
