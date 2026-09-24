"""Team migration, validated reloads and provider-specific reasoning controls."""
import asyncio
import copy
from pathlib import Path

import pytest

from troupe import config
from troupe.engine import Engine, Wake
from troupe.runners import RunResult, RunSpec, make_runner
from troupe.store import Store


def roster():
    return {"agents": [
        {"id": "lead_1", "role": "lead", "provider": "claude", "model": "opus", "level": "high"},
        {"id": "builder_1", "role": "builder", "providers": [
            {"provider": "codex", "level": "max"}, {"provider": "claude", "model": "sonnet"}]}]}


def install_team(cfg, document):
    config.save_team(cfg.root, document)


def test_legacy_migration_and_notice_once(project):
    cfg, store = project
    path = cfg.state_dir / config.CONFIG_FILE
    original = path.read_text()
    assert (cfg.state_dir / config.TEAM_FILE).exists()
    loaded = config.load(cfg.root)
    assert loaded.agent("builder-1").backend == "claude"
    assert loaded.agent("builder-2").providers[0].provider == "codex"
    assert path.read_text() == original
    config.load(cfg.root)
    assert len([e for e in store.events() if "Migrated" in e["text"]]) == 1
    assert len([e for e in store.events() if "ignored" in e["text"]]) == 1


def test_order_shorthand_derived_ids_and_comment_roundtrip(project):
    cfg, _ = project
    document = roster()
    document["agents"].append({"role": "builder", "provider": "local", "idle_minutes": 0, "enabled": False})
    install_team(cfg, document)
    path = cfg.state_dir / config.TEAM_FILE
    path.write_text("# retain this comment\n" + path.read_text())
    loaded = config.load(cfg.root)
    assert [p.provider for p in loaded.agent("builder_1").providers] == ["codex", "claude"]
    assert loaded.agent("builder_1").effort == "max"
    assert not loaded.agent("builder_2").enabled
    assert loaded.agent("builder_2").idle_seconds == 0
    config.save_team(cfg.root, config.read_team(cfg.root))
    assert "# retain this comment" in path.read_text()


@pytest.mark.parametrize("field,value", [
    ("providers", []), ("providers", [{"provider": "codex"}, {"provider": "codex"}]),
    ("providers", [{"provider": "bad"}]), ("providers", [{"provider": "codex", "level": "xhigh"}]),
    ("role", "unknown"), ("idle_minutes", -1), ("enabled", "yes"), ("extra_args", "--oops"),
    ("id", "lead_1"),
])
def test_invalid_team_reports_agent_and_field(field, value):
    document = roster()
    document["agents"][1][field] = value
    with pytest.raises(ValueError, match="team.yaml:.*(?:builder_1|lead_1)"):
        config.parse_agents(document)


def test_invalid_yaml_and_toml_keep_independent_last_good_configs(project):
    cfg, store = project
    engine = Engine(cfg)
    path = cfg.state_dir / config.TEAM_FILE
    original = path.read_text()
    path.write_text("agents: [broken")
    engine.reload_config()
    assert engine.cfg is cfg
    assert store.kv_get("config_error.team.yaml")
    count = len(store.events())
    engine.reload_config()
    assert len(store.events()) == count
    toml = cfg.state_dir / config.CONFIG_FILE
    toml.write_text(toml.read_text().replace("max_concurrent = 3", "max_concurrent = 9"))
    engine.reload_config()
    assert engine.cfg.budget.max_concurrent == 9
    assert store.kv_get("config_error.team.yaml")
    path.write_text(original)
    engine.reload_config()
    assert not store.kv_get("config_error.team.yaml")
    toml.write_text("[budget]\nmax_concurrent = -2")
    engine.reload_config()
    assert engine.cfg.budget.max_concurrent == 9
    assert "budget.max_concurrent" in store.kv_get("config_error.troupe.toml")
    assert store.unread("lead")


def test_reload_updates_next_run_preserves_inflight_and_clears_provider_session(project, monkeypatch):
    cfg, store = project
    engine = Engine(cfg)
    agent = cfg.agent("builder-1")
    store.set_agent(agent.id, session_id="old", session_runs=3)
    document = copy.deepcopy(cfg.team_data)
    row = next(r for r in document["agents"] if r["id"] == agent.id)
    row.update(model="new-model", level="max")
    install_team(cfg, document)
    engine.reload_config()
    assert engine.cfg.agent(agent.id).model == "new-model"
    assert store.agent(agent.id)["session_id"] == "old"
    specs = []
    class HeldRunner:
        cancelled = False
        async def run(self, spec, emit):
            specs.append(spec)
            await release.wait()
            return RunResult(ok=True, session_id="old-provider-result")
    monkeypatch.setattr("troupe.engine.make_runner", lambda _: HeldRunner())
    async def check():
        nonlocal release
        release = asyncio.Event()
        await engine.launch(Wake(1, engine.cfg.agent(agent.id), "poke"))
        running = engine.running[agent.id][1]
        await asyncio.sleep(0)
        assert specs[0].agent.effort == "max"
        row.update(provider="codex", model="other", level="high")
        install_team(cfg, document)
        engine.reload_config()
        assert not store.agent(agent.id)["session_id"]
        assert specs[0].agent.backend == "claude"
        assert engine.cfg.agent(agent.id).backend == "codex"
        assert not running.done()
        release.set()
        await running
        assert store.agent(agent.id)["session_id"] is None
    release = None
    asyncio.run(check())


