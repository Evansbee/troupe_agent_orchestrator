# Mac app — the native SwiftUI client

The desktop GUI moves from raylib/Python to a native macOS app. The GUI requirements in `specs/40-gui.md`
(REQ-GUI-0xx) stay the behavioral source of truth; this file says how the Mac app meets them, adds
native-only requirements, and sets milestones. The app is a pure client of the engine local API (the API,
`specs/50-api.md`): it never opens `.troupe/troupe.db` or writes the `commands` table directly. The raylib GUI
(`src/troupe/gui/`) stays the daily GUI until the Mac app reaches parity (REQ-MAC-070).
Code: `mac/` (SwiftPM package; macOS 27+, Swift 6.4, SwiftUI, Metal / Canvas for Pulse and Stage).
Status legend: **[x]** implemented · **[ ]** not yet · **[~]** partial.

## Package & build
- **REQ-MAC-001 [ ]** `mac/Package.swift` defines three targets: `TroupeApp` (executable, SwiftUI views),
  `TroupeKit` (library: API client, Codable models, per-project stores, the Pulse/Stage scene model, `Theme`), and
  `TroupeKitTests` (test target).
  - From a clean checkout, `cd mac && swift build` and `swift test` pass with no Xcode project, no generated files
    and no network access beyond SwiftPM itself. `swift run TroupeApp` opens the window.
  - Swift 6 strict concurrency is on (`swiftLanguageModes: [.v6]`) and the build has no concurrency warnings.
