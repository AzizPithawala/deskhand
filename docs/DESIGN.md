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

## The logo
**The idea: a request handed on.** A filled station (the person who asked), a line, and a half-filled station (whoever has it now). The half-filled shape is the product's own "in progress" status symbol, so the logo and the interface speak one language.

**Construction** (64 x 64 grid, in `app/static/icon.svg` and, as theme-aware `<symbol id="logo">`, in the sprite in `index.html`):
- tile: rounded square, radius 15
- filled station: circle at (17, 32), radius 9
- line: x 17 to 37, y 29.75 to 34.25 (4.5 thick), ends hidden under the second station
- half-filled station: ring at (46, 32), radius 7.5, stroke 5, left half filled (`M46 24.5a7.5 7.5 0 0 0 0 15z`)

**Colour.** Tab icon and app icon: tile `#1e4fff`, mark white (a blue tile reads on light and dark tab strips). In the app the tile is `var(--accent)` and the mark `var(--on-accent)`, so it follows the theme: tile `#7c9bff` and mark `#0a0e1a` at night. A test keeps the two drawings identical. `apple-touch-icon.png` is the same mark on a full-bleed square (iOS rounds it); `icon-32.png` is the fallback for browsers that ignore SVG icons.

**Wordmark.** "Deskhand", capital D, Atkinson Hyperlegible Next 700, tracking -0.025em, set as live text next to the mark; in the app header the muted "request desk" follows it. There is no outlined wordmark file.

**Use.** Clear space of a quarter of the tile on every side. Smallest: 16 px in a tab, 24 px in the app (the app uses 28). Do not recolour the mark on its own, add a gradient or shadow, rotate it, or draw the tile without the mark.

**Why heavier than it first looked.** A first version had a 5 px line on a 64 px grid with smaller stations and read like a dumbbell. A thin line and larger stations read as a route; at a true 16 px the line must still be about 1 px, so the final line is 4.5 and the stations 9 and 7.5. Checked at 16 and 32 px, rendered pixel for pixel.

**Tried and dropped.** Direction B, a lowercase "d" built from a half-filled station and a line (busy at 16 px). Direction C, a capital D drawn as a route with one station (a letter, says little about what the product does). A diagonal and an elbow version of the route fill the tile better but changed the character of the approved straight mark. A tile-less blue mark would be about 2.7:1 on a dark tab strip.

**Not in emails.** Mail clients block remote images and strip data: SVG, and an image link would sit behind the shared password, so emails keep the text header "Deskhand".

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
