import atexit
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from troupe import config, gitops
from troupe.service import _identity
from troupe.store import Store


@pytest.fixture
def project(tmp_path):
    """A fresh troupe project with the default team, in a git repo."""
    state = tmp_path / config.STATE_DIR
    state.mkdir()
    (state / config.CONFIG_FILE).write_text(config.DEFAULT_TOML.format(name="test-project", local_model="local-test"))
    gitops.ensure_repo(tmp_path)
    cfg = config.load(tmp_path)
    store = Store(cfg.db_path)
    baseline = store.kv_get("safety.config")
    store.answer(baseline["qid"], "Approve")
    cfg = config.load(tmp_path)
    # Tests start after human onboarding, with an empty activity/mail inbox.
    store.x("DELETE FROM messages")
    store.x("DELETE FROM events WHERE kind IN ('safety','question','answer','message')")
    store.sync_agents(cfg.agents)
    return cfg, store


# ── #103/REQ-ENG-060: no `troupe.cli engine` this session spawns may outlive it ─────────────────
# `start_service` spawns a fully detached process (start_new_session=True), so nothing else in the
# process tree notices if the test that owns it dies without running its own teardown — a raised
# exception still hits a fixture's `finally`, but pytest being OOM-killed or SIGTERM'd (e.g.
# `timeout 60 uv run pytest`) skips Python-level cleanup entirely. `_SPAWNED_STATE_DIRS` plus the
# session-scoped `_engine_leak_guard` fixture below are the backstop: every engine this session
# might have spawned gets reaped (by pid, identity-checked — see `_still_our_engine`) regardless of
# whether the specific test that spawned it ever reached its own teardown, and the session itself
# fails if one somehow survives anyway.
_SPAWNED_STATE_DIRS: set[Path] = set()


def track_engine_state_dir(state_dir: Path) -> None:
    """Register a project's .troupe dir with the session-wide reaper. Call this as soon as the
    directory exists, before anything might call start_service on it — a registration with nothing
    to reap yet is harmless (`_still_our_engine` just finds no valid record)."""
    _SPAWNED_STATE_DIRS.add(state_dir)


def untrack_engine_state_dir(state_dir: Path) -> None:
    _SPAWNED_STATE_DIRS.discard(state_dir)


def _looks_like_troupe_engine(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return "troupe.cli engine" in out


def _still_our_engine(state_dir: Path) -> int | None:
    """The pid `engine.pid` names, if and only if it's verifiably still the same process that
    wrote it — never a bare number. #103 QA finding: under this session's process churn, a pid can
    be reused by an unrelated process (the human's editor, browser, anything) within seconds of the
    engine that held it exiting; a blind `kill(pid)` would then hit that unrelated process instead.
    For the normal JSON record (written by `run_foreground`'s `_atomic_json`), this is the exact
    check `stop_service`/`service_status` already trust: `_identity` fingerprints `ps -p pid -o
    lstart=,args=`, so a recycled pid with a different start time or command never matches. A
    legacy bare-int record predates that field; the fallback there is confirming the *current*
    process at that pid still looks like a troupe engine at all, by command line."""
    try:
        record = json.loads((state_dir / "engine.pid").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(record, dict):
        pid, identity = record.get("pid"), record.get("identity")
        if isinstance(pid, int) and pid > 0 and identity and _identity(pid) == identity:
            return pid
        return None
    if isinstance(record, int) and record > 0 and _looks_like_troupe_engine(record):
        return record
    return None


def _kill_engine_by_pid_file(state_dir: Path) -> None:
    """Kill only the pid `_still_our_engine` verifies is still ours. Never raises: this runs from
    fixture teardown, atexit and a signal handler, none of which can afford to."""
    pid = _still_our_engine(state_dir)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _reap_all_spawned_engines() -> None:
    for state_dir in list(_SPAWNED_STATE_DIRS):
        _kill_engine_by_pid_file(state_dir)


@pytest.fixture(scope="session", autouse=True)
def _engine_leak_guard():
    """Installs the session-wide SIGTERM handler (here, not at import time in test_service.py —
    that would silently replace SIGTERM handling for the *entire* session merely by importing that
    file) and the atexit safety net, then asserts at session end that nothing this session spawned
    is still alive: a future leak becomes a failing test, not something QA has to notice by hand on
    the human's laptop (REQ-ENG-060)."""
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum, frame):
        _reap_all_spawned_engines()
        os._exit(143)  # 128 + SIGTERM; the reap above is the part that matters

    signal.signal(signal.SIGTERM, _on_sigterm)
    atexit.register(_reap_all_spawned_engines)
    yield
    _reap_all_spawned_engines()
    time.sleep(0.5)  # give a just-issued SIGKILL a moment to land before the final check
    survivors = {str(d): pid for d in _SPAWNED_STATE_DIRS if (pid := _still_our_engine(d)) is not None}
    signal.signal(signal.SIGTERM, previous_sigterm)
    assert not survivors, f"REQ-ENG-060: engine(s) survived the test session: {survivors}"
