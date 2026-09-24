# Safety guards and remaining sandbox work (#42)

The human approves exact protected diffs and safety/merge-check configuration. Gate logic lives in
`gates.py`; policy, scanning and guards live in `safety.py`. A missing baseline requests explicit human approval and blocks merges; no process may self-approve
the initial migration baseline. Later config edits keep the approved snapshot until the human accepts the card. Approval
is invalidated by a changed diff or policy. The approved charter wording is unchanged; session prompt
hashes force fresh sessions when that wording or a role prompt changes.

Claude receives a per-run `PreToolUse` command hook via `--settings`. It returns a deny decision
with an ask_human error, never an allow override. The JSON contract follows the
[official hooks reference](https://code.claude.com/docs/en/hooks#pretooluse-decision-control).
Normal pushes to approved remotes and SSH authentication remain available. Local file tools enforce
path boundaries and secret-store checks, redact results and label file contents as untrusted data.
Local currently has no shell or web tools; future web tools must call the same guard and wrap results.

These guards are defense in depth, **not an OS sandbox**. Shell inspection cannot reliably determine
what an arbitrary script, interpreter, alias, encoded command or downloaded executable will do.
Secret scanning recognizes specified patterns, not every possible credential format or personal datum.
Codex has no equivalent interception wired here: its native shell and web calls are not filtered by
this Claude hook. Troupe-originated commits and stored mail/events are scanned/redacted on every backend.
Task #43 must implement and verify backend sandbox/exec policies and document remaining bypasses.
A process with full filesystem access can directly alter the SQLite database or hook configuration;
this is also a sandbox boundary, not an authentication guarantee provided by this change.

Stop everything persists before dispatch, kills registered process groups with SIGTERM then SIGKILL,
blocks chat as well as autonomous runs, and leaves the engine service alive. Local HTTP requests are
cancelled. The API/GUI/CLI share stop_now/resume commands; no agent tool exposes Resume. Descendants
that deliberately detach into new sessions are outside process-group termination and need the outer
sandbox/process supervisor in #43. Guards do not claim to stop unrelated processes on the Mac.

No live quota-bearing provider run is used to verify this change. Tests exercise the actual hook JSON
entry point, disposable git repositories, and real SIGTERM-resistant child processes. Native backend
interoperability under least-privilege profiles remains #43's acceptance work.
