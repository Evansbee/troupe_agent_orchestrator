"""#100 acceptance: rerunning `troupe` must get the team back to work without re-asking the human
for anything, whether the previous session ended via Ctrl-C's clean stop (which interrupts
in-flight runs, REQ-ENG-004) or via a `kill -9` of the TUI itself (which can't be caught, so the
engine it started is left running -- #100 F3's re-adopt handles that case instead)."""
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from troupe import config
from troupe.service import service_status, set_owner, start_service, stop_service
from troupe.store import Store
from troupe.tui import lifecycle

FAKE_BACKEND = '''#!/usr/bin/env python3
import json, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "resume-test"}), flush=True)
time.sleep(300)
'''


@pytest.fixture
def project(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="troupe-resume-", dir="/tmp") as tmp:
        root = Path(tmp) / "project"
        state = root / ".troupe"
        state.mkdir(parents=True)
        home = Path(tmp) / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        fake = Path(tmp) / "fake.py"
        fake.write_text(FAKE_BACKEND)
        fake.chmod(0o755)
        (state / "troupe.toml").write_text(
            f'[project]\nname="Resume Test"\n[git]\nautocommit=false\n'
            f'[backends]\nclaude_command="{fake}"\ncodex_command="{fake}"\n'
            f'local_base_url="http://127.0.0.1:9"\n'
        )
        (state / "team.yaml").write_text(
            "agents:\n"
            "  - id: builder-1\n    role: builder\n    provider: claude\n"
            "  - id: lead\n    role: lead\n    provider: local\n    enabled: false\n"
        )
        cfg = config.load(root)
        store = Store(cfg.db_path)
        baseline = store.kv_get("safety.config")
        store.answer(baseline["qid"], "Approve")
        cfg = config.load(root)
        yield cfg
        stop_service(cfg, timeout=2)


def _wait_running(store: Store, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store.one("SELECT state FROM agents WHERE id='builder-1'")
        if row and row["state"] == "running":
            return
        time.sleep(0.1)
    raise TimeoutError("builder-1 never started running")


def _dead_pid() -> int:
    """A pid guaranteed not alive: waited-on, so it can't still be running, and not yet recycled."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_ctrl_c_stop_then_rerun_redispatches_interrupted_task(project):
    cfg = project
    store = Store(cfg.db_path)
    store.add_task("Do the thing", status="ready", assignee="builder-1")
    start_service(cfg)
    _wait_running(store)

    assert stop_service(cfg, timeout=5)  # the product-level effect of a clean Ctrl-C
    assert store.runs()[0]["status"] == "interrupted"
    assert store.kv_get("safety.approved") is not None  # baseline untouched, no re-ask

    start_service(cfg)  # rerunning `troupe`
    _wait_running(store)  # re-dispatched to the same assignee, no human intervention needed
    assert store.questions("open") == []  # no safety-baseline card


def test_relaunch_after_owner_killed_readopts_without_interrupting_runs(project):
    """#100 F3: a `kill -9` of the TUI can't be handled by the TUI at all, so the engine it started
    is left running, ownerless. A later `troupe` must adopt it (not attach passively forever, and
    not restart it -- restarting would needlessly interrupt whatever's still in flight)."""
    cfg = project
    store = Store(cfg.db_path)
    store.add_task("Do the thing", status="ready", assignee="builder-1")
    pid = start_service(cfg)["pid"]
    _wait_running(store)
    set_owner(cfg.state_dir, _dead_pid())  # simulate: the TUI that started this engine is gone

    import asyncio
    owns = asyncio.run(lifecycle.ensure_engine(cfg))

    assert owns is True
    assert service_status(cfg.root)["pid"] == pid  # same engine process, never restarted
    assert store.runs()[0]["status"] == "running"  # never interrupted
    assert store.questions("open") == []


def _spawn_tui_on_pty(root: Path) -> tuple[subprocess.Popen, int, threading.Event]:
    """A real `troupe tui` under a real pty -- Textual puts the terminal in raw mode itself, which
    is what makes a literal Ctrl-C byte arrive as a *key event* the composer can swallow, not an
    OS signal (that distinction is exactly why F1 and F2 needed separate fixes).

    Also starts a background drain thread: Textual repaints continuously, and if nothing reads the
    master side, the pty's kernel buffer fills and the child blocks inside a write() call trying to
    restore the terminal on exit -- a hang in the *test harness*, not the app, that looks exactly
    like a stuck shutdown if you don't know to look for it."""
    master_fd, slave_fd = pty.openpty()
    env = dict(os.environ, TERM="xterm-256color")
    proc = subprocess.Popen(
        [sys.executable, "-m", "troupe.cli", "tui"], cwd=root,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env=env, start_new_session=True, close_fds=True,
    )
    os.close(slave_fd)
    stop_draining = threading.Event()

    def drain():
        while not stop_draining.is_set():
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                try:
                    os.read(master_fd, 65536)
                except OSError:
                    break

    threading.Thread(target=drain, daemon=True).start()
    return proc, master_fd, stop_draining


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_real_ctrl_c_byte_over_a_pty_stops_tui_and_engine(project):
    """Regression for QA's exact repro (msg #912, 2a): `tmux send-keys C-c` did nothing because the
    composer ate the raw byte. This sends that same byte over a real pty, not Pilot's simulated
    key press, so it also proves Textual's raw-mode terminal driver delivers it the way a real
    terminal does."""
    cfg = project
    proc, master_fd, stop_draining = _spawn_tui_on_pty(cfg.root)
    try:
        assert _wait_for(lambda: service_status(cfg.root)["state"] == "running", 15)
        time.sleep(0.5)  # let the TUI finish connecting/loading and focus the composer (#83)
        os.write(master_fd, b"\x03")
        assert _wait_for(lambda: proc.poll() is not None, 10)
        assert proc.returncode == 0
        assert _wait_for(lambda: service_status(cfg.root)["state"] != "running", 10)
        assert not (cfg.state_dir / "engine.pid").exists()
    finally:
        stop_draining.set()
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_real_sigint_to_the_tui_process_stops_the_engine_too(project):
    """Regression for QA's exact repro (msg #912, 2b): a real SIGINT used to exit the TUI rc=0 but
    leave the engine, its lock and its pid file orphaned, because _install_signal_handlers only
    covered SIGHUP/SIGTERM."""
    cfg = project
    proc, master_fd, stop_draining = _spawn_tui_on_pty(cfg.root)
    try:
        assert _wait_for(lambda: service_status(cfg.root)["state"] == "running", 15)
        time.sleep(0.5)
        os.kill(proc.pid, signal.SIGINT)
        assert _wait_for(lambda: proc.poll() is not None, 10)
        assert proc.returncode == 0
        assert _wait_for(lambda: service_status(cfg.root)["state"] != "running", 10)
        assert not (cfg.state_dir / "engine.pid").exists()
    finally:
        stop_draining.set()
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
