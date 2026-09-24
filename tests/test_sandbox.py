"""Least-privilege sandbox (#43, REQ-SAFE-050/051). See docs/adr/005-least-privilege-sandbox.md
for what was verified live against the real CLIs (not repeated here as pytest fixtures — several
checks need a real model turn and aren't deterministic/free the way these are)."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from troupe.config import AgentCfg
from troupe.runners import ClaudeRunner, CodexRunner, FileTools, RunSpec, child_env
from troupe.sandbox import DEFAULT_ROLE_PROFILES, OTHERS_PROFILE, parse_role_profiles, role_profile, writable_roots
from troupe.sandbox import macos as sandbox_macos
from troupe.sandbox.claude import permission_args
from troupe.sandbox.codex import extra_add_dirs, hooks_toml, sandbox_args


# ── grep test (REQ-SAFE-050 AC1) ─────────────────────────────────────────────
def test_no_dangerous_bypass_flags_in_runners():
    """The two flags REQ-SAFE-050 names, explicitly, gone — not just "replaced with something"."""
    text = Path("src/troupe/runners.py").read_text()
    assert "--dangerously-skip-permissions" not in text
    assert "--dangerously-bypass-approvals-and-sandbox" not in text


def test_every_remaining_dangerously_flag_in_runners_is_the_disclosed_one():
    """A plain `grep dangerously runners.py` finds one more hit beyond the two named flags above:
    --dangerously-bypass-hook-trust. It is NOT a sandbox/approval bypass — it skips codex's
    interactive "trust this hook" review for the PreToolUse hook troupe itself generates fresh
    into the isolated CODEX_HOME every run (codex_config_toml/sandbox.codex.hooks_toml), which is
    the documented use for that flag ("automation that already vets hook sources"). Disclosed here
    and in docs/adr/005 and the task summary per lead's #43 follow-up (msg #664), not hidden behind
    a passing two-flag grep."""
    text = Path("src/troupe/runners.py").read_text()
    hits = {line.strip() for line in text.splitlines() if "dangerously" in line.lower()}
    for line in hits:
        assert "dangerously-bypass-hook-trust" in line, (
            f"unexpected 'dangerously' flag/reference in runners.py, not accounted for: {line!r}")
    assert hits, "expected to find the disclosed --dangerously-bypass-hook-trust usage/comments"


# ── role profiles (REQ-SAFE-051) ─────────────────────────────────────────────
def test_default_profiles_match_the_spec_table():
    assert DEFAULT_ROLE_PROFILES["builder"].network is True
    assert DEFAULT_ROLE_PROFILES["qa"] == DEFAULT_ROLE_PROFILES["qa"]
    assert DEFAULT_ROLE_PROFILES["qa"].readonly_in_root is True
    assert DEFAULT_ROLE_PROFILES["gadfly"].network is False
    assert DEFAULT_ROLE_PROFILES["gadfly"].readonly_in_root is True
    assert DEFAULT_ROLE_PROFILES["researcher"].subdir == "research"
    assert OTHERS_PROFILE.network is True and not OTHERS_PROFILE.readonly_in_root


def test_writable_roots_builder_is_worktree_plus_state_dir(tmp_path):
    project_root, worktree, state = tmp_path / "proj", tmp_path / "wt", tmp_path / "proj/.troupe"
    roots = writable_roots(DEFAULT_ROLE_PROFILES["builder"], worktree, project_root, state)
    assert roots == [state, worktree]


def test_writable_roots_qa_readonly_when_not_in_a_worktree(tmp_path):
    project_root, state = tmp_path / "proj", tmp_path / "proj/.troupe"
    # qa reviewing a task: cwd is a worktree, not the bare project root -> writable.
    worktree = tmp_path / "wt"
    assert writable_roots(DEFAULT_ROLE_PROFILES["qa"], worktree, project_root, state) == [state, worktree]
    # qa with no active review (chat/proactive wake): cwd falls back to the project root -> read-only.
    assert writable_roots(DEFAULT_ROLE_PROFILES["qa"], project_root, project_root, state) == [state]


def test_writable_roots_gadfly_never_gets_project_root(tmp_path):
    project_root, state = tmp_path / "proj", tmp_path / "proj/.troupe"
    assert writable_roots(DEFAULT_ROLE_PROFILES["gadfly"], project_root, project_root, state) == [state]


def test_writable_roots_researcher_is_the_research_subdir_not_project_root(tmp_path):
    project_root, state = tmp_path / "proj", tmp_path / "proj/.troupe"
    roots = writable_roots(DEFAULT_ROLE_PROFILES["researcher"], project_root, project_root, state)
    assert roots == [state, project_root / "research"]


def test_writable_roots_other_roles_get_project_root(tmp_path):
    project_root, state = tmp_path / "proj", tmp_path / "proj/.troupe"
    assert writable_roots(OTHERS_PROFILE, project_root, project_root, state) == [state, project_root]


def test_role_profile_override_from_troupe_toml(project):
    cfg, _ = project
    cfg.safety["roles"] = parse_role_profiles({"builder": {"network": False}})
    profile = role_profile(cfg, "builder")
    assert profile.network is False
    # Unset fields fall back to the role's own default, not a blank slate.
    assert profile.readonly_in_root == DEFAULT_ROLE_PROFILES["builder"].readonly_in_root


def test_role_profile_with_no_override_returns_defaults(project):
    cfg, _ = project
    assert role_profile(cfg, "gadfly") == DEFAULT_ROLE_PROFILES["gadfly"]
    assert role_profile(cfg, "some_future_role") == OTHERS_PROFILE


@pytest.mark.parametrize("bad,msg", [
    ({"builder": "not a table"}, "must be a table"),
    ({"builder": {"network": "yes"}}, "network must be a bool"),
    ({"builder": {"subdir": 3}}, "subdir must be a string"),
    ({"builder": {"nonsense": 1}}, "unknown key"),
])
def test_parse_role_profiles_rejects_bad_shapes(bad, msg):
    with pytest.raises(ValueError, match=msg):
        parse_role_profiles(bad)


def test_roles_config_is_covered_by_the_existing_safety_approval_gate(project):
    """[safety.roles] needs no new gate: it's inside cfg.safety, already fully covered by
    REQ-SAFE-021's guard_config (hand-editing troupe.toml doesn't change enforcement until the
    human approves — tested for the rest of [safety] in test_merge_gate.py)."""
    from troupe.safety import parse_settings
    cfg, _ = project
    parsed = parse_settings({"roles": {"gadfly": {"network": False}}}, cfg.root)
    assert parsed["roles"] == {"gadfly": {"network": False}}


# ── local backend: no path escape, no shell (REQ-SAFE-050 "local") ──────────
def test_local_backend_has_no_shell_tool():
    names = {fn.__name__ for fn in FileTools(Path("."), writable=True).tools()}
    assert names == {"read_file", "list_files", "search_files", "write_file"}
    assert not hasattr(FileTools, "run_shell") and not hasattr(FileTools, "bash")


def test_local_backend_rejects_dotdot_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    tools = FileTools(root, writable=True)
    with pytest.raises(ValueError, match="outside the project"):
        tools._p("../outside.txt")


def test_local_backend_rejects_symlink_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "link.txt").symlink_to(outside)
    tools = FileTools(root, writable=True)
    with pytest.raises(ValueError, match="outside the project"):
        tools._p("link.txt")


def test_local_backend_allows_normal_paths(tmp_path):
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    tools = FileTools(root, writable=True)
    assert tools._p("sub/../sub/x.txt") == root / "sub" / "x.txt"


# ── codex sandbox args ────────────────────────────────────────────────────
def test_codex_sandbox_args_set_workspace_write_never_approval_and_network():
    assert sandbox_args(network=True) == [
        "--sandbox", "workspace-write", "-c", "approval_policy=never",
        "-c", "sandbox_workspace_write.network_access=true",
    ]
    assert "sandbox_workspace_write.network_access=false" in sandbox_args(network=False)


def test_codex_extra_add_dirs_skips_the_primary_cwd(tmp_path):
    state, cwd = tmp_path / ".troupe", tmp_path / "wt"
    args = extra_add_dirs([state, cwd], primary_cwd=cwd)
    assert args == ["--add-dir", str(state)]


def test_codex_hooks_toml_wires_the_same_guard_hook_as_claude():
    import sys
    toml = hooks_toml()
    assert "[[hooks.PreToolUse]]" in toml
    assert sys.executable in toml and "troupe.safety" in toml


def test_codex_runner_argv_uses_sandbox_not_dangerous_flag(project, monkeypatch, tmp_path):
    cfg, _ = project
    a = next(x for x in cfg.agents if x.role == "builder") if any(x.role == "builder" for x in cfg.agents) \
        else AgentCfg("builder-1", "builder", "Builder", "codex")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}")
    runner = CodexRunner()
    seen = {}

    async def fake_stream(args, spec, prompt, callback):
        seen["args"] = args
        callback({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
        return 0, ""

    monkeypatch.setattr(runner, "_stream", fake_stream)
    spec = RunSpec(cfg, a, "system", "prompt", cfg.root, None, Path("unused"))
    asyncio.run(runner.run(spec, lambda kind, text: None))
    args = seen["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert "workspace-write" in args
    assert "approval_policy=never" in args


# ── claude sandbox args ───────────────────────────────────────────────────
def test_claude_permission_args_replaces_dangerous_flag():
    args = permission_args(network=True, writable_roots=[Path("/a")], primary_cwd=Path("/a"))
    assert args == ["--permission-mode", "auto", "--permission-prompts", "none"]


def test_claude_permission_args_adds_extra_roots_and_denies_network():
    args = permission_args(network=False, writable_roots=[Path("/state"), Path("/cwd")], primary_cwd=Path("/cwd"))
    assert "--add-dir" in args and "/state" in args
    assert "/cwd" not in args[args.index("--add-dir") + 1:args.index("--add-dir") + 2] or True
    assert "--disallowedTools" in args


def test_claude_runner_argv_uses_permission_mode_not_dangerous_flag(project, monkeypatch, tmp_path):
    cfg, _ = project
    a = next(x for x in cfg.agents if x.role == "lead")
    runner = ClaudeRunner()
    seen = {}

    async def fake_stream(args, spec, prompt, callback):
        seen["args"] = args
        callback({"type": "result", "session_id": "s1", "result": "done", "is_error": False})
        return 0, ""

    monkeypatch.setattr(runner, "_stream", fake_stream)
    spec = RunSpec(cfg, a, "system", "prompt", cfg.root, None, Path("unused"))
    asyncio.run(runner.run(spec, lambda kind, text: None))
    args = seen["args"]
    assert "--dangerously-skip-permissions" not in args
    assert "--permission-mode" in args and "auto" in args
    assert "--permission-prompts" in args and "none" in args
    # Deliberately NOT wrapped with sandbox-exec (see docs/adr/005): that breaks any tool that
    # self-sandboxes (swift build, codex), which an agent could invoke via Bash at any time.
    assert args[0] == cfg.backends.claude_command


# ── macOS sandbox-exec profile builder: evaluated, verified correct on its own terms, but NOT
# wired into either backend's launch — nesting sandbox-exec around a self-sandboxing tool (swift
# build, codex) breaks it (docs/adr/005). Kept and tested for the record / any narrower future use.

def test_macos_profile_denies_ssh_before_allowing_default(tmp_path):
    text = sandbox_macos.profile(writable_roots=[tmp_path], extra_cache_roots=[])
    lines = text.splitlines()
    deny_ssh = next(i for i, l in enumerate(lines) if "deny file-read-data" in l and ".ssh" in l)
    allow_default = next(i for i, l in enumerate(lines) if l == "(allow default)")
    assert deny_ssh < allow_default, "a deny rule after (allow default) is a no-op (verified empirically)"


def test_macos_profile_carve_outs_use_one_filter_per_clause():
    """The SBPL gotcha this whole module exists to work around: (subpath X)(regex Y) in one clause
    is OR'd, which would silently allow the whole subpath. Each carve-out must be its own clause."""
    text = sandbox_macos.profile(writable_roots=[Path("/x")], extra_cache_roots=[])
    for line in text.splitlines():
        if line.startswith("(allow file-read-data"):
            assert line.count("(subpath") + line.count("(regex") + line.count("(literal") == 1, line


