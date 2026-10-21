# viaSocket facts: what is verified and what is not

Rule for Claude Code: only rely on items marked VERIFIED. Everything else is UNVERIFIED and must be behind an
adapter interface with a mock. If you need an UNVERIFIED fact, ask the owner or list it as a question.
Public pages change (example: Keka showed a "Deactivate Employee" action on older cached pages but not on
live pages on 2026-10-21). Treat the catalog snapshot as dated 2026-10-21.

## VERIFIED
Platform behaviour
- Free plan: 10,000 tasks and 500 AI credits per month. A task is counted for every step that runs, including
  the trigger. Credits are spent on premium actions such as WhatsApp, SMS and AI steps. (public pricing page)
- Run logs record the input and output of each step. (public docs) Consequence: never pass secrets through
  a step.
- Error handling: a failed step and the steps that depend on it are skipped; independent steps continue.
  (public docs)
- Triggers are instant where the app supports it; scheduled triggers run at most every 15 minutes. (public pages)
- viaSocket does NOT retry failed steps, and a redelivered webhook starts a second run (no dedupe).
  (confirmed by the project owner from direct experience, not in public docs)
- Flows can be started from outside and their results read back automatically. (confirmed by the project owner)
- Built-in steps exist for: AI Agent, workspace memory, Human Intervention (via Slack and via email), Delay,
  JavaScript, HTTP request. Wait limits of Human Intervention and Delay are UNVERIFIED.
- Plug Builder lets you create custom actions and triggers. (public docs)

Catalog (public integration pages, 2026-10-21). Full lists are in data/viasocket_catalog_snapshot.json.
- Slack triggers: New Message in Channels, New Mention, New Thread Message, New Message on Slack (all channels
  and DMs, no filter), others. Actions include Send Message, Send Message as Thread, Send Interactive Message
  (buttons or forms for approvals), Send Direct Message, Get User Information (lookup by email or name),
  and a modal action.
- Gmail: trigger New Email Arrives (filter by label, sender, subject; include or ignore replies). Actions:
  Send Email, Reply To Thread, Search Email Messages, Get Gmail Message by ID, and more.
- Keka (91 actions): Create Leave Request (by email or employee ID, dates, sessions, leave type, reason), List All
  Leave Balances, List User Leave Requests, List leave types, Find Employees (selectable fields), Create
  Helpdesk Ticket, List Helpdesk Tickets, List Helpdesk Categories, Assign Asset to Employees, List Assets,
  List All Policies, departments, groups, locations. Only one trigger: Employee Clocks In.
  There is no exit/termination trigger.
- Channels with both receive and reply: Slack, Gmail, Outlook, Telegram, WhatsApp via Wati, Twilio, Google Chat,
  Zoho Cliq, Mattermost, Discord, Instagram DMs. Send-only: official WhatsApp, Gupshup. Microsoft Teams was not
  found in the public catalog.
- Admin and destructive actions are rare. Of 63 popular apps audited, only 4 can remove or suspend a person
  (Google Workspace Admin: Suspend User; GitHub: Remove Member from Organization; LastPass: Delete User;
  WordPress: Delete an User). Slack, Zoom, Notion, HubSpot and Salesforce have none.

External facts that affect design
- Slack retries event delivery up to three times if it does not receive a 2xx response within 3 seconds, so
  the same event can arrive four times. A bot's own post is a message event and can retrigger it.
- Keka has its own Slack integration (/apply-leave, balances, clock-in), so leave in chat alone is not a
  differentiator.

## DOCUMENTED but not yet tested (viaSocket skill 2026-10-16 and developer docs, read 2026-10-21)
- Embed token: HS256 JWT with org_id, project_id, unique_identifier; header `authorization: <token>`, no Bearer, no exp.
- Actions run with POST https://flow.sokt.io/func/<script_id> and body { action_version_id, inputData }; the script_id is the
  credential. Connections: browser popup via openViasocketConnection. Enable: POST /embed/enable/<service_id>/<auth_id>.
- Events: subscription with a handler that runs on viaSocket; the handler can call our own API with our auth header.
- Refused calls and unreadable connections still answer HTTP 200 with the failure in the body.
- viaSocket also offers: automations from code, an AI endpoint with a model chosen per call (including Claude), a built-in
  Run AI Model action, and a prebuilt UI. No Python SDK; the optional viasocket-apps package needs Node 20+.
Full detail and the spike questions: docs/VIASOCKET_INTEGRATION.md.

## UNVERIFIED (treat as questions)
- Whether Keka "Find Employees" returns the reporting manager.
- Whether viaSocket acknowledges Slack events within 3 seconds, and whether trigger outputs expose a unique
  event or message ID usable as a dedupe key.
- Max wait of Human Intervention and Delay steps.
- (Now DOCUMENTED, see above, but not yet tested) How external code calls viaSocket actions.
- viaSocket Table limits and API.
- Whether MSG91's WhatsApp trigger delivers real inbound messages (only delivery-report triggers were seen).
- Credit cost of one AI Agent step.
- Microsoft Teams support through a generic webhook.

## Competitor and price snapshot (for context only, Oct 2026)
ClearFeed (Bangalore): from $24 per agent per month or $40 per month usage-based. Unthread: $50 per agent per
month, 5-seat minimum. Suptask: from $15 per agent. Moveworks: roughly $100 to $200 per employee per year.
A flat-rate leave bot: $10 per month. Approvals, SLAs, reminders and dashboards are standard in this category.

## SPIKE M1.5 RESULTS (real calls, 2026-10-21; no ids or secrets recorded)
VERIFIED
- Step 1, token auth: an HS256 embed token (claims org_id, project_id, unique_identifier, no exp) sent as the bare `authorization`
  header on `GET flow-api.viasocket.com/embed/authentications` answers HTTP 200 `{"success":true,"data":[]}` (empty until a connection exists).
  A garbage token answers 401; no token answers 498.
- The API edge answers HTTP 403 to Python's default `urllib` User-Agent with the same valid token. The adapter sends its own User-Agent.
- App search `GET flow.sokt.io/func/scri12BSufQM?key=<name>` answers `{success, message, data:[...]}`, not a bare array as the skill text shows.
  Slack and Gmail each have one clear match (Slack's first hit is a lookalike named "slackkkk": pick by exact name, `Slack`).
- Documents `documentation/<service_id>.md?format=http&org=..&project=..` download without sign-in for Slack and Gmail.
UNVERIFIED (next, needs a person to sign in on the Connections tab)
- Slack Get User Information and Send Message real responses; Gmail Send Email real response; enable/script_id behaviour; picker shapes.
