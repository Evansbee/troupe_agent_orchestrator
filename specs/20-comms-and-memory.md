# Communication, questions & memory

Code: `src/troupe/team.py` (tool API), `mcp_server.py` (MCP exposure), `store.py`.

## Tools every agent has (MCP server `troupe`, one stdio process per agent run)
`send_message, check_inbox, ask_human, propose_idea, create_task, update_task, list_tasks, get_task,
complete_task, review_task, remember, recall, set_status, team` (+ `update_decision`, REQ-COM-034..036).
- **REQ-COM-001 [x]** Identity comes from `TROUPE_AGENT`; the same API is used natively by local models.
- **REQ-COM-002 [x]** Tool results are short plain text written for an LLM; errors start with `ERROR:` and say
  what to do instead.
- **REQ-COM-005 [ ]** (#25) Every agent has a **handle** `<role>_<N>@<project>` (human: so it's clear who is talking
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
- **REQ-COM-025 [ ]** Keyboard answering in the inbox. (#12)
  - Keys 1–9 pick that option on the hovered question card, or the top card if none is hovered.
  - Ignored whenever a text input has focus (typing "3" in a reply never answers a question).
  - The answered card gets the same feedback as a click (toast + card leaves).

## Memory
- **REQ-COM-030 [x]** `remember(kind=decision|note|fact|idea|preference, rationale=…)`; team-visible unless
  `private`. Recent decisions are injected into every wake prompt.
- **REQ-COM-031 [x]** `recall(query)` searches memories and answered questions.
- **REQ-COM-032 [ ]** Supersede: `remember(..., supersedes=<id>)` marks memory `<id>` superseded by the new one. (#8)
  - Superseded memories are excluded from wake prompts and `recall` results by default (`recall(...,
    include_superseded=True)` shows them); they are never deleted.
  - Superseding a nonexistent id, or one already superseded, returns `ERROR:` with the current successor's id.
  - The Memory view shows superseded items struck through with a link to their successor.
- **REQ-COM-033 [ ]** Human memory controls in the Memory view. (#8)
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
    human comment is open records this outcome automatically.
  - `reverted`: the decision is withdrawn and, like a superseded one, excluded from prompts and default recall.
  - Setting an outcome with no open human comment is allowed (the lead cleaning up). An invalid outcome returns
    `ERROR:` listing the valid ones.
- **REQ-COM-037 [ ]** Open human comments (no outcome after them) appear at the top of the wake prompt of the owner,
  lead and pm, under "The human commented on these decisions". The owner is told it owns the response, and an open
  comment counts as work for the owner's wake (REQ-ENG-010 `messages`). They drop out once an outcome is recorded.
  - Test: routing (author, lead, pm, dedup, owner fallback), outcome permissions, the auto-superseded outcome, and
    open-comment prompt inclusion and removal.

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — acceptance criteria for COM-024/025/032/033 (from backlog #4,#8,#12). Team room with no @mention
  wakes lead + pm only; other agents see it next time they wake.
- 2026-09-23 — REQ-COM-005 handles `role_N@project` (human request via pm).
- 2026-09-23 — COM-005 tagged #25; full handles wherever communication is shown (human, via pm msg #64).
- 2026-09-23 — COM-034..037 major decisions, human comment threads and outcomes (human request via pm; #27, after
  #8). New tool `update_decision`.
