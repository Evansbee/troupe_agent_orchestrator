# Communication, questions & memory

Code: `src/troupe/team.py` (tool API), `mcp_server.py` (MCP exposure), `store.py`.

## Tools every agent has (MCP server `troupe`, one stdio process per agent run)
`send_message, check_inbox, ask_human, propose_idea, create_task, update_task, list_tasks, get_task,
complete_task, review_task, remember, recall, set_status, team` (+ `update_decision` REQ-COM-034..036, `save_skill` REQ-COM-041, `share_memory`/`propose_global`
REQ-COM-045/046, `milestone` REQ-ENG-045).
- **REQ-COM-001 [x]** Identity comes from `TROUPE_AGENT`; the same API is used natively by local models.
- **REQ-COM-002 [x]** Tool results are short plain text written for an LLM; errors start with `ERROR:` and say
  what to do instead.
- **REQ-COM-005 [x]** (#25) Every agent has a **handle** `<role>_<N>@<project>` (human: so it's clear who is talking
  to whom across projects), e.g. `lead_1@troupe`, `builder_2@troupe`.
  - `<project>` is `[project] name`, with runs of whitespace turned into `_` and characters outside
    `[A-Za-z0-9_.-]` dropped. Always numbered, even for single-seat roles.
  - `<N>` is the trailing number of the agent's id (`builder-2` → 2, `builder_2` → 2), or 1 if the id has none
    (`spec` → `spec_1`). Two agents mapping to the same handle is a team.yaml validation error (REQ-ENG-019).
  - The stored id never changes, so existing DBs keep working. The handle is derived.
  - The **full** handle is shown wherever communication is shown or leaves the window: the charter and roster,
    wake prompts, tool results, mail rows, the activity feed, chat headers, Stage comet labels, toasts and
    notifications, Decisions authors, `engine.log` and `troupe status`. Only purely spatial labels (avatar
    initials, board-card assignee chips, sidebar rows under the project header, Stage nodes) may drop
    `@<project>`. (Human: full handles "so it's clear who is talking to whom.")
  - Tools accept, case-insensitively: the full handle, the local handle (`builder_2`), the legacy id
    (`builder-2`), a role (fan-out), `team` and `human`. A handle for another project returns `ERROR:` (no
    cross-project mail yet).
  - Test: handle derivation (legacy and new ids, project names with spaces), address resolution for every form,
    and the collision error.
- **REQ-COM-003 [x]** Permissions: only the Lead (or human) can re-prioritize/re-assign or move tasks to
  arbitrary statuses; assignees can block/unblock their own task; only QA/Lead can review.

## Mailboxes
- **REQ-COM-010 [x]** `send_message(to=…)` accepts an agent id (or handle, REQ-COM-005), a role (fan-out to all of that role),
  `team`, or `human`. Every message is an event in the activity feed and wakes the recipient.
