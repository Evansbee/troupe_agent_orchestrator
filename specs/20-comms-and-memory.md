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
- **REQ-COM-024 [ ]** A "Team" chat room: one message from the human reaches everyone (or @mentioned agents),
  replies interleave in one conversation.
- **REQ-COM-025 [ ]** Keyboard answering in the inbox (1-9 picks an option on the focused card).

## Memory
- **REQ-COM-030 [x]** `remember(kind=decision|note|fact|idea|preference, rationale=…)`; team-visible unless
  `private`. Recent decisions are injected into every wake prompt.
- **REQ-COM-031 [x]** `recall(query)` searches memories and answered questions.
- **REQ-COM-032 [ ]** Supersede: a new decision can mark an old one superseded; superseded decisions are hidden
  from prompts but kept in history.
- **REQ-COM-033 [ ]** The human can pin preferences/decisions and edit or delete memories from the GUI.

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
