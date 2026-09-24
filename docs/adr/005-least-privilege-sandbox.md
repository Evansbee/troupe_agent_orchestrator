# Least-privilege sandbox for every agent run (#43, REQ-SAFE-050/051)

`--dangerously-skip-permissions` (claude) and `--dangerously-bypass-approvals-and-sandbox` (codex)
are gone from `runners.py`. Both backends now run under real, verified enforcement instead of a
prompt-only guard. `src/troupe/sandbox/` holds the shared per-role policy (`RoleProfile`,
`writable_roots`) and each backend's arg-building (`codex.py`, `claude.py`, `macos.py`).

## The headline finding: a sandboxed process can't self-restrict further, so sandbox-exec can't wrap Bash

The original plan was a `sandbox-exec` outer layer wrapping *both* backends' whole process tree —
the uniform enforcement REQ-SAFE-050 asks to "evaluate". Testing it against real tools found a
blocker: **a process already running inside a Seatbelt jail cannot call `sandbox_apply`/
`sandbox_init` on itself to self-restrict further.** Confirmed with a fully permissive `(allow
default)` outer profile (so it wasn't about anything this specific profile denies):

```
$ sandbox-exec -f permissive.sb sh -c 'swift build'
error: 'swifttest': Invalid manifest (compiled with: [...swiftc manifest compile...])
sandbox_apply: Operation not permitted

$ sandbox-exec -f permissive.sb sh -c 'codex exec ...'
sandbox_apply: Operation not permitted
```

Both `swift build` (SwiftPM sandboxes its own manifest compilation) and `codex exec` (sandboxes
its own shell commands) self-sandbox internally by calling `sandbox_init`/`sandbox_apply` on
*themselves* once already running — and that specific pattern is what's disallowed. It's narrower
than "sandbox-exec can't nest at all": re-*exec*ing a fresh process under a new profile via
`sandbox-exec X` (spawn-time wrapping) works fine even from inside an outer jail — verified
`sandbox-exec -f outer.sb sandbox-exec -f inner.sb true` exits 0. It's specifically the
self-restriction pattern (a running, already-sandboxed process narrowing its own privileges
further) that macOS refuses. Both swift and codex use that pattern internally, not the
spawn-a-fresh-process one, so both fail identically under an outer wrap.

**`swift build` working under the sandbox is a hard acceptance requirement** (AC4), and an agent
can run arbitrary Bash — there is no way to predict or enumerate every tool a builder might invoke
that also self-restricts this way, so there's no safe way to wrap general Bash execution in an
outer Seatbelt jail on this OS. `sandbox/macos.py` is
still in the tree — built, and verified *correct on its own terms* (see below) — but it is **not
wired into either backend's actual launch**. This is the main content of "known gaps" this task
was asked to document, found by testing against the real tools rather than assumed.

Practical effect: **Claude has no OS-level enforcement for Bash-driven writes.** Its own
`--permission-mode`/`Write`/`Edit` path-checks (below) don't cover Bash, and this task doesn't
close that — it's the same gap the #42 ADR already named, now confirmed to have no available
macOS-native fix compatible with `swift build`.

## What does work, verified against the real CLIs

- **`claude --permission-mode auto --permission-prompts none`** (replaces the skip-permissions
  flag): Write/Edit tool calls outside `--add-dir` roots are denied by Claude's own code; anything
  that would otherwise need an interactive prompt (no TTY in `-p` mode) is auto-denied and reported
  back to the model immediately — no hang.
- **`codex --sandbox workspace-write -c approval_policy=never`** (replaces the bypass flag):
  writes outside the workspace + `--add-dir` roots are denied and reported straight back to the
  model (no prompt, no hang) — genuinely OS-level (Seatbelt-backed) enforcement, since this is
  codex's *own* internal jail, not an outer wrap. Codex's native sandbox does not restrict *reads*
  though — `cat ~/.ssh/config` succeeds under a plain `workspace-write` sandbox. Network is denied
  by default in `workspace-write`; `-c sandbox_workspace_write.network_access=true` turns it on per
  role.
- **codex's `PreToolUse` hook is wire-compatible with Claude's.** This was the second big unknown
  — the #42 ADR explicitly flagged "codex has no equivalent interception wired here." Testing
  `[[hooks.PreToolUse]]` in the isolated `CODEX_HOME`'s generated `config.toml` (with
  `--dangerously-bypass-hook-trust`, since troupe generates this hook itself fresh every run — no
  interactive trust review makes sense for that) showed the exact same payload shape
  (`tool_name`/`tool_input`/`cwd`) and the exact same deny-decision response schema Claude uses.
  **The unmodified `guard()` from `safety.py` now runs for codex too** — this is the real fix for
  codex's read-side gap (not sandbox-exec): secret reads (`~/.ssh`, `~/.aws`, Keychain commands),
  remote changes, and the new troupe.db/api.sock checks below all apply to both backends through
  one code path. `--dangerously-bypass-hook-trust` is a distinct flag from the two named in
  REQ-SAFE-050 — it skips trust review for a hook troupe itself controls, not the sandbox or
  approval flow; flagged here for anyone grepping for "dangerously" expecting literally none.
