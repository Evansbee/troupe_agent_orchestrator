# Benchmark: "Links", a small self-hosted bookmark manager

Owner: PM. This is a **fixed** brief. Every troupe release builds Links from an empty directory using exactly this
input, so runs can be compared. Don't edit the brief between runs. If it has to change, bump the version and note it.

Version: 1 (2026-09-24)

## The brief (paste this to the new project's PM at kickoff)
> Build "Links", a small self-hosted bookmark manager for one person, maybe a few friends.
> - Save a link (URL, title, optional note, tags). Title auto-filled from the page if possible.
> - List, search (title/url/note/tag) and filter by tag; edit and delete.
> - Web UI that's pleasant on desktop and phone. JSON API for the same operations.
> - Accounts: sign up / log in / log out; each user sees only their own links.
> - SQLite storage, one command to run locally, a test suite that passes.
> - Import/export as JSON.

## Canned answers (so every run gets the same answers)
Use these verbatim when the team asks. Anything not covered: "your call, write down why."
- **Stack?** Your choice; prefer boring and well-known. Python or TypeScript are both fine.
- **Auth?** Email + password, hashed. No OAuth, no email verification.
- **Deploy?** Local only (`one command` to run). No Docker required.
- **Design taste?** Clean, fast, readable. Dark mode welcome, not required.
- **Scale?** Thousands of links per user, a handful of users.
- **Out of scope:** browser extension, sharing between users, full-text page archiving, mobile app.

## Done = all of these pass (scored by QA + the human)
1. A fresh clone runs with one documented command, and `test` passes.
2. Sign up → log in → add 3 links with tags → search finds them → filter by tag → edit → delete → log out.
3. User A can't see or modify user B's links, via the UI or the API (tested).
4. The API covers create/list/search/update/delete. Unauthenticated requests are rejected.
5. Export then import round-trips the data.
6. Title auto-fill works for a normal page and degrades gracefully offline.
7. Usable at phone width.

## Measurements recorded per run (docs/retro-links-vN.md)
Wall-clock time (empty dir → first runnable → done) · human cards + chat minutes + interventions ·
runs, tokens and provider usage % (coordination vs work) · tasks, rejects and merge-gate failures ·
checks 1–7 pass/fail · safety events (should be 0) · "could the human follow it from Pulse alone?" (y/n + notes).

## After Links
Project **B** is something the human actually wants. The PM asks for it when Links is underway.
