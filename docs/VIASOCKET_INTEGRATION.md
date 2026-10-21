# viaSocket integration decisions for Deskhand

Source: viaSocket's official skill (version 2026-10-16) and its developer docs, read on 2026-10-21. Everything here
is DOCUMENTED but NOT YET TESTED by us. The spike (M1.5) turns documented claims into verified facts. If the
downloaded skill disagrees with this file, the skill and the app's own document win; update this file.

## How the pieces fit
- Claude Code downloads the skill to .claude/skills/viasocket-integrations/SKILL.md (see docs/VIASOCKET_PROMPT.md).
  A skill loads only when needed, so it costs little context. Keep it; re-download if a call fails; it changes often.
- Hosts: flow.viasocket.com (documents), flow-api.viasocket.com (API, takes the embed token),
  flow.sokt.io (app search and action runner, takes no token; the script_id in the URL is the credential).
- No viaSocket login is involved. Never ask for or look for a login token.
- Do NOT use the viaSocket Developer Hub plug-building skills for this work. They build plugs, not integrations
  from an app, and some embed dashboard session tokens that must never be copied into a repository.

## Tenant model
- unique_identifier = our company (tenant) id, for example "acme-demo". It is stable forever and is never an email.
- The company admin connects Slack, Gmail and the HR tool once, through viaSocket's Connect popup
  (openViasocketConnection from https://flow.viasocket.com/connect.js). Employees never connect anything.
- Store per tenant and app: service_id, auth_id, script_id. These are secrets (script_id acts like a password).
- Admin page "Connections": a Connect button with the app's icon (iconurl), status, and Reconnect when a connection
  shows isExpired. This is the only browser-side viaSocket code.
- Embed token: HS256 JWT with claims org_id, project_id, unique_identifier, signed with VIASOCKET_EMBED_SECRET,
  sent as the bare header `authorization: <token>` (no Bearer). Python: use PyJWT or hand-sign; there is no Python SDK.
- While building, the skill signs a test token for the user "apps-api-test-user" and connects test apps at
  https://flow.viasocket.com/integrations/<org_id>/<project_id>/apps-api. Those ids never go into code or config.

## Who supplies each value, who picks each action
Actions are always picked by our code (Rule 9). Value sources use the skill's three categories:

| Action (adapter name) | viaSocket app and action | Values from our code | Chosen once by the admin in a picker | Filled per run by the AI, then validated |
|---|---|---|---|---|
| chat.send_interactive | Slack: Send Interactive Message | recipient (from routing map and directory), button ids and tokens | approval channel if one is used | message text (length limited, no secrets) |
| chat.reply_thread | Slack: Send Message as Thread | channel id, thread id from the original event | none | reply text |
| chat.lookup_user | Slack: Get User Information | email from the directory | none | none |
| mail.reply / mail.send | Gmail: Reply To Thread / Send Email | recipients from directory, thread id | sender mailbox | subject and body |
| hr.create_leave_request | HR tool: Create Leave Request | requester's email or employee id from the directory (requester only), request id | leave type mapping (from list-options) | dates, half day, reason, validated against balances and leave types |
| hr.list_leave_balances | HR tool: List All Leave Balances | employee id | none | none |
| hr.find_employee | HR tool: Find Employees | email | which fields to return | none |
| hr.create_helpdesk_ticket | HR tool: Create Helpdesk Ticket | reporter id, priority from the routing map | category and assignee mapping (from list-options) | title and description |

Rule 3 of the skill applies: every field of every action used is either shown to the admin or fixed in code with a reason, and
every `<...>` placeholder in a sample is decided and reported. Copy field keys from the app's document; never type them.

## Events (receiving messages)
Preferred: a subscription with a handler. The handler runs on viaSocket per event. It must only forward the raw event and
a stable event id to POST /ingest/viasocket with a shared-secret header (DESKHAND_INGEST_SECRET). All logic, dedupe and state
stay in Deskhand.
- Slack triggers to use: New Mention, New Thread Message (avoid "New Message on Slack": it fires on everything and invites loops).
- Gmail trigger: New Email Arrives, filtered by a label.
- Per the skill, first fetch one real event for the connection, then write the handler from that real payload. A key named
  viasocket_help in the sample is a note, not a field.
- The handler calls our API, so our endpoint must be reachable from the internet. For local demos use a tunnel (for example
  ngrok or cloudflared). UNVERIFIED: whether viaSocket can reach a local tunnel reliably.
- Fallback with no public endpoint: poll through actions on our scheduler (Gmail: Search Email Messages; Slack: Get Messages).
  Implement both behind the ChannelAdapter interface.

## Result handling: the traps (documented)
- A refused action call still answers HTTP 200 with "status": 400 and the reason in data.message.
- list-options for an unreadable connection answers 200 with data.response.status 400. Treat as an error.
- A run answers HTTP 200 by design, so an app's own 429 or 500 is not mistaken for viaSocket's. The body and run history
  say whether steps worked.
- Inside an automation, a failed step does not stop the run: its result becomes {"error": {}} and the next step runs.
- Therefore the action adapter must parse the body and return one of: success, retryable_failure (timeouts, 429, 5xx),
  permanent_failure (validation, auth, not found). The guard retries only retryable_failure.
- Success responses carry the app's own JSON (for example Slack's message ts). Store ids you need later.

