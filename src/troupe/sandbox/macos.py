"""macOS sandbox-exec (Seatbelt) profile builder — evaluated for REQ-SAFE-050's "macOS outer
layer" and NOT currently wired into any backend's actual launch. Built, verified correct on its
own, and kept for the record and any narrower future use — see the "why not applied" note below.

**Not applied to Claude** (the backend that would benefit most, since it has no native sandbox of
its own — verified its Bash tool applies no directory restriction at all): wrapping the whole
claude process broke `swift build` (a hard acceptance requirement) and codex, because macOS
Seatbelt does not allow an already-sandboxed process to apply a further sandbox to itself
(`sandbox_apply: Operation not permitted`) — confirmed this happens even with a fully permissive
`(allow default)` outer profile, so it isn't about this module's specific rules, it's a categorical
OS restriction on nesting. Since an agent can run arbitrary Bash commands, and any of them might
invoke a tool with its own internal Seatbelt use (swift build and codex both do; others surely
exist unenumerated), there's no safe way to wrap general-purpose Bash execution with sandbox-exec
on this OS. **Not applied to codex** for the same reason (it self-sandboxes already). See
docs/adr/005-least-privilege-sandbox.md for the full investigation.

SBPL gotcha (from empirical testing, not documented anywhere obvious): multiple filters in one
`(allow/deny op (subpath X) (regex Y))` clause are OR'd, not AND'd — `(subpath ".ssh") (regex
"\\.pub$")` allows the *whole* subpath, not just the matching files within it. Carve-outs here use
one filter per clause instead. Rule order also matters: a `(deny ...)` only takes effect if it
appears *before* the `(allow default)` (or other broader allow) it's meant to narrow.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

AVAILABLE = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _sbpl_literal(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def profile(*, writable_roots: list[Path], extra_cache_roots: list[Path]) -> str:
    """Build an SBPL profile: deny ~/.ssh private-key data and non-listed home-dir writes, allow
    everything else (network, reads, execs) — those are each backend's/role's own job (native
    sandbox, guard() hook, [safety.roles].network)."""
    home = Path.home()
    ssh = home / ".ssh"
    lines = ["(version 1)"]
    lines.append(f'(deny file-read-data (subpath "{_sbpl_literal(ssh)}"))')
    lines.append('(allow file-read-data (regex #"\\.pub$"))')
    lines.append(f'(allow file-read-data (literal "{_sbpl_literal(ssh / "known_hosts")}"))')
    lines.append(f'(deny file-write* (subpath "{_sbpl_literal(home)}"))')
    for root in [*writable_roots, *extra_cache_roots]:
        lines.append(f'(allow file-write* (subpath "{_sbpl_literal(root)}"))')
    lines.append("(allow default)")
    return "\n".join(lines) + "\n"


# Common toolchain cache locations outside the project that legitimate builds/tests need to write
# to (uv, pip, cargo, npm, Xcode/swiftpm) — found by testing `uv sync` under a naive deny-home
# profile, which failed on `~/.cache/uv`. Best-effort, not exhaustive: an unlisted tool's cache dir
# is a real, expected gap (documented in the ADR) until it's added here.
DEFAULT_CACHE_SUBDIRS = (
    ".cache", "Library/Caches", ".cargo", ".rustup", ".npm", "Library/Developer", ".swiftpm",
    ".local/share/uv",
)


def default_cache_roots(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / d for d in DEFAULT_CACHE_SUBDIRS]


def wrap(argv: list[str], profile_path: Path) -> list[str]:
    return ["sandbox-exec", "-f", str(profile_path), *argv]
