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
