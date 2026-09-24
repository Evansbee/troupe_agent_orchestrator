"""REQ-ENG-050: the run watchdog kills silent/overlong runs and sweeps zombie rows."""
import asyncio
import time

from troupe.engine import Engine, Wake
from troupe.runners import RunResult


class _FakeProc:
    """Stands in for asyncio.subprocess.Process for runners that don't spawn a real one."""

    def __init__(self):
        self.pid = 999999  # never a real pid; kill attempts on it are harmless no-ops
        self.returncode = None
        self.stdout = None
        self.stderr = None


class SilentStallRunner:
    """Emits once, then spawns a real detached process tree and waits on it forever —
    simulating a backend that stopped producing output but is still alive."""
    cancelled = False

    def __init__(self, child_pid_file, ready_file):
        self.proc = None
        self.child_pid_file = child_pid_file
        self.ready_file = ready_file

    async def run(self, spec, emit):
        emit("text", "starting")
        self.proc = await asyncio.create_subprocess_shell(
            f'python3 -c "import os,time; os.setsid(); '
            f'open(\'{self.ready_file}\', \'w\').close(); time.sleep(60)" & '
            f'echo $! > {self.child_pid_file}; sleep 60',
            start_new_session=True,
        )
        await self.proc.wait()
        return RunResult(ok=False, error="killed")


class ChattyRunner:
    """Emits on a timer until told to go quiet; only stops once cancelled (by the watchdog or the
    test). Flip `silent = True` mid-run to simulate a backend that stops producing output."""
    cancelled = False

    def __init__(self):
        self.proc = _FakeProc()
        self.silent = False

    async def run(self, spec, emit):
        while not self.cancelled:
            if not self.silent:
                emit("text", "still working")
            await asyncio.sleep(0.01)
        return RunResult(ok=False, error="killed")


def _pid_alive(pid: int) -> bool:
    try:
        import os
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


def test_stall_kills_silent_runner_and_cleans_up_descendants(project, tmp_path, monkeypatch):
    cfg, store = project
    cfg.budget.stall_minutes = 0.01  # ~0.6s, small per the acceptance criteria
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    engine = Engine(cfg)
    a = cfg.agent("builder-1")
    child_pid_file = tmp_path / "child.pid"
    ready_file = tmp_path / "ready"
    runner = SilentStallRunner(child_pid_file, ready_file)
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: runner)
    store.send("lead", a.id, "please work")

    async def scenario():
        await engine.launch(Wake(0, a, "messages"))
        await _wait_for(lambda: runner.proc is not None)
        await _wait_for(lambda: ready_file.exists())  # the descendant has setsid()'d already
        parent_pid = runner.proc.pid
        child_pid = int(child_pid_file.read_text().strip())
        assert _pid_alive(parent_pid) and _pid_alive(child_pid)

        clock["t"] += 5  # well past stall_minutes, nowhere near the run-minutes cap
        engine.watchdog_sweep()
        await engine.running[a.id][1]  # let _run's post-processing finish

        await _wait_for(lambda: not _pid_alive(parent_pid) and not _pid_alive(child_pid))

    asyncio.run(scenario())

    run = store.runs(a.id, limit=1)[0]
    assert run["status"] == "stalled"
    assert any(m["sender"] == "lead" for m in store.unread(a.id))  # mail requeued
    assert engine.failures.get(a.id, (0, 0))[0] == 1  # REQ-ENG-015 backoff applied
    assert any(e["kind"] == "stalled" and e["agent"] == a.id for e in store.events(limit=200))


def test_worktree_run_survives_until_max_run_minutes_then_times_out(project, monkeypatch):
    cfg, store = project
    cfg.budget.stall_minutes = 1000
    cfg.budget.max_run_minutes = 10
    cfg.budget.max_coord_run_minutes = 1000
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    engine = Engine(cfg)
    a = cfg.agent("builder-1")
    runner = ChattyRunner()
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: runner)
    store.send("lead", a.id, "please work")

    async def scenario():
        await engine.launch(Wake(0, a, "messages"))
        await asyncio.sleep(0.05)  # let a few emits happen, keeping it far from stalling
        engine._run_worktree[a.id] = True  # simulate an actual worktree run
        engine.watchdog_sweep()
        assert not runner.cancelled  # well within max_run_minutes

        clock["t"] += 11 * 60  # past max_run_minutes
        engine.watchdog_sweep()
        await engine.running[a.id][1]

    asyncio.run(scenario())
    assert store.runs(a.id, limit=1)[0]["status"] == "timeout"


def test_chat_run_exempt_from_hard_cap_but_not_from_stall(project, monkeypatch):
    cfg, store = project
    cfg.budget.stall_minutes = 1000
    cfg.budget.max_coord_run_minutes = 1
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    engine = Engine(cfg)
    a = cfg.agent("lead")
    runner = ChattyRunner()
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: runner)
    store.send("human", a.id, "hi", kind="chat")

    async def scenario():
        await engine.launch(Wake(0, a, "chat"))
        await asyncio.sleep(0.05)
        clock["t"] += 3600  # far past what max_coord_run_minutes would have been
        engine.watchdog_sweep()
        assert not runner.cancelled  # chat is exempt from the hard cap
        runner.cancelled = True  # let the fake run wind down cleanly
        await engine.running[a.id][1]

    asyncio.run(scenario())
    assert store.runs(a.id, limit=1)[0]["status"] != "timeout"


