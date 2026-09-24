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

- **REQ-BE-015 [ ]** (#62; Principle 0) Codex runs are isolated from the human's personal Codex setup. Found
  2026-09-24: agents inherited `~/.codex/config.toml` plugins, including computer-use, browser, app tools and a notify
  hook into the Computer Use app. Those could drive the human's desktop and apps, and are a likely cause of the hangs.
  - Codex runs with `CODEX_HOME=.troupe/codex-home/<agent>`. That directory has a minimal generated `config.toml`
    (model/level from team.yaml plus the troupe MCP server, **no plugins, no notify**) and a symlink to the human's
    `~/.codex/auth.json` for login only.
  - Session resume (`exec resume`) keeps working, because sessions live under the new home. BE-014 usage reading
    looks in these homes (and the app-server fallback).
  - This is the first layer of the codex sandbox (REQ-SAFE-050, #43). `runners.py` is protected, so the human
    approves the merge (REQ-SAFE-020).
  - Test:
    - the launch env sets `CODEX_HOME`;
    - the generated config has no `plugins` or `notify`;
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

- **REQ-BE-014 [ ]** (#52; human: "you really need to dig in on how to get the codex usage numbers") Codex usage.
  - The source to verify first: the `token_count` events' `rate_limits` object in Codex's session rollout files
    (`~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread-id>.jsonl`, for the thread ids troupe stores as codex sessions).
    It has `primary` and `secondary` windows, each with `used_percent`, `window_minutes` and `resets_at`, plus
    `plan_type` and `rate_limit_reached_type`.
  - Other sources to check: CLI status commands, newer `--json` event types, and the app-server protocol. The builder
    records which sources exist, which one is used, why, and the fallback, in `docs/codex-usage.md` and in this REQ.
  - `src/troupe/usage.py` reads it after each codex run and on a timer (at most once a minute). It stores kv
    `usage:codex` in the same shape as Claude's usage (used % per window, window length, reset time, plan), which
    feeds BE-011 caps.
  - The top bar shows Codex meters next to Claude's, reading kv only.
  - `rate_limit_reached_type` set, or `used_percent ≥ 100`, marks codex limited until `resets_at` (REQ-ENG-016).
  - Privacy (Principle 0): only the `rate_limits` and `token_count` fields are read. Conversation content in `~/.codex`
    is never copied (a test asserts it).
  - Test: fixture rollout files for primary only, primary + secondary, a missing file, and malformed lines.

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
- 2026-09-24 — BE-011 shared window names (five_hour/seven_day/window_<minutes>) and stale-sample rule (#52/#38).
- 2026-09-24 — BE-015 isolated CODEX_HOME for agents (#62).

## Open questions
- Codex usage source: `codex exec --json` stdout appears not to carry limits, but the session rollout files do (#52
  verifies this; BE-014). Until #52 ships, codex counts as uncapped, per BE-011.
