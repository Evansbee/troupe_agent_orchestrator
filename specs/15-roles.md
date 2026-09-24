# Roles: duties and boundaries

Status legend: **[x]** implemented · **[ ]** not yet · **[~]** partial.
Code: `src/troupe/roles.py` (role prompts + charter; protected, REQ-SAFE-020). Roster and providers: REQ-ENG-041.
The vision's roles table (specs/00-vision.md) says *what* each role is for. This file holds the testable duties of the
newer roles and the cross-role duties that aren't obvious from their names. Every role prompt starts from the charter
(Principle 0, REQ-SAFE-001).

Test for every REQ here: the rendered role prompt contains the duty, and the role's tool permissions match (unit test
over `roles.py` and `team.py`).

## Architect (#36; human: "make sure what we're building is sustainable long term")
- **REQ-ROLE-001 [ ]** The architect owns HOW the code is structured and writes no product code.
  - It maintains `docs/architecture.md` (module map, boundaries, data flow, key invariants) and ADRs in
    `docs/adr/NNNN-title.md` for structural decisions (e.g. the engine API, the SwiftUI client, schema changes).
  - It works in main like other non-builder roles (REQ-ENG-036). It may edit only `docs/architecture.md` and
    `docs/adr/`, and is told not to edit anything else.
  - Its first job once seated: `docs/architecture.md` for the current project, plus an ADR for any structural decision
    already made without one (for troupe: the engine API / SwiftUI split).
- **REQ-ROLE-002 [ ]** Brief notes (advisory). When the lead creates or promotes a builder task, the architect may add a
  task note with structural constraints (where the code lives, what not to couple) before a builder starts. The notes
  never block dispatch.
- **REQ-ROLE-003 [ ]** Review gate for risky changes only (REQ-ENG-043). The architect has `review_task` permission and is
  woken only for tasks that match the gate. It reviews structure, boundaries and the contract. Functional checks are
  QA's job.
- **REQ-ROLE-004 [ ]** Health checks on a proactive cadence (default: after every 5 merges or daily, whichever comes
  first, and only if something merged).
  - It looks for hotspots (file size/complexity), duplication, dependency creep, test gaps against
    `docs/architecture.md`, and stale skills (REQ-COM-040).
  - It files P2/P3 refactor or tech-debt tasks (landing in backlog for the lead) rather than rewriting code itself.
- **REQ-ROLE-005 [ ]** It answers every gadfly architecture challenge (REQ-ROLE-010) concretely, in one of two ways:
  - **defend:** reply with the reasoning *and* write it into `docs/architecture.md` or the ADR;
  - **change course:** update the ADR and/or file a task.
  A bare acknowledgement doesn't count, and the prompt says so.

## Gadfly: architecture challenges (#36; human: "gadfly can ask it questions periodically to challenge the architecture")
- **REQ-ROLE-010 [ ]** On its proactive cadence (after every 3 merges or daily, only if something changed), the gadfly
  reads `docs/architecture.md`, recent ADRs and recently merged diffs. It sends the architect 1–3 pointed questions,
  e.g. "X couples A to B — what happens when…?" or "the ADR assumes Y — still true after #N?".
  - Challenges and answers are ordinary mail, so they show up in Mail and Pulse.
  - With no enabled architect, the gadfly sends them to the lead.

## Researcher (#41; human: "going out to the web and combing for information on the domain … throwing questions to the team about how we work")
- **REQ-ROLE-020 [ ]** Domain scouting (proactive, about hourly, only when the world changed, principle 4).
  - It reads `specs/00-vision.md` and current work, then searches the web (comparable products, standards, APIs,
    pitfalls, recent developments).
  - It keeps `research/` as one markdown note per topic: summary, key findings, **every claim with a source URL and a
    short quote**, date, and confidence (high/medium/low). Anything it couldn't verify is marked **unverified**.
  - It sends short digests to pm/lead/gadfly when something matters to current work.
- **REQ-ROLE-021 [ ]** Process questions: it sends sharp questions about HOW the team works ("comparable teams do X —
  should we?", "#N took 3 rejects; others use Y"), mostly to gadfly, lead and pm.
- **REQ-ROLE-022 [ ]** Research tasks. Anyone can create a task with role `researcher`, and it's dispatched to a
  researcher (REQ-ENG-031). The deliverable is `research/<topic>.md`, and `complete_task` carries the path plus a
  ≤5-line summary. It isn't a code task: no worktree, and it's auto-committed from main (REQ-ENG-036).
- **REQ-ROLE-023 [ ]** Boundaries.
  - No direct human contact: `ask_human` and `propose_idea` return `ERROR:` for the researcher, telling it to go
    through the PM.
  - It doesn't edit specs, design or code. The prompt says so, and its sandbox profile allows writes only to
    `research/` (REQ-SAFE-051).
  - Claims that drive a decision are spot-checked by pm or gadfly on a stronger model before they're relied on. The pm
    and gadfly prompts say so.
- **REQ-ROLE-024 [ ]** The PM delegates legwork research to the researcher (a research task or mail) and keeps vision
  and proposals. The PM prompt says so.

## PM: the human's single contact and the team's communicator (human, 2026-09-24)
- **REQ-ROLE-030 [ ]** The PM coordinates and communicates. It doesn't build or assign work.
  - It is the only agent the human talks to (REQ-COM-027..029).
  - It routes each human instruction to the right teammate: the lead for work and priorities (tasks go through the
    lead's board), spec for behavior, the designer for look. It follows up until the instruction is done or
    reported blocked, per Principle 0's "relentless" rule (REQ-ENG-048).
  - It triages escalations (REQ-COM-028): answer what's already decided, batch what isn't urgent, frame everything
    with options, never sit on anything.
  - It keeps the vision and proposals (REQ-ROLE-024 delegates legwork research).
  - It runs on the **strongest available model**: default `claude · fable`, with fallback `claude · opus`
    (REQ-ENG-041 / team.yaml `providers` order, REQ-BE-012).
  - Test: the PM prompt contains these duties, and the generated roster puts the PM on fable → opus.

## Docs visibility
- `research/` shows as a "Research" group and skills as a "Skills" group in the Docs tab (REQ-GUI-015).

## Changelog
- 2026-09-23 — written: architect (#36), gadfly architecture challenges, researcher (#41), PM delegation. Human: the
  architect gates risky changes only.
- 2026-09-24 — REQ-ROLE-030: the PM as sole human contact and communicator, on the strongest model (human, via pm).