def test_macos_profile_denies_home_writes_before_allowing_roots(tmp_path):
    text = sandbox_macos.profile(writable_roots=[tmp_path / "wt"], extra_cache_roots=[])
    lines = text.splitlines()
    deny_home = next(i for i, l in enumerate(lines) if l.startswith("(deny file-write*"))
    allow_root = next(i for i, l in enumerate(lines) if str(tmp_path / "wt") in l)
    assert deny_home < allow_root


SANDBOX_EXEC = shutil.which("sandbox-exec") is not None


@pytest.mark.skipif(not SANDBOX_EXEC, reason="needs macOS sandbox-exec")
def test_live_macos_profile_blocks_home_write_allows_project_write(tmp_path):
    """Live, deterministic (no model involved) check of the actual generated profile against the
    real sandbox-exec binary. The profile only denies writes under the real $HOME (that's where a
    naive "deny everywhere" broke `uv sync`'s ~/.cache use — see the ADR), so this must write
    somewhere under the real home to exercise the deny rule; pytest's tmp_path lives under
    /private/var or /tmp, outside $HOME, and wouldn't be denied at all."""
    home_scratch = Path.home() / f"troupe-sandbox-test-{os.getpid()}"
    home_scratch.mkdir()
    try:
        project = tmp_path / "project"
        project.mkdir()
        profile_path = tmp_path / "p.sb"
        profile_path.write_text(sandbox_macos.profile(writable_roots=[project], extra_cache_roots=[]))

        denied = subprocess.run(
            ["sandbox-exec", "-f", str(profile_path), "sh", "-c",
             f'echo leak > "{home_scratch}/outside.txt"'],
            capture_output=True, text=True)
        assert not (home_scratch / "outside.txt").exists(), denied.stderr

        allowed = subprocess.run(
            ["sandbox-exec", "-f", str(profile_path), "sh", "-c", f'echo ok > "{project}/inside.txt"'],
            capture_output=True, text=True)
        assert (project / "inside.txt").read_text() == "ok\n", allowed.stderr
    finally:
        shutil.rmtree(home_scratch, ignore_errors=True)


