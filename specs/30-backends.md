# Backends — claude, codex, local

Code: `src/troupe/runners.py`.

- **REQ-BE-001 [x]** `claude`: `claude -p --output-format stream-json --verbose --append-system-prompt …
  --mcp-config … --dangerously-skip-permissions [--strict-mcp-config] [--model] [--effort] [--resume <session>]`,
  prompt on stdin. Parses init/assistant/user/result/rate_limit events.
- **REQ-BE-002 [x]** `codex`: `codex exec --json --dangerously-bypass-approvals-and-sandbox -c mcp_servers.troupe…`
  (`exec resume <thread>` for continuity); role instructions prepended to the first prompt of a session.
- **REQ-BE-003 [x]** `local`: OpenAI-compatible `/chat/completions` with a native tool loop: troupe tools +
  `read_file/list_files/search_files` (+ `write_file` except gadfly/qa), scoped to the cwd; compact history
  persisted in kv as the "session".
- **REQ-BE-004 [x]** Sessions resume across wakes; if resume fails before any output, retry fresh.
- **REQ-BE-005 [x]** Every run's raw JSONL is saved to `.troupe/runs/<run>-<agent>.jsonl`.
- **REQ-BE-006 [x]** Stop kills the whole process group.
- **REQ-BE-007 [ ]** Fallback: if an agent's backend is unavailable (e.g. LM Studio down), fall back to a
  configured alternative (`fallback_backend`, `fallback_model`).
- **REQ-BE-008 [ ]** Local backend streaming + reasoning display.
- **REQ-BE-009 [ ]** Cost for codex/local runs (token-based estimate with configurable prices).
- **REQ-BE-010 [ ]** Per-agent `level` (from `team.yaml`, REQ-ENG-019) maps to each provider's reasoning control. (#20)
  | level | claude | codex | local |
  |---|---|---|---|
  | (empty) | no flag | no flag | nothing sent |
  | low / medium / high | `--effort <level>` | `-c model_reasoning_effort=<level>` | ignored |
  | max | `--effort max` | `-c model_reasoning_effort=xhigh` | ignored |
  - local ignores `level` because OpenAI-compatible servers disagree on the parameter and some reject unknown
    fields. The Settings view shows "not supported" next to level for local agents.
  - Test: argv built for each provider/level pair.

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — rate-limit handling is specified in REQ-ENG-016 (runners detect it, the engine backs off).
- 2026-09-23 — BE-010 level mapping (claude --effort, codex model_reasoning_effort, local ignored).
