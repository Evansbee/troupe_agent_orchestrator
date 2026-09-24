"""REQ-BE-015: codex runs are isolated from the human's personal ~/.codex (no plugins, no notify)."""
import asyncio
import os
from pathlib import Path

from troupe.runners import (
    CodexRunner, RunSpec, codex_config_toml, codex_home_dir, ensure_codex_home, link_codex_auth,
)


def test_codex_home_dir_is_per_agent(project):
    cfg, _ = project
    home = codex_home_dir(cfg, "builder-1")
    assert home == cfg.state_dir / "codex-home" / "builder-1"


def test_generated_config_has_no_plugins_or_notify(project):
    cfg, _ = project
    a = cfg.agents[0]
    a.model, a.effort = "gpt-5-codex", "high"
    toml = codex_config_toml(cfg, a)
    body = "\n".join(line for line in toml.splitlines() if not line.startswith("#"))
    assert "plugin" not in body.lower()
    assert "notify" not in body.lower()
    assert "[mcp_servers.troupe]" in toml
    assert 'model = "gpt-5-codex"' in toml
    assert 'model_reasoning_effort = "high"' in toml


def test_generated_config_maps_max_effort_to_xhigh(project):
    cfg, _ = project
    a = cfg.agents[0]
    a.effort = "max"
    toml = codex_config_toml(cfg, a)
    assert 'model_reasoning_effort = "xhigh"' in toml


def test_link_codex_auth_symlinks_without_copying(tmp_path):
    src = tmp_path / "real-auth.json"
    src.write_text('{"secret": "token"}')
    dest = tmp_path / "home" / "auth.json"
    dest.parent.mkdir()

    link_codex_auth(dest, src)

    assert dest.is_symlink()
    assert os.readlink(dest) == str(src)
    assert dest.read_text() == src.read_text()  # readable via the link, never duplicated on disk


def test_link_codex_auth_is_idempotent_and_replaces_stale_target(tmp_path):
    src = tmp_path / "real-auth.json"
    src.write_text("v1")
    other = tmp_path / "other-auth.json"
    other.write_text("v2")
    dest = tmp_path / "home" / "auth.json"
    dest.parent.mkdir()

    link_codex_auth(dest, other)
    assert os.readlink(dest) == str(other)

    link_codex_auth(dest, src)  # re-pointed at the real source
    assert os.readlink(dest) == str(src)

    link_codex_auth(dest, src)  # already correct: no-op, doesn't raise
    assert os.readlink(dest) == str(src)


def test_link_codex_auth_never_touches_a_real_file(tmp_path):
    src = tmp_path / "real-auth.json"
    src.write_text("v1")
    dest = tmp_path / "home" / "auth.json"
    dest.parent.mkdir()
    dest.write_text("not-a-symlink")

    link_codex_auth(dest, src)

    assert not dest.is_symlink()
    assert dest.read_text() == "not-a-symlink"


def test_link_codex_auth_skips_missing_source(tmp_path):
    src = tmp_path / "missing-auth.json"
    dest = tmp_path / "home" / "auth.json"
    dest.parent.mkdir()

    link_codex_auth(dest, src)

    assert not dest.exists()
    assert not dest.is_symlink()


def test_ensure_codex_home_builds_config_and_auth_link(project, tmp_path):
    cfg, _ = project
    a = cfg.agents[0]
    auth_src = tmp_path / "auth.json"
    auth_src.write_text('{"token": "abc"}')
    home = codex_home_dir(cfg, a.id)

    result = ensure_codex_home(cfg, a, home, auth_src=auth_src)

    assert result == home
    assert (home / "config.toml").exists()
    assert (home / "auth.json").is_symlink()
    assert os.readlink(home / "auth.json") == str(auth_src)


def test_codex_runner_launch_env_sets_isolated_codex_home(project, monkeypatch, tmp_path):
    cfg, _ = project
    a = cfg.agents[0]
    auth_src = tmp_path / "auth.json"
    auth_src.write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").symlink_to(auth_src)

    runner = CodexRunner()
    seen_env = {}

    async def fake_stream(args, spec, prompt, callback):
        seen_env.update(runner.env_override)
        callback({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
        return 0, ""

    monkeypatch.setattr(runner, "_stream", fake_stream)
    spec = RunSpec(cfg, a, "system", "prompt", cfg.root, None, Path("unused"))
    result = asyncio.run(runner.run(spec, lambda kind, text: None))

    assert result.ok
    expected_home = str(codex_home_dir(cfg, a.id))
    assert seen_env["CODEX_HOME"] == expected_home
    assert (Path(expected_home) / "config.toml").exists()
    assert (Path(expected_home) / "auth.json").is_symlink()
