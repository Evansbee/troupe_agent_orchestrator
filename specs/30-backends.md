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
- **REQ-BE-007 [ ]** Superseded by REQ-BE-012: fallback now comes from each agent's ordered `providers` list, not
  from `fallback_backend`/`fallback_model`.
- **REQ-BE-008 [ ]** Local backend streaming + reasoning display.
- **REQ-BE-009 [ ]** Cost for codex/local runs (token-based estimate with configurable prices).
- **REQ-BE-010 [x]** Per-agent `level` (from `team.yaml`, REQ-ENG-019) maps to each provider's reasoning control. (#20)
  | level | claude | codex | local |
  |---|---|---|---|
  | (empty) | no flag | no flag | nothing sent |
  | low / medium / high | `--effort <level>` | `-c model_reasoning_effort=<level>` | ignored |
  | max | `--effort max` | `-c model_reasoning_effort=xhigh` | ignored |
  - local ignores `level` because OpenAI-compatible servers disagree on the parameter and some reject unknown
    fields. The Settings view will show "not supported" next to level for local agents (#5).
  - Test: argv built for each provider/level pair.

- **REQ-BE-015 [x]** (#62; Principle 0) Codex runs are isolated from the human's personal Codex setup. Found
  2026-09-24: agents inherited `~/.codex/config.toml` plugins, including computer-use, browser, app tools and a notify
  hook into the Computer Use app. Those could drive the human's desktop and apps, and are a likely cause of the hangs.
  - Codex runs with `CODEX_HOME=.troupe/codex-home/<agent>`. That directory has a minimal generated `config.toml`
    (model/level from team.yaml plus the troupe MCP server, **no plugins, no notify**) and a symlink to the human's
    `~/.codex/auth.json` for login only.
  - **Allowlist, not just omission** (QA rejection of the first cut, 2026-09-24): a bare CODEX_HOME with no
    `[features]` section still auto-enables `apps`/`plugins` from the account linked via the symlinked auth, which
    sync in the human's ChatGPT-account connectors (Gmail send/delete/forward, Canva, ...) as tools. The generated
    config explicitly sets `apps`, `plugins`, `remote_plugin`, `computer_use`, `browser_use`, `browser_use_external`,
    `in_app_browser` and `tool_suggest` to `false` under `[features]`. The same names are repeated as
    `-c features.<name>=false` launch argv, so a stale or hand-edited `config.toml` in the home can't re-enable them.
    Only `mcp_servers.troupe` is configured — no other MCP server.
  - Session resume (`exec resume`) keeps working, because sessions live under the new home. BE-014 usage reading
    looks in these homes (and the app-server fallback).
  - This is the first layer of the codex sandbox (REQ-SAFE-050, #43). `runners.py` is protected, so the human
    approves the merge (REQ-SAFE-020).
  - Test:
    - the launch env sets `CODEX_HOME`;
    - the generated config has no `plugins` or `notify`, and explicitly disables every feature above with
      `mcp_servers` containing only `troupe` (parsed with `tomllib`);
    - the launch argv repeats the same feature disables as `-c` overrides;
    - live (skipped without a logged-in `codex` CLI): a real isolated CODEX_HOME shows nothing enabled in
      `codex plugin list` and every listed feature above as `false` in `codex features list`;
    - live: during a codex run, `ps` shows no ChatGPT.app, cua_node or node_repl children;
    - login and resume work.

## Provider usage caps and fallback (#38; human: "if we hit 50% (for example) claude usage, we can stop. Those agents
should be able to move to codex or even local models as defined in the setup yaml file. Ordered by preference.")
- **REQ-BE-011 [ ]** Usage tracking and caps.
  - `team.yaml` `provider_limits` = a percent cap per provider window, e.g. `claude: {five_hour: 50, seven_day: 50}`,
    `codex: {five_hour: 70, seven_day: 60}`. An omitted window, or `local`, means uncapped.
  - **Window names** are shared by caps, kv usage and the API: `five_hour`, `seven_day`, or `window_<minutes>` for any
    other length. Each provider's usage is normalized to `{used_percent, window_minutes, resets_at}` per window,
    plus `plan_type`, `observed_at` and `source` (BE-014).
  - **Stale samples:** caps are enforced against the last known sample until that window's `resets_at` passes.
    After that, the window counts as 0% until a fresh sample arrives. A cap naming a window the provider doesn't
    report is ignored, with a one-time event.
  - The engine tracks each provider's used % per window from what the backend reports: claude from `rate_limit`
    events (REQ-BE-001), codex from its rate-limit/token events if they carry a used percent.
  - A provider that reports nothing is treated as uncapped, with a one-time event saying so. Local is never capped.
  - Caps are account-wide: every project's service reads the provider's reported %, so they agree without coordinating.
  - Test: parsing each backend's usage events into per-window %, and cap comparison.
- **REQ-BE-016 [x]** (#72; human: "running this morning", milestone #2) MVP Claude cap, ahead of the full BE-011/012.
  - Per-window caps in `troupe.toml`: `[budget] claude_cap_5h_percent` and `claude_cap_7d_percent` (each 0 = off).
    If either is unset it falls back to `claude_cap_percent` (default 50), so older configs keep working. Troupe's own
    values are the human's choice (question #18): **5h = 80, 7d = 50**.
  - Each window is checked against its own cap. If either window's latest Claude usage is ≥ its cap, no new
    **autonomous** runs start for claude-backed agents, and "capped until" is the reset time of the window that
    tripped. Codex and local agents are unaffected, and in-flight runs finish.
  - The API exposes `cap_pct` per window (REQ-API Usage).
  - Chat with the human still runs, since they're present and can decide, but it's labeled as over the cap. This
    differs from real rate limits (REQ-ENG-016), which block chat, because the cap is troupe's own seatbelt.
  - It reuses the ENG-016 per-backend limit state with reason `cap` and resets when the window resets. Affected agents
    show `waiting_on` kind `providers` (REQ-ENG-046).
  - A feed event and a needs-help notification (REQ-ENG-047) fire when it engages. The API usage snapshot exposes the
    cap and "capped until HH:MM" for the TUI header (REQ-TUI-010).
  - When #38 ships, `provider_limits.claude` (BE-011) replaces these keys. The present `claude_cap_*` values are
    migrated to `five_hour`/`seven_day` once, with an event.
  - **Deploying:** the live values must be in `troupe.toml` before the reinstall. Otherwise the default 50 on a busier
    5h window caps every claude agent on the first tick, the lead included.
  - Test:
    - 5h over its cap with 7d under, and the reverse;
    - the fallback to `claude_cap_percent`;
    - off;
    - the reset clears it;
    - codex/local are unaffected;
    - chat is allowed.
- **REQ-BE-012 [ ]** Provider selection at each wake.
  - A provider is **available** when it is under all its caps, not rate-limited (REQ-ENG-016), and up. Up means its
    CLI is present and, for local, the server answers.
  - Each run starts on the first available entry in the agent's `providers` list (REQ-ENG-019).
  - A run in flight is never killed when a cap is crossed or a provider goes down.
  - **Switching** providers starts a fresh session. The first wake prompt on the new provider gets a **Continuity**
    section: current task brief, the agent's last run's final text, recent mail and private notes.
  - When the preferred provider is available again (its window reset, the server is back), the agent returns to it
    at its next wake, again with a Continuity section.
  - **All unavailable:** the agent doesn't run. Its wait state is `providers`, with the earliest reset time
    (REQ-ENG-046), and a needs-help notification fires (task #35).
  - **Reviewer diversity:** for QA and architect reviews, pick the first available entry whose provider differs from
    the one that wrote the task's last commit. If none differs, use the first available entry.
  - **Visibility:**
    - a feed event on every switch, e.g. "builder_1@troupe → codex (claude at 51% of 5h cap)";
    - the top-bar usage meters draw the cap line;
    - the agent's model chip shows the provider it's actually on, marked "fallback" when that isn't its first choice.
  - Test:
    - claude at 51% against a 50% cap moves the next run of a `[claude, codex]` agent to codex and logs the event;
    - an in-flight run isn't killed;
    - the agent is back on claude once usage is under the cap;
    - with every provider out, the agent waits with a reset time;
    - rate-limited and down providers trigger the same fallback;
    - the Continuity section appears on the first run after a switch;
    - reviewer diversity.

- **REQ-BE-014 [x]** (#52; human: "you really need to dig in on how to get the codex usage numbers") Codex usage.
  - Sources checked (see `docs/codex-usage.md` for the full writeup): `codex exec --json` stdout does not carry
    `rate_limits` (only per-run token totals); the CLI has no `usage`/`status` subcommand (codex-cli 0.156.1).
    The session rollout files (`<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*-<thread-id>.jsonl`, thread id = the
    session id troupe already stores per codex agent) do carry it, in each `token_count` event's
    `payload.rate_limits`: `primary`/`secondary` windows with `used_percent`, `window_minutes`, `resets_at`, plus
    `plan_type` and `rate_limit_reached_type`. `codex app-server --stdio`'s `account/rateLimits/read` JSON-RPC
    method returns the same shape (camelCase) read-only, with no completed run needed and no model quota spent.
  - Used: the app-server RPC is primary (read-only, 5s timeout, process torn down immediately after the one
    response — never left running). Rollout-file parsing (bounded to each file's last 4MB) is the fallback when the
    RPC is unavailable, times out, or errors.
  - **Isolated CODEX_HOME integration (BE-015, #62):** since each codex agent runs under its own
    `.troupe/codex-home/<agent>` rather than a shared `~/.codex`, both readers respect that. The post-run hook and
    the rollout fallback look in the specific agent's `codex_home_dir(cfg, agent.id)/sessions`, never a different
    agent's home or the human's real `~/.codex`; multiple codex agents' latest samples are merged by
    `observed_at`. The app-server RPC subprocess is launched with `CODEX_HOME` set to a codex agent's isolated
    home (built/refreshed via `ensure_codex_home` first, so its `[features]` allowlist is always in place before
    the read happens) — it never inherits the engine process's own environment's `CODEX_HOME` or the human's
    `~/.codex`. Same account, since `auth.json` is symlinked identically into every agent's home; only the
    config differs.
  - `src/troupe/usage.py` reads it after each codex run (`engine.py`'s `_run`, right after the runner returns —
    kept out of `runners.py` since that file is protected under REQ-SAFE-020 and this plumbing doesn't need to
    live there) and on a timer off the engine's own loop, at most once a minute, only while a codex agent is
    configured. It stores kv `usage:codex` in
    the same shape as Claude's usage (used % per window, window length, reset time, plan, plus `observed_at`/
    `source`), which feeds BE-011 caps. A newer-observed sample never overwrites a fresher cached one.
  - The top bar shows Codex meters next to Claude's, reading kv only (no file I/O in draw code); a meter is marked
    stale if its sample is older than 120s. `api.py`'s `Data.usage()` also serializes `usage:codex`'s canonical
    per-window fields (`used_percent`/`window_minutes`/`resets_at`) plus `plan`/`age`/`source` for the codex
    provider entry — no new API method, just an enrichment of the existing `usage` read.
  - `rate_limit_reached_type` set, or `used_percent ≥ 100`, marks codex limited until `resets_at` (REQ-ENG-016); a
    missing reset falls back to `observed_at + 900s` so a stale reached-state can't wedge the backend forever.
  - Privacy (Principle 0): only the `rate_limits` and `token_count` fields are read, through an explicit allowlist
    (`usage.normalize`) that drops `credits` and any other nested field before it reaches disk. Conversation content
    is never copied (tests assert it).
  - Test (`tests/test_codex_usage.py`): fixture rollout files for primary only, primary + secondary, a missing
    file, malformed lines, the app-server RPC handshake (allowlisted response, timeout reaps the child process),
    the post-run hook, the minute timer's RPC-then-fallback order, stale/missing-data cache retention, the RPC
    subprocess's `CODEX_HOME` env matching the agent's isolated home rather than an inherited/real one, and the
    post-run hook ignoring a rollout sitting under a different agent's isolated home.

## Web tools for the local backend (#41)
- **REQ-BE-013 [ ]** The local tool loop gains `web_search(query, n=8)` → title/url/snippet list and
  `web_fetch(url, max_chars)` → readable text.
  - HTML is stripped to text without a heavy new dependency (httpx is already present).
  - Results carry the untrusted-content marker (REQ-SAFE-002).
  - Provider is set in `troupe.toml` `[research]`: `search = "duckduckgo" | "searxng" | "brave"`, plus `url` and
    `api_key`. The default is keyless DuckDuckGo HTML, with a polite rate limit (≥1 s between requests) and an honest
    user-agent.
  - Enabled only for roles whose sandbox profile allows network and that need it: researcher, and pm/gadfly when
    they're on local.
  - Test: mocked httpx for both tools and each search provider's parser, plus a live test marked skip-if-offline.

## Changelog
- 2026-09-23 — written from the bootstrap implementation.
- 2026-09-23 — rate-limit handling is specified in REQ-ENG-016 (runners detect it, the engine backs off).
- 2026-09-23 — BE-010 level mapping (claude --effort, codex model_reasoning_effort, local ignored).
- 2026-09-23 — BE-011/012 provider caps and ordered fallback (human; #38), absorbing BE-007. BE-013 local web tools
  (researcher, #41).
- 2026-09-23 — BE-014 Codex usage from rollout `rate_limits` (#52, human request).
- 2026-09-24 — BE-014 implemented: app-server RPC primary source, rollout-file fallback, top-bar meters, BE-011/
  ENG-016 wiring (#52).
- 2026-09-24 — BE-011 shared window names (five_hour/seven_day/window_<minutes>) and stale-sample rule (#52/#38).
- 2026-09-24 — BE-015 isolated CODEX_HOME for agents (#62).
- 2026-09-24 — BE-016 MVP Claude cap (#72).
- 2026-09-24 — BE-016 split into per-window caps (5h/7d); troupe's values 80/50 (human, question #18).
- 2026-09-24 — BE-014 integrated with BE-015: both the app-server RPC and the rollout-file fallback now read
  through each codex agent's isolated CODEX_HOME (#52), never the human's ~/.codex.

## Open questions
- None outstanding for backends. (Codex usage source resolved by #52/BE-014: app-server RPC primary, rollout-file
  fallback; see `docs/codex-usage.md`.)
