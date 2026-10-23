# Design notes: the "Route map"

Written for whoever changes the look next. It records the idea, the tokens, what was tried and what was dropped, so a later pass does not start from nothing.

## The idea
Deskhand is a request desk. The three things every screen must make obvious are **who has it, what happens next, and how long it has waited.** The product's own vernacular is a request travelling between people, so the design is one drawing used everywhere: a **route**.
- **Stations** are stages. They are the status shapes (done = filled check, in progress = half, upcoming = dashed ring, rejected = square, parked = diamond, cancelled = slashed), so status is always shape plus word, never colour alone.
- **Lines** join stations. Solid means travelled, dashed means still ahead. A request's line has a colour by type (time off blue, software and access teal, equipment magenta, help and questions slate). The colour is stroke only, never a fill, and the type's name is always written.
- The same drawing appears in the side menu (a line with a station per page and a filled "you are here"; slate for the admin pages), in the bottom bar on a phone, in board rows (a small route), in a request's ticket (the full route), in the Admin line, and in the landing hero (the example request drawn left to right, once).

Spend boldness in one place: everything else is flat and quiet (hairlines only where they group, radii by hierarchy, no identical-shadow card grids).

## Tokens (app/static/app.css, top of file)
- **Day service** (primary): page `#eef1f6`, surfaces `#ffffff`, ink `#0d1321`, muted `#4b5870`, line blue `#1e4fff`.
- **Night service**: page `#0a0e1a`, surfaces `#111729`, ink `#e8ecf8`, muted `#9aa6c4`, line blue `#7c9bff`.
- Other lines: teal `#0a8f8f`, magenta `#c0267a`, slate `#56637e` (lightened at night). Status colours keep their meanings (amber pending, green approved, red rejected, violet parked) and appear only on stations and their words.
- Checked by script: text 4.5:1 or better in both themes, field borders and every line colour 3:1 or better.
- **Type:** one family, Atkinson Hyperlegible Next Variable (SIL OFL 1.1, self-hosted, Latin and Latin-extended files). Body 16/26, scale 13 / 14 / 16 / 18 / 22 / 28 / 36 / 56 / 72, tight tracking on large sizes, tabular figures for waiting times. Its zero is slashed on purpose (legibility); it was left as is.
- Space, radius (controls 8, panels 12), elevation and motion tokens sit next to the colours. Token names that the page script uses inline (`--bg --surface --ink --muted --rule --accent --pending --approved --rejected --parked ...`) must not be renamed.

## What was tried and dropped
- A different line colour for every request type (a rainbow): reads like a children's map and fights the status colours. Four lines only.
- A transit-map menu with named interchanges: too cute. A plain line with dots and one "you are here".
- Repeating the horizontal route in "How it works" under the hero route: decorative repetition. That section is now a numbered, type-led sequence (it is a real sequence), and the hero carries the route.
- Six identical cards for the admin hub: the pages are places on one route, so they are drawn as the Admin line.
- Generic tells removed: middle-dot strings in the interface, "→" on links and buttons, tracked-out all-caps labels, gradient washes, monospace data labels. ("casual 12 → 11" is data produced by the server and stays.)

## Rules for the next pass
- Presentation only unless the task says otherwise: markup, class names and wording in the page script; never an API call, state, handler or permission.
- Keep the `/* ---------- ... ---------- */` section comments in `app.js` (tests slice the script on them) and every id or `data-` attribute a handler or test uses.
- Check any new colour pair with the contrast script before using it, and look at every page at 375, 768, 1024 and 1440 in both themes.
- Atkinson is wider than most sans faces: tables and filters need re-checking at 1024.