def test_zombie_running_row_becomes_interrupted(project):
    cfg, store = project
    engine = Engine(cfg)
    rid = store.start_run("lead", "poke", None, str(cfg.root), False)
    store.kv_set(f"run_mail.{rid}", [])
    store.set_agent("lead", state="running", current_run=rid)

    engine.sweep_zombie_runs()

    row = store.runs("lead", limit=1)[0]
    assert row["status"] == "interrupted"
    assert store.agent("lead")["state"] == "idle"
    assert store.agent("lead")["current_run"] is None


def test_zombie_sweep_ignores_agents_this_engine_is_actually_running(project):
    cfg, store = project
    engine = Engine(cfg)
    rid = store.start_run("lead", "poke", None, str(cfg.root), False)
    store.set_agent("lead", state="running", current_run=rid)
    engine.running["lead"] = (object(), object(), object())

    engine.sweep_zombie_runs()

    assert store.runs("lead", limit=1)[0]["status"] == "running"


def test_chat_run_that_goes_silent_is_still_killed_and_marked_stalled(project, monkeypatch):
    """The cap-exemption test only proves chat survives a long *healthy* run — this proves a
    silent chat run still gets caught by the stall rule (QA's #61 review, point (b))."""
    cfg, store = project
    cfg.budget.stall_minutes = 0.01
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    engine = Engine(cfg)
    a = cfg.agent("lead")
    runner = ChattyRunner()
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: runner)
    store.send("human", a.id, "hi", kind="chat")

    async def scenario():
        await engine.launch(Wake(0, a, "chat"))
        await asyncio.sleep(0.05)  # a few healthy emits
        runner.silent = True
        clock["t"] += 5  # well past stall_minutes
        engine.watchdog_sweep()
        await engine.running[a.id][1]

    asyncio.run(scenario())

    run = store.runs(a.id, limit=1)[0]
    assert run["status"] == "stalled"
    assert engine.failures.get(a.id, (0, 0))[0] == 1


def test_notifier_turns_a_stalled_event_into_a_needs_help_notification(project):
    """QA's #61 review point (c): the AC says "a notification is raised", not just an event row."""
    from troupe.notify import Notifier

    cfg, store = project
    notifier = Notifier(store)  # cursor starts at the current max event id
    store.event("builder-1", "stalled", "builder-1's run stalled and was killed (1x)")

    notifier.collect(cfg, {})

    assert any(item["kind"] == "stalled" for item in notifier.state["pending"].values())
    assert any(e["kind"] == "needs_help" for e in store.events(limit=200))


_FAKE_BACKEND_SCRIPT = '''#!/usr/bin/env python3
import json, os, sys, time

print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}))
sys.stdout.flush()

if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        time.sleep(90)  # grandchild: reparents to init (ppid 1) once the middle process exits
        os._exit(0)
    os._exit(0)
else:
    os.wait()  # reap the middle child

time.sleep(90)  # the "backend" itself also hangs, matching the observed stall
'''


def test_stall_through_real_stream_unsticks_a_pipe_held_by_a_reparented_grandchild(project, tmp_path, monkeypatch):
    """QA's #61 rejection repro, through the real Runner._stream path (not a fake runner): a
    daemonized grandchild inherits stdout/stderr, reparents to init before the watchdog can walk
    pid ancestry to it, and is correctly never killed (not a verified descendant) — but it must
    not leave the run hung forever. The watchdog kills the backend, then, after a grace period,
    forces EOF on the still-open pipes so _run's completion handling can finish."""
    cfg, store = project
    script = tmp_path / "fake_claude"
    script.write_text(_FAKE_BACKEND_SCRIPT)
    script.chmod(0o755)
    cfg.backends.claude_command = str(script)
    cfg.budget.stall_minutes = 0.01
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr("troupe.engine.now", lambda: clock["t"])
    engine = Engine(cfg)
    a = cfg.agent("builder-1")
    store.send("lead", a.id, "please work")

    async def scenario():
        await engine.launch(Wake(0, a, "messages"))
        runner = engine.running[a.id][0]
        await _wait_for(lambda: runner.proc is not None)
        await asyncio.sleep(0.5)  # real time: let it print its line and finish forking

        clock["t"] += 5  # past stall_minutes
        engine.watchdog_sweep()
        await asyncio.sleep(0.5)  # real time: let the SIGKILL actually land
        assert a.id in engine.running  # still stuck: the reparented grandchild still holds the pipe

        clock["t"] += 25  # past the pipe-unstick grace period
        engine.watchdog_sweep()
        await asyncio.wait_for(engine.running[a.id][1], timeout=5)

    asyncio.run(scenario())

    run = store.runs(a.id, limit=1)[0]
    assert run["status"] == "stalled"
    assert any(m["sender"] == "lead" for m in store.unread(a.id))
    assert engine.failures.get(a.id, (0, 0))[0] == 1