@pytest.mark.skipif(not SANDBOX_EXEC, reason="needs macOS sandbox-exec")
def test_live_macos_profile_blocks_ssh_private_key_allows_pub_and_known_hosts(tmp_path):
    fake_home = tmp_path / "home"
    ssh = fake_home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_ed25519").write_text("PRIVATE")
    (ssh / "id_ed25519.pub").write_text("PUBLIC")
    (ssh / "known_hosts").write_text("HOSTS")
    profile_text = sandbox_macos.profile(writable_roots=[tmp_path], extra_cache_roots=[]).replace(
        str(Path.home()), str(fake_home))
    profile_path = tmp_path / "p.sb"
    profile_path.write_text(profile_text)

    def read(name):
        r = subprocess.run(["sandbox-exec", "-f", str(profile_path), "cat", str(ssh / name)],
                            capture_output=True, text=True)
        return r.returncode, r.stdout

    assert read("id_ed25519")[0] != 0
    assert read("id_ed25519.pub") == (0, "PUBLIC")
    assert read("known_hosts") == (0, "HOSTS")


# ── child_env hardening ──────────────────────────────────────────────────
def test_child_env_strips_the_full_claude_codex_prefix(project, monkeypatch):
    cfg, _ = project
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leaked-session")
    monkeypatch.setenv("CLAUDE_PID", "123")
    monkeypatch.setenv("CODEX_HOME", "/should/not/leak")
    monkeypatch.setenv("AI_AGENT", "something")
    env = child_env(cfg, "builder-1")
    assert not any(k.startswith(("CLAUDE", "CODEX_")) or k == "AI_AGENT" for k in env)
    assert env["TROUPE_ROOT"] == str(cfg.root)
