# troupe — Vision

> Run one command in a directory. A team of AI agents with distinct roles spins up, talks to you and to
> each other, and builds the software — from an empty directory to a finished product.

## The problem
Single coding agents are powerful but myopic: one context, one role, no memory of *why*, no one asking
"is this actually what we meant?". Getting from an idea to a *finished* product needs the whole team:
someone who owns what gets done, someone who writes it down precisely, someone who builds, someone who
verifies, someone who designs, someone who pushes back, and someone looking over the horizon.

## Who it's for
A builder (the **human**, the project owner) who wants to direct a product, not type every line. They have
Claude / Codex subscriptions and maybe local models, and want those used where each is best.

## What "finished" means
troupe is finished when the human can point it at an empty directory, describe a project, and use it
to build that project into working software, while seeing at every moment what each agent is doing and
why. Until then, anything that blocks that end-to-end use or hides agent activity takes priority.

## Principles
1. **The human is the owner, and their attention is scarce.** Agents batch questions, offer concrete
   options (yes / no / later / sort of), never block on an answer, and never re-ask what was answered.
2. **Specs are the source of truth.** Behavior lives in `specs/`, design in `design/`, decisions (with
   *why*) in team memory. If it isn't written down, it isn't decided.
3. **Roles, not a monolith.** Each agent has one job and a model suited to it. Exactly one Lead owns
   *what* gets done.
4. **Proactive, but not busy.** Agents look for work when the world changes, not on a treadmill. Budgets
   cap spend. Doing nothing is a valid outcome.
5. **Safe parallelism.** Builders work in isolated git worktrees on tasks with explicit territories; QA
   verifies by effect before anything merges.
6. **Everything is visible.** A beautiful, crazy-useful desktop app shows who's doing what, how they're
   talking, the backlog, the mailboxes, the decisions, and what needs the human — live.
7. **Local-first & hackable.** Plain files + one SQLite DB in `.troupe/`. Python + uv. Config is a TOML file.

## The team (roles)
| Role | Job |
|---|---|
| **Lead** (exactly 1) | Owns WHAT is getting done: backlog, priorities, assignment, unblocking. |
| **Product Manager / Visionary** | Interviews the human from zero to a crisp vision; scouts the industry for what's next; proposes ideas. |
| **Spec Writer** | Writes/maintains `specs/`: numbered, testable requirements. Talks detail with the human. |
| **Designer** | HOW it looks and flows: `design/` system + screens. |
| **Builder(s)** | Write code, one task at a time, each in its own worktree. |
| **QA** | Verifies things work and meet spec; approves or rejects every task before merge. |
| **Gadfly** | The pesky one: "the spec says X — does that mean Y? what about Z?" — to the human and the agents. |

## Non-goals (for now)
- Cloud hosting / multi-user collaboration. troupe runs on your machine.
- Replacing the underlying agent CLIs. We orchestrate `claude`, `codex` and OpenAI-compatible local models.
- A general workflow engine. troupe is opinionated about how a software team works.

## Roadmap themes
1. **Dogfood** — troupe builds troupe. Everything needed to make that pleasant comes first.
2. **Trust** — tests, verification, transparency of prompts/costs, safe merges.
3. **Flow** — faster conversations with the human, smarter wake-ups, less noise.
4. **Polish** — the GUI should feel like a premium native app.
5. **Reach** — more backends, templates for common project types, research tooling for the PM.

## Inspiration
OpenRig (`~/.openrig/reference/`): derived "parked" diagnosis (idle while owing work), exclusive file
territories for parallel builders, reviewers on a different runtime than writers, "the brief is the unit".

## Changelog
- 2026-09-23 — initial vision written during bootstrap (from the human's brief).
- 2026-09-23 — added "What finished means" (human: a tool we can use to build a project; agent activity
  always visible). Merge gate approved (idea #1 → REQ-ENG-039).
