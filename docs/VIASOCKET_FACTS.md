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
- Connect popup, enable and the Slack channel picker worked for a signed-in test account (connections list showed the connection, enable returned a runner).
- Step 2a, Slack Send Message (action_version rowj2u3wc8h5) to the admin-picked channel: HTTP 200, body is Slack's own reply
  `{ok:true, channel, ts, message:{...}, warning:"missing_charset"}` with no viaSocket envelope. Success needs `ok:true` and a `ts`.
  The reply echoes our runner id (script_id) inside `message.metadata.event_payload`, so raw Slack replies must never be logged, shown or
  returned. The adapter now returns only `ts` and `channel`, and every result is deep-scrubbed. The message arrived under the bot name "Deskhand".
- Step 2b, Slack Get User Information (rowrubgppa5r, search_by email_search): a workspace member answers HTTP 200 with the profile fields
  (`id`, `name`, `real_name`, as requested); an unknown address answers HTTP 200 `{"email":"<asked>","message":"User not found"}` in about 1 s.
  Both are HTTP 200, so "not found" is read from the body and treated as a clean answer (found:false), not an error. One earlier
  call for the unknown address ended in a 25 s read timeout and was correctly classed retryable; the next two calls answered in about a second.
- Step 3, Gmail Send Email (rowwj0sfmhub) with an optional From mailbox chosen on the Connections tab: HTTP 200 envelope
  `{success:true, data:{id, threadId}}`; success requires `data.id`. The test message reached the recipient's Gmail (proved by the API answer only; delivery was not checked from the recipient's side).
- viaSocket's check script (`make check-viasocket`) passes: 0 FAIL; catalog ids match; the embed token is accepted; connect.js loads. It flags
  API hosts written as literals in code, so the hosts now come from .env (VIASOCKET_API_URL, VIASOCKET_RUN_URL), as in viaSocket's examples.
UNVERIFIED
- Whether viaSocket retries or dedupes an action call (nothing in the responses shows either); whether the Connect popup's Reconnect
  (a fresh connection that replaces the stored one) leaves the old runner active; Keka (not in this phase); events (phase 2).

## OUTBOUND DELIVERY (live-only; built 2026-10-21, tested with the fake transport only)
Design facts (ours, not viaSocket's):
- No switch, no mock. Delivery starts by itself once Slack and its channel are set (chat rows) and Gmail and the notification email are set
  (email rows); until then rows are marked skipped, nothing is replayed later, and a red banner on every tab names what is missing.
- Delivery is a sweep over outbox rows, never part of approving or rejecting. A down Slack or Gmail cannot block or undo a decision.
- Every notification kind is delivered, except the system notice "delivery_failed" (otherwise a dead channel would create endless notices).
- Exactly once per outbox idempotency_key: atomic claim (UPDATE ... WHERE delivery_status='pending', rows affected must be 1), a single-flight
  sweep, an action_runs record of success, and a retry only when the call certainly did not take effect (429, ratelimited, could not connect).
  A read timeout or a 5xx may already have been delivered, so it is parked as "outcome unknown" and an admin retries by hand after checking.
- viaSocket gives no idempotency key and no dedupe for action calls (UNVERIFIED either way), so these rules are ours.
- Backstop cap 150 deliveries per UTC day, counted per real send attempt in a ledger that survives Reset demo; at the cap new rows are skipped and
  a banner shows; it resumes the next UTC day by itself.
- Slack text is escaped (&, <, >) so message text cannot ping the channel or inject links; email bodies are HTML-escaped. Email goes only to the one
  stored, validated notification address (never to directory addresses). The address is configuration: it is not written to the audit log.
- The earlier toggle, DESKHAND_LIVE_DELIVERY and DESKHAND_LIVE_EMAIL_OVERRIDE were removed.
UNVERIFIED
- A real end-to-end run on the hosted instance: an approval message in Slack with the prefix and inbox link, one email to the notification address.
- How Slack renders the escaped text and the link in the real channel.
