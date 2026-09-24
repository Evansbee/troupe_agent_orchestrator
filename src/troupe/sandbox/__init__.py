"""Least-privilege sandbox profiles (#43, REQ-SAFE-050/051).

Per-role write/network policy, plus the two backend-specific enforcement layers:
- Codex: its own native Seatbelt-backed `--sandbox workspace-write`, driven with `-c` overrides
  computed here (see `codex.py`).
- Claude: no native OS sandbox for Bash exists, so a macOS `sandbox-exec` profile (see `macos.py`)
  is the real enforcement; `--permission-mode`/`--permission-prompts` (see `claude.py`) additionally
  scope its own Read/Write/Edit tools and fail fast instead of prompting.
Both backends run the same `python -m troupe.safety` PreToolUse hook (REQ-SAFE-034) — codex's hook
protocol turned out to be wire-compatible with Claude's (same payload/response shape), closing the
gap the #42 ADR flagged ("codex has no equivalent interception wired").

See docs/adr/005-least-privilege-sandbox.md for the verification behind these choices and the
known gaps.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Config


@dataclass(frozen=True)
class RoleProfile:
    network: bool
    # If True, the resolved run cwd is NOT added as a writable root when it equals the project root
    # (qa outside a review worktree, gadfly always) — REQ-SAFE-051 "read-only in main".
    readonly_in_root: bool = False
    # If set, this subdir of the project root replaces cwd as the writable root (researcher).
    subdir: str = ""


DEFAULT_ROLE_PROFILES: dict[str, RoleProfile] = {
    "builder": RoleProfile(network=True),
    "qa": RoleProfile(network=True, readonly_in_root=True),
    "gadfly": RoleProfile(network=False, readonly_in_root=True),
    "researcher": RoleProfile(network=True, subdir="research"),
}
OTHERS_PROFILE = RoleProfile(network=True)  # lead, pm, spec, designer, ...


def role_profile(cfg: Config, role_key: str) -> RoleProfile:
    """DEFAULT_ROLE_PROFILES, overridable per-project via troupe.toml [safety.roles.<role>]
    (guarded by REQ-SAFE-021, same as the rest of [safety])."""
    overrides = (cfg.safety.get("roles") or {}).get(role_key, {})
    base = DEFAULT_ROLE_PROFILES.get(role_key, OTHERS_PROFILE)
    if not overrides:
        return base
    return RoleProfile(
        network=bool(overrides.get("network", base.network)),
        readonly_in_root=bool(overrides.get("readonly_in_root", base.readonly_in_root)),
        subdir=str(overrides.get("subdir", base.subdir)),
    )


def parse_role_profiles(raw: dict) -> dict:
    """Validate troupe.toml [safety.roles]. Called from safety.parse_settings; raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("safety.roles must be a table")
    for role_key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"safety.roles.{role_key} must be a table")
        extra = set(entry) - {"network", "readonly_in_root", "subdir"}
        if extra:
            raise ValueError(f"safety.roles.{role_key}: unknown key(s) {sorted(extra)}")
        if "network" in entry and not isinstance(entry["network"], bool):
            raise ValueError(f"safety.roles.{role_key}.network must be a bool")
        if "readonly_in_root" in entry and not isinstance(entry["readonly_in_root"], bool):
            raise ValueError(f"safety.roles.{role_key}.readonly_in_root must be a bool")
        if "subdir" in entry and not isinstance(entry["subdir"], str):
            raise ValueError(f"safety.roles.{role_key}.subdir must be a string")
    return raw


def writable_roots(profile: RoleProfile, cwd: Path, project_root: Path, state_dir: Path) -> list[Path]:
    """Absolute writable roots for one run: always .troupe/, plus either the role's subdir
    (researcher), or the resolved cwd (worktree for builder/qa, project root for others) — unless
    readonly_in_root says not to add cwd when it's the bare project root."""
    roots = [state_dir]
    if profile.subdir:
        roots.append(project_root / profile.subdir)
    elif not (profile.readonly_in_root and cwd == project_root):
        roots.append(cwd)
    return roots
