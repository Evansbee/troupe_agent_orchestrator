"""Claude sandbox args (REQ-SAFE-050).

Replaces `--dangerously-skip-permissions` with `--permission-mode auto --permission-prompts none`,
verified empirically (docs/adr/005): "auto" runs Bash/Read/Edit/Write without stalling for a human,
Write/Edit are still checked against `--add-dir`-derived allowed roots by Claude's own code (a
write outside them is denied, not prompted), and "none" makes anything that *would* need a prompt
(no interactive surface in `-p` mode) fail immediately instead of hanging.

Claude has no native OS-level sandbox for Bash itself, though — verified empirically that "auto"
mode lets a Bash-run `echo ... > ~/outside` through untouched. The real enforcement for Bash is the
macOS sandbox-exec wrap (macos.py) plus the existing PreToolUse guard() hook (secrets, remotes, now
troupe.db/api.sock).
"""
from __future__ import annotations

from pathlib import Path


def permission_args(*, network: bool, writable_roots: list[Path], primary_cwd: Path) -> list[str]:
    args = ["--permission-mode", "auto", "--permission-prompts", "none"]
    for root in writable_roots:
        if root != primary_cwd:
            args += ["--add-dir", str(root)]
    if not network:
        args += ["--disallowedTools", "WebFetch WebSearch"]
    return args