def test_add_disable_remove_and_role_change_return_tasks(project):
    cfg, store = project
    engine = Engine(cfg)
    document = copy.deepcopy(cfg.team_data)
    removed = store.add_task("Removed owner", status="blocked", assignee="builder-1")
    changed = store.add_task("Changed role", status="in_progress", assignee="builder-2")
    document["agents"] = [a for a in document["agents"] if a["id"] != "builder-1"]
    next(a for a in document["agents"] if a["id"] == "builder-2")["role"] = "spec"
    next(a for a in document["agents"] if a["id"] == "pm")["enabled"] = False
    document["agents"].append({"id": "builder_3", "role": "builder", "provider": "codex"})
    install_team(cfg, document)
    engine.reload_config()
    for tid in (removed, changed):
        assert store.task(tid)["status"] == "ready"
        assert store.task(tid)["assignee"] is None
    assert store.agent("builder_3")
    assert not store.agent("pm")["enabled"]
    assert store.agent("builder-1") is None


@pytest.mark.parametrize("backend", ["claude", "codex", "local"])
@pytest.mark.parametrize("level", ["", "low", "medium", "high", "max"])
def test_provider_level_mapping(project, monkeypatch, backend, level):
    cfg, _ = project
    a = cfg.agents[0]
    a.backend, a.effort = backend, level
    runner = make_runner(backend)
    captured = {}
    async def stream(args, spec, prompt, callback):
        captured["args"] = args
        return 0, ""
    monkeypatch.setattr(runner, "_stream", stream)
    if backend == "local":
        import httpx
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, *, headers, json):
                captured["body"] = json
                return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
        monkeypatch.setattr("troupe.runners.httpx.AsyncClient", Client)
    spec = RunSpec(cfg, a, "system", "prompt", cfg.root, None, cfg.runs_dir / "flags.jsonl")
    assert asyncio.run(runner.run(spec, lambda *_: None)).ok
    if backend == "claude":
        args = captured["args"]
        assert ("--effort" in args) == bool(level)
        if level: assert args[args.index("--effort") + 1] == level
    elif backend == "codex":
        expected = "xhigh" if level == "max" else level
        assert any(x.startswith("model_reasoning_effort=") for x in captured["args"]) == bool(level)
        if level: assert f"model_reasoning_effort={expected}" in captured["args"]
    else:
        assert "reasoning_effort" not in captured["body"]
        assert "effort" not in captured["body"]


@pytest.mark.parametrize("local_model", [None, "detected-model"])
def test_default_roster_validates(tmp_path, local_model):
    config.write_default(tmp_path, "new-project", local_model)
    cfg = config.load(tmp_path)
    assert [a.id for a in cfg.agents] == ["lead_1", "pm_1", "spec_1", "designer_1", "builder_1", "builder_2", "qa_1", "gadfly_1"]
    assert cfg.agent("pm_1").backend == "codex"
    assert cfg.agent("qa_1").backend == "claude"
    assert cfg.agent("builder_1").providers[1].model == "sonnet"
    assert cfg.agent("gadfly_1").backend == ("local" if local_model else "codex")
    assert cfg.provider_limits == {"claude": {"five_hour": 50, "seven_day": 50}}
    assert "[[agents]]" not in (cfg.state_dir / config.CONFIG_FILE).read_text()
    if not local_model:
        assert "# - {provider: local" in (cfg.state_dir / config.TEAM_FILE).read_text()


def test_mcp_uses_last_good_config_while_error_remains_visible(project):
    from troupe.mcp_server import build_server
    cfg, store = project
    engine = Engine(cfg)
    (cfg.state_dir / config.TEAM_FILE).write_text("agents: [bad")
    engine.reload_config()
    server, api = build_server(cfg.root, "pm")
    assert api.cfg.agent("pm").role == "pm"
    assert any(tool.name == "ask_human" for tool in asyncio.run(server.list_tools()))
    assert store.kv_get("config_error.team.yaml")
    assert any("invalid" in e["text"] for e in store.events())


def test_fresh_init_uses_detected_local_model_and_new_pm_id(tmp_path, monkeypatch):
    from argparse import Namespace
    from troupe import cli
    monkeypatch.setattr(cli, "detect_local_model", lambda: "detected")
    cli.cmd_init(Namespace(dir=str(tmp_path), name="fresh"))
    cfg = config.load(tmp_path)
    assert cfg.agent("gadfly_1").model == "detected"
    assert Store(cfg.db_path).messages()[0]["sender"] == "pm_1"
    cfg.team_data["agents"][4]["providers"][0]["level"] = "low"
    assert cfg.team_data["agents"][5]["providers"][0]["level"] == "high"
