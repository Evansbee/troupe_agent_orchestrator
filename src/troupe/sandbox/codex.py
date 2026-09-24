"""Codex sandbox args and PreToolUse hook config (REQ-SAFE-050).

Replaces `--dangerously-bypass-approvals-and-sandbox` with codex's own native Seatbelt-backed
`--sandbox workspace-write`, verified empirically (docs/adr/005): writes outside the workspace +
`--add-dir` roots are denied and reported back to the model immediately (no hang, no approval
prompt) with `-c approval_policy=never`; `sandbox_workspace_write.network_access` toggles network
per role. Reads are NOT restricted by codex's native sandbox (verified: `cat ~/.ssh/config`
succeeds under a plain workspace-write sandbox) — that gap is closed by the PreToolUse hook below,
not by sandbox-exec (nesting sandbox-exec around codex breaks codex's own internal sandbox
application — verified, see the ADR).
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path


def sandbox_args(*, network: bool) -> list[str]:
    return [
        "--sandbox", "workspace-write",
        "-c", "approval_policy=never",
        "-c", f"sandbox_workspace_write.network_access={'true' if network else 'false'}",
    ]


def hooks_toml() -> str:
    """A PreToolUse hook running the same `python -m troupe.safety` guard Claude uses (REQ-SAFE-034)
    — verified wire-compatible: codex's hook payload/response shape matches Claude's exactly, so
    the unmodified guard() logic (secrets, remotes, now troupe.db/api.sock) applies to codex too."""
    command = shlex.join([sys.executable, "-m", "troupe.safety"])
    return (
        "\n[[hooks.PreToolUse]]\n"
        'matcher = "*"\n\n'
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(command)}\n"
        "timeout = 10\n"
    )


def extra_add_dirs(writable_roots: list[Path], primary_cwd: Path) -> list[str]:
    """--add-dir args for writable roots other than the primary -C workspace (which codex already
    treats as writable)."""
    args = []
    for root in writable_roots:
        if root != primary_cwd:
            args += ["--add-dir", str(root)]
    return args


def git_writable_roots(cwd: Path) -> list[Path]:
    """#96: `--add-dir` roots so `git commit` works from inside a git worktree. A worktree's git
    metadata lives outside the worktree directory itself: `git rev-parse --git-dir` from inside one
    resolves to `<root>/.git/worktrees/<name>/` (its private index/HEAD/COMMIT_EDITMSG/logs/HEAD —
    the codex sandbox's existing `-C <cwd>` grant doesn't reach it), and a commit also writes new
    objects and the branch's own ref/reflog into the *shared* `<root>/.git` (`--git-common-dir`).

    Narrowed to exactly what a commit needs, verified empirically (live codex run, #96 task
    summary): the private gitdir in full; `objects/` (shared, content-addressed, so a stray write
    can't overwrite another branch's history); and `refs/heads/troupe/` + `logs/refs/heads/troupe/`
    — every task branch lives under that one prefix (`troupe/<slug>`), so this reaches this task's
    own ref/reflog without ever including `refs/heads/main` (a sibling directory entry, not a
    descendant of `refs/heads/troupe/`) — confirmed live: `git update-ref refs/heads/main HEAD`
    inside this same sandbox fails with `Operation not permitted`, main untouched. Top-level
    `<root>/.git` itself is deliberately NOT granted: an opportunistic `packed-refs.lock` write
    during commit was denied in testing, but harmlessly (loose refs are git's fallback, and the
    commit's own exit code was still 0) — granting it would trade that cosmetic stderr line for
    write access to every branch's top-level state, not worth it for a warning git already
    tolerates.

    Returns `[]` when `cwd` isn't a worktree checkout (`--git-dir` == `--git-common-dir`, the main
    checkout) — that case's git metadata already lives inside the role's existing writable cwd, and
    granting the *entire* common `.git` there would be far broader than this task's narrow intent.
    Also `[]` if `cwd` isn't a git repo at all (`git rev-parse` fails) — must never raise and break
    argv building over something this optional."""
    from .. import gitops
    git_dir = gitops.git(cwd, "rev-parse", "--git-dir", check=False)
    common_dir = gitops.git(cwd, "rev-parse", "--git-common-dir", check=False)
    if not git_dir or not common_dir:
        return []
    git_dir = (cwd / git_dir).resolve()
    common_dir = (cwd / common_dir).resolve()
    if git_dir == common_dir:
        return []
    return [
        git_dir,
        common_dir / "objects",
        common_dir / "refs" / "heads" / "troupe",
        common_dir / "logs" / "refs" / "heads" / "troupe",
    ]
