# The prompt that connects Claude Code to viaSocket

WHEN to use it: at milestone M1.5 (the viaSocket spike) and again at M8. Not earlier. Until then the simulator
and mocks are the default.

BEFORE you paste it:
1. Open https://flow.viasocket.com/integrations, pick your project, and click "Create embed" if there is none.
   Copy the org ID, project ID and secret into .env as VIASOCKET_ORG_ID, VIASOCKET_PROJECT_ID and
   VIASOCKET_EMBED_SECRET. Never paste the secret into the chat. Check .gitignore covers .env.
2. Make sure the machine can reach flow.viasocket.com, flow-api.viasocket.com and flow.sokt.io.
3. Have a test account ready for each app you want to try (Slack workspace, Gmail, Keka sandbox if you have one).
   Connecting an app needs a person in a browser; Claude Code will ask you to do it.

## Paste this (viaSocket's own prompt, unchanged)
Integrate apps into this product with viaSocket.
Download https://flow.viasocket.com/documentation/skill.md to .claude/skills/viasocket-integrations/SKILL.md and follow it.
Keep it there for next time; download it again if a call in it fails.
My org ID, project ID and secret are at https://flow.viasocket.com/integrations. Ask me for them; they go in .env only.
Build it complete for my use case: decide who supplies each value (my code, my user in a picker, or my assistant per run) and who picks the action, then report each line of the skill's "Finished when" list.

## Then add this Deskhand context (same message)
Read docs/VIASOCKET_INTEGRATION.md first; it holds our decisions. In short:
- Deskhand is a Python/FastAPI app with its own database, so use native code that calls app actions through
  viaSocket over HTTP (there is no Python SDK). Events arrive through subscriptions whose handler only forwards
  the raw event and its stable id to our /ingest endpoint. Our app is the system of record.
- Our code always picks the action. The AI never does. The AI may only supply values (for example leave dates),
  which our code validates.
- The company admin connects Slack, Gmail and the HR tool once. Employees never connect anything.
- Do not call any real app until I have connected a test account. Ask me when you need it.
- Scope of this run: <write "spike M1.5" or "M8">. Follow that section of docs/BUILD_PLAN.md.
- As you prove each item, move it in docs/VIASOCKET_FACTS.md from UNVERIFIED to VERIFIED with a one-line
  summary of the real response (no secrets, no ids).
- Report each line of the skill's "Finished when" list using the report template in
  docs/VIASOCKET_INTEGRATION.md.