- **REQ-COM-011 [x]** Messages are marked read when delivered in a wake prompt (or via `check_inbox`).
- **REQ-COM-012 [ ]** Threads: group messages by `reply_to` chains in the Mail view.
- **REQ-COM-013 [x]** (#46) FYI mail: `send_message(..., fyi=True)` (additive column).
  - FYI mail never triggers a wake. It's delivered in the recipient's next natural wake under "FYI since last time".
  - The engine ignores the flag, and the mail wakes as normal, for mail from the human, mail about the recipient's
    own active task or review, blocking questions, and the ENG-049 pending-mail count guard.
  - Charter rule, human-approved verbatim (question #12): "Mark mail fyi=True unless you need the recipient to act
    or reply." Until this ships, the team
    convention is "FYI" in the subject and no replies to FYIs (lead, 2026-09-23).
  - Test: an FYI doesn't wake the recipient and appears in its next prompt; exempt mail still wakes.

## The human
- **REQ-COM-020 [x]** Live chat: the human can talk to any agent; replies arrive as the agent's final text.
- **REQ-COM-021 [x]** `ask_human` puts a question card in "Needs you" with options + free-text reply; the
  answer is delivered to the asker as mail. Max 4 open questions per agent.
- **REQ-COM-022 [x]** `propose_idea` = question with options Yes / No / Later / Sort of.
- **REQ-COM-023 [x]** Dismissing a question tells the asker to use their judgment.
- **REQ-COM-024 [ ]** A "Team" chat room: one conversation between the human and the whole team. (#4)
  - "Team" is the first entry in the Chat partner list. Room messages are stored with a room marker so they
    appear only in the room thread, not in 1:1 chats.
  - Routing: `@<agent-id>` or `@<role>` mentions wake exactly those agents; `@all` wakes everyone enabled;
    no mention wakes **lead + pm** only. Unknown mentions are shown as plain text and ignored.
  - Woken agents get the room message as chat (REQ-ENG-011 priority) plus the last 20 room messages for context;
    their final text is posted back to the room, not to a 1:1 thread.
  - Agents not woken are not interrupted; the message appears in their next wake prompt under team chat.
  - The room thread interleaves human and agent replies in time order, each bubble in the agent's role color.
  - Test: routing (mention, role mention, @all, no mention, unknown mention).
- **REQ-COM-025 [x]** Keyboard answering in the inbox. (#12)
  - Keys 1–9 pick that option on the hovered question card, or the top card if none is hovered.
  - Ignored whenever a text input has focus (typing "3" in a reply never answers a question).
  - The answered card gets the same feedback as a click (toast + card leaves).
- **REQ-COM-026 [x]** (#37) Decision questions asked in live chat also live in "Needs you", and answering them in chat
  closes the card. (Human: "that should be in a needs you box, if I answer in chat, you should close the needs you
  box with the decision.")
  - Charter rule, human-approved verbatim (question #7): "If you ask the human a decision question in live chat, ALSO
    file it with ask_human (with options), so it sits in Needs you. If the human answers in chat, call
    resolve_question with their answer, then remember() the decision."
  - `resolve_question(question_id, answer, via="chat")`:
    - The asker, or the lead or pm, marks an **open** question answered.
    - The answer is stored exactly like an inbox answer and `recall()` finds it. A new additive column
      `answered_via` = `chat | inbox` records how it was answered.
    - No duplicate answer mail goes to the asker.
    - The card leaves Needs you with an "Answered in chat ✓" toast.
    - Resolving another agent's question (unless you're the lead or pm), an answered, dismissed or unknown question
      returns `ERROR:` with guidance.
  - When human chat arrives and the agent has open questions, the chat wake prompt lists them under "Your open
    questions (did the human just answer one? if so, resolve_question)".
  - Nice-to-have: a card filed during a live-chat run shows "also asked in chat".
  - The API exposes `answered_via` on questions (specs/50-api.md), so the Mac app inherits this.
  - Test: ask → resolve via chat (card closed, `answered_via=chat`, recall finds it, no duplicate mail); the error
    cases; open questions in the chat wake prompt; the charter line present verbatim.

### The PM is the human's single point of contact (#65)
Human, 2026-09-24: "all communications should go through [the PM]. I don't like lead talking to me; he talks to you,
then you figure out if he should know it or if you need my involvement."
- **REQ-COM-027 [ ]** Escalations. When any agent other than the PM calls `ask_human`, `propose_idea` or
  `send_message(to="human")`, the call creates an **escalation** in the PM's inbox instead of a Needs-you card or
  human mail.
  - An escalation records the original text, options, context, task, sender handle and urgency (`normal |
    urgent`). It's stored in an additive `escalations` table.
  - The caller gets a normal, non-blocking result: "Escalated to pm_1@troupe; the answer will arrive in your mailbox".
  - **Exception:** during a run that is answering the human's own live chat (REQ-ENG-011), `ask_human` from that
    agent goes straight to Needs you. The human chose to talk to that agent directly (REQ-COM-026 still applies).
- **REQ-COM-028 [ ]** PM triage tools:
  - `forward_to_human(escalation_id, question, options, context)` creates the Needs-you card, credited "via pm_1
    from lead_1". The answer is delivered to the **original asker and the PM**.
  - `answer_escalation(escalation_id, answer, rationale)` resolves it from existing decisions or memory with no
    human card. The asker gets the answer and the rationale.
  - `batch_to_human([ids…])` combines several escalations into one card or digest. Each answer is routed back to
    its own asker.
  - The PM prompt gives the triage duty: answer what's already decided, batch what isn't urgent, frame everything
    with options, and never sit on anything. Every agent's charter says to reach the human through the PM. The
    `roles.py` change is protected, so the human approves it.
- **REQ-COM-029 [ ]** Nothing can be buried: bypass and auto-forward.
  - **Always direct to the human, never filterable by any agent:**
    - safety approval cards (REQ-SAFE-020/021);
    - kill-switch and stop events (REQ-SAFE-010);
    - engine needs-help notifications (REQ-ENG-047) and ★ "Done" reports (REQ-ENG-048), which come from the engine,
      not an agent;
    - `ask_human(..., bypass_pm=True, reason=…)`, for when an agent believes the PM is acting against the human's
      interests (Principle 0).
    Every bypass is logged as a visible feed event with its reason.
  - **Auto-forward:** an escalation the PM hasn't handled within `[escalation] timeout_minutes` (default 30; 5 for
    `urgent`) is forwarded to the human automatically, marked "auto-forwarded: PM didn't respond". A busy or down
    PM can't bury it.
  - The human's default chat partner is the PM, in the GUI (REQ-GUI-010) and the TUI (REQ-TUI). Direct chat with
    other agents stays available as an inspection tool.
  - #65's diff also adds the bypass rule to specs/05-safety.md (protected, so the human approves it with the merge).
  - Test:
    - the lead's `ask_human` becomes an escalation, not a card;
    - `forward_to_human` delivers the answer to both the asker and the PM;
    - `answer_escalation` resolves with no card;
    - safety cards, needs-help notifications and `bypass_pm` reach the human directly and are logged;
    - an unhandled escalation auto-forwards after the timeout;
    - a live-chat `ask_human` goes direct.

## Memory
- **REQ-COM-030 [x]** `remember(kind=decision|note|fact|idea|preference, rationale=…)`; team-visible unless
  `private`. Recent decisions are injected into every wake prompt.
- **REQ-COM-031 [x]** `recall(query)` searches memories and answered questions.
- **REQ-COM-032 [x]** Supersede: `remember(..., supersedes=<id>)` marks memory `<id>` superseded by the new one. (#8)
  - Superseded memories are excluded from wake prompts and `recall` results by default (`recall(...,
    include_superseded=True)` shows them); they are never deleted.
  - Superseding a nonexistent id, or one already superseded, returns `ERROR:` with the current successor's id.
  - **The human's memories are theirs (Principle 0; found by QA in #8 review).** An agent superseding a memory that is
    **pinned**, **authored by the human**, or of kind **`preference`** gets
    `ERROR: that's the human's — ask the human (ask_human) instead`. Superseding is a de-facto delete, and
    REQ-COM-033 lets only the human delete.
    - The human's own supersede, edit and delete stay allowed.
    - Agent-to-agent supersedes of ordinary decisions are unaffected.
    - Test: an agent superseding a human-pinned preference fails and the preference stays in prompts; the human's
      supersede works.
  - The Memory view shows superseded items struck through with a link to their successor.
- **REQ-COM-033 [x]** Human memory controls in the Memory view. (#8)
  - Pin: pinned memories appear in every wake prompt regardless of age, before recent decisions. Pinning does not
    un-supersede.
  - Edit (title, content, rationale) and delete (with confirmation). Only the human can pin, edit or delete;
    agents have no tool for it.
  - Test: prompt building excludes superseded, always includes pinned.

### Decisions the human reviews (#27; GUI in REQ-GUI-041)
- **REQ-COM-034 [ ]** Major decisions: `remember(kind="decision", major=True)` flags a decision as major (scope,
  architecture, process, or anything that changes a spec or the vision). The charter tells agents when to use it.
  The author, lead or pm can flag or unflag later via `update_decision(id, major=…)`. Other agents get `ERROR:`.
- **REQ-COM-035 [ ]** Human comments on decisions.
  - The human can comment on any decision. Comments are stored in a new table keyed by the decision id, holding
    author, time, body and optional outcome (additive schema).
  - A human comment mails the decision's author, lead and pm (deduplicated), which wakes them. The mail quotes the
    decision and the comment and names the **owner**: the author, or the lead if the author is gone or disabled.
  - Agents reply with `update_decision(id, comment=…)`. Replies land in the same thread (not as mail to the human),
    and the human is notified with a toast.
- **REQ-COM-036 [ ]** Outcomes: the owner, lead or pm closes an open human comment with
  `update_decision(id, outcome=…)`:
  - `acknowledged`: the decision stands; a comment is required saying why.
  - `revised`: the decision's content/rationale are updated in place. The previous text stays visible in the thread.
  - `superseded`: a new decision is created with `remember(..., supersedes=id)` (REQ-COM-032). Doing that while a
    human comment is open records this outcome automatically, shown exactly like a manually set one.
  - `reverted`: the decision is withdrawn and, like a superseded one, excluded from prompts and default recall.
  - Setting an outcome with no open human comment is allowed (the lead cleaning up). An invalid outcome returns
    `ERROR:` listing the valid ones.
- **REQ-COM-037 [ ]** Open human comments (no outcome after them) appear at the top of the wake prompt of the owner,
  lead and pm, under "The human commented on these decisions". The owner is told it owns the response, and an open
  comment counts as work for the owner's wake (REQ-ENG-010 `messages`). They drop out once an outcome is recorded.
  - Test: routing (author, lead, pm, dedup, owner fallback), outcome permissions, the auto-superseded outcome, and
    open-comment prompt inclusion and removal.

## Skills (#39; human: "if an agent learns to do something it should create a skill md file… agents should be able to share skills")
- **REQ-COM-040 [ ]** Skills follow the Agent Skills standard.
  - Each skill is a directory `.agents/skills/<name>/SKILL.md` in the project repo: git-tracked and present in every
    worktree.
  - YAML frontmatter holds `name` and `description` (when to use it and when not to), plus troupe's fields: `author`
    (full handle), `created`, `updated`, `source_task`, and optional `roles`. The body is markdown, and supporting
    files may sit alongside.
  - `.claude/skills` is a symlink to `.agents/skills`, so Claude and Codex load the same set natively (progressive
    disclosure: only name + description sit in context until a skill is used).
  - Whether claude loads project skills with `--strict-mcp-config` and inside worktrees is verified and written down
    in the task summary.
- **REQ-COM-041 [ ]** `save_skill(name, description, body, roles=[])` creates or updates a skill.
  - `name` is slugged. The frontmatter is validated. Updating keeps one directory and bumps `updated`.
  - The body is secret-scanned (REQ-SAFE-031).
  - Builders write into their worktree, so the skill merges with the task and QA reviews it with the diff.
    Non-builders write in main and it's auto-committed.
  - A feed event records each create or update.
- **REQ-COM-042 [ ]** The local backend gets `list_skills()` (names + descriptions, filtered by `roles`) and
  `read_skill(name)`. Its system prompt lists the available skills' names and descriptions.
- **REQ-COM-043 [ ]** The charter tells agents:
  - Save a skill when you work out a non-obvious, repeatable procedure: it took several attempts, you'd need it again,
    or another role would.
  - Keep it short, concrete and command-level.
  - Update an existing skill rather than duplicating it.
  - Never put secrets in skills.
- **REQ-COM-044 [ ]** The Docs tab has a "Skills" group showing each skill with its author and source task
  (REQ-GUI-015). Stale skills are reviewed by the architect's health check (REQ-ROLE-004).
  - Test: save/update/validate, the slug, the symlink, the local tools, secret rejection, and worktree vs main writes.

## Sharing memories, and global scope (#40; human: "agents should be able to share skills and memories if required")
- **REQ-COM-045 [ ]** `share_memory(memory_id, to="team"|<handle>)`: the author makes one of its private memories
  team-visible, or mails a copy to one agent. `recall` by teammates then finds it. A feed event records it. Only the
  author can share its own private memory.
- **REQ-COM-046 [ ]** Global scope for things about the **human or the craft**, not one project (e.g. "trust codex",
  "decision questions always go in Needs you", skills useful everywhere).
  - Stored in `~/.troupe/global.db`, with skills in `~/.troupe/skills/`.
  - Every project's wake prompts include a compact "Global preferences" section, and `recall` searches global items
    too, labeled as global.
  - Promotion is human-approved: `propose_global(memory_id | skill_name, why)` creates a Needs-you card (Yes / No).
    Only Yes copies the item to global scope; No changes nothing.
  - Global skills are synced into each project's skill set at run time, without writing the user's own `~/.claude`
    or `~/.agents`. Writing there needs a separate, explicit second confirmation from the human, because it affects
    their non-troupe use.
  - The human can view, edit, demote and delete global items in the Memory tab (a "Global" filter; builds on
    REQ-COM-033). Agents can't edit or delete global items.
  - Test: with two temp projects, a promoted memory appears in the other project's prompt and recall, a promoted
    skill is usable there, and nothing is written to `~/.claude` or `~/.agents`.
- **REQ-COM-047 [ ]** Seed: once #40 ships, the PM proposes the human's existing preferences (trust codex, the
  Needs-you rule, visual taste) as the first global items. They go through the same Yes/No cards.

- **REQ-COM-048 [ ]** Retiring skills: the lead or architect may retire a **project** skill by moving it to
  `.agents/skills/_retired/<name>/` with a `retired_reason` (and who and when) in its frontmatter, which logs a feed
  event. Retired skills aren't loaded by any backend. Only the human can retire or delete **global** items
  (REQ-COM-046). Test: retire moves the dir and removes the skill from `list_skills` and the synced sets; a non-lead or
  non-architect gets `ERROR:`.

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — acceptance criteria for COM-024/025/032/033 (from backlog #4,#8,#12). Team room with no @mention
  wakes lead + pm only; other agents see it next time they wake.
- 2026-09-23 — REQ-COM-005 handles `role_N@project` (human request via pm).
- 2026-09-23 — COM-005 tagged #25; full handles wherever communication is shown (human, via pm msg #64).
- 2026-09-23 — COM-034..037 major decisions, human comment threads and outcomes (human request via pm; #27, after
  #8). New tool `update_decision`.
- 2026-09-23 — COM-040..044 skills (#39), COM-045..047 memory sharing and human-approved global scope (#40); human
  request via pm.
- 2026-09-23 — COM-048 skill retirement (pm's call; closes the open question).
- 2026-09-23 — COM-026 resolve questions answered in chat (#37; human request; charter line human-approved).
- 2026-09-24 — COM-013 FYI mail (#46).
- 2026-09-24 — COM-013 charter line recorded as human-approved (question #12).
- 2026-09-24 — COM-027..029 the PM as the human's single point of contact: escalations, PM triage tools, safety bypass
  and auto-forward (#65, human request).
- 2026-09-24 — COM-032: agents can't supersede pinned, human-authored or `preference` memories (QA finding on #8).
