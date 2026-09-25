import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from conftest import (
    _kill_engine_by_pid_file,
    _list_troupe_engine_pids,
    _process_cwd,
    _still_our_engine,
    track_engine_state_dir,
    untrack_engine_state_dir,
)
from troupe import config
from troupe.service import (
    _identity,
    projects,
    reap_engines_under,
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
        track_engine_state_dir(state)  # #103/REQ-ENG-060: see conftest.py for the session-wide reaper
        # #103: every engine this fixture spawns exits on its own if this test process (the
        # closest thing a spawned-with-start_new_session=True engine has to a "parent" that would
        # otherwise reap it) is gone — a second, independent backstop alongside the root-disappears
        # check, since it also covers pytest itself being OOM-killed or SIGKILL'd, where no
        # Python-level cleanup on this side runs at all.
        monkeypatch.setenv("TROUPE_EXIT_WITH_PARENT_PID", str(os.getpid()))
        try:
            yield cfg
        finally:
            if state.is_dir():  # a test may have deleted it itself (e.g. the root-disappears test)
                stop_service(cfg, timeout=2)
            _kill_engine_by_pid_file(state)  # #103: unconditional fallback, see conftest.py
            untrack_engine_state_dir(state)


def test_kill_engine_by_pid_file_kills_by_recorded_pid(tmp_path):
    """#103: the fixture's fallback reads engine.pid and kills that pid directly — no
    service_status() involved, so it works even when that classification can't be trusted (the
    exact situation a half-torn-down temp project leaves behind). Identity must match a real
    engine record (see the next test for what happens when it doesn't)."""
    state_dir = tmp_path / ".troupe"
    state_dir.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = None
        deadline = time.time() + 5
        while not identity and time.time() < deadline:
            identity = _identity(proc.pid)  # empty until `ps` can see the just-forked child
            if not identity:
                time.sleep(0.05)
        (state_dir / "engine.pid").write_text(json.dumps({"pid": proc.pid, "identity": identity}))
        _kill_engine_by_pid_file(state_dir)
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_kill_engine_by_pid_file_never_kills_a_pid_whose_identity_does_not_match(tmp_path):
    """#103 QA finding: a blind SIGKILL-by-pid is a real hazard on the human's machine — under this
    session's process churn a pid can be reused within seconds, so killing by number alone can hit
    an unrelated process (the human's editor, browser, anything). `_kill_engine_by_pid_file` must
    refuse when the live process at that pid isn't verifiably the one that wrote the record: a
    made-up/stale identity string (simulating a reused pid) must not match a real, live process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        state_dir = tmp_path / ".troupe"
        state_dir.mkdir()
        (state_dir / "engine.pid").write_text(
            json.dumps({"pid": proc.pid, "identity": "not the real identity, simulating a reused pid"}))
        assert _still_our_engine(state_dir) is None
        _kill_engine_by_pid_file(state_dir)
        assert proc.poll() is None  # still alive — must NOT have been killed
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_kill_engine_by_pid_file_kills_a_legacy_bare_int_record_only_if_it_looks_like_an_engine(tmp_path):
    """Pre-identity (legacy) engine.pid records are a bare int, not a JSON object — the fallback
    there is confirming the live process's own command line still says "troupe.cli engine", so a
    non-engine pid (the human's own shell, in this test) is left alone."""
    state_dir = tmp_path / ".troupe"
    state_dir.mkdir()
    (state_dir / "engine.pid").write_text(json.dumps(os.getpid()))  # this very pytest process
    assert _still_our_engine(state_dir) is None
    _kill_engine_by_pid_file(state_dir)  # must be a no-op — if this actually killed us, nothing
    # after this line would run; the test process being alive to reach the next assertion is itself
    # part of the proof.
    assert os.kill(os.getpid(), 0) is None  # still alive


def test_session_guard_fails_the_run_for_an_engine_leaked_outside_any_fixture(tmp_path):
    """#103 QA finding: the session guard's tracked-state-dir mechanism only sees engines started
    through test_service.py's `project` fixture. The original 74-orphan leak never went through
    any fixture at all — just a bare `start_service()` call whose caller forgot to stop it. This
    drives a real, separate pytest *session* (so the outer session's own tracking never gets
    involved) with exactly that shape and checks two things from the outside: the inner session's
    exit code is non-zero with "REQ-ENG-060" in its output, and no engine survives it.

    The probe does *not* clear TROUPE_EXIT_WITH_PARENT_PID: the session-wide guard sets it for
    every test now (fix 1), so a realistically-forgotten stop_service call inherits it same as any
    other subprocess a test spawns — that's exactly what makes the env-attribution check in (2)
    able to recognize it as this session's own. The inner session's own teardown check runs
    synchronously and well within the ~2s self-exit poll interval, so what's actually observed here
    is the untracked-detection fallback catching (and reaping) the engine itself, not the slower
    parent-pid self-heal winning the race."""
    leaky_test = Path(__file__).parent / "_leaky_engine_probe.py"
    leaky_test.write_text(
        "def test_leaks_an_engine_outside_any_fixture(monkeypatch):\n"
        "    import tempfile\n"
        "    from pathlib import Path\n"
        "    from troupe import config\n"
        "    from troupe.service import start_service\n"
        # /tmp, not pytest's own (deeply-nested) tmp_path: a long AF_UNIX socket path errors out
        # before the engine ever starts, and the original 74-orphan leak was in /tmp too.
        "    tmp = Path(tempfile.mkdtemp(prefix='troupe-service-leaky-', dir='/tmp'))\n"
        "    root = tmp / 'leaky_project'\n"
        "    state = root / '.troupe'\n"
        "    state.mkdir(parents=True)\n"
        "    home = tmp / 'home'\n"
        "    home.mkdir()\n"
        "    monkeypatch.setenv('HOME', str(home))\n"
        "    (state / 'troupe.toml').write_text('[project]\\nname=\"Leaky\"\\n[git]\\nautocommit=false\\n')\n"
        "    (state / 'team.yaml').write_text(\n"
        "        'agents:\\n  - id: lead\\n    role: lead\\n    provider: local\\n    enabled: false\\n')\n"
        "    cfg = config.load(root)\n"
        "    start_service(cfg)  # deliberately never stopped, and never registered with the guard\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(leaky_test)],
            cwd=str(Path(__file__).parent.parent), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "REQ-ENG-060" in result.stdout + result.stderr
        # The inner test itself must have run and leaked the engine (not failed for some other
        # reason before ever reaching start_service) — otherwise a non-zero exit + REQ-ENG-060
        # elsewhere in the output could pass this assertion for the wrong cause.
        assert "1 passed" in result.stdout or "1 failed" in result.stdout
    finally:
        leaky_test.unlink(missing_ok=True)
    # By the time the inner pytest process has exited, its own session-guard teardown already
    # killed what it leaked — confirm from out here too, since that's the actual point of #103.
    for pid in _list_troupe_engine_pids():
        cwd = _process_cwd(pid)
        assert not (cwd and "leaky_project" in cwd), f"leaked engine pid {pid} at {cwd} survived"


def test_session_guard_never_kills_another_concurrent_sessions_legitimate_engine():
    """#103 QA finding: concurrent pytest sessions are routine here (three builders, QA and the
    merge gate's own `uv run pytest` checks run side by side). Before the env-attribution fix,
    "not in my snapshot + temp cwd + started after I began" was true for *any* recently-started
    engine, including a completely different session's legitimate one — a short session B whose own
    start-of-session snapshot predates another session A's engine, and whose own session doesn't
    end until *after* A's engine exists, would see it as new, kill it, and fail itself for no
    reason. QA's exact repro shape: B starts, THEN (~2s later) A starts and spawns a real engine it
    holds open — B must still be running (not yet at its own teardown) when that happens, or there
    is nothing for the bug to bite on.

    Session B: starts immediately, then sleeps long enough that its own teardown runs well after
    A's engine exists. Session A: starts ~2s after B (so A's engine is created *after* B's own
    start-of-session snapshot, and while B is still mid-sleep), holds the engine open past B's
    finish, then stops it cleanly. Both must pass, and A's own inner assertion proves the engine it
    stops is the *same* one it started (one pid) — if B had killed it, this would catch that even if
    B's own exit code somehow didn't."""
    probe_a = Path(__file__).parent / "_leaky_engine_probe_a.py"
    probe_b = Path(__file__).parent / "_leaky_engine_probe_b.py"
    probe_a.write_text(
        "import time\n"
        "def test_session_a_holds_a_legitimate_engine_open(monkeypatch):\n"
        "    import tempfile\n"
        "    from pathlib import Path\n"
        "    from troupe import config\n"
        "    from troupe.service import start_service, stop_service, service_status\n"
        "    tmp = Path(tempfile.mkdtemp(prefix='troupe-service-concurrent-a-', dir='/tmp'))\n"
        "    root = tmp / 'project'\n"
        "    state = root / '.troupe'\n"
        "    state.mkdir(parents=True)\n"
        "    home = tmp / 'home'\n"
        "    home.mkdir()\n"
        "    monkeypatch.setenv('HOME', str(home))\n"
        "    (state / 'troupe.toml').write_text('[project]\\nname=\"A\"\\n[git]\\nautocommit=false\\n')\n"
        "    (state / 'team.yaml').write_text(\n"
        "        'agents:\\n  - id: lead\\n    role: lead\\n    provider: local\\n    enabled: false\\n')\n"
        "    cfg = config.load(root)\n"
        "    pid = start_service(cfg)['pid']\n"
        "    time.sleep(6)  # outlast session B's own end\n"
        "    assert service_status(cfg.root)['pid'] == pid, \\\n"
        "        'my engine was replaced/restarted while B ran — it must have been killed'\n"
        "    assert stop_service(cfg, timeout=5)\n"
    )
    probe_b.write_text(
        "import time\n"
        "def test_session_b_stays_open_a_while_but_spawns_no_engine():\n"
        "    time.sleep(6)  # still running (not yet at teardown) when A's engine appears at ~2s\n"
        "    assert 1 + 1 == 2\n"
    )
    proc_b = None
    try:
        proc_b = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", str(probe_b)],
            cwd=str(Path(__file__).parent.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(2)  # B's own start-of-session snapshot is taken; now let A's engine appear
        result_a = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(probe_a)],
            cwd=str(Path(__file__).parent.parent), capture_output=True, text=True, timeout=30,
        )
        assert result_a.returncode == 0, result_a.stdout + result_a.stderr
        stdout_b, _ = proc_b.communicate(timeout=30)
        assert proc_b.returncode == 0, stdout_b
    finally:
        if proc_b is not None and proc_b.poll() is None:
            proc_b.kill()
            proc_b.wait(timeout=5)
        probe_a.unlink(missing_ok=True)
        probe_b.unlink(missing_ok=True)


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
    # #71: the real detached engine subprocess only writes a fresh heartbeat once per ~1s tick
    # (TICK in engine.py); a single 1.1s sleep left almost no margin for that process to actually
    # get scheduled under load, so poll generously (10s) instead.
    deadline = time.monotonic() + 10
    while store.kv_get("heartbeat") <= heartbeat and time.monotonic() < deadline:
        time.sleep(0.1)
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


def test_legacy_pid_alive_fresh_heartbeat_reports_running_and_skips_spawn(project, monkeypatch):
    """REQ-ENG-003: a pre-#24 engine (bare-int pid, no lock) is adopted, not duplicated."""
    cfg = project
    store = Store(cfg.db_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (cfg.state_dir / "engine.pid").write_text(str(child.pid))
        store.kv_set("heartbeat", time.time())

        status = service_status(cfg.root)
        assert status["state"] == "running"
        assert status["pid"] == child.pid
        assert status["legacy"] is True

        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **k: pytest.fail("start_service must not spawn a child for a live legacy engine"),
        )
        result = start_service(cfg)
        assert result["state"] == "running"
        assert result["pid"] == child.pid
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_legacy_pid_dead_or_stale_heartbeat_is_stale_and_start_service_spawns(project):
    cfg = project
    store = Store(cfg.db_path)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    (cfg.state_dir / "engine.pid").write_text(str(dead.pid))
    store.kv_set("heartbeat", time.time())
    status = service_status(cfg.root)
    assert status["state"] == "stale"
    assert status["pid"] is None
    assert status["legacy"] is False

    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (cfg.state_dir / "engine.pid").write_text(str(alive.pid))
        store.kv_set("heartbeat", time.time() - 10)  # stale (REQ-ENG-005: >5s)
        status = service_status(cfg.root)
        assert status["state"] == "stale"
        assert status["pid"] is None
        assert status["legacy"] is False

        result = start_service(cfg)  # spawns and becomes ready, same as today
        assert result["state"] == "running"
        assert result["legacy"] is False
        assert result["pid"] != alive.pid
    finally:
        alive.terminate()
        alive.wait(timeout=5)


def test_stop_service_sigterms_legacy_engine_and_waits_for_heartbeat(project):
    cfg = project
    store = Store(cfg.db_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (cfg.state_dir / "engine.pid").write_text(str(child.pid))
    store.kv_set("heartbeat", time.time())

    assert stop_service(cfg, timeout=5) is True
    assert child.wait(timeout=5) is not None  # SIGTERM actually reached the process
    assert not (cfg.state_dir / "engine.pid").exists()
    assert service_status(cfg.root)["state"] != "running"


def test_stop_service_never_signals_a_dead_legacy_pid(project):
    cfg = project
    store = Store(cfg.db_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    (cfg.state_dir / "engine.pid").write_text(str(dead.pid))
    store.kv_set("heartbeat", time.time())  # fresh heartbeat, but the recorded pid is already dead

    assert stop_service(cfg, timeout=1) is False  # never adopted, so nothing gets signalled


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


def test_foreground_engine_sighup_stops_the_engine_and_its_run(monkeypatch):
    """#122: closing the terminal of a foreground `troupe engine` delivers it SIGHUP. Before this
    fix, run_foreground handled only SIGTERM/SIGINT, so the default SIGHUP disposition killed the
    engine process instantly -- its own shutdown (which kills every in-flight run's process group,
    engine.py's _serve()) never ran, orphaning a real claude/codex process that keeps running (and
    spending the human's quota) with nothing left to supervise it."""
    with tempfile.TemporaryDirectory(prefix="troupe-service-sighup-", dir="/tmp") as tmp:
        root = Path(tmp) / "project"
        state = root / ".troupe"
        state.mkdir(parents=True)
        home = Path(tmp) / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        fake = Path(tmp) / "fake.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, time\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'x'}), flush=True)\n"
            "time.sleep(300)\n"
        )
        fake.chmod(0o755)
        (state / "troupe.toml").write_text(
            '[project]\nname="SIGHUP Test"\n[git]\nautocommit=false\n'
            f'[backends]\nclaude_command="{fake}"\ncodex_command="{fake}"\n'
            'local_base_url="http://127.0.0.1:9"\n'
        )
        (state / "team.yaml").write_text(
            "agents:\n"
            "  - id: builder-1\n    role: builder\n    provider: claude\n"
            "  - id: lead\n    role: lead\n    provider: local\n    enabled: false\n"
        )
        cfg = config.load(root)
        track_engine_state_dir(state)
        try:
            store = Store(cfg.db_path)
            baseline = store.kv_get("safety.config")
            store.answer(baseline["qid"], "Approve")
            store.add_task("Do the thing", status="ready", assignee="builder-1")

            env = os.environ.copy()
            env["TROUPE_EXIT_WITH_PARENT_PID"] = str(os.getpid())
            proc = subprocess.Popen([sys.executable, "-m", "troupe.cli", "engine"], cwd=root, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                fake_pid = None
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and fake_pid is None:
                    found = subprocess.run(["pgrep", "-f", str(fake)], capture_output=True,
                                           text=True).stdout.split()
                    if found:
                        fake_pid = int(found[0])
                    else:
                        time.sleep(0.1)
                assert fake_pid is not None, "the fake backend never got dispatched a run"

                os.kill(proc.pid, signal.SIGHUP)
                assert proc.wait(timeout=10) is not None  # the engine itself exits

                deadline = time.monotonic() + 10
                orphaned = True
                while time.monotonic() < deadline and orphaned:
                    orphaned = bool(subprocess.run(["ps", "-p", str(fake_pid)], capture_output=True,
                                                   text=True).stdout.strip().splitlines()[1:])
                    if orphaned:
                        time.sleep(0.1)
                assert not orphaned, "the fake backend process outlived the engine (orphaned)"

                assert json.loads((state / "service.json").read_text())["state"] == "stopped"
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            _kill_engine_by_pid_file(state)
            untrack_engine_state_dir(state)


def _wait_for_child_exit(pid: int, timeout: float) -> bool:
    """True once `pid` — a direct child of this test process, via start_service's subprocess.Popen
    — has exited. Reaps it via waitpid rather than checking `os.kill(pid, 0)`: a kill-0 check can't
    tell a real orphan from our own not-yet-reaped zombie (still "alive" by that check even once the
    engine has fully exited), and under this session's process churn a bare pid can even get reused
    by an unrelated process before a naive poll notices — waitpid is scoped to actual parent/child
    relationships, so neither failure mode applies."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True  # already reaped (e.g. by Popen's own bookkeeping) — gone either way
        if reaped_pid == pid:
            return True
        time.sleep(0.1)
    return False


def test_engine_exits_when_its_project_root_disappears(project):
    """#103's product-side fix: a detached engine (start_new_session=True) has nothing else
    watching for its project to vanish out from under it, which is exactly how test runs orphaned
    dozens of these processes on the human's laptop once their /tmp project dirs were cleaned up
    around them mid-run."""
    cfg = project
    pid = start_service(cfg)["pid"]
    shutil.rmtree(cfg.root)
    assert _wait_for_child_exit(pid, 10)


def test_engine_exits_when_its_project_root_is_deleted_and_recreated_at_the_same_path(project):
    """#115: QA's exact repro (msg #1088, the 14:28-18:40 leak) -- a harness that rmtrees and
    RE-CREATES its scratch dir at the same path (reused across iterations, e.g. /tmp/qacut) defeats
    the plain existence check the test above covers: the recreated directory exists again at that
    path, so `cfg.root.is_dir()` alone reads it as "still there, still mine". Only comparing the
    directory's identity (inode), not just its existence, actually notices the swap."""
    cfg = project
    pid = start_service(cfg)["pid"]
    shutil.rmtree(cfg.root)
    cfg.root.mkdir(parents=True)  # same path, fresh inode -- no .troupe/ inside, unlike the original
    assert _wait_for_child_exit(pid, 10)


def test_engine_exits_when_its_spawning_process_is_gone(project, monkeypatch, tmp_path):
    """The second #103 backstop: TROUPE_EXIT_WITH_PARENT_PID catches the case the root-disappears
    check can't — the spawning process (in production, nothing sets this; in tests, pytest itself)
    dying without its directory going anywhere, e.g. an OOM SIGKILL that skips every bit of
    Python-level cleanup on the test side."""
    fake_parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setenv("TROUPE_EXIT_WITH_PARENT_PID", str(fake_parent.pid))
    cfg = project
    try:
        pid = start_service(cfg)["pid"]
        fake_parent.terminate()
        fake_parent.wait(timeout=5)
        assert _wait_for_child_exit(pid, 10)
    finally:
        if fake_parent.poll() is None:
            fake_parent.kill()
            fake_parent.wait(timeout=5)


def _make_scratch_project(root: Path) -> config.Config:
    """A minimal project the same shape as the `project` fixture builds, but callable more than
    once per test -- reap_engines_under's own tests need two independent projects at once."""
    state = root / ".troupe"
    state.mkdir(parents=True)
    (state / "troupe.toml").write_text('[project]\nname="Reap Test"\n[git]\nautocommit=false\n')
    (state / "team.yaml").write_text(
        "agents:\n  - id: builder-1\n    role: builder\n    provider: local\n    enabled: false\n"
        "  - id: lead\n    role: lead\n    provider: local\n    enabled: false\n"
    )
    return config.load(root)


def test_reap_engines_under_stops_only_engines_at_or_under_path(monkeypatch):
    """#115 acceptance: reap_engines_under(tmp) must stop an engine nested under `tmp` (a harness
    can have its project several directories deep) and must leave a live engine under a sibling,
    unrelated directory completely alone."""
    with tempfile.TemporaryDirectory(prefix="troupe-reap-target-", dir="/tmp") as target_tmp, \
         tempfile.TemporaryDirectory(prefix="troupe-reap-other-", dir="/tmp") as other_tmp, \
         tempfile.TemporaryDirectory(prefix="troupe-reap-home-", dir="/tmp") as home_tmp:
        monkeypatch.setenv("HOME", home_tmp)
        monkeypatch.setenv("TROUPE_EXIT_WITH_PARENT_PID", str(os.getpid()))
        target_root = Path(target_tmp) / "nested" / "project"
        other_root = Path(other_tmp) / "project"
        target_cfg = _make_scratch_project(target_root)
        other_cfg = _make_scratch_project(other_root)
        track_engine_state_dir(target_cfg.state_dir)
        track_engine_state_dir(other_cfg.state_dir)
        try:
            target_pid = start_service(target_cfg)["pid"]
            other_pid = start_service(other_cfg)["pid"]

            stopped = reap_engines_under(Path(target_tmp))

            assert stopped == [target_pid]
            assert _wait_for_child_exit(target_pid, 10)
            assert other_pid == service_status(other_cfg.root)["pid"]  # untouched, still running
        finally:
            stop_service(other_cfg, timeout=2)
            _kill_engine_by_pid_file(target_cfg.state_dir)
            _kill_engine_by_pid_file(other_cfg.state_dir)
            untrack_engine_state_dir(target_cfg.state_dir)
            untrack_engine_state_dir(other_cfg.state_dir)


def test_reap_engines_under_ignores_a_stale_pid_file_whose_identity_does_not_match(tmp_path):
    """Same rule as every other stop path in this module: a pid file naming a pid that's alive but
    isn't actually the engine that wrote the record (a reused pid) must never be signalled."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        state = tmp_path / "proj" / ".troupe"
        state.mkdir(parents=True)
        (state / "engine.pid").write_text(
            json.dumps({"pid": proc.pid, "identity": "not the real identity, simulating a reused pid"}))
        stopped = reap_engines_under(tmp_path)
        assert stopped == []
        assert proc.poll() is None  # still alive -- must NOT have been touched
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reap_engines_under_is_a_no_op_below_the_given_path(tmp_path):
    assert reap_engines_under(tmp_path / "does-not-exist") == []


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
