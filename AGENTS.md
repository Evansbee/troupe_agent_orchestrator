# Working on troupe (for agents)

troupe is built by its own troupe. You are editing the orchestrator's source code while a stable,
installed copy of it (`uv tool install`) runs the team. Your changes take effect only after they merge
and the human reinstalls, so a bug you introduce won't break the session you're running in.

## Layout
- `src/troupe/engine.py` — scheduler: wake reasons, dispatch, runs, merges
- `src/troupe/team.py` — the agent tool API (exposed via `mcp_server.py`)
- `src/troupe/store.py` — SQLite schema + data access (shared by every process)
- `src/troupe/runners.py` — claude / codex / local backends
- `src/troupe/roles.py` — role definitions and prompts (the charter)
- `src/troupe/gui/` — raylib desktop app (`core.py` toolkit, `app.py` shell, `views.py` tabs, `data.py`, `theme.py`)
- `specs/` — the source of truth. Keep `[x]`/`[ ]` status markers accurate when you ship something.

## Commands
- `uv sync` — install deps (run this first in a fresh worktree)
- `uv run pytest` — tests
- `uv run troupe doctor` — backend availability
- Verify GUI changes **visually**: `TROUPE_SHOT=/tmp/shot.png TROUPE_TAB=Board uv run troupe gui` renders
  about 90 frames against the current directory's `.troupe` and saves a PNG you can look at. Run it from a
  directory that has a `.troupe/` (for example the main checkout).

## Conventions
- Python 3.12, type hints, small functions, no new dependencies without a reason in your task summary.
- Match the surrounding style: short docstrings, sparse comments that explain *why*.
- The GUI is immediate-mode: draw functions run every frame. Never do blocking I/O in draw code. Read
  through `gui/data.py` snapshots, and cache anything expensive.
- Store schema changes must be additive (`ALTER TABLE ... ADD COLUMN` guarded in `Store.__init__`), because
  existing `.troupe/troupe.db` files must keep working.
