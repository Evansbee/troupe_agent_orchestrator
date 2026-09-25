# troupe, modeled on OpenRig — gap analysis and reshaped plan

Written by the PM on 2026-09-24 evening, for the human's approval before any new feature work.
Draft lives here because `specs/00-vision.md` is frozen tonight; it replaces the vision's
"Inspiration" section once approved. Ten-minute read. Three questions at the end.

Sources read: `~/.openrig/reference/` (agent-spec, rig-spec, agent-startup-guide,
agent-state-taxonomy, wave-sdlc, sdlc-conventions, worktree-builds, scoped-operating-posture,
release-boundary) and `rig --help`. Nothing under `~/.openrig/transcripts`, `secrets` or the
database was opened.

## 1. What OpenRig is, in one page

**A control plane, not a scheduler.** A daemon (`rig start`) plus a CLI (`rig`) is the product.
Every capability is a command; the mission-control TUI (`rig tui`) only *renders* state the daemon
computes. `rig ps`, `rig parked`, `rig send`, `rig transcript`, `rig attach`, `rig up`, `rig down`,
`rig snapshot`/`restore`, `rig crash-cart`, `rig discover`/`adopt`, `rig export`/`import`.

**Agents are residents, not runs.** Each agent is a long-lived interactive session in a tmux seat.
The human can attach to it, send it a line, read its transcript. The daemon observes it through an
evidence ladder (Claude/Codex lifecycle hooks, session files) rather than driving it turn by turn.
Context persists across the day; nothing re-primes a 15k-token brief on every wake.

**A rig is a spec.** `rig.yaml` declares pods of members (agent ref, runtime `claude-code` or
`codex`, model, cwd, profile), edges (`delegates_to`), services (compose), startup files, a
culture file, a continuity policy (session log, restore brief, peer-driven restore). Rigs bootstrap
from the spec, export back to it, bundle, snapshot and restore. An agent is its own spec
(`agent.yaml`): profiles that pull in skills, guidance, subagents, hooks; startup files with
delivery hints. Context is progressive: skill names sit hot, bodies load on demand
(`rig context get`).

**State is a formal language with one oracle.** Three orthogonal axes: session
(`present · detached · exited · absent`), activity (`working · idle-at-prompt · unknown`, with
needs-input as a *count + reason*, never a status), resumability (`live · resumable ·
context-walled`). `unknown` is a first-class honest value. Derived at read time, never stored:
**PARKED** (idle while owing work: the disease), **HELD** (stopped on purpose with an armed wake),
**DONE-UNSEEN** (finished, nobody looked). Every surface renders the same vocabulary.

**Work is the markdown control plane.** The human records intent at altitude; agents turn it into
plan, build, proof. "The brief is the unit": goal, acceptance, exact context routes, exclusive file
territory, and a standing grant to navigate the build yourself. Waves: slices build in parallel in
disjoint territories, one integrator merges serially, review happens once per wave by two reviewers
on different runtimes who never wrote the code, and reviews check for *drift* as a first-class
finding. Proportionality: the simple SDLC is the default; the rigorous overlay only when the human
assigns it to a named piece. A planning dial (P0 pointers, P1 spec, P2 proof contract) chosen per
piece.

**Operating posture is a setting.** `human-led` is quiet by default: no process interruptions,
no notifications for diagnosis. `delegated` opens oversight and notifications for a named scope.
Nothing infers delegation from activity.

## 2. Gap table: troupe today against OpenRig