- **`sandbox-exec` (Seatbelt/SBPL) itself works correctly** for what it does — the nesting problem
  above is about wrapping a process that *also* self-sandboxes, not a defect in the profile. Two
  non-obvious gotchas found by testing, not reading docs: (1) multiple filters in one `(allow/deny
  op (subpath X) (regex Y))` clause are **OR'd, not AND'd** — `(subpath ".ssh") (regex "\.pub$")`
  allows the whole `.ssh` subpath, not just the matching files in it; carve-outs use one filter per
  clause instead. (2) rule **order matters**: `(deny X)` only takes effect if it appears *before*
  the `(allow default)` it's meant to narrow — `(allow default)(deny X)` silently does nothing.
  Verified (in isolation, not wrapping a self-sandboxing tool): denies `~/.ssh` private-key reads
  while allowing `.pub`/`known_hosts` and real ssh-agent + `known_hosts` authentication; denies
  home-dir writes outside an allowlist that includes common toolchain caches
  (`~/.cache`, `~/Library/Caches`, `~/.cargo`, `~/.rustup`, `~/.npm`, `~/Library/Developer`,
  `~/.swiftpm`, `~/.local/share/uv` — found by testing `uv sync`, which failed on `~/.cache/uv`
  under a naive deny-everything profile).
- **A subtler, unrelated finding along the way**: `child_env()` only stripped three
  `CLAUDECODE`/`CLAUDE_CODE_*` variable names. Running a nested `claude` invocation for this
  research (from inside a live Claude Code session) showed the child inherited
  `CLAUDE_CODE_SESSION_ID` and others and silently ran with the *parent* session's permission mode
  instead of the flags troupe passed. Fixed to strip the whole `CLAUDE*`/`CODEX_*`/`AI_AGENT`
  prefix set. The engine itself isn't normally launched from inside an agent session in production,
  so this wasn't an active production bug, but it must not depend on that being true.
- **`git push` to the project remote, and `uv sync`/`uv run pytest`/`swift build`, all verified
  working** under the new codex sandbox and the new claude permission mode, in disposable scratch
  repos/projects (not the live troupe checkout).

## Per-role profiles (REQ-SAFE-051)

