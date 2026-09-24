# Portfolio: many projects, one troupe

Status: PROPOSAL (PM, 2026-09-24). Waiting on the human's call on the coordinator shape (question in Needs you).
Once the Architect role (#36) exists, it takes this over as an ADR.

## The problem
troupe will run several projects at once: software, research papers, docs, human-interaction work. They share
the human's attention, one machine, and the same provider accounts (Claude/Codex limits are account-wide).
The human wants to see across projects, prioritize them ("all in on P1, but P2 moves up when P1 has nothing
actionable"), and give each project a different kind of team.

## Principle: autonomy decides *what*, the portfolio decides *when and how much*
The apparent conflict ("go figure out what to do" vs. "P1 first") goes away if we split the two:
- **Inside a project**, agents stay fully autonomous. They never think about other projects. The lead picks the
  next task, builders build, and so on, exactly as today.
- **Across projects**, a mechanical scheduler hands out **run slots**. It decides which project's agents get to
  run next, not what they do. No LLM judgment in this hot path: it's cheap, predictable and explainable.

## 1. Priority: "focus with spillover" (work-conserving priority)
Each project has `priority` (1 = highest) and an optional `share` and `budget` in the portfolio config.
- A free slot goes to the **highest-priority project that has actionable work**.
- **Actionable** is computed by the engine from data it already has (#50 wait states): a ready task with its
  dependencies met, a review waiting, unread mail that needs action, or a human answer just arrived. A project
  whose agents are all waiting on the human, on reviews in flight or on rate limits is *not* actionable, so its
  slot **spills over** to the next project. That's the "P2 bumps up when P1 has nothing crazy actionable" behavior,
  with no agent deciding it.
- **Chat with the human always preempts**, in any project (as today).
- Optional modes per portfolio: `focus` (strict priority + spillover, the default) or `shares` (e.g. 60/30/10
  of slots when everyone is actionable). There's also a starvation guard: a low-priority project still gets a
  proactive check-in at least every N hours, so it never silently rots.
- **The human's attention is prioritized too.** Needs-you cards from all projects sit in one inbox, sorted by
  project priority. Low-priority projects batch their questions (at most one card per X hours unless it's blocking).

## 2. Project types = team templates, not different engines
A project is the same engine with a different **team** (team.yaml) and different **checks**:
| Template | Roles | "Done"/review means | Merge gate check |
|---|---|---|---|
| `software` (today) | lead, pm, spec, designer, architect, builders, qa, gadfly, researcher | tests + spec | `pytest` / `npm test` |
| `research` | lead, pm, researcher ×2, writer, editor/reviewer, gadfly | claims cited, argument holds | citation/link check, lint |
| `docs` | lead, pm, writer ×2, tech reviewer, designer (optional) | accurate, readable, complete | link check, build the docs |
| `human` (coaching, interviews, planning) | pm, researcher, writer, gadfly | the human's goals met | none (the human approves) |
New generic roles: **writer** (produces prose artifacts in worktrees, like a builder) and **reviewer** (QA generalized:
verifies against the spec by the template's standard). `troupe init --template research` writes the right team.yaml.
Roles stay data: prompts live in role files, so a template is a folder with team.yaml + role prompts + checks.

## 3. The coordinator question: a thin broker + one "Director", not a god-agent
**Recommendation:** two pieces, deliberately split.
1. **Portfolio broker** (code, no LLM): a small machine-level service (`~/.troupe/portfolio.db`). Each project
   engine asks it for a **slot lease** before starting a run. It enforces priority/spillover, the machine-wide
   `max_concurrent`, provider usage caps (#38, which are account-wide), and per-project budgets. Engines stay
   per-project and isolated. If the broker is down, each engine falls back to its own local limits.
2. **Director** (one agent on the smartest model, e.g. Fable): woken rarely (a morning brief, when a
   milestone completes or stalls, or when the human asks), never in the scheduling hot path. It reads each
   project's milestone/digest/Needs-you and then:
   - writes the human **one cross-project brief** ("P1 is blocked on you; P2 finished its draft; P3 idle 2 days");
   - **recommends** priority changes. The human decides; the Director can't reprioritize on its own (Principle 0:
     no single agent with power over everything);
   - spots cross-project leverage: a skill or memory worth sharing (#40), duplicated work, a conflict;
   - routes cross-project messages ("links_pm_1 asks troupe's architect about X").

**Why not one grand coordinator in the loop:** it'd be a single point of failure and a cost sink (it would need
everyone's context), it'd add latency to every decision, and it would concentrate power in one agent. Projects must
keep working if it's off. The OS analogy: the kernel scheduler is mechanism (the broker), and the chief of staff
is advice (the Director).

## What it would take (rough order)
1. Portfolio config + broker with slot leases, priority, spillover, the machine-wide concurrency cap (after #24/#28).
2. Actionability score exposed by each engine (builds on #50).
3. Cross-project Needs-you + project rail (#29), sorted by priority.
4. Team templates + writer/reviewer roles; `troupe init --template`.
5. The Director agent + daily brief.
6. Account-wide provider caps moved into the broker (#38).
