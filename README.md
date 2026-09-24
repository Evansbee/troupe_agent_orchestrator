# troupe

A team of AI agents that builds software with you. Run it in a directory. A Lead, a Product Manager, a Spec
Writer, a Designer, Builders, QA and a Gadfly spin up. They talk to you and to each other, keep a backlog
and a decision log, and ship code through isolated git worktrees. A native desktop app shows all of it
live.

```
uv tool install --reinstall /path/to/troupe     # installs the `troupe` command
cd my-new-project && troupe up                  # init (if needed) + engine + GUI
```

## Requirements
- Python 3.12+, [uv](https://docs.astral.sh/uv/), git
- At least one backend: [`claude`](https://claude.com/claude-code) (Claude Code CLI), [`codex`](https://github.com/openai/codex),
  or an OpenAI-compatible local server (LM Studio on `:1234` by default)
- `troupe doctor` checks all of these

## Commands
| | |
|---|---|
| `troupe up` | init if needed, start the engine + GUI |
| `troupe init [--name N]` | create `.troupe/troupe.toml`, git repo, and the first PM greeting |
| `troupe engine` / `troupe gui` | run them separately (the GUI attaches to a running engine) |
| `troupe status` | team, board and open questions in the terminal |
| `troupe say <agent> <text>` | chat to an agent from the terminal |
| `troupe doctor` | check the backends |

## How it works
- **State**: `.troupe/troupe.db` (SQLite) holds mail, tasks, questions, memory, runs and events.
  `.troupe/troupe.toml` holds the team roster (backend + model per agent) and the budget.
- **Agents** are headless CLI sessions (`claude -p`, `codex exec`) or a local-model tool loop. They are
  woken for a reason (chat, mail, task, review, proactive check-in) and use MCP tools to talk and act:
  `send_message`, `ask_human`, `propose_idea`, `create_task`, `complete_task`, `review_task`, `remember`,
  `recall`, and more.
- **Tasks** flow `backlog → ready → in_progress → review → done`. Each builder task gets its own git
  worktree and branch. QA verifies the work in that worktree, and approval merges it into main.
- **You** chat with anyone (usually the PM and Spec Writer), answer questions in **Needs you**, and steer
  the board.

The specs in [`specs/`](specs/) describe the intended behavior; items marked `[ ]` are not built yet.
troupe is developed by its own troupe.
