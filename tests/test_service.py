import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from troupe import config
from troupe.service import (
    projects,
    register_project,
    service_status,
    start_service,
    stop_service,
)
from troupe.store import Store


@pytest.fixture
def project(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="troupe-service-", dir="/tmp") as tmp:
        root = Path(tmp) / "project"
        state = root / ".troupe"
        state.mkdir(parents=True)
        home = Path(tmp) / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        (state / "troupe.toml").write_text(
            '[project]\nname="Service Test"\n[git]\nautocommit=false\n'
        )
        (state / "team.yaml").write_text(
            "agents:\n  - id: builder-1\n    role: builder\n    provider: local\n    enabled: false\n  - id: lead\n    role: lead\n    provider: local\n    enabled: false\n"
        )
        cfg = config.load(root)
        yield cfg
        stop_service(cfg, timeout=2)


def invoke(cfg, *args):
    return subprocess.run(
        [sys.executable, "-m", "troupe.cli", *args],
        cwd=cfg.root,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_detach_concurrent_start_stale_and_registry(project):
    cfg = project
    (cfg.state_dir / "engine.pid").write_text(
        str(os.getpid())
    )  # unrelated live pid must not be adopted/killed
    assert service_status(cfg.root)["state"] == "stale"
    children = [
        subprocess.Popen(
            [sys.executable, "-m", "troupe.cli", "start"],
            cwd=cfg.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [p.communicate(timeout=15) for p in children]
    assert all(p.returncode == 0 for p in children), outputs
    state = service_status(cfg.root)
    assert state["state"] == "running" and state["pid"] != os.getpid()
    assert all(str(state["pid"]) in out for out, _ in outputs)
    assert os.getsid(state["pid"]) == state["pid"]
    store = Store(cfg.db_path)
    heartbeat = store.kv_get("heartbeat")
    time.sleep(1.1)
    assert store.kv_get("heartbeat") > heartbeat
    assert start_service(cfg)["pid"] == state["pid"]
    assert projects()[0]["state"] == "running"
    assert invoke(cfg, "status").returncode == 0
    assert "version" in invoke(cfg, "status").stdout
    assert invoke(cfg, "stop").returncode == 0
    assert not (cfg.state_dir / "engine.pid").exists()
    assert service_status(cfg.root)["state"] == "stopped"
    assert invoke(cfg, "status").returncode == 1
    assert invoke(cfg, "stop").stdout.strip() == "not running"
    assert "Engine started" in (cfg.state_dir / "engine.log").read_text()
    assert "Engine stopped" in (cfg.state_dir / "engine.log").read_text()
    missing = cfg.root / "gone"
    register_project(missing, "Gone")
    assert next(p for p in projects() if p["name"] == "Gone")["state"] == "missing"


def test_api_stop_and_crash_recovery(project):
    from troupe.api_client import Client

    cfg = project
    pid = start_service(cfg)["pid"]
    os.kill(pid, signal.SIGKILL)
    deadline = time.time() + 3
    while service_status(cfg.root)["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert service_status(cfg.root)["state"] == "stale"
    second = start_service(cfg)["pid"]
    assert second != pid
    with Client(cfg.root) as client:
        assert client.call("stop_team") == {"accepted": True}
    deadline = time.time() + 5
    while (cfg.state_dir / "engine.pid").exists() and time.time() < deadline:
        time.sleep(0.02)
    assert not (cfg.state_dir / "engine.pid").exists()


def test_up_and_gui_never_stop_service(project, monkeypatch):
    import argparse

    from troupe import cli
    from troupe.gui import app

    monkeypatch.setattr(cli, "require_root", lambda: project.root)
    monkeypatch.setattr(config, "find_root", lambda: project.root)
    attached = []
    monkeypatch.setattr(
        app, "run_gui", lambda cfg: attached.append(service_status(cfg.root)["pid"])
    )
    cli.cmd_up(argparse.Namespace())
    pid = attached[-1]
    cli.cmd_gui(argparse.Namespace())
    assert attached == [pid, pid]
    assert service_status(project.root)["pid"] == pid


@pytest.mark.parametrize("spawn_delay", [0, .3])
def test_shutdown_requeues_mail_and_kills_process_group(project, monkeypatch, spawn_delay):
    from troupe.engine import Engine, Wake
    from troupe.runners import Runner, RunResult

    cfg = project
    cfg.agents[0].enabled = True
    engine = Engine(cfg)
    engine.store.sync_agents(cfg.agents)
    mid = engine.store.send("human", "builder-1", "hello", kind="chat")

    class Slow(Runner):
        async def run(self, spec, emit):
            await asyncio.sleep(spawn_delay)
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)",
                start_new_session=True,
            )
            await self.proc.wait()
            return RunResult(ok=False)

    monkeypatch.setattr("troupe.engine.make_runner", lambda _: Slow())

    async def run():
        await engine.launch(Wake(0, cfg.agents[0], "chat"))
        await asyncio.sleep(0.15)
        runner = engine.running["builder-1"][0]
        engine.stop()
        monkeypatch.setattr(engine, "recover", lambda: None)
        await engine._serve()
        assert runner.proc.returncode is not None

    asyncio.run(run())
    assert engine.store.one("SELECT * FROM messages WHERE id=?", mid)["read_at"] is None
    assert engine.store.runs()[0]["status"] == "interrupted"
    assert engine.store.agent("builder-1")["state"] == "idle"


def test_catchup_pure_selection_and_focus(project):
    from troupe.gui.data import Data, catchup_items

    since = time.time() - 1000
    events = [
        dict(ts=since + 1, id=1, kind="task", text="Merged #1 title", ref="task:1"),
        dict(ts=since - 1, id=2, kind="task", text="Merged #2 old", ref="task:2"),
    ]
    qs = [
        dict(
            id=1,
            ts=since + 1,
            status="answered",
            question="answered",
            context="",
            answer="yes",
            asker="builder-1",
        ),
        dict(
            id=2,
            ts=since + 2,
            status="open",
            question="open",
            context="",
            answer=None,
            asker="builder-1",
        ),
    ]
    selected = catchup_items(events, qs, [], [], since)
    assert len(selected["Merges"]) == 1
    assert selected["Questions"][0]["label"] == "open"
    data = Data(project)
    data.store.kv_set("human_last_seen", since)
    data.store.event("system", "task", "Merged #1 title", ref="task:1")
    data.focus_changed(True)
    assert data.catchup["Merges"]
    assert data.store.kv_get("human_last_seen") == since
    data.dismiss_catchup()
    assert not data.catchup and data.store.kv_get("human_last_seen") > since


def test_log_rotation_and_handles(project):
    cfg = project
    log = cfg.state_dir / "engine.log"
    log.write_text("x" * (10 * 1024 * 1024 + 1))
    start_service(cfg)
    s = Store(cfg.db_path)
    s.event("builder-1", "run", "builder-1 woke up")
    deadline = time.time() + 3
    while "builder_1@Service_Test" not in log.read_text() and time.time() < deadline:
        time.sleep(0.05)
    assert "builder_1@Service_Test" in log.read_text()
    assert (cfg.state_dir / "engine.log.1").exists()
    assert log.stat().st_size < 10 * 1024 * 1024


def test_forced_stop_marks_runs_and_requeues_checkpoint(project):
    cfg = project
    pid = start_service(cfg)["pid"]
    s = Store(cfg.db_path)
    rid = s.start_run("builder-1", "messages", None, str(cfg.root), False)
    mid = s.send("lead", "builder-1", "work")
    s.mark_read([mid])
    s.kv_set(f"run_mail.{rid}", [mid])
    s.set_agent("builder-1", state="running", current_run=rid)
    os.kill(pid, signal.SIGSTOP)
    assert stop_service(cfg, timeout=0.2)
    assert s.runs()[0]["status"] == "interrupted"
    assert s.one("SELECT read_at FROM messages WHERE id=?", mid)["read_at"] is None
    assert not (cfg.state_dir / "engine.pid").exists()


@pytest.mark.parametrize('newer_runs', [1, 205])
def test_catchup_navigation_survives_agent_view_initialization(project, monkeypatch, newer_runs):
    from unittest.mock import MagicMock
    from troupe.gui.app import App
    from troupe.gui import views
    from troupe.gui.core import Rect
    cfg = project
    s = Store(cfg.db_path)
    s.sync_agents(cfg.agents)
    rid = s.start_run('builder-1','task',None,str(cfg.root),False,prompt='FAILED TARGET PROMPT')
    s.end_run(rid,'failed',0,0,'failed')
    for _ in range(newer_runs):
        latest=s.start_run('builder-1','task',None,str(cfg.root),False,prompt='NEWER SUCCESS PROMPT')
        s.end_run(latest,'ok',0,0,'ok')
    app=App(cfg)
    app.ui=MagicMock()
    app.ui.button_w.return_value=70
    app.ui.text_fit.return_value=20
    app.ui.measure.return_value=20
    app.ui.pill.return_value=30
    app.ui.button.return_value=False
    app.ui.chip.return_value=(False,30)
    app.data.refresh(force=True)
    app.run_view='Prompt'
    rendered=[]
    monkeypatch.setattr(views,'_run_selector',lambda *a:None)
    monkeypatch.setattr(views,'_agent_side',lambda *a:None)
    monkeypatch.setattr(views,'_prompt_view',lambda app,run,r:rendered.append(run))
    app.navigate_run('builder-1',rid)
    # Exercise the actual view initialization before and after lazy history loading.
    views.agent_view(app,Rect(0,0,1200,800))
    app.data.refresh(force=True)
    views.agent_view(app,Rect(0,0,1200,800))
    app.data.refresh(force=True)
    views.agent_view(app,Rect(0,0,1200,800))
    assert app.sel_run==rid
    assert rendered[-1]['prompt']=='FAILED TARGET PROMPT'
    assert rendered[-1]['id']==rid