`sandbox.RoleProfile` (network, readonly_in_root, subdir) + `DEFAULT_ROLE_PROFILES`, matching the
spec table. `writable_roots()` combines a profile with the run's actual resolved `cwd` (already
worktree-vs-project-root correct — `engine.py` resolves that before `RunSpec` is built) to get the
concrete roots for both backends' `--add-dir` allowlists. Overridable per-project via `troupe.toml
[safety.roles.<role>]`, validated by `sandbox.parse_role_profiles` and folded into
`safety.parse_settings` — so it's covered by the *existing* REQ-SAFE-021 human-approval gate on
`[safety]` with no new gate code needed.

## troupe.db / api.sock (from #57's review, folded into this task)

Neither backend's native sandbox can distinguish "the MCP server (a legitimate child of the same
process) writing troupe.db to serve a tool call" from "the agent's own Bash command writing
troupe.db directly" — they're indistinguishable processes with the same UID at the OS/file-
permission layer, since the MCP server is spawned as a child of the claude/codex process either
way. This is handled instead at the same layer as secrets/remotes: `guard()` now blocks any Bash
command whose text references `troupe.db` or `api.sock`, on both backends (via the shared
PreToolUse hook above). This is pattern-based, not an OS boundary — the same limitation the #42
ADR already states for every guard() check.

## Known gaps

- **Claude has no OS-level Bash write-scoping**, and this task could not close that on macOS
  without breaking `swift build` (see above) — the headline finding, not a minor caveat.
- **Claude has no OS-level `~/.ssh` read restriction either**, for the same nesting reason. It
  relies entirely on the PreToolUse guard() pattern (same as codex, and same as it already had
  since #42 — not a regression, just not improved by this task the way codex's hook coverage was).
- **Codex reads are guarded, not sandboxed.** `guard()`'s command-text inspection catches `cat
  ~/.ssh/id_ed25519`-shaped commands but, as documented since #42, "cannot reliably determine what
  an arbitrary script, interpreter, alias, encoded command or downloaded executable will do."
- **The cache-dir allowlist in `sandbox/macos.py`** (unused by default, see above) **is best-effort
  and untested against `swift build` specifically** — it was written and verified with `uv sync`
  before the nesting discovery made the whole module unusable for Claude; kept accurate for any
  future narrower use, not re-verified end-to-end since.
- **Keychain and the api.sock network path are guarded by text pattern, not OS enforcement.**
  `security find-*-password` is blocked by the existing #42 pattern; a `(deny network-outbound
  (literal ".../api.sock"))` Seatbelt rule was tried and did not actually block a real
  `AF_UNIX`/`SOCK_STREAM` connect in testing — not shipped rather than shipped unverified, and moot
  for Claude anyway given sandbox-exec isn't wired in.
- **`sandbox-exec` itself is a deprecated, undocumented-by-Apple API**, on top of the nesting
  restriction. No alternative (Endpoint Security framework, App Sandbox entitlements) fits a CLI
  tool launched this way without substantially more engineering investment.
- **Non-macOS: no OS-level layer at all** for either backend's Bash execution beyond codex's own
  native sandbox (which is macOS/Linux Seatbelt-or-equivalent already, per codex's own
  implementation — not re-verified here on Linux). Out of scope for this pass — troupe currently
  only ships for macOS.
- **A process with full filesystem access can still directly alter the SQLite database if it gets
  a shell outside these two backends' guarded paths entirely** (the #42 ADR's own caveat, repeated
  here since it's still true: these are defense-in-depth guards, not a multi-user OS security
  boundary. Running the MCP server as a genuinely different, lower-privileged OS user than the
  agent CLI is the real fix for the troupe.db distinction above, and is a much larger change than
  this task's scope.)

## Not attempted

- A comprehensive default-deny Seatbelt profile enumerating every legitimate operation (process
  exec, dynamic linking, temp dirs, DNS) for the cases where sandbox-exec *is* usable. Moot for
  Claude given the nesting finding; codex's own native sandbox is already built this way with far
  more engineering investment than fits this task.
- Reworking the MCP server launch so it isn't a descendant of the sandboxed CLI process (would let
  troupe.db/api.sock be denied at the OS level for the agent while staying open for the MCP
  server). Real fix for that specific gap, but a materially larger architecture change than this
  task's scope — noted for a future task, not attempted here.
