# Communication, questions & memory

Code: `src/troupe/team.py` (tool API), `mcp_server.py` (MCP exposure), `store.py`.

## Tools every agent has (MCP server `troupe`, one stdio process per agent run)
`send_message, check_inbox, ask_human, propose_idea, create_task, update_task, list_tasks, get_task,
complete_task, review_task, remember, recall, set_status, team`.
- **REQ-COM-001 [x]** Identity comes from `TROUPE_AGENT`; the same API is used natively by local models.
- **REQ-COM-002 [x]** Tool results are short plain text written for an LLM; errors start with `ERROR:` and say
  what to do instead.
- **REQ-COM-003 [x]** Permissions: only the Lead (or human) can re-prioritize/re-assign or move tasks to
  arbitrary statuses; assignees can block/unblock their own task; only QA/Lead can review.

## Mailboxes
- **REQ-COM-010 [x]** `send_message(to=…)` accepts an agent id, a role (fan-out to all of that role),
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

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — acceptance criteria for COM-024/025/032/033 (from backlog #4,#8,#12). Team room with no @mention
  wakes lead + pm only; other agents see it next time they wake.