- **REQ-MAC-002 [ ]** Bundle and launch.
  - `mac/scripts/bundle.sh` builds release and writes `mac/build/troupe.app`: `Info.plist` (bundle id
    `dev.troupe.app`, `LSMinimumSystemVersion` 27.0), the AppIcon from `src/troupe/assets/icon/troupe.icns` (task
    #34), and the fonts. It ad-hoc signs the bundle (`codesign -s -`). `bundle.sh --install` copies it to
    `~/Applications/troupe.app`.
  - **Decision:** a new `troupe app` CLI command opens the Mac app (`open -a`) on the current directory's project,
    registering it first (REQ-ENG-008). It looks in `~/Applications`, then `/Applications`. If the app is not
    installed, it prints the `bundle.sh --install` command and exits 1. `troupe up`, `troupe gui` and
    `troupe watch` keep opening raylib until M2 (REQ-MAC-070).
  - The app finds the `troupe` CLI (for Start team and Add project) at `~/.local/bin/troupe`, then through
    `command -v troupe` in the user's login shell, because an app launched from Finder doesn't inherit the shell's
    PATH. If it can't find it, the related actions are disabled with a tooltip saying why.
- **REQ-MAC-003 [ ]** No third-party Swift packages unless the task summary gives a reason. Inter and JetBrains
  Mono (OFL) come from `src/troupe/assets/fonts/` as package resources, with no copies checked in under `mac/`.

## API client (TroupeKit)
- **REQ-MAC-010 [ ]** One connection per project in `~/.troupe/projects.json`, to `<path>/.troupe/api.sock`.
  - On connect: `hello`. The app checks the returned `api_version` against the range it supports. On a mismatch,
    that project shows "Engine API vN; app supports vM–vK: update troupe" in its engine pill and rail tooltip.
    It shows no data and sends no commands for that project, and nothing crashes.
  - Then `snapshot`, then `subscribe` from the snapshot's seq. Projects added to or removed from the registry file
    attach or detach without a relaunch. The app watches the file and does not poll it.
  - A project with no socket, or a refused connection, is **offline** (REQ-GUI-029). The app watches `.troupe/` so
    the project attaches within 2 s of its service starting.
- **REQ-MAC-011 [ ]** Event stream: the client stores the `epoch` and last applied `seq` for each project. After
  a reconnect it sends `hello` again, and if the epoch is unchanged it subscribes from that seq. A new epoch (the
  engine restarted or reloaded) is handled like `resync_required`. On `resync_required` it fetches a fresh snapshot and swaps it in as a single update,
  so the UI never shows an empty state in between. Events already applied (seq ≤ last) are ignored.
- **REQ-MAC-012 [ ]** Reconnect: when a service restarts, reloads (REQ-ENG-009) or crashes (REQ-ENG-042), the
  client retries with backoff (0.5 s, doubling, capped at 5 s, indefinitely). Until it reconnects, the project
  keeps its last data on screen, and the engine pill shows the service state (Reloading… / Restarting… /
  Offline). Test: kill the fake server mid-stream and restart it. The store never becomes empty, and events after
  the resume point are applied exactly once.
- **REQ-MAC-013 [ ]** Threading and state: all socket I/O and JSON decoding run off the main actor (an `actor`
  per connection). Each project has one `@Observable @MainActor` store that is updated only from snapshot and
  event application, with no timers polling for data. Events that arrive during one frame are applied as one batch.
- **REQ-MAC-014 [ ]** Commands (the API's command set: chat, answer or dismiss a question, wake, stop, enable,
  pause/resume, create or update a task, stop everything, …) are async calls that return the API's result.
  Failures show as a toast with the API's error text. UI changes wait for the confirming event: the store is
  never updated optimistically, except for the drag-and-drop ghost (REQ-MAC-031).
- **REQ-MAC-015 [ ]** Service control without a socket: **Start team** on an offline project, and **Add
  project…** on a folder with no `.troupe/`, run the `troupe` CLI (REQ-MAC-002): `troupe start` starts a
  detached service only (REQ-ENG-006), and `troupe init` sets a folder up. They never open a second GUI. With no
  socket, the pill reads `.troupe/service.json` to show "Restarting…" or "Crashed" (REQ-API-074).
- **REQ-MAC-016 [ ]** Tests: `TroupeKitTests` runs an in-process fake socket server that replays recorded
  fixtures (`mac/Tests/Fixtures/*.jsonl`, the API's wire frames verbatim). It covers the version mismatch,
  snapshot → events, seq resume, `resync_required`, reconnect without an empty store, and command round-trips.
  None of these tests needs a running engine.

## Parity with specs/40-gui.md
- **REQ-MAC-020 [ ]** Every REQ-GUI-0xx below has a Mac implementation that meets its acceptance criteria, reading
  data only through the API. "Spike" = covered by M0 (REQ-MAC-070).

| REQ | Mac view / feature | Spike? | Native improvement |
|---|---|---|---|
| GUI-001 top bar | Toolbar: engine pill, stats, usage meters, + Task, Pause | pill only | Real `NSToolbar`, customizable |
| GUI-002 team sidebar | `List` sidebar with avatar ring, activity, badges, parked | **yes** | Native selection and context menu (Chat, Wake, Stop) |
| GUI-003 Needs you | Inspector column, question/idea cards | no | Keyboard navigation, VoiceOver (MAC-037) |
| GUI-004 tabs ⌘1–7 | Tab picker, ⌘1–9 via menu | Board/Pulse | Menu bar items (MAC-032) |
| GUI-005 toasts + notifications | Toast overlay + `UNUserNotificationCenter` | no | Click-through to the item (MAC-033) |
| GUI-006 20/60 fps | Animation paused when quiet | **yes** | Display link paused, near-zero idle CPU (MAC-040) |
| GUI-007 screenshots | `TROUPE_SHOT` mode; F12 → ⌘⇧S | **yes** (shot) | Headless render, fixture mode (MAC-050/051) |
| GUI-010 Chat | Chat view, markdown bubbles, composer | no | Native text editing, spell check, selection |
| GUI-011 Pulse | Pulse scene + feed + Work panel (#32) | **yes** | Metal/Canvas rendering (MAC-040) |
| GUI-012 Board | Columns, cards, task detail sheet | **yes** | Drag-and-drop (MAC-031) |
| GUI-013 Mail | Mail list with filters, expanding rows | no | Selectable text |
| GUI-014 Memory | Memory list by kind | no | Selectable text |
| GUI-015 Docs | Markdown docs, live reload | no | Selectable text, find in page (⌘F) |
| GUI-016 Agent | Agent header, run chips, paged transcript | no | Selectable transcript |
| GUI-020 [ ] drag cards | SwiftUI `draggable` / `dropDestination` | no | Native drag (MAC-031) |
| GUI-021 [ ] Settings | Settings tab, and ⌘, opens it | no | Standard ⌘, (needs a settings API command) |
| GUI-022 [ ] prompt inspector | Transcript / Prompt segmented control | no | — |
| GUI-023 [ ] copy text | Text selection everywhere + Copy buttons | no | Satisfied natively, including the stretch (MAC-030) |
| GUI-024 [ ] ⌘K palette | Palette overlay over the store's data | no | — |
| GUI-025 [ ] usage charts | Swift Charts | no | Swift Charts (system framework) |
| GUI-026 [ ] doc history | Docs change list | no | — |
| GUI-027 [ ] icon, title, badge | AppIcon, window title, dock badge | icon | Dock badge (MAC-034) |
| GUI-028 [ ] while you were away | Catch-up sheet on focus | no | Uses app activation events |
| GUI-029 [ ] service states | Engine pill states, Start team | offline only | — |
| GUI-030 [ ] enter/leave Stage | Stage as a native full-screen space | no | Real macOS full screen; `troupe watch` → Stage (M2) |
| GUI-031 [ ] Stage agents | Shared Pulse/Stage scene | no | — |
| GUI-032 [ ] comets | Shared scene | no | — |
| GUI-033 [ ] task cards | Shared scene | no | — |
| GUI-034 [ ] YOU glow | Shared scene | no | — |
| GUI-035 [ ] ticker | Shared scene | no | — |
| GUI-036 [ ] quiet mode | Shared scene | no | — |
| GUI-037 [ ] Stage perf | MAC-040/041; scene mapping in TroupeKit, unit-tested | no | — |
| GUI-040 [ ] multi-project | Project rail (design/projects.md), All inbox | no | Multiple connections come from the design (MAC-010) |
| GUI-041 [ ] Decisions | Decisions tab (design/decisions.md) | no | — |

  - Some parity items need data or commands the API doesn't have yet, for example Settings editing and doc git
    history. The builder files an API task for each such item rather than reading files or the DB directly.
    Docs (GUI-015) is the one exception: the app may read `specs/`, `design/` and `docs/` under the project path.

## Native wins
- **REQ-MAC-030 [ ]** Text selection wherever text is shown: chat, mail, memory, decisions, docs, transcripts,
  question cards, task detail. Drag-select, ⌘A and ⌘C work, and the copy is the rendered text. The Copy buttons
  from GUI-023 remain and copy the raw markdown.
- **REQ-MAC-031 [ ]** Board drag-and-drop with SwiftUI drag and drop. The column → status mapping, no-op drops,
  Esc cancel and the event come from REQ-GUI-020. Branch → `approved` on Done follows REQ-ENG-039. The move is one
  API command, and the card snaps back if the command fails. A click without a drag opens the detail sheet.
- **REQ-MAC-032 [ ]** A real menu bar. Every item is disabled when it doesn't apply.
  - **File:** New Task ⌘N, Add Project…, Close Window ⌘W. **Edit:** the standard items, plus Find ⌘F in Docs,
    Chat and transcripts.
  - **View:** the tabs ⌘1–9 in 40-gui order (Decisions after Memory), Stage ⌘⇧F (ignored while a text field has
    focus, REQ-GUI-030), Zoom In ⌘+ / ⌘=, Zoom Out ⌘-, Actual Size ⌘0.
  - **Team:** Pause/Resume ⌘P, Search ⌘K, Start team, Stop team… (with confirmation), and **Stop everything ⌘⇧.**
  - **Window:** the standard items, plus projects ⌘⌥1–9 (REQ-GUI-040).
  - **Stop everything** is the kill switch (`specs/05-safety.md`, task #42). It sends the API's `stop_now` command
    to **every attached project** at once, with no confirmation. If a project's socket is down, it falls
    back to `troupe stop --now` for that project. It shows which projects confirmed within 2 s.
- **REQ-MAC-033 [ ]** Notifications through `UNUserNotificationCenter`, for new questions, chat replies while the
  app is unfocused, and the engine's needs-help events (task #35), titled with full handles (REQ-COM-005).
  - Clicking a notification activates the app, switches to its project and opens the item: the question card,
    chat thread, task or agent.
  - No duplicates: while the app is running, it tells each engine through the API that it is the notifier (`hello`
    with `notifications: true`, REQ-API-010), and the engine's own OS notifications stay quiet for that project.
    Focused-app behavior follows #35 (toasts, no OS notification).
- **REQ-MAC-034 [ ]** The dock badge is the number of open questions across all attached projects, and has no
  badge at 0. The window title is `troupe — <project>` with an `(N) ` prefix (REQ-GUI-027, total across projects).
  Both update within 1 s of a question arriving or being answered.
- **REQ-MAC-035 [ ]** Window state restoration: after relaunch, the window's frame, selected project, tab, sidebar
  and inspector widths and zoom are restored. Stage is never restored: a relaunch opens the last normal tab.
- **REQ-MAC-036 [ ]** Dark "midnight" theme only. The app forces the dark appearance whatever the system setting,
  and system light mode never produces light controls. A light appearance is **not** required.
- **REQ-MAC-037 [ ]** Accessibility.
  - VoiceOver labels on agent nodes and sidebar rows ("builder_2, Builder, working: Edit views.py, 3 unread"),
    task cards ("#6, P1, <title>, In progress, builder_2, 2 hours"), and question cards. Each question option is a
    named accessibility action.
  - The Needs-you panel works from the keyboard alone: Tab into it, ↑/↓ between cards, 1–9 to pick an option
    (REQ-COM-025), Return to focus the reply field, ⌘Return to send, Esc to leave.
  - With Reduce Motion on, comets and card flights fade in place instead of travelling.
- **REQ-MAC-038 [ ]** Zoom (task #23): ⌘+ / ⌘- / ⌘0 scale all type and layout metrics together. The default is
  115%, the range 80–160% in 5% steps, and the setting persists. A "Zoom 115%" toast appears on each change.

## Pulse and Stage rendering
- **REQ-MAC-040 [ ]** Pulse and Stage are one scene (#32, `design/stage.md`, and `design/pulse.md` if present),
  drawn with SwiftUI `Canvas` in a `TimelineView` or with an `MTKView`. The builder picks one and gives the reason
  in the task summary.
  - The scene model (nodes, comets, cards, ticker, quiet timers) is a pure TroupeKit type, advanced by `(events,
    dt)`, with unit tests for the GUI-037 cases: fan-out split, the 12-comet cap, the reject bounce and the
    question glow.
  - The scene runs at 60 fps while anything animates or an agent is working. When quiet (REQ-GUI-006/037) it drops
    to ≤ 20 fps, or pauses the timeline entirely, and resumes on the next event or input.
- **REQ-MAC-041 [ ]** Frame budget: with 12 agents and 20 comets in flight, main-thread frame time has a p95 below
  8 ms on an Apple-silicon Mac.
  - The scene emits `os_signpost` intervals (`PulseFrame`). A `swift test` performance test renders 600 frames
    offscreen from a fixture and checks the p95. An Instruments trace goes in the task summary.

## Screenshot verification (required for every Swift UI task)
- **REQ-MAC-050 [ ]** `TROUPE_SHOT=/path.png` renders the main window once data is loaded, plus
  `TROUPE_SHOT_FRAME` frames (default 90) so animations settle. It writes the PNG at the window's backing scale
  and exits 0.
  - `TROUPE_TAB=<tab>` (including `Stage`), `TROUPE_TASK=<id>` (opens the detail sheet), `TROUPE_STAGE_DEMO=1`
    (the design/stage.md demo scene) and `TROUPE_PROJECT=<name>` behave as in the raylib app. The default project
    is the one whose path contains the current directory.
  - It works from a builder's shell with no interaction: no focus stealing, no dialogs, no notification permission
    prompt. Metal and Canvas layers appear in the PNG and are not blank.
  - If no data arrives within 15 s, it still writes the PNG, prints the reason to stderr and exits 1.
- **REQ-MAC-051 [ ]** `TROUPE_API_FIXTURE=<file.jsonl>` replaces every live socket with a recorded
  hello + snapshot + events (the REQ-MAC-016 format), replayed on a fixed clock, so a screenshot is the same on
  every run. Commands are logged to stderr and ignored. Repository fixtures cover 1 and 3 projects, an 8-agent and
  a 12-agent team, and an offline project.

## Performance & resources
- **REQ-MAC-060 [ ]** Idle CPU stays below 1% (averaged over 60 s) with 3 projects attached, no agent working and
  the window visible. Measure with `top -l` and record the result in the task summary.
- **REQ-MAC-061 [ ]** Memory is bounded. Messages, mail, the activity feed and transcripts are paged through the
  API: the newest page loads first, and older pages load on scroll. Only the selected run's transcript is kept in
  full. Test: replaying a 10,000-event fixture keeps each store's collections within their page caps.

## Design sources
- **REQ-MAC-080 [ ]** Visual language comes from `design/*.md`. `TroupeKit.Theme` translates `design/system.md`'s
  tokens: base surfaces, text, accent and semantic colors, role colors, type faces and sizes, radii and motion
  speeds.
  - A unit test parses design/system.md's color tables and fails if `Theme` disagrees, so either the doc or the
    code gets fixed (the system.md rule).
  - A role color the API reports for a role missing from the table (for example Architect or Researcher) takes
    precedence. The designer is asked to add that role to system.md.

## Milestones
- **REQ-MAC-070 [ ]** The app ships in stages. Each milestone ends with its REQs `[x]` here and QA's approval.
  - **M0, spike (#49):** REQ-MAC-001–003, 010–016, 040, 050 and 051, plus the "Spike? yes" rows: the sidebar,
    Board and Pulse, updated live from the API (or a fixture until #48 merges). It is the go/no-go on the native
    look.
  - **M1, parity:** every `[x]` REQ-GUI in 40-gui.md, plus REQ-MAC-030–038, 041, 060, 061 and 080. The human uses
    the Mac app for a working day with raylib available as a fallback.
  - **M2, retire raylib:** `troupe up`, `troupe gui` and `troupe watch` open the Mac app when it is installed, and
    fall back to raylib when it isn't. Removing `src/troupe/gui/` and the raylib dependency **needs the human's
    OK through `ask_human`**. After removal, 40-gui.md's Code line points to `mac/`.
  - **M3, native only:** the remaining `[ ]` GUI REQs, built only in Swift.

## Open questions
- Minimum macOS version: 27+ is assumed. Task #49 says 14+. Confirm with the human before M0 merges.
- Distribution and signing: an ad-hoc local signature for now. Should there be a Developer ID and notarization
  later?
- Once installed, should `troupe up` launch the Mac app automatically (M2 as written), or only through
  `troupe app`?

## Out of scope
- iOS / iPadOS, and GUIs for Windows or Linux.
- App Store distribution.
- A light appearance.

## Changelog
- 2026-09-23 — written (human confirmed the native SwiftUI app; pm msg #110).