| OpenRig element | troupe today | Call | Why |
|---|---|---|---|
| Daemon + CLI as the product; TUI renders | Engine service + API socket; TUI and GUI both drive it; CLI is thin (`status`, `stop`, `spend`) | **Adopt** | The human runs everything from a terminal; every capability must be a `troupe` command the TUI mirrors |
| Agents as attachable resident seats | One-shot `claude -p` / `codex exec` runs per wake, full prompt rebuilt each time | **Adopt** | This is the shape the human asked for, and the per-wake prompt rebuild is where most of today's tokens went |
| `send` / `transcript` / `attach` | Mail via MCP tools; transcripts only in the GUI's Agent tab | **Adopt** | Talking to an agent directly is the OpenRig experience; mail stays for agent-to-agent |
| Rig spec (pods, runtimes, startup files, culture, continuity, services) | `team.yaml` (provider/model/level per agent) + `troupe.toml` | **Adapt** | Grow `team.yaml` into a troupe spec: pods, startup/guidance files, culture, continuity; export/import |
| Agent spec with skills/guidance/profiles, progressive disclosure | Role prompts hard-coded in `roles.py`; skills task (#39) parked | **Adopt** | Roles become agent specs on disk; skills and guidance load on demand |
| Three-axis state + needs-input count/reason + PARKED/HELD/DONE-UNSEEN | Ad-hoc status strings; "stalled"/"timeout" notifications; no parked diagnosis | **Adopt** | One honest vocabulary, rendered everywhere; `troupe parked` answers "why is nothing moving" |
| Evidence ladder (hooks, session files) instead of driving turns | Engine owns each run's lifecycle | **Adopt with seats** | Falls out of the resident model |
| Snapshot / restore / crash-cart / discover / adopt | None (crash watcher #91 planned) | **Adapt** | Snapshot + crash-cart first; discover/adopt later |
| The brief is the unit; exclusive territories; integrator merges serially | Task briefs with territory; merge gate; QA approves | **Keep** | Already OpenRig-shaped; keep the gate |
| Two reviewers, different runtimes, never the writer; drift review | One QA; roster now Codex-first so writer and reviewer often share a runtime | **Adapt** | Second reviewer only for wave-level review; per-slice QA stays single |
| Waves as the care dial; simple SDLC default; rigorous overlay by assignment | Everything gets the same ceremony (QA + gate + cards) | **Adopt** | Proportionality is what today lacked: a spec checkbox got the same ceremony as a sandbox change |
| Planning dial P0/P1/P2 | Task priority only | **Adopt** | Cheap for small work, rigorous only where assigned |
| Operating posture human-led (quiet) vs delegated | Notifier fires "needs you" for a dozen kinds | **Adopt** | Replaces #97's card-per-notification with a posture: quiet unless the human delegates |
| Proof: failing-test-first, receipts cite the final revision | QA verifies by effect; tests in gate | **Keep** | Fine as is |
| Multi-host, gateway, services (compose) | Out of scope | **Skip for now** | Not what the human is missing |
| Mission-control TUI | Textual TUI (cutover done today) | **Keep, reshape** | Panes render the taxonomy; Needs-you becomes posture-aware |
| Desktop GUI (raylib) | Exists, spawned windows into the human's face today | **Drop** | Not in OpenRig's model; a native Mac portfolio app can come much later |

## 3. What troupe stops doing

- **The raylib GUI as a product surface.** Archive the code, keep nothing in the gate for it.
- **One-shot wake/run as the agent model.** Wakes become `send`s into resident seats; the engine
  stops rebuilding a full brief per wake. This is also the single biggest token cut available.
- **A card for everything.** Cards only for the four things the human chose (safety/sandbox/gate
  code, security weakening, spending, public actions). Everything else is posture-gated.
- **Broadcast-and-acknowledge team chatter.** Already stopped tonight; the resident model makes it
  structural (a `send` doesn't spawn a run).
- **Team-wide proactive check-ins.** PARKED diagnosis replaces them: the daemon computes who is idle
  while owing work; nobody wakes to look for work.

## 4. Reshaped roadmap, three waves

Constraints on top of every wave: Codex-first roster with spend accounting visible, and the human
runs everything from one terminal like mission control.

**Wave 1 — the shape (resident seats, one vocabulary, CLI first).**
Resident seats: each agent runs as an interactive `claude`/`codex` session in the troupe tmux
session; the engine becomes a daemon that delivers mail via send and observes via hooks.
`troupe ps`, `troupe parked`, `troupe send <agent> <text>`, `troupe transcript <agent>`,
`troupe attach <agent>`. The three-axis state taxonomy with PARKED/HELD/DONE-UNSEEN, rendered in
the TUI team pane and header. GUI archived. Exit criterion: the human runs a real task end to end
from the TUI and `troupe send`, sees every agent's honest state, and the day's run count drops by
half or better.

**Wave 2 — the spec and recovery (rig-shaped project files).**
`team.yaml` grows into a troupe spec: pods, startup/guidance files per role, culture file,
continuity policy (session log + restore brief). Roles move from `roles.py` to agent specs on
disk with skills that load on demand. `troupe snapshot`/`restore`, crash-cart, discover/adopt of a
stray seat. Exit criterion: `troupe export` of this project reproduces the team on a fresh
checkout.

**Wave 3 — proportionality and posture.**
Planning dial per task (P0 pointers / P1 spec / P2 proof contract); wave-level review with a
second reviewer on the other runtime only when assigned; operating posture human-led vs delegated
replacing notification kinds; calibre tiers (#75) routing cheap work to cheap models; spend
rollups per seat in the PM's brief. Exit criterion: a week where the human approves fewer than
five things and the spend view shows rework under a fifth of runs.

Everything on today's backlog is re-triaged against these waves; anything that serves none of
them is cancelled, not parked.

## 5. Three questions (with recommendations)

1. **Standalone or on top of OpenRig?** A) troupe stays its own daemon + CLI + TUI, built to
   OpenRig's shape (this document). B) troupe becomes a rig bundle for OpenRig itself: agent specs,
   skills and the software-team conventions, using OpenRig's daemon, seats, TUI and state.
   *Recommendation: A, because troupe is public and MIT and you built it to stand alone; but B is
   the fastest way to a working OpenRig-shaped team, and the agent specs and skills from Wave 2
   are the same work either way. If B is what you imagined, say so and Wave 1 shrinks to a week.*
2. **Resident seats in Wave 1, or later?** Seats are the biggest change and the biggest token cut.
   *Recommendation: Wave 1. Without seats, the rest is a coat of paint on the run model.*
3. **Drop the raylib GUI now?** *Recommendation: yes, archive it this week; the native Mac
   portfolio app stays a later idea, not a commitment.*

Answer these three and I re-triage the whole backlog before anyone touches code.