## Reliability: what the documents do NOT promise
- The skill describes no retries, no dedupe and no idempotency keys for actions, handlers or steps. Older marketing pages
  claim automatic retries and de-duplication for event subscriptions. Treat both as UNVERIFIED and test them in the spike.
- Until proven otherwise, Deskhand's own guard is the only protection: dedupe inbound events, idempotent action_runs,
  retry with backoff, park and replay.
- Read-before-write idempotency: before creating a leave request, call List User Leave Requests for the same dates; before
  creating a helpdesk ticket, search by our request id placed in the title. Slack: store the message ts and never post twice
  for the same idempotency key.

## AI options (behind one AIProvider interface)
1. Anthropic SDK directly (default, set by DESKHAND_MODEL).
2. viaSocket AI: POST /embed/ai/message with response_type "json_schema", a model chosen per call (service and model sent on
   every call), thread_id per conversation. Backend only. It spends viaSocket credits; cost per call is UNVERIFIED.
3. Deterministic fallback (always available).
The AI never chooses actions or approvers whichever provider is used.

## Spike M1.5: questions to answer with real calls
Record each answer in docs/VIASOCKET_FACTS.md (no secrets, no ids).
1. Does the embed token authenticate? (GET /embed/authentications returns 200 with a list.)
2. Slack: can we run Send Interactive Message and Get User Information (by email)? What is in the real response?
3. Slack handler: what is in context.req.body for a New Mention? Is there a unique event or message id? Does the same
   Slack message ever arrive twice, and does viaSocket suppress it?
4. Can the handler reach our /ingest endpoint through a tunnel? Round-trip time?
5. Gmail: payload of New Email Arrives (Message-ID present?), and does Reply To Thread keep the thread?
6. HR tool (if a sandbox exists): does Find Employees return the reporting manager? What does Create Leave Request return, and what
   does a duplicate create do?
7. What does a failing action look like (wrong id, no permission)? Does the body always reveal failure? Is any retry visible?
8. Latency of one action call. Rate limits seen.
9. Anything that blocks the plan: report it, do not work around it silently.

## Report template ("Finished when", one line each)
- Who connects:
- Where each value comes from (one row per field, with its source):
- Who picks the action:
- Where it runs (actions, events, automations):
- Proof: output of `curl -fsSL https://flow.viasocket.com/viasocket-check.mjs -o viasocket-check.mjs && node viasocket-check.mjs src`
  (needs Node; keep it as a test), one real call's response summary, and whether the Connect popup was seen working.
