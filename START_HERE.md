# How to run the prototype and give Claude Code the whole context

FIRST, to just see it working: follow the "Run it" steps in README.md (about 3 minutes), open http://localhost:8000, and
follow the 7-minute script in docs/BUILD_PLAN.md. The rest of this file is for continuing the build with Claude Code.

Claude Code starts every session with a blank context window. It automatically reads CLAUDE.md in the project
root, and treats it as guidance rather than enforced rules, so every rule that must hold is also written as a
test. The detailed specs live in docs/ and data/, and the first prompt tells Claude Code to read them.

## Steps
1. Create an empty folder for the project and copy everything from this pack into it (CLAUDE.md, docs/, data/,
   .env.example).
2. Run `git init` in that folder, then start Claude Code from inside the folder.
3. Paste the "Start" prompt from the bottom of docs/BUILD_PLAN.md. It asks Claude Code to read everything, restate
   the plan, list its assumptions and wait for your approval before writing code.
4. Approve, then work one milestone at a time (M0 to M9) with the per-milestone prompts in docs/BUILD_PLAN.md.
5. When you reach M1.5, follow docs/VIASOCKET_PROMPT.md: put your viaSocket org ID, project ID and secret in .env, then
   paste the prompt. Claude Code downloads viaSocket's skill into .claude/skills/viasocket-integrations/ itself.
6. Keep CLAUDE.md short. If a rule keeps being forgotten, add a test for it.
7. Put your real Anthropic API key in .env only. The app also works without a key (fallback classifier).

## What is in the pack
- CLAUDE.md: standing instructions and the non-negotiable rules.
- docs/PRD.md: scope, user stories and acceptance criteria.
- docs/ARCHITECTURE.md: state machine, data model, guard design, security rules.
- docs/VIASOCKET_FACTS.md: what is verified, and what must be mocked.
- docs/VIASOCKET_INTEGRATION.md: how Deskhand uses viaSocket (decisions, traps, spike questions, report template).
- docs/VIASOCKET_PROMPT.md: the exact prompt to paste, with Deskhand context.
- docs/BUILD_PLAN.md: milestones, the 7-minute demo script and the prompts.
- data/: demo directory, routing map, 38 synthetic test requests, viaSocket catalog snapshot.
