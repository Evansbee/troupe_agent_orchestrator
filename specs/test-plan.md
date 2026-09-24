# Verification plan

The numbered specs define behavior; this file maps shipped `[x]` requirements to repeatable
verification. A mapping is not a claim that a manual check has passed. Automated checks below run
without model credentials, using fake runners and temporary git repositories. Run `uv sync`, then
`uv run pytest`. New coverage lives in separate files to avoid collisions with feature tasks.

Test names below omit the `test_` prefix. **E** = `tests/test_engine_coverage.py`, **C** =
`tests/test_core.py`, **T** = `tests/test_team_coverage.py`. Manual checks are pending unless an
execution record explicitly says otherwise. Composite requirements need both their automated and
manual checks; mocks do not establish real CLI/model interoperability.

## Engine — specs/10-engine.md

| Requirement | Verification and expected effect |
|---|---|
| REQ-ENG-001 | E `cli_starts_once_and_attaches`; manual: in a disposable initialized project, run `troupe engine`, attach `troupe gui`, then separately exercise `troupe up`; GUI and headless modes share state and stop cleanly. |
| REQ-ENG-002 | E `shared_store_commands_recovery_and_heartbeat`: separate Store instances see writes, WAL is active, commands consumed exactly once. Manual: separate GUI/engine processes observe the same task updates. |
| REQ-ENG-003 | E `cli_starts_once_and_attaches`: second start attaches without another engine. Manual: repeat from a second terminal and check one engine PID; concurrent-start race is not covered. |
| REQ-ENG-004 | E `shared_store_commands_recovery_and_heartbeat`: abandoned running run becomes interrupted and agent idle. |
| REQ-ENG-005 | E `shared_store_commands_recovery_and_heartbeat`: tick writes heartbeat. Manual: stop the disposable engine, wait >5s, see Engine offline. |
| REQ-ENG-010 | E `wake_precedence_debounce_and_pause`, `proactive_requires_elapsed_cadence_and_external_change`; C `dispatch_assigns_one_task_per_builder`. Manual: ready task wakes its owner after mail is consumed; inspect reason in Agent history. |
| REQ-ENG-011 | E `wake_precedence_debounce_and_pause`, `chat_consumed_by_other_wake_gets_final_reply`: chat suppresses other reasons even during debounce; chat consumed by a poke produces final reply. |
| REQ-ENG-012 | E `concurrency_reserves_two_extra_chat_slots`: autonomous cap and two extra chat slots enforced in tick. |
| REQ-ENG-013 | E `budget_limits_ignore_chat_count_and_clear_throttle`: hourly and cost caps stop budget eligibility, chat excluded from run count, reason cleared after recovery. Manual: top bar displays Throttled and correct reason at each cap. |
| REQ-ENG-014 | E `wake_precedence_debounce_and_pause`: paused agents still eligible for chat, autonomous wakes suppressed. Manual: Pause/Resume changes actual dispatch and leaves chat responsive. |
| REQ-ENG-015 | E `failures_requeue_mail_and_backoff_to_cap`: 30/60/120/240/480/600/600s retry delays, unread mail restored, candidates suppressed. |
| REQ-ENG-016 | R = `tests/test_rate_limits.py`: R `provider_events` (claude/codex limit detection), `reset_formats`, `backend_gate_survives_restart_and_expires`, `limited_run_requeues_without_failure_or_attempt`, `gui_snapshot_label_expires`, `event_order_persists_authoritative_expiry`, `persisted_provenance_across_restart_and_independent_reports`. Manual (verified in #2 review): top-bar limit pill and the queued-chat "limited until" label. |
| REQ-ENG-020 | E `prompts_include_role_and_context`: role prompt and roster included in charter. |
| REQ-ENG-021 | E `prompts_include_role_and_context`: mail, brief/acceptance, other tasks, board, questions, decisions/reasons, own private notes, team and changes present; others' private notes absent. E `main_checkout_autocommit_policy` checks idle-builder instruction. |
| REQ-ENG-030 | C `lead_tasks_are_ready_others_backlog`; manual: human +Task creates Ready. |
| REQ-ENG-031 | C `dispatch_assigns_one_task_per_builder`; E `dispatch_waits_for_dependency_and_skips_disabled`. Known gap reproduced during #14: a roster containing only one builder assigns both ready tasks to it. Follow-up bug filed with Lead; blocked/review workload also needs cap verification. |
| REQ-ENG-032 | E `worktree_review_reject_and_merge`: branch naming and isolated task worktree. |
| REQ-ENG-033 | E `worktree_review_reject_and_merge`: completion commits, QA runs in task tree, rejection mails feedback, approval merges with two parents and removes tree. |
| REQ-ENG-034 | E `conflict_aborts_and_returns_to_builder`: divergent file edits conflict, main unchanged, merge aborted, tree kept and builder receives merge-main instructions. |
| REQ-ENG-035 | E `unfinished_task_backs_off_then_escalates`: first retry delayed 45s, configured attempt cap blocks and mails Lead. |
| REQ-ENG-036 | E `main_checkout_autocommit_policy` (builder/spec): idle builder gets no-edit instruction and no automatic commit; spec changes are committed. |
| REQ-ENG-038 | `tests/test_worktree_setup.py`: real temporary git repositories verify config default/command, setup once across restart, stdout/stderr and exit logging, failure note/prompt with provider continuation, responsive event loop and stop during setup, merged branch deletion, and startup removal of closed/missing trees while preserving open/external worktrees. |
| REQ-ENG-040 | `tests/test_merge_gate.py`: temp-git pass/fail/timeout/empty/no-branch, current main integrated before check, conflict bounce, unchanged main on failure, output tail/full log, stale-tree protection, worker responsiveness/chat/single-check, stop, config validation, and cached chip reset. Board checking/failed chips visually inspected in `/tmp/troupe-t15-board.png`. |
| REQ-ENG-019 / REQ-ENG-041 (existing roles) / REQ-BE-010 | `tests/test_team_yaml.py`: legacy migration/one-time notices, ordered/shorthand providers, derived IDs, comment round-trip, validation, independent last-good reloads, in-flight/session protection, task reassignment, CLI init/default roster/local detection, MCP fallback, and all backend/level pairs. Visual error pill verified in isolated GUI (`/tmp/troupe-t20-error.png`). Architect/researcher rows remain #36/#41; local Settings label remains #5. |

## Safety — specs/05-safety.md

| Requirement | Verification and expected effect |
|---|---|
| REQ-SAFE-001 | `tests/test_charter.py`: `charter_reaches_provider` (7 roles × claude/codex/local, the rendered system prompt starts with the approved Principle 0 text) and `every_wake_has_footer` (6 wake reasons end with the reminder). Rollout: resumed Codex sessions need a New session to receive the charter (#42). |
| REQ-SAFE-004 | Charter text only: covered by REQ-SAFE-001's tests (the appendix text includes both duties). The mechanics are #45. |

## Communication — specs/20-comms-and-memory.md

| Requirement | Verification and expected effect |
|---|---|
| REQ-COM-001 | T `mcp_environment_identity`: entry point uses TROUPE_ROOT/TROUPE_AGENT and stdio. Manual: real MCP and local tool calls identify their configured agent in mailbox/event records. |
| REQ-COM-002 | T `errors_offer_recipient_and_status_guidance`, `mail_routing_events_and_inbox_consumption`: plain text success, ERROR prefix and corrective guidance for invalid recipient/status. Manual: inspect remaining tool errors for actionable language. |
| REQ-COM-003 | T `permission_boundaries`; C `only_lead_changes_priority`: owner block/unblock, unauthorized status/assignment/priority/review denied, Lead review and human priority allowed. |
| REQ-COM-010 | T `mail_routing_events_and_inbox_consumption` covers id/human/role/team and feed events; C `message_fanout_by_role`; E `wake_precedence_debounce_and_pause` covers mail wake. |
| REQ-COM-011 | T `mail_routing_events_and_inbox_consumption` for check_inbox; E `chat_consumed_by_other_wake_gets_final_reply` for wake delivery. |
| REQ-COM-020 | E `chat_consumed_by_other_wake_gets_final_reply`; manual Chat procedure below verifies real model text arrives. |
| REQ-COM-021 | C `question_limit_and_answer_delivery`; manual Needs you: options and free-text answer each remove card and deliver mail to asker; fifth open question rejected. |
| REQ-COM-022 | T `idea_options_and_dismissal_notification`: idea kind and four standard choices. |
| REQ-COM-023 | T `idea_options_and_dismissal_notification`: GUI data action removes open question and sends judgment instruction. |
| REQ-COM-026 | `tests/test_resolve_question.py`: chat resolution, permissions, closed/unknown/invalid inputs, additive migration, inbox compatibility, concurrent answer protection, recall, cached card removal/toast signal, MCP registration, and open-question chat hints. Visual check: isolated GUI run confirmed card removal and "Answered in chat ✓" toast (`/tmp/troupe-t37-answer.png`). Optional "also asked in chat" badge not implemented. |
| REQ-COM-030 | T `memory_visibility_rationale_and_answer_search`; E `prompts_include_role_and_context`: rationale retained, private memory isolated and recent decisions injected. |
| REQ-COM-031 | T `memory_visibility_rationale_and_answer_search`; C `recall_finds_decisions`: memory and answered-question keyword search. |

## Backends — specs/30-backends.md

Use a disposable project and low-cost, brief prompts with configured provider credentials. Record
backend/version, run ID, command, observed result and log path. These are manual integration checks,
not exercised by fake-runner engine tests.

| Requirement | Manual verification and expected effect |
|---|---|
| REQ-BE-001 | Wake a Claude agent; inspect spawned argv for print/stream-json/verbose/system/MCP/permissions/model/effort and resume options. Confirm stdin prompt, session init, assistant text, tool result, final cost and rate-limit event parsing in transcript/DB using captured fixtures where quotas cannot be triggered safely. |
| REQ-BE-002 | Wake Codex twice; inspect exec JSON/MCP config and second exec resume thread. First prompt includes role instructions; transcript text/tool/error events visible. |
| REQ-BE-003 | With local endpoint active, ask for a file read/search and a team message; verify native tools, cwd containment and persisted compact history. Builder can write inside cwd; QA/gadfly lack write_file. Try ../ and absolute paths outside cwd; access denied. |
| REQ-BE-004 | Two wakes recall an earlier unique token using same session; invalidate session and confirm pre-output resume failure retries fresh once, while failure after output does not duplicate execution. |
| REQ-BE-005 | For each backend, inspect `.troupe/runs/<run>-<agent>.jsonl`; raw events exist and correspond to transcript, including failure runs. |
| REQ-BE-006 | Start a harmless long-running tool with a child process, click Stop; parent and child PIDs exit and no further transcript arrives. |

## GUI — specs/40-gui.md

Use a disposable project seeded with agents idle/running/disabled, unread chats, tasks in every status,
questions/ideas, long markdown mail, memory of each kind, docs, and successful/failed runs. Do not inject
fixtures or control the live dogfood team. Capture each tab with
`TROUPE_SHOT=/tmp/troupe-Board.png TROUPE_TAB=Board uv run troupe gui` from the project directory;
use equivalent paths/tab names for other views. Inspect PNGs at native scale. A still image does not
verify clicks, animation, keyboard input or frame rates: run the paired interactive checks.

| Requirement | Verification and expected effect |
|---|---|
| REQ-GUI-001 | TROUPE_SHOT Board: logo/project, state pill, counters and usage meters legible. Manual: exercise Live/Paused/Throttled/offline, +Task, Pause/Resume and ⌘P; compare counts/cost to DB. |
| REQ-GUI-002 | TROUPE_SHOT Agent: roles, backend/model, activity/status, task/last-run/unread counts and parked diagnosis visible. Manual: working ring animates; left-click selects Agent, right-click opens correct Chat. |
| REQ-GUI-003 | TROUPE_SHOT Chat with populated and empty Needs you; manual choose option, submit free text, dismiss and verify mail/card removal. |
| REQ-GUI-004 | Manual: click each of seven tabs and use ⌘1–7; each selects corresponding view with no lost state. Capture each using TROUPE_SHOT. |
| REQ-GUI-005 | Manual: background window, deliver question/chat, see macOS notification; focus window and verify toast behavior without duplicate notifications after refresh. |
| REQ-GUI-006 | Manual: measure frame timing in idle and working/animating states; approximately 20 and 60 fps respectively; interaction remains responsive. |
| REQ-GUI-007 | TROUPE_SHOT command creates valid PNG and exits; manual F12 produces readable screenshot under `.troupe/`. |
| REQ-GUI-010 | TROUPE_SHOT Chat empty/populated: PM/Spec/Lead order, suggestions, markdown and working bubble/activity. Manual Enter sends, Shift+Enter adds newline, final backend text replaces waiting state and scroll remains usable. |
| REQ-GUI-011 | TROUPE_SHOT Pulse: agent constellation and YOU, feed visible. Manual send mail: curved moving particles and recent edge glow; feed filters show matching events. |
| REQ-GUI-012 | TROUPE_SHOT Board: all five columns, blocked/approved grouping, card metadata readable. Manual open modal; details, notes/actions correct; added note reaches assignee as mail. |
| REQ-GUI-013 | TROUPE_SHOT Mail: agent filters and message list; manual filter then expand long markdown, confirm full body. |
| REQ-GUI-014 | TROUPE_SHOT Memory: each kind and rationale readable; manual switch kinds and confirm items correspond. |
| REQ-GUI-015 | TROUPE_SHOT Docs: README/specs/design/docs markdown. Manual edit fixture doc externally, observe reload and unchanged navigation. |
| REQ-GUI-016 | TROUPE_SHOT Agent: controls/history/transcript/tasks/memory/mail. Manual Chat, Wake, Stop, Enable/Disable, New session; verify effects in DB/next run. Select historical runs; tool/text/result/error contents follow selection. |

## Open requirements

Unshipped `[ ]` requirements are not counted as verified by this matrix. When shipping one, use its
acceptance sub-bullets in specs/10, 20 or 40 (including negative/failure paths), add exact test names here,
and update its spec marker only after review. In particular, upcoming board Done overrides must follow
REQ-ENG-039 and the merge gate REQ-ENG-040; current merge tests do not claim to cover those features.
Backend fallback/streaming/cost requirements remain pending in specs/30.

## Execution record

- 2026-09-23, task #14: `uv run pytest -q` — 31 passed (7 existing, 24 new). New tests cover engine
  scheduling/lifecycle/prompts/git and team permissions/mail/memory. No real backend calls or GUI manual
  checks executed in this task; those rows describe the repeatable release/review procedure.

- 2026-09-23: isolated dispatch reproduction with a single-builder roster assigned both A and B to
  builder-1, violating REQ-ENG-031. Reported separately; production code unchanged by this test-plan task.
